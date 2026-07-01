"""Door-sensor fast-polling mixin for ``AjaxDataCoordinator``.

When the system is disarmed (or in night mode for sensors excluded from it),
Ajax pushes no SSE/SQS events, so door / transmitter / wire-input contacts are
polled on a fast 5 s loop for responsive UX. This subsystem is a self-contained
mixin so the main coordinator file stays focused on the core update pipeline.

The mixin keeps all state on ``self``: the coordinator's ``__init__`` owns the
``_door_sensor_*`` attributes; the methods here just consume them. There is no
parallel state and no behaviour change versus the inline version.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .api import AjaxRestApiError, AjaxRestAuthError
from .const import UPDATE_INTERVAL_DOOR_SENSORS
from .models import DeviceType, SecurityState

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .api import AjaxRestApi
    from .const import AjaxConfigEntry
    from .models import AjaxAccount, AjaxSpace

_LOGGER = logging.getLogger(__name__)


class AjaxDoorPollingMixin:
    """Fast door-sensor polling while disarmed / night mode.

    Purely an extraction — it relies on the host coordinator for the shared
    ``_door_sensor_*`` state, the API client, and ``async_set_updated_data`` /
    ``_normalize_device_attributes``.
    """

    # Attributes / methods the host coordinator must provide. Declared here only
    # for the type checker — the values are set in the coordinator's __init__
    # (or provided by other mixins / DataUpdateCoordinator) and the mixin never
    # assigns them.
    if TYPE_CHECKING:
        hass: HomeAssistant
        api: AjaxRestApi
        account: AjaxAccount | None
        config_entry: AjaxConfigEntry | None
        _sse_url: str | None
        _door_sensor_poll_task: asyncio.Task[Any] | None
        _door_sensor_poll_security_state: SecurityState
        _door_sensor_fast_poll_enabled: bool

        def async_set_updated_data(self, data: AjaxAccount) -> None: ...
        def _normalize_device_attributes(
            self, api_attributes: dict[str, Any], device_type: DeviceType
        ) -> dict[str, Any]: ...
        def _apply_smart_lock_rest_state(
            self, space: AjaxSpace, device_id: str, device_data: dict[str, Any]
        ) -> None: ...

    @staticmethod
    def _parse_door_state_from_wiring(external_state: str | None, wiring_details: dict[str, Any] | None) -> bool:
        """Derive door-opened state from externalContactState + wiring scheme.

        Handles TWO_EOL / ONE_EOL / NO_EOL wiring schemas with the OR logic
        historically duplicated across update paths. Returns True when the
        door/contact is considered OPEN.
        """
        # Default based on top-level contact state
        new_state = external_state != "OK" if external_state is not None else False
        if not isinstance(wiring_details, dict):
            return new_state

        wiring_type = wiring_details.get("wiringSchemeType")
        if wiring_type == "TWO_EOL":
            contact_state = wiring_details.get("contactTwoDetails", {}).get("contactState")
            if contact_state:
                new_state = contact_state != "OK"
        elif wiring_type == "ONE_EOL":
            contact_state = wiring_details.get("contactDetails", {}).get("contactState")
            if contact_state and contact_state != "OK":
                new_state = True
        elif wiring_type == "NO_EOL":
            contact_state = wiring_details.get("contactState")
            if contact_state and contact_state != "OK":
                new_state = True
        return new_state

    def _manage_door_sensor_polling(self, should_poll: bool, security_state: SecurityState) -> None:
        """Start or stop door sensor fast polling.

        Args:
            should_poll: True to start polling, False to stop
            security_state: Current security state (used to filter sensors in night mode)
        """
        # The polling task is shared across ALL spaces, so a single armed space
        # must not cancel it while another space is still disarmed/night. Only
        # stop when NO space needs polling. (This call is made per-space with
        # just that space's state, so re-scan every space here.)
        if not should_poll and self.account is not None:
            should_poll = any(
                space.security_state in (SecurityState.DISARMED, SecurityState.NIGHT_MODE)
                for space in self.account.spaces.values()
            )

        # Check if fast polling is enabled (can be disabled to reduce API calls)
        # Also disable fast polling in proxy mode to reduce load on shared proxy
        if not self._door_sensor_fast_poll_enabled or self._sse_url:
            should_poll = False

        # Store[Any] security state for the polling loop
        self._door_sensor_poll_security_state = security_state

        if should_poll and self._door_sensor_poll_task is None and self.config_entry is not None:
            # Start door sensor polling when disarmed or in night mode
            self._door_sensor_poll_task = self.config_entry.async_create_background_task(
                self.hass, self._async_poll_door_sensors_loop(), "ajax_door_sensor_poll"
            )
            _LOGGER.info(
                "Started door sensor fast polling (every %ds, state: %s)",
                UPDATE_INTERVAL_DOOR_SENSORS,
                security_state.value,
            )
        elif not should_poll and self._door_sensor_poll_task is not None:
            # Stop door sensor polling when armed
            self._door_sensor_poll_task.cancel()
            self._door_sensor_poll_task = None
            _LOGGER.info("Stopped door sensor fast polling (system armed)")

    async def _async_poll_door_sensors_loop(self) -> None:
        """Continuous polling loop for door sensors.

        Polls door sensors in two scenarios:
        - When disarmed: poll all door sensors
        - When in night mode: poll only sensors excluded from night mode
        """
        try:
            while True:
                await asyncio.sleep(UPDATE_INTERVAL_DOOR_SENSORS)

                if not self.account:
                    continue

                # Poll door sensors and transmitters for each space.
                # Notify HA once after all spaces are processed so that N
                # changed spaces do not trigger N full entity recomputes.
                any_updated = False
                # Snapshot the spaces: the main poll coroutine can add a hub
                # to ``self.account.spaces`` during the ``await`` below, which
                # would otherwise raise "dictionary changed size during
                # iteration" and kill this background task permanently.
                for space_id, space in list(self.account.spaces.items()):
                    current_state = space.security_state

                    # Determine which sensors to poll based on security state
                    if current_state == SecurityState.DISARMED:
                        # When disarmed: poll all door sensors and transmitters
                        contact_sensors = [
                            device
                            for device in space.devices.values()
                            if device.type in (DeviceType.DOOR_CONTACT, DeviceType.TRANSMITTER, DeviceType.WIRE_INPUT)
                        ]
                    elif current_state == SecurityState.NIGHT_MODE:
                        # When in night mode: only poll sensors excluded from night mode
                        contact_sensors = [
                            device
                            for device in space.devices.values()
                            if device.type in (DeviceType.DOOR_CONTACT, DeviceType.TRANSMITTER, DeviceType.WIRE_INPUT)
                            and not device.attributes.get("night_mode_arm", False)
                        ]
                    else:
                        # Skip for other armed states
                        continue

                    # Smart locks ride the same enriched payload: a bridged lock
                    # (e.g. Yale Doorman behind LockBridge) emits no realtime
                    # events, so this fast poll is its only sub-minute state
                    # source (#88). Poll the space if it has either kind.
                    if not contact_sensors and not space.smart_locks:
                        continue

                    if not space.hub_id:
                        continue

                    try:
                        # Get all devices with enriched data (includes reedClosed)
                        devices_data = await self.api.async_get_devices(space.hub_id, enrich=True)
                        updated = False

                        for device_summary in devices_data:
                            device_id = device_summary.get("id")
                            if not device_id:
                                continue

                            if device_id in space.smart_locks:
                                # A bridged smart lock's lockStatus/doorStatus is in
                                # this same payload — apply it at the 5 s cadence so
                                # its lock/door state does not lag on the 30-60 s
                                # main poll (#88). A recent realtime event still wins
                                # inside _apply_smart_lock_rest_state.
                                lock = space.smart_locks[device_id]
                                lock_data = dict(device_summary)
                                if isinstance(device_summary.get("model"), dict):
                                    lock_data.update(device_summary["model"])
                                previous_lock_state = (lock.is_locked, lock.is_door_open)
                                self._apply_smart_lock_rest_state(space, device_id, lock_data)
                                if (lock.is_locked, lock.is_door_open) != previous_lock_state:
                                    updated = True
                                continue

                            if device_id in space.devices:
                                device = space.devices[device_id]

                                # With enrich=True, detailed data is in "model" sub-object
                                device_data = dict(device_summary)
                                # Guard against a non-dict ``model`` (null / list)
                                # so a bad payload cannot crash the refresh cycle.
                                if isinstance(device_summary.get("model"), dict):
                                    device_data.update(device_summary["model"])

                                if device.type == DeviceType.DOOR_CONTACT:
                                    # Get current state
                                    old_door_state = device.attributes.get("door_opened")

                                    # Check reedClosed directly from device_data
                                    reed_closed = device_data.get("reedClosed")
                                    external_state = device_data.get("externalContactState")

                                    # Calculate new door state
                                    if reed_closed is not None:
                                        new_door_state = not reed_closed
                                    elif external_state is not None:
                                        wiring_details = device_data.get("wiringSchemeSpecificDetails", {})
                                        new_door_state = self._parse_door_state_from_wiring(
                                            external_state, wiring_details
                                        )
                                    else:
                                        # Try attributes as fallback
                                        api_attrs = device_data.get("attributes", {})
                                        normalized_attrs = self._normalize_device_attributes(api_attrs, device.type)
                                        new_door_state = bool(normalized_attrs.get("door_opened", old_door_state))

                                    if old_door_state != new_door_state:
                                        device.attributes["door_opened"] = new_door_state
                                        _LOGGER.debug(
                                            "Door sensor %s state changed: %s -> %s",
                                            device.name,
                                            old_door_state,
                                            new_door_state,
                                        )
                                        updated = True

                                elif device.type == DeviceType.TRANSMITTER:
                                    # Handle Transmitter external contact state
                                    old_triggered = device.attributes.get("externalContactTriggered")
                                    new_triggered = device_data.get("externalContactTriggered")

                                    if new_triggered is not None and old_triggered != new_triggered:
                                        device.attributes["externalContactTriggered"] = new_triggered
                                        _LOGGER.debug(
                                            "Transmitter %s contact changed: %s -> %s",
                                            device.name,
                                            old_triggered,
                                            new_triggered,
                                        )
                                        updated = True

                                elif device.type == DeviceType.WIRE_INPUT:
                                    # Handle MultiTransmitter WireInput (same logic as DOOR_CONTACT)
                                    old_door_state = device.attributes.get("door_opened")
                                    external_state = device_data.get("externalContactState")

                                    if external_state is not None:
                                        wiring_details = device_data.get("wiringSchemeSpecificDetails", {})
                                        new_door_state = self._parse_door_state_from_wiring(
                                            external_state, wiring_details
                                        )

                                        if old_door_state != new_door_state:
                                            device.attributes["door_opened"] = new_door_state
                                            _LOGGER.debug(
                                                "WireInput %s state changed: %s -> %s",
                                                device.name,
                                                old_door_state,
                                                new_door_state,
                                            )
                                            updated = True

                        if updated:
                            any_updated = True

                    except (AjaxRestApiError, AjaxRestAuthError) as err:
                        _LOGGER.debug(
                            "Error polling door sensors for space %s: %s",
                            space_id,
                            err,
                        )

                if any_updated:
                    self.async_set_updated_data(self.account)

        except asyncio.CancelledError:
            _LOGGER.debug("Door sensor polling loop cancelled")
            raise
