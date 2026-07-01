"""Devices polling mixin for ``AjaxDataCoordinator``.

Owns the heavy device-reconciliation pipeline:

* `_async_update_devices`: the big per-space loop that walks the REST
  payload, normalises the attributes, creates the AjaxDevice entries and
  fans out `SIGNAL_NEW_DEVICE` on first sight.
* `_async_cleanup_stale_devices`: prunes the HA device registry for IDs
  that no longer appear in the Ajax account.

The stateless helpers `_normalize_device_attributes` (raw Ajax field names ->
handler shapes) and `_reset_expired_motion_detections` (clears the
motion_detected impulse after the 30 s window) live in ``_device_normalize``
(``AjaxDeviceNormalizeMixin``), inherited by this mixin.

State stays on ``self`` (``account``, ``api``, ``hass``, the optimistic
guards on each ``AjaxDevice``); the mixin owns no attributes.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send

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

            # Update basic device attributes from list endpoint
            device.online = device_data.get("online", True)
            device.bypassed = device_data.get("bypassed", False)

            # malfunctions can be a list or an int - normalize to int (count)
            malfunctions_data = device_data.get("malfunctions", 0)
            if isinstance(malfunctions_data, list):
                device.malfunctions = len(malfunctions_data)
            else:
                device.malfunctions = malfunctions_data

            # Battery and signal are present in the enriched response — always
            # update so the UI reflects battery swaps / radio quality changes.
            # Battery level: try multiple field names
            # Round to int to avoid jitter on last decimal
            battery = device_data.get("batteryChargeLevelPercentage")
            if battery is None:
                battery = device_data.get("batteryPercents")
            if battery is None:
                battery = device_data.get("battery_level")
            if battery is not None:
                battery = round(battery)
            device.battery_level = battery
            device.battery_state = device_data.get("batteryState", device_data.get("battery_state"))
            # Convert signal level string to percentage
            signal_level = device_data.get("signalLevel", device_data.get("signal_strength"))
            if isinstance(signal_level, str):
                signal_map = {
                    "EXCELLENT": 100,
                    "STRONG": 85,
                    "GOOD": 70,
                    "NORMAL": 60,
                    "MEDIUM": 50,
                    "WEAK": 30,
                    "POOR": 15,
                }
                device.signal_strength = signal_map.get(signal_level.upper())
            elif signal_level is not None:
                # Round to int to avoid jitter on last decimal
                device.signal_strength = round(signal_level)
            else:
                device.signal_strength = signal_level

            device.firmware_version = device_data.get("firmwareVersion", device_data.get("firmware_version"))
            device.hardware_version = device_data.get("hardwareVersion", device_data.get("hardware_version"))
            device.states = device_data.get("states", [])

            # Store tampered status in attributes
            if "tampered" in device_data:
                device.attributes["tampered"] = device_data.get("tampered", False)

            # Store temperature if available (DoorProtect Plus)
            # Round to 1 decimal to avoid jitter on last decimal
            if "temperature" in device_data:
                temp = device_data.get("temperature")
                if temp is not None:
                    temp = round(temp, 1)
                device.attributes["temperature"] = temp

            # Store other useful attributes
            # For MultiTransmitterWireInput, settings are in wiredDeviceSettings
            wired_settings = device_data.get("wiredDeviceSettings") or {}

            if not device.is_optimistic("always_active"):
                if "alwaysActive" in device_data:
                    device.attributes["always_active"] = device_data.get("alwaysActive", False)
                elif "alwaysActive" in wired_settings:
                    device.attributes["always_active"] = wired_settings.get("alwaysActive", False)

            if not device.is_optimistic("night_mode_arm"):
                if "nightModeArm" in device_data or "armedInNightMode" in device_data:
                    night_mode_value = device_data.get(
                        "nightModeArm",
                        device_data.get("armedInNightMode", False),
                    )
                    device.attributes["armed_in_night_mode"] = night_mode_value
                    device.attributes["night_mode_arm"] = night_mode_value  # Alias for handlers
                elif "nightModeArm" in wired_settings:
                    # MultiTransmitterWireInput: nightModeArm is in wiredDeviceSettings
                    night_mode_value = wired_settings.get("nightModeArm", False)
                    device.attributes["armed_in_night_mode"] = night_mode_value
                    device.attributes["night_mode_arm"] = night_mode_value

            # DoorProtect Plus specific attributes. These four back optimistic
            # config switches (switch.py marks them optimistic on toggle), so the
            # poller must honour the reservation or the switch bounces back.
            if "extraContactAware" in device_data and not device.is_optimistic("extra_contact_aware"):
                device.attributes["extra_contact_aware"] = device_data.get("extraContactAware", False)
            if "shockSensorAware" in device_data and not device.is_optimistic("shock_sensor_aware"):
                device.attributes["shock_sensor_aware"] = device_data.get("shockSensorAware", False)
            if "accelerometerAware" in device_data and not device.is_optimistic("accelerometer_aware"):
                device.attributes["accelerometer_aware"] = device_data.get("accelerometerAware", False)
            if "shockSensorSensitivity" in device_data:
                device.attributes["shock_sensor_sensitivity"] = device_data.get("shockSensorSensitivity", 0)
            if "accelerometerTiltDegrees" in device_data:
                device.attributes["accelerometer_tilt_degrees"] = device_data.get("accelerometerTiltDegrees", 5)
            if "ignoreSimpleImpact" in device_data and not device.is_optimistic("ignore_simple_impact"):
                device.attributes["ignore_simple_impact"] = device_data.get("ignoreSimpleImpact", False)
            if "sirenTriggers" in device_data and not device.is_optimistic("siren_triggers"):
                device.attributes["siren_triggers"] = device_data.get("sirenTriggers", [])

            # Door contact state (reedClosed -> door_opened)
            if "reedClosed" in device_data:
                # reedClosed=True means door is closed, so door_opened=False
                device.attributes["door_opened"] = not device_data.get("reedClosed", True)
            # External contact state (extraContactClosed -> external_contact_opened)
            if "extraContactClosed" in device_data:
                device.attributes["external_contact_opened"] = not device_data.get("extraContactClosed", True)
            # Multitransmitter state (externalContactState -> door_opened)
            if "externalContactState" in device_data:
                ext_state = device_data.get("externalContactState")
                # externalContactState: OK=closed, TRIGGERED=open
                door_opened = ext_state != "OK"

                # Check wiringSchemeSpecificDetails for more accurate state
                wiring_details = device_data.get("wiringSchemeSpecificDetails", {})
                wiring_type = wiring_details.get("wiringSchemeType")

                # Store wiring type for handler to know if tamper sensor is available
                if wiring_type:
                    device.attributes["wiring_type"] = wiring_type

                if wiring_type == "TWO_EOL":
                    # TWO_EOL: contactOneDetails is tamper, contactTwoDetails is door
                    contact_one = wiring_details.get("contactOneDetails", {})
                    contact_two = wiring_details.get("contactTwoDetails", {})

                    # Parse tamper state from contactOneDetails
                    tamper_state = contact_one.get("contactState")
                    if tamper_state:
                        device.attributes["tampered"] = tamper_state != "OK"

                    # Parse door state from contactTwoDetails
                    contact_state = contact_two.get("contactState")
                    if contact_state:
                        door_opened = contact_state != "OK"
                elif wiring_type == "ONE_EOL":
                    # OR logic: open if externalContactState OR contactDetails says TRIGGERED
                    contact_details = wiring_details.get("contactDetails", {})
                    contact_state = contact_details.get("contactState")
                    if contact_state and contact_state != "OK":
                        door_opened = True
                elif wiring_type == "NO_EOL":
                    # OR logic: open if externalContactState OR contactState says TRIGGERED
                    contact_state = wiring_details.get("contactState")
                    if contact_state and contact_state != "OK":
                        door_opened = True

                device.attributes["door_opened"] = door_opened

            # Transmitter external contact triggered (boolean, separate from externalContactState)
            if "externalContactTriggered" in device_data:
                device.attributes["externalContactTriggered"] = device_data.get("externalContactTriggered", False)

            # Sensitivity (GlassProtect, MotionProtect, etc.)
            if "sensitivity" in device_data:
                device.attributes["sensitivity"] = device_data.get("sensitivity")

            # Device color
            if "color" in device_data:
                device.attributes["color"] = device_data.get("color")

            # Siren specific attributes
            # Prefer v2sirenVolumeLevel (supports DISABLED), fallback to deprecated sirenVolumeLevel
            if "v2sirenVolumeLevel" in device_data:
                device.attributes["siren_volume_level"] = device_data.get("v2sirenVolumeLevel")
            elif "sirenVolumeLevel" in device_data:
                device.attributes["siren_volume_level"] = device_data.get("sirenVolumeLevel")
            if "beepVolumeLevel" in device_data:
                device.attributes["beep_volume_level"] = device_data.get("beepVolumeLevel")
            if "alarmDuration" in device_data:
                device.attributes["alarm_duration"] = device_data.get("alarmDuration")
            if not device.is_optimistic("led_indication"):
                if "v2sirenIndicatorLightMode" in device_data:
                    device.attributes["led_indication"] = device_data.get("v2sirenIndicatorLightMode")
                elif "blinkWhileArmed" in device_data:
                    device.attributes["led_indication"] = device_data.get("blinkWhileArmed")
            # Siren beep/chime settings
            if "beepOnArmDisarm" in device_data:
                device.attributes["beep_on_arm_disarm"] = device_data.get("beepOnArmDisarm")
            if "beepOnDelay" in device_data:
                device.attributes["beep_on_delay"] = device_data.get("beepOnDelay")
            if "chimesEnabled" in device_data and not device.is_optimistic("chimes_enabled"):
                device.attributes["chimes_enabled"] = device_data.get("chimesEnabled")
            if "buzzerState" in device_data:
                device.attributes["buzzer_state"] = device_data.get("buzzerState")
            # StreetSiren specific
            if "alertIfMoved" in device_data:
                device.attributes["alert_if_moved"] = device_data.get("alertIfMoved")
            if "externallyPowered" in device_data and not device.is_optimistic("externally_powered"):
                device.attributes["externally_powered"] = device_data.get("externallyPowered")
            # Siren advanced settings
            if "postAlarmIndicationMode" in device_data:
                device.attributes["post_alarm_indication_mode"] = device_data.get("postAlarmIndicationMode")
            if "alarmRestrictionMode" in device_data:
                device.attributes["alarm_restriction_mode"] = device_data.get("alarmRestrictionMode")
            if "blinkWhileArmed" in device_data and not device.is_optimistic("blink_while_armed"):
                device.attributes["blink_while_armed"] = device_data.get("blinkWhileArmed")

            # LED indicator mode (all devices)
            if "indicatorLightMode" in device_data:
                device.attributes["indicatorLightMode"] = device_data.get("indicatorLightMode")

            # FireProtect 2 specific attributes (smoke, CO, temperature, steam alarms)
            if "coAlarmEnable" in device_data:
                device.attributes["coAlarmEnable"] = device_data.get("coAlarmEnable")
            if "tempAlarmEnable" in device_data:
                device.attributes["tempAlarmEnable"] = device_data.get("tempAlarmEnable")
            if "tempDiffAlarmEnable" in device_data:
                device.attributes["tempDiffAlarmEnable"] = device_data.get("tempDiffAlarmEnable")
            if "smokeAlarm" in device_data:
                device.attributes["smokeAlarm"] = device_data.get("smokeAlarm")
            if "coAlarm" in device_data:
                device.attributes["coAlarm"] = device_data.get("coAlarm")
            if "steamAlarm" in device_data:
                device.attributes["steamAlarm"] = device_data.get("steamAlarm")
            if "tempAlarm" in device_data:
                device.attributes["tempAlarm"] = device_data.get("tempAlarm")
                # Create boolean for binary sensor (TEMP_ALARM_DETECTED vs TEMP_ALARM_NOT_DETECTED)
                device.attributes["temperatureAlarmDetected"] = device_data.get("tempAlarm") == "TEMP_ALARM_DETECTED"
            if "tempHighDiffAlarm" in device_data:
                device.attributes["tempHighDiffAlarm"] = device_data.get("tempHighDiffAlarm")
                # Create boolean for binary sensor
                device.attributes["highTemperatureDiffDetected"] = (
                    device_data.get("tempHighDiffAlarm") == "TEMP_HIGH_DIFF_ALARM_DETECTED"
                )

            # Alerts by sirens setting
            if "alertsBySirens" in device_data:
                device.attributes["alertsBySirens"] = device_data.get("alertsBySirens", False)

            # MotionCam specific attributes
            if "imageResolution" in device_data:
                device.attributes["imageResolution"] = device_data.get("imageResolution")
            if "photosPerAlarm" in device_data:
                device.attributes["photosPerAlarm"] = device_data.get("photosPerAlarm")

            # LifeQuality specific attributes (CO2, temperature, humidity sensors)
            if "actualCO2" in device_data:
                device.attributes["actualCO2"] = device_data.get("actualCO2")
            if "actualTemperature" in device_data:
                device.attributes["actualTemperature"] = device_data.get("actualTemperature")
            if "actualHumidity" in device_data:
                device.attributes["actualHumidity"] = device_data.get("actualHumidity")
            if "minComfortCO2" in device_data:
                device.attributes["minComfortCO2"] = device_data.get("minComfortCO2")
            if "maxComfortCO2" in device_data:
                device.attributes["maxComfortCO2"] = device_data.get("maxComfortCO2")
            if "minComfortTemperature" in device_data:
                device.attributes["minComfortTemperature"] = device_data.get("minComfortTemperature")
            if "maxComfortTemperature" in device_data:
                device.attributes["maxComfortTemperature"] = device_data.get("maxComfortTemperature")
            if "minComfortHumidity" in device_data:
                device.attributes["minComfortHumidity"] = device_data.get("minComfortHumidity")
            if "maxComfortHumidity" in device_data:
                device.attributes["maxComfortHumidity"] = device_data.get("maxComfortHumidity")
            if "calibrationState" in device_data:
                device.attributes["calibrationState"] = device_data.get("calibrationState")
            if "indication" in device_data:
                device.attributes["indication"] = device_data.get("indication")

            # WaterStop specific attributes (smart water valve).
            # ``valveState`` is handled separately so an in-flight optimistic
            # open/close is not bounced back by a poll (see AjaxValve._set_valve_state).
            waterstop_attrs = [
                "motorState",
                "tempProtectState",
                "extPower",
                "preventionEnable",
                "preventionDaysPeriod",
                "preventionExecuteHours",
                "preventionExecuteMinutes",
                "buttonCfg",
                "indicationMode",
            ]
            for attr in waterstop_attrs:
                if attr in device_data:
                    device.attributes[attr] = device_data.get(attr)
            if "valveState" in device_data and not device.is_optimistic("valveState"):
                device.attributes["valveState"] = device_data.get("valveState")

            # Socket/Relay/WallSwitch: Parse switchState to is_on (direct from enriched data).
            # Skip while an optimistic toggle is in flight, otherwise a poll arriving
            # before the device reports its new state bounces the switch back.
            if "switchState" in device_data and not device.is_optimistic("is_on"):
                switch_state = device_data["switchState"]
                # switchState is a list: [] = on, ["SWITCHED_OFF"] = off
                # Device is OFF only if SWITCHED_OFF is explicitly in the list
                if isinstance(switch_state, list):
                    device.attributes["is_on"] = "SWITCHED_OFF" not in switch_state
                else:
                    device.attributes["is_on"] = True

            # SocketOutlet (Type E/F): Parse socketState to is_on
            # socketState is a list: ["FIRST_CHANNEL_ON"] = on, [] or ["FIRST_CHANNEL_OFF"] = off
            if "socketState" in device_data and not device.is_optimistic("is_on"):
                socket_state = device_data["socketState"]
                if isinstance(socket_state, list):
                    device.attributes["is_on"] = "FIRST_CHANNEL_ON" in socket_state
                else:
                    device.attributes["is_on"] = False

            # Socket specific attributes (power monitoring, protection settings)
            if "indicationEnabled" in device_data:
                device.attributes["indicationEnabled"] = device_data.get("indicationEnabled")
            if "indicationBrightness" in device_data:
                device.attributes["indicationBrightness"] = device_data.get("indicationBrightness")
            if "currentProtectionEnabled" in device_data:
                device.attributes["currentProtectionEnabled"] = device_data.get("currentProtectionEnabled")
            if "voltageProtectionEnabled" in device_data:
                device.attributes["voltageProtectionEnabled"] = device_data.get("voltageProtectionEnabled")
            if "contactNormalState" in device_data:
                device.attributes["contactNormalState"] = device_data.get("contactNormalState")
            if "lockupRelayMode" in device_data:
                device.attributes["lockupRelayMode"] = device_data.get("lockupRelayMode")
            if "lockupRelayTimeSeconds" in device_data:
                device.attributes["lockupRelayTimeSeconds"] = device_data.get("lockupRelayTimeSeconds")
            # Power monitoring values
            # powerConsumedWattsPerHour = energy consumed (for Socket without Outlet suffix)
            if "powerConsumedWattsPerHour" in device_data:
                wh = device_data.get("powerConsumedWattsPerHour")
                device.attributes["energy"] = (wh / 1000.0) if wh is not None else 0  # Wh -> kWh
            # powerConsumptionWatts = instantaneous power (for SocketOutlet)
            if "powerConsumptionWatts" in device_data:
                device.attributes["power"] = device_data.get("powerConsumptionWatts")
            # currentMilliAmpers (with 's') for Socket
            if "currentMilliAmpers" in device_data:
                ma = device_data.get("currentMilliAmpers")
                device.attributes["current"] = (ma / 1000.0) if ma is not None else 0
            # currentMilliAmpere (without 's') for SocketOutlet
            if "currentMilliAmpere" in device_data:
                ma = device_data.get("currentMilliAmpere")
                device.attributes["current"] = (ma / 1000.0) if ma is not None else 0
            if "voltageVolts" in device_data:
                device.attributes["voltage"] = device_data.get("voltageVolts")
            # Current threshold (SocketOutlet)
            if "currentThresholdAmpere" in device_data:
                device.attributes["current_threshold"] = device_data.get("currentThresholdAmpere")
            # Indication settings (SocketOutlet)
            if "indicationMode" in device_data:
                device.attributes["indicationMode"] = device_data.get("indicationMode")
                # indicationEnabled derived from indicationMode
                device.attributes["indicationEnabled"] = device_data.get("indicationMode") == "ENABLED"
            if "indicationBrightnessV2" in device_data:
                device.attributes["indicationBrightness"] = device_data.get("indicationBrightnessV2")

            # Button specific attributes (panic button, keyfob)
            if "buttonMode" in device_data:
                device.attributes["button_mode"] = device_data.get("buttonMode")
            if "brightness" in device_data:
                device.attributes["brightness"] = device_data.get("brightness")
            if "falsePressFilter" in device_data:
                device.attributes["false_press_filter"] = device_data.get("falsePressFilter")
            if "customAlarmType" in device_data:
                device.attributes["custom_alarm_type"] = device_data.get("customAlarmType")
                # Also store camelCase for TransmitterHandler compatibility
                device.attributes["customAlarmType"] = device_data.get("customAlarmType")
            if "associatedUserId" in device_data:
                device.attributes["associated_user_id"] = device_data.get("associatedUserId")

            # LightSwitch multi-gang: Parse channelStatuses and button names
            # These are at root level of device_data, not inside "attributes"
            if "channelStatuses" in device_data:
                channel_statuses = device_data.get("channelStatuses", [])
                _LOGGER.debug(
                    "LightSwitch %s channelStatuses from API: %s",
                    device.name,
                    channel_statuses,
                )
                # Check if device has pending optimistic update (don't overwrite for 15 seconds)
                optimistic_until = device.attributes.get("_optimistic_until", 0)
                if time.time() < optimistic_until:
                    _LOGGER.debug(
                        "Skipping channelStatuses update for %s (optimistic update pending)",
                        device.name,
                    )
                else:
                    device.attributes["channelStatuses"] = channel_statuses  # Raw list
                    device.attributes["channel_1_on"] = "CHANNEL_1_ON" in channel_statuses
                    device.attributes["channel_2_on"] = "CHANNEL_2_ON" in channel_statuses

            # LightSwitchDimmer: Parse brightness attributes (at root level).
            # Honour the optimistic guard light.py sets on turn_on/off so a poll
            # landing inside the TTL window does not revert the user's change.
            if "actualBrightnessCh1" in device_data and not device.is_optimistic("actualBrightnessCh1"):
                device.attributes["actualBrightnessCh1"] = device_data.get("actualBrightnessCh1")
            if "minBrightnessLimitCh1" in device_data:
                device.attributes["minBrightnessLimitCh1"] = device_data.get("minBrightnessLimitCh1")
            if "maxBrightnessLimitCh1" in device_data:
                device.attributes["maxBrightnessLimitCh1"] = device_data.get("maxBrightnessLimitCh1")
            if "armActionBrightnessCh1" in device_data:
                device.attributes["armActionBrightnessCh1"] = device_data.get("armActionBrightnessCh1")
            if "disarmActionBrightnessCh1" in device_data:
                device.attributes["disarmActionBrightnessCh1"] = device_data.get("disarmActionBrightnessCh1")
            if "brightnessChangeSpeed" in device_data:
                device.attributes["brightnessChangeSpeed"] = device_data.get("brightnessChangeSpeed")

            # LightSwitch settings and protection statuses
            if "settingsSwitch" in device_data and not device.is_optimistic("settingsSwitch"):
                device.attributes["settingsSwitch"] = device_data.get("settingsSwitch", [])
            if "protectStatuses" in device_data:
                device.attributes["protectStatuses"] = device_data.get("protectStatuses", [])
            if "touchSensitivity" in device_data:
                device.attributes["touchSensitivity"] = device_data.get("touchSensitivity")
            if "touchMode" in device_data:
                device.attributes["touchMode"] = device_data.get("touchMode")
            if "dimmerSettings" in device_data:
                device.attributes["dimmerSettings"] = device_data.get("dimmerSettings", {})
            if "dataChannelSignalQuality" in device_data:
                device.attributes["dataChannelSignalQuality"] = device_data.get("dataChannelSignalQuality")
            if "dataChannelOk" in device_data:
                device.attributes["dataChannelOk"] = device_data.get("dataChannelOk")
            if "panelColor" in device_data:
                device.attributes["panelColor"] = device_data.get("panelColor")

            if "buttonOne" in device_data or "buttonTwo" in device_data:
                button_one = device_data.get("buttonOne")
                button_two = device_data.get("buttonTwo")
                _LOGGER.debug(
                    "LightSwitch %s (%s): buttonOne=%s (type=%s), buttonTwo=%s (type=%s)",
                    device.name,
                    device.raw_type,
                    button_one,
                    type(button_one).__name__,
                    button_two,
                    type(button_two).__name__,
                )
                # Handle both formats: string directly ("Switch 1") or object {"buttonName": "..."}
                if button_one:
                    if isinstance(button_one, str):
                        device.attributes["channel_1_name"] = button_one
                    elif isinstance(button_one, dict):
                        device.attributes["channel_1_name"] = button_one.get("buttonName", "Channel 1")
                    else:
                        device.attributes["channel_1_name"] = "Channel 1"
                if button_two:
                    if isinstance(button_two, str):
                        device.attributes["channel_2_name"] = button_two
                    elif isinstance(button_two, dict):
                        device.attributes["channel_2_name"] = button_two.get("buttonName", "Channel 2")
                    else:
                        device.attributes["channel_2_name"] = "Channel 2"
                # Multi-gang only if BOTH buttons exist (LightSwitchTwoGang, LightSwitchTwoChannelTwoWay)
                # LightSwitchTwoWay has only buttonOne (single channel with two-way control)
                device.attributes["is_multi_gang"] = bool(button_one and button_two)
                # Track which channels exist for proper switch creation
                device.attributes["has_channel_1"] = bool(button_one)
                device.attributes["has_channel_2"] = bool(button_two)

            # Update device metadata (API uses "color" not "device_color")
            device.device_color = device_data.get("color")
            device.device_label = device_data.get("device_label")
            device.device_marketing_id = device_data.get("device_marketing_id")

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
