"""State-update mixin for ``AjaxDataCoordinator``.

Carves out three families of methods from the main coordinator:

* **Parsers** that turn raw Ajax payloads into typed enums
  (``_parse_security_state``, ``_parse_device_type``).
* **Video-edge / smart-lock pollers** that reconcile the Ajax catalogue
  with the in-memory account (``_async_update_video_edges``,
  ``_async_update_smart_locks``) and the no-op notification updater.
* **Lookup helper** ``get_smart_lock``.

All state is owned by the host coordinator (``self.account``,
``self.api``, ``self.hass``, ``self._initial_load_done``); the mixin
adds no attributes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send

from ._device_type_map import DEVICE_TYPE_MAP
from ._ids import device_identifier
from .api import AjaxRestAuthError
from .const import SIGNAL_NEW_SMART_LOCK, SIGNAL_NEW_VIDEO_EDGE
from .models import (
    AjaxSmartLock,
    AjaxVideoEdge,
    DeviceType,
    SecurityState,
    VideoEdgeType,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .api import AjaxRestApi
    from .models import AjaxAccount, AjaxSpace

_LOGGER = logging.getLogger(__name__)


class AjaxStateUpdaterMixin:
    """Coordinator mixin: video-edge / smart-lock updates and parsers.

    These methods only read/write ``self.account`` and call out through
    ``self.api`` / ``self.hass``; the mixin does not introduce any
    attribute of its own.
    """

    # Host attributes — provided by the coordinator __init__.
    if TYPE_CHECKING:
        account: AjaxAccount | None
        api: AjaxRestApi
        hass: HomeAssistant
        entry_id: str
        _initial_load_done: bool

        def get_space(self, space_id: str) -> AjaxSpace | None: ...

    # ------------------------------------------------------------------
    # Notifications (no-op — kept for the public surface)
    # ------------------------------------------------------------------

    async def _async_update_notifications(self, space_id: str, limit: int = 50) -> None:
        """Update notifications for a specific space.

        Notifications are delivered via SQS/SSE real-time events; there is
        no polling API endpoint for them. This method is a no-op kept for
        the public surface — it only ensures ``unread_notifications``
        stays in sync.
        """
        if self.account is None:
            return
        space = self.account.spaces.get(space_id)
        if not space:
            return
        space.unread_notifications = sum(1 for n in space.notifications if not n.read)

    # ------------------------------------------------------------------
    # Video edges (surveillance cameras)
    # ------------------------------------------------------------------

    async def _async_update_video_edges(self, space_id: str) -> None:
        """Update video edge devices (surveillance cameras) for a specific space."""
        if self.account is None:
            return
        space = self.account.spaces.get(space_id)
        if not space:
            return

        # Need real_space_id to fetch video edges.
        if not space.real_space_id:
            _LOGGER.debug("No real_space_id for space %s, skipping video edges", space_id)
            return
        _LOGGER.debug("Fetching video edges for space %s with real_space_id %s", space_id, space.real_space_id)

        try:
            video_edges_data = await self.api.async_get_video_edges(space.real_space_id)
            _LOGGER.debug(
                "Found %d video edge(s) for space %s",
                len(video_edges_data),
                space_id,
            )

            processed_ve_ids: set[str] = set()

            for ve_data in video_edges_data:
                ve_id = ve_data.get("id")
                if not ve_id:
                    continue

                processed_ve_ids.add(ve_id)

                ve_type_str = ve_data.get("type", "UNKNOWN")
                try:
                    ve_type = VideoEdgeType(ve_type_str)
                except ValueError:
                    ve_type = VideoEdgeType.UNKNOWN

                network = ve_data.get("networkInterface", {})
                ethernet = network.get("ethernet", {})
                wifi = network.get("wifi", {})

                # IP from ethernet, fallback to wifi.
                ip_address = None
                eth_config = ethernet.get("configuration", {}) if ethernet else {}
                wifi_config = wifi.get("configuration", {}) if wifi else {}
                if eth_config.get("v4", {}).get("address"):
                    ip_address = eth_config["v4"]["address"]
                elif wifi_config.get("v4", {}).get("address"):
                    ip_address = wifi_config["v4"]["address"]

                mac_address = (ethernet.get("macAddress") if ethernet else None) or (
                    wifi.get("macAddress") if wifi else None
                )

                firmware = ve_data.get("firmware", {})
                firmware_version = firmware.get("currentVersion")
                connection_state = ve_data.get("connectionState", "UNKNOWN")

                # Normalise channels to a list of dicts.
                channels_raw = ve_data.get("channels", [])
                if isinstance(channels_raw, dict):
                    channels = [channels_raw]
                elif isinstance(channels_raw, list):
                    channels = [c for c in channels_raw if isinstance(c, dict)]
                else:
                    channels = []

                room_id = None
                room_name = None
                if channels:
                    first_channel = channels[0]
                    space_settings = first_channel.get("spaceSettings", {})
                    room_id = space_settings.get("roomId")
                    if room_id and room_id in space.rooms:
                        room_name = space.rooms[room_id].name

                if ve_id not in space.video_edges:
                    video_edge = AjaxVideoEdge(
                        id=ve_id,
                        name=ve_data.get("name", f"Camera {ve_id[:6]}"),
                        space_id=space_id,
                        video_edge_type=ve_type,
                        color=ve_data.get("color"),
                        ip_address=ip_address,
                        mac_address=mac_address,
                        firmware_version=firmware_version,
                        connection_state=connection_state,
                        channels=channels,
                        room_id=room_id,
                        room_name=room_name,
                        raw_data=ve_data,
                    )
                    space.video_edges[ve_id] = video_edge
                    if self._initial_load_done:
                        async_dispatcher_send(self.hass, SIGNAL_NEW_VIDEO_EDGE, space_id, ve_id)
                    _LOGGER.info(
                        "Added video edge: %s (%s) - %s",
                        video_edge.name,
                        video_edge.video_edge_type.value,
                        connection_state,
                    )
                else:
                    video_edge = space.video_edges[ve_id]
                    video_edge.name = ve_data.get("name", video_edge.name)
                    video_edge.video_edge_type = ve_type
                    video_edge.color = ve_data.get("color")
                    video_edge.ip_address = ip_address
                    video_edge.mac_address = mac_address
                    video_edge.firmware_version = firmware_version
                    video_edge.connection_state = connection_state
                    video_edge.channels = channels
                    video_edge.room_id = room_id
                    video_edge.room_name = room_name
                    video_edge.raw_data = ve_data

            # Clean up video edges that disappeared from Ajax. Safety: only
            # remove when the API returned at least one device, otherwise an
            # empty response (network blip) would wipe the lot.
            if processed_ve_ids and space.video_edges:
                existing_ve_ids = set(space.video_edges.keys())
                removed_ve_ids = existing_ve_ids - processed_ve_ids

                if removed_ve_ids:
                    device_registry = dr.async_get(self.hass)
                    for ve_id in removed_ve_ids:
                        removed_ve = space.video_edges.get(ve_id)
                        ve_name = removed_ve.name if removed_ve else ve_id

                        ha_device = device_registry.async_get_device(
                            identifiers={device_identifier(self.entry_id, ve_id)}
                        )
                        if ha_device:
                            device_registry.async_remove_device(ha_device.id)
                            _LOGGER.info(
                                "Auto-removed video edge '%s' (ID: %s) from Home Assistant - deleted from Ajax",
                                ve_name,
                                ve_id,
                            )
                        else:
                            _LOGGER.debug(
                                "Video edge '%s' (ID: %s) not found in HA registry, removing from internal tracking only",
                                ve_name,
                                ve_id,
                            )

                        del space.video_edges[ve_id]

                    _LOGGER.info(
                        "Cleaned up %d removed video edge(s) from space %s",
                        len(removed_ve_ids),
                        space_id,
                    )

        except AjaxRestAuthError:
            # Token expiry must propagate so it counts toward the reauth
            # threshold in the coordinator; do not swallow it here.
            raise
        except Exception as err:
            _LOGGER.warning("Error updating video edges for space %s: %s", space_id, err)

    # ------------------------------------------------------------------
    # Smart locks (LockBridge Jeweller)
    # ------------------------------------------------------------------

    async def _async_update_smart_locks(self, space_id: str) -> None:
        """Update smart lock devices for a specific space."""
        if self.account is None:
            return
        space = self.account.spaces.get(space_id)
        if not space:
            return

        if not space.real_space_id:
            _LOGGER.debug("No real_space_id for space %s, skipping smart locks", space_id)
            return

        try:
            smart_locks_data = await self.api.async_get_smart_locks(space.real_space_id)
            _LOGGER.debug(
                "Found %d smart lock(s) for space %s",
                len(smart_locks_data),
                space_id,
            )

            processed_ids: set[str] = set()

            for sl_data in smart_locks_data:
                sl_id = sl_data.get("id")
                if not sl_id:
                    continue

                processed_ids.add(sl_id)

                if sl_id not in space.smart_locks:
                    smart_lock = AjaxSmartLock(
                        id=sl_id,
                        name=sl_data.get("name", f"Smart Lock {sl_id[:6]}"),
                        space_id=space_id,
                        raw_data=sl_data,
                    )

                    # Yale cloud locks return minimal data (only 'id', no
                    # 'name'/'type') — let the native Yale integration
                    # handle them instead of half-creating empty entities.
                    if smart_lock.is_yale_cloud_device:
                        _LOGGER.debug(
                            "Skipping Yale cloud lock %s (no name/type in API, use native Yale integration)",
                            sl_id,
                        )
                        continue

                    space.smart_locks[sl_id] = smart_lock
                    if self._initial_load_done:
                        async_dispatcher_send(self.hass, SIGNAL_NEW_SMART_LOCK, space_id, sl_id)
                    _LOGGER.info("Discovered smart lock: %s (%s)", smart_lock.name, sl_id)
                else:
                    existing = space.smart_locks[sl_id]
                    existing.raw_data = sl_data
                    if sl_data.get("name"):
                        existing.name = sl_data["name"]

            # Clean up API-discovered locks that disappeared; SSE/SQS-
            # discovered locks have no raw_data and must be preserved.
            if processed_ids and space.smart_locks:
                existing_ids = set(space.smart_locks.keys())
                removed_ids = existing_ids - processed_ids

                if removed_ids:
                    device_registry = dr.async_get(self.hass)
                    for sl_id in removed_ids:
                        removed_sl = space.smart_locks.get(sl_id)

                        if removed_sl and not removed_sl.raw_data:
                            _LOGGER.debug(
                                "Preserving SSE/SQS-discovered smart lock '%s' (ID: %s)",
                                removed_sl.name,
                                sl_id,
                            )
                            continue

                        sl_name = removed_sl.name if removed_sl else sl_id

                        ha_device = device_registry.async_get_device(
                            identifiers={device_identifier(self.entry_id, sl_id)}
                        )
                        if ha_device:
                            device_registry.async_remove_device(ha_device.id)
                            _LOGGER.info(
                                "Auto-removed smart lock '%s' (ID: %s) from Home Assistant",
                                sl_name,
                                sl_id,
                            )

                        del space.smart_locks[sl_id]

        except AjaxRestAuthError:
            # Token expiry must propagate so it counts toward the reauth
            # threshold in the coordinator; do not swallow it here.
            raise
        except Exception as err:
            _LOGGER.warning("Error updating smart locks for space %s: %s", space_id, err)

    def get_smart_lock(self, space_id: str, smart_lock_id: str) -> AjaxSmartLock | None:
        """Get a smart lock by space and smart lock ID."""
        space = self.get_space(space_id)
        return space.smart_locks.get(smart_lock_id) if space else None

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_security_state(self, state_value: Any) -> SecurityState:
        """Parse security state from an API response value."""
        if isinstance(state_value, str):
            state_str = state_value.upper()
            # Check DISARMED first, before ARMED — "DISARMED" contains "ARMED".
            if "DISARMED" in state_str:
                return SecurityState.DISARMED
            if "PARTIALLY" in state_str:
                return SecurityState.PARTIALLY_ARMED
            # NIGHT_MODE_ON specifically, not "NIGHT" alone:
            # ARMED_NIGHT_MODE_OFF contains "NIGHT" but is actually ARMED.
            # Also handle the bare "NIGHT_MODE" form.
            if "NIGHT_MODE_ON" in state_str or state_str == "NIGHT_MODE":
                return SecurityState.NIGHT_MODE
            if "ARMED" in state_str:
                return SecurityState.ARMED

        return SecurityState.NONE

    def _parse_device_type(self, type_str: str) -> DeviceType:
        """Parse a raw Ajax type string into a DeviceType."""
        if not isinstance(type_str, str):
            return DeviceType.UNKNOWN

        # Clean up formatting artifacts (e.g. "wire_input_mt {\n}\n").
        type_cleaned = type_str.strip().split()[0].lower() if type_str else ""
        type_lower = type_str.lower()

        if type_cleaned in DEVICE_TYPE_MAP:
            return DEVICE_TYPE_MAP[type_cleaned]
        if type_lower in DEVICE_TYPE_MAP:
            return DEVICE_TYPE_MAP[type_lower]

        # Partial match fallback (alias hit on substrings).
        for key, device_type in DEVICE_TYPE_MAP.items():
            if key in type_lower or type_lower in key:
                return device_type

        _LOGGER.warning(
            "Unknown device type '%s' - please report this to help improve the integration. "
            "Device will be marked as UNKNOWN.",
            type_str,
        )
        return DeviceType.UNKNOWN
