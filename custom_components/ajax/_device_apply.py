"""Pure appliers: enriched hub-device record → ``AjaxDevice`` mutations.

Extracted from ``_coordinator_devices._async_update_devices`` so the
reconciliation loop (fetch, dedup, create/remove, dispatch) and the
per-device attribute application live in separate modules. Every function
here is stateless — ``(device, device_data)`` in, attribute mutations out —
grouped by device family and called in order by ``apply_device_payload``.

Optimistic guards: several attributes back optimistic UI updates (switch
toggles, dimmer brightness, valve state…). Each applier must honour
``device.is_optimistic(key)`` for those keys, otherwise a poll landing
inside the TTL window bounces the user's change back.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .models import AjaxDevice

_LOGGER = logging.getLogger(__name__)

# Jeweller signal-level strings → percentage scale.
_SIGNAL_LEVEL_MAP = {
    "EXCELLENT": 100,
    "STRONG": 85,
    "GOOD": 70,
    "NORMAL": 60,
    "MEDIUM": 50,
    "WEAK": 30,
    "POOR": 15,
}


def _apply_common_state(device: AjaxDevice, device_data: dict[str, Any]) -> None:
    """Online / bypass / malfunctions / battery / signal / firmware / states."""
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
        device.signal_strength = _SIGNAL_LEVEL_MAP.get(signal_level.upper())
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


def _apply_config_flags(device: AjaxDevice, device_data: dict[str, Any]) -> None:
    """always_active / night-mode arming / DoorProtect Plus config switches."""
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


def _apply_contact_states(device: AjaxDevice, device_data: dict[str, Any]) -> None:
    """Reed / extra contact / MultiTransmitter wiring-scheme door state."""
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


def _apply_siren(device: AjaxDevice, device_data: dict[str, Any]) -> None:
    """HomeSiren / StreetSiren volume, indication and chime settings."""
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


def _apply_fire_protect(device: AjaxDevice, device_data: dict[str, Any]) -> None:
    """LED indicator, FireProtect 2 alarms, alertsBySirens, MotionCam."""
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


def _apply_life_quality(device: AjaxDevice, device_data: dict[str, Any]) -> None:
    """LifeQuality CO2 / temperature / humidity readings and comfort bounds."""
    for attr in (
        "actualCO2",
        "actualTemperature",
        "actualHumidity",
        "minComfortCO2",
        "maxComfortCO2",
        "minComfortTemperature",
        "maxComfortTemperature",
        "minComfortHumidity",
        "maxComfortHumidity",
        "calibrationState",
        "indication",
    ):
        if attr in device_data:
            device.attributes[attr] = device_data.get(attr)


def _apply_waterstop(device: AjaxDevice, device_data: dict[str, Any]) -> None:
    """WaterStop valve settings and (optimistic-guarded) valve state."""
    # ``valveState`` is handled separately so an in-flight optimistic
    # open/close is not bounced back by a poll (see AjaxValve._set_valve_state).
    for attr in (
        "motorState",
        "tempProtectState",
        "extPower",
        "preventionEnable",
        "preventionDaysPeriod",
        "preventionExecuteHours",
        "preventionExecuteMinutes",
        "buttonCfg",
        "indicationMode",
    ):
        if attr in device_data:
            device.attributes[attr] = device_data.get(attr)
    if "valveState" in device_data and not device.is_optimistic("valveState"):
        device.attributes["valveState"] = device_data.get("valveState")


def _apply_socket_power(device: AjaxDevice, device_data: dict[str, Any]) -> None:
    """Socket / Relay / WallSwitch on-off state and power monitoring."""
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
    for attr in (
        "indicationEnabled",
        "indicationBrightness",
        "currentProtectionEnabled",
        "voltageProtectionEnabled",
        "contactNormalState",
        "lockupRelayMode",
        "lockupRelayTimeSeconds",
    ):
        if attr in device_data:
            device.attributes[attr] = device_data.get(attr)
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


def _apply_button(device: AjaxDevice, device_data: dict[str, Any]) -> None:
    """Button / keyfob settings (mode, brightness, false-press filter)."""
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


def _apply_lightswitch(device: AjaxDevice, device_data: dict[str, Any]) -> None:
    """LightSwitch channels / dimmer brightness / settings / button names."""
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
    for attr in (
        "minBrightnessLimitCh1",
        "maxBrightnessLimitCh1",
        "armActionBrightnessCh1",
        "disarmActionBrightnessCh1",
        "brightnessChangeSpeed",
    ):
        if attr in device_data:
            device.attributes[attr] = device_data.get(attr)

    # LightSwitch settings and protection statuses
    if "settingsSwitch" in device_data and not device.is_optimistic("settingsSwitch"):
        device.attributes["settingsSwitch"] = device_data.get("settingsSwitch", [])
    if "protectStatuses" in device_data:
        device.attributes["protectStatuses"] = device_data.get("protectStatuses", [])
    for attr in (
        "touchSensitivity",
        "touchMode",
        "dataChannelSignalQuality",
        "dataChannelOk",
        "panelColor",
    ):
        if attr in device_data:
            device.attributes[attr] = device_data.get(attr)
    if "dimmerSettings" in device_data:
        device.attributes["dimmerSettings"] = device_data.get("dimmerSettings", {})

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


def _apply_metadata(device: AjaxDevice, device_data: dict[str, Any]) -> None:
    """Device metadata (API uses "color" not "device_color")."""
    device.device_color = device_data.get("color")
    device.device_label = device_data.get("device_label")
    device.device_marketing_id = device_data.get("device_marketing_id")


# Order matters: e.g. WaterStop's ``indicationMode`` write is refined by
# ``_apply_socket_power`` (derives ``indicationEnabled``) exactly like the
# original inline sequence did.
_APPLIERS = (
    _apply_common_state,
    _apply_config_flags,
    _apply_contact_states,
    _apply_siren,
    _apply_fire_protect,
    _apply_life_quality,
    _apply_waterstop,
    _apply_socket_power,
    _apply_button,
    _apply_lightswitch,
    _apply_metadata,
)


def apply_device_payload(device: AjaxDevice, device_data: dict[str, Any]) -> None:
    """Apply an enriched hub-device record onto ``device``.

    Mutates ``device`` in place; honours the per-key optimistic guards.
    The nested ``attributes`` normalisation (``_normalize_device_attributes``)
    stays in the coordinator because it is shared with the fast door poll.
    """
    for apply in _APPLIERS:
        apply(device, device_data)
