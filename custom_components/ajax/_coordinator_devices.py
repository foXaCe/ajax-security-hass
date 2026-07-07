"""Devices polling mixin for ``AjaxDataCoordinator``.

Owns the heavy device-reconciliation pipeline:

* `_async_update_devices`: the big per-space loop that walks the REST
  payload, normalises the attributes, creates the AjaxDevice entries and
  fans out `SIGNAL_NEW_DEVICE` on first sight.
* `_async_cleanup_stale_devices`: prunes the HA device registry for IDs
  that no longer appear in the Ajax account.

The per-family attribute application (battery/signal, contacts, siren,
sockets, lightswitch…) lives in ``_device_apply.apply_device_payload``
(pure functions). The stateless helpers `_normalize_device_attributes`
(raw Ajax field names -> handler shapes) and
`_reset_expired_motion_detections` (clears the motion_detected impulse
after the 30 s window) live in ``_device_normalize``
(``AjaxDeviceNormalizeMixin``), inherited by this mixin.

State stays on ``self`` (``account``, ``api``, ``hass``, the optimistic
guards on each ``AjaxDevice``); the mixin owns no attributes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send

from ._device_apply import apply_device_payload
from ._device_normalize import AjaxDeviceNormalizeMixin
from ._ids import device_identifier
from .const import DOMAIN, SIGNAL_NEW_DEVICE
from .models import AjaxDevice, DeviceType

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .api import AjaxRestApi
    from .models import AjaxAccount, AjaxSpace

_LOGGER = logging.getLogger(__name__)


class AjaxDevicesMixin(AjaxDeviceNormalizeMixin):
    """Coordinator mixin: device polling + reconciliation + cleanup.

    Stateless attribute normalisation and motion-impulse expiry live in
    ``AjaxDeviceNormalizeMixin`` (``_device_normalize``), inherited here.
    """

    # Host attributes — provided by the coordinator __init__.
    if TYPE_CHECKING:
        account: AjaxAccount | None
        api: AjaxRestApi
        hass: HomeAssistant
        entry_id: str
        _initial_load_done: bool

        def _parse_device_type(self, type_str: str) -> DeviceType: ...

    def _async_cleanup_stale_devices(self) -> None:
        """Remove HA device registry entries for devices no longer in Ajax.

        After initial load, compare HA device registry with known Ajax devices.
        This catches devices deleted from Ajax while HA was offline.
        """
        if self.account is None:
            return

        # Collect all known Ajax device IDs across all spaces
        known_ids: set[str] = set()
        for space in self.account.spaces.values():
            # Hub/space itself is registered as a device
            known_ids.add(space.id)
            if space.hub_id:
                known_ids.add(space.hub_id)
            # Regular devices
            known_ids.update(space.devices.keys())
            # Video edge cameras
            known_ids.update(space.video_edges.keys())
            # Smart locks
            known_ids.update(space.smart_locks.keys())

        # Scan THIS entry's HA devices only (multi-account: never touch another
        # entry's devices). Identifiers are namespaced f"{entry_id}_{ajax_id}",
        # so strip the prefix before comparing against the bare Ajax ids.
        device_registry = dr.async_get(self.hass)
        prefix = f"{self.entry_id}_"
        stale_devices: list[tuple[str, str]] = []  # (ha_device_id, ajax_id)

        for ha_device in dr.async_entries_for_config_entry(device_registry, self.entry_id):
            for id_tuple in ha_device.identifiers:
                if len(id_tuple) == 2 and id_tuple[0] == DOMAIN:
                    ajax_id = id_tuple[1].removeprefix(prefix)
                    if ajax_id not in known_ids:
                        stale_devices.append((ha_device.id, ajax_id))

        # Remove stale devices
        for ha_device_id, ajax_id in stale_devices:
            device_registry.async_remove_device(ha_device_id)
            _LOGGER.info(
                "Auto-removed stale device (Ajax ID: %s) from HA registry - no longer in Ajax account",
                ajax_id,
            )

        if stale_devices:
            _LOGGER.info(
                "Cleaned up %d stale device(s) from HA registry",
                len(stale_devices),
            )

    def _apply_smart_lock_rest_state(self, space: AjaxSpace, device_id: str, device_data: dict[str, Any]) -> None:
        """Apply a smart lock's state from its enriched hub-device record.

        The dedicated smart-locks endpoint exposes no lock state, and
        Yale-bridged locks emit no ``smartlock*`` events, so the device record's
        ``lockStatus`` / ``doorStatus`` is the only state source for them (#88).
        A real-time event is fresher than the poll, so a recent one wins.
        """
        lock = space.smart_locks.get(device_id)
        if lock is None:
            return  # not discovered yet; applied on a later poll cycle
        if lock.last_event_time is not None and (datetime.now(UTC) - lock.last_event_time).total_seconds() < 30:
            return
        lock_status = device_data.get("lockStatus")
        if lock_status in ("LOCKED", "UNLOCKED"):
            lock.is_locked = lock_status == "LOCKED"
        door_status = device_data.get("doorStatus")
        if door_status in ("OPEN", "CLOSED"):
            lock.is_door_open = door_status == "OPEN"

    async def _async_update_devices(self, space_id: str) -> None:
        """Update devices for a specific space."""
        if self.account is None:
            return
        space = self.account.spaces.get(space_id)
        if not space or not space.hub_id:
            return

        # Get all devices with full details in one API call (enrich=True)
        # The detailed data is nested in a "model" object that we merge into the device
        devices_list = await self.api.async_get_devices(space.hub_id, enrich=True)

        # Battery / signal fields are included in the enriched API response,
        # so we read them every refresh — no bespoke throttling needed.

        new_devices_count = 0
        processed_ids: set[str] = set()  # Track processed IDs to skip duplicates

        # Rebuild room membership from scratch every poll. device_ids is only
        # appended to (guarded by "not in"), so without this reset a device
        # moved between rooms would stay counted in its old room and a deleted
        # device would leave a stale id behind, inflating the per-room
        # device_count surfaced in the alarm panel attributes.
        for room in space.rooms.values():
            room.device_ids.clear()

        for device_summary in devices_list:
            device_id = device_summary.get("id")
            if not device_id:
                continue

            # Skip duplicate device IDs in the same API response
            # (MultiTransmitter can appear twice with different names)
            if device_id in processed_ids:
                _LOGGER.warning(
                    "Skipping duplicate device ID %s (%s) - already processed",
                    device_id,
                    device_summary.get("deviceName", "unknown"),
                )
                continue
            processed_ids.add(device_id)

            # With enrich=True, detailed data is in the "model" sub-object
            # Merge model into device_data so the rest of the code works
            device_data = dict(device_summary)
            # A non-conformant API variant can return ``model`` as null / a list
            # instead of the documented object; guard so a single bad payload
            # cannot abort the whole reconciliation cycle with a TypeError.
            if isinstance(device_summary.get("model"), dict):
                device_data.update(device_summary["model"])

            # Parse device type - API uses camelCase (deviceType, deviceName)
            raw_device_type = device_data.get("deviceType", device_data.get("type", "unknown"))
            device_type = self._parse_device_type(raw_device_type)

            # Smart locks get their own entities (see _async_update_smart_locks),
            # so they are skipped from the regular device flow — but this enriched
            # hub-device record is the ONLY place the lock state (lockStatus /
            # doorStatus) is exposed; the dedicated smart-locks endpoint returns
            # just the id, and Yale-bridged locks emit no smartlock* events, so
            # REST polling is their only state source (#88).
            if device_type == DeviceType.SMART_LOCK:
                self._apply_smart_lock_rest_state(space, device_id, device_data)
                continue

            # Get room_id and room_name
            room_id = device_data.get("roomId", device_data.get("room_id"))
            rooms_map = space.rooms_map
            room_name = rooms_map.get(room_id) if room_id else None

            # Create or update device
            if device_id not in space.devices:
                device = AjaxDevice(
                    id=device_id,
                    name=device_data.get("deviceName", device_data.get("name", "Unknown Device")),
                    type=device_type,
                    space_id=space_id,
                    hub_id=device_data.get("hub_id", space.hub_id or ""),
                    raw_type=raw_device_type,
                    room_id=room_id,
                    room_name=room_name,
                    group_id=device_data.get("groupId", device_data.get("group_id")),
                )
                space.devices[device_id] = device
                new_devices_count += 1
                if self._initial_load_done:
                    async_dispatcher_send(self.hass, SIGNAL_NEW_DEVICE, space_id, device_id)

                # Log new device with details for debugging
                multi_tx_id = device_data.get("multiTransmitterId", "")
                _LOGGER.debug(
                    "New device: %s (id=%s, type=%s, multiTxId=%s)",
                    device.name,
                    device_id,
                    raw_device_type,
                    multi_tx_id,
                )

                # Log warning for unknown device types
                if device_type == DeviceType.UNKNOWN:
                    _LOGGER.warning(
                        "Unknown device type detected: '%s' for device '%s' (ID: %s). "
                        "Please report this to the integration developer.",
                        raw_device_type,
                        device.name,
                        device_id,
                    )
            else:
                device = space.devices[device_id]
                # Update raw_type in case it changed
                device.raw_type = raw_device_type
                # Update room info
                device.room_id = room_id
                device.room_name = room_name

            # Apply the enriched payload onto the device — the per-family
            # attribute appliers (battery/signal, contacts, siren, sockets,
            # lightswitch…) live in ``_device_apply`` and honour the per-key
            # optimistic guards.
            apply_device_payload(device, device_data)

            # Update device attributes dict
            if "attributes" in device_data:
                # Normalize API attributes to internal format. Skip any key with
                # an optimistic update in flight (e.g. ``is_on`` recomputed from a
                # nested ``switchState``), otherwise this late merge would undo
                # the per-key guards applied to the root-level fields above.
                normalized_attrs = self._normalize_device_attributes(device_data["attributes"], device.type)
                device.attributes.update(
                    {key: value for key, value in normalized_attrs.items() if not device.is_optimistic(key)}
                )
            # Update room association
            if device.room_id and device.room_id in space.rooms:
                room = space.rooms[device.room_id]
                if device_id not in room.device_ids:
                    room.device_ids.append(device_id)

        # Log summary of devices loaded
        if new_devices_count > 0:
            _LOGGER.info("Discovered %d new device(s) in space %s", new_devices_count, space_id)

        # Clean up devices that no longer exist in Ajax
        # Safety: only remove if API returned at least some devices (avoid wiping on empty response)
        if processed_ids and space.devices:
            existing_device_ids = set(space.devices.keys())
            absent_device_ids = existing_device_ids - processed_ids

            # A single non-empty-but-partial 200 response (e.g. a caching proxy
            # serving a truncated device list) must not permanently delete
            # devices and their HA registry entries. Require the device to be
            # absent for several consecutive polls before removing it; a device
            # that reappears resets its counter.
            absent_threshold = 3
            removed_device_ids: set[str] = set()
            for device_id in existing_device_ids:
                device = space.devices[device_id]
                if device_id in absent_device_ids:
                    count = device.attributes.get("_absent_poll_count", 0) + 1
                    device.attributes["_absent_poll_count"] = count
                    if count >= absent_threshold:
                        removed_device_ids.add(device_id)
                else:
                    # Device is present again — clear any stale absence counter.
                    device.attributes.pop("_absent_poll_count", None)

            if removed_device_ids:
                device_registry = dr.async_get(self.hass)
                for device_id in removed_device_ids:
                    removed_device = space.devices.get(device_id)
                    device_name = removed_device.name if removed_device else device_id

                    # Remove from HA device registry
                    ha_device = device_registry.async_get_device(
                        identifiers={device_identifier(self.entry_id, device_id)}
                    )
                    if ha_device:
                        device_registry.async_remove_device(ha_device.id)
                        _LOGGER.info(
                            "Auto-removed device '%s' (ID: %s) from Home Assistant - deleted from Ajax",
                            device_name,
                            device_id,
                        )
                    else:
                        _LOGGER.debug(
                            "Device '%s' (ID: %s) not found in HA registry, removing from internal tracking only",
                            device_name,
                            device_id,
                        )

                    # Remove from internal tracking
                    del space.devices[device_id]

                _LOGGER.info(
                    "Cleaned up %d removed device(s) from space %s",
                    len(removed_device_ids),
                    space_id,
                )
