"""Ajax real-time event-mapping tables.

Single source of truth for the event-tag / event-code lookup tables shared by
both real-time transports (``sse_manager`` and ``sqs_manager``). Pure data only
— no Home Assistant, coordinator or transport imports — so both managers can
import from here without a circular dependency.
"""

from __future__ import annotations

from .models import SecurityState

# Map SQS event tags to SecurityState
EVENT_TAG_TO_STATE = {
    "arm": SecurityState.ARMED,
    "armwithmalfunctions": SecurityState.ARMED,
    "disarm": SecurityState.DISARMED,
    "nightmodeon": SecurityState.NIGHT_MODE,
    "nightmodeonwithmalfunctions": SecurityState.NIGHT_MODE,
    "nightmodeoff": SecurityState.DISARMED,
    "partialarm": SecurityState.PARTIALLY_ARMED,
    "grouparm": SecurityState.PARTIALLY_ARMED,
    "groupdisarm": SecurityState.PARTIALLY_ARMED,  # Triggers refresh to get actual state
}

# Event tags by category (eventTag -> (action_key, is_triggered))
DOOR_EVENTS = {
    "dooropened": ("door_opened", True),
    "doorclosed": ("door_closed", False),
    "doorrestored": ("door_closed", False),  # Alternative tag for door closed
    "doornormal": ("door_closed", False),  # Alternative tag for door closed
    "extcontactopened": ("ext_contact_opened", True),
    "extcontactclosed": ("ext_contact_closed", False),
}

MOTION_EVENTS = {
    "motiondetected": ("motion_detected", True),
    "nomotiondetected": ("motion_cleared", False),
}

SMOKE_EVENTS = {
    "smokedetected": ("smoke_detected", True),
    "nosmokedetected": ("smoke_cleared", False),
    "temperatureabovethreshold": ("temp_high", True),
    "temperaturebacktonormal": ("temp_normal", False),
    "rapidtemperaturerise": ("rapid_temp_rise", True),
    "codetected": ("co_detected", True),
    "colevelok": ("co_cleared", False),
}

FLOOD_EVENTS = {
    "leakagedetected": ("leak_detected", True),
    "noleakagedetected": ("leak_cleared", False),
}

GLASS_EVENTS = {
    "glassbreakdetected": ("glass_break", True),
}

# WireInput alarm events (when system is armed)
# These are sent by MultiTransmitterWireInput and MultiTransmitterWireInputRs devices
WIRE_INPUT_EVENTS = {
    "intrusionalarm": ("intrusion_alarm", True),
    "s1alarm": ("s1_alarm", True),
    "s2alarm": ("s2_alarm", True),
    "s3alarm": ("s3_alarm", True),
    "rollershutteralarm": ("roller_shutter_alarm", True),
    "rollershutteroffline": ("roller_shutter_offline", True),
}

TAMPER_EVENTS = {
    "lidopen": ("tamper_open", True),
    "lidclosed": ("tamper_closed", False),
    "tampered": ("tamper_open", True),
    "tamperopened": ("tamper_open", True),  # Uses transition for actual state
    "tamperclosed": ("tamper_closed", False),
}

DEVICE_STATUS_EVENTS = {
    "online": ("device_online", False),
    "offline": ("device_offline", True),
    "lowbattery": ("low_battery", True),
    "batterycharged": ("battery_ok", False),
    "externalpowerdisconnected": ("power_disconnected", True),
    "externalpowerrestored": ("power_restored", False),
}

RELAY_EVENTS = {
    "switchedon": ("switched_on", True),
    "switchedoff": ("switched_off", False),
    "turnedon": ("light_on", True),
    "turnedoff": ("light_off", False),
    "relayonbyuser": ("relay_on", True),
    "relayoffbyuser": ("relay_off", False),
    # WallSwitch Jeweller — relay toggled automatically on arm/disarm
    # eventTypeV2=SMART_HOME_ACTUATOR, sourceObjectType=WALL_SWITCH
    "relayonbyarming": ("relay_on_by_arming", True),
    "relayoffbyarming": ("relay_off_by_arming", False),
    "relayonbydisarming": ("relay_on_by_disarming", True),
    "relayoffbydisarming": ("relay_off_by_disarming", False),
}

BUTTON_EVENTS = {
    "buttonpressed": ("single_press", True),
    "buttonsinglepress": ("single_press", True),
    "buttondoublepress": ("double_press", True),
    "buttonlongpress": ("long_press", True),
    "buttonshortpress": ("single_press", True),
    "panicbuttonpressed": ("panic", True),
    "emergencybuttonpressed": ("emergency", True),
}

# Doorbell events
DOORBELL_EVENTS = {
    "doorbellpressed": True,
    "doorbellbuttonpressed": True,
    "doorbellring": True,
    "doorbell": True,
}

# Hub system/malfunction events (informational, logged but not actionable)
HUB_EVENTS: set[str] = {
    "firmwareupdateinprogress",
    "firmwareupdatecompleted",
    "ethernetconnectionloss",
    "ethernetconnectionrestored",
    "gsmconnectionloss",
    "gsmconnectionrestored",
    "serverconnectionloss",
    "serverconnectionrestored",
    "huboffline",
    "hubonline",
    "powersupplyloss",
    "powersupplyrestored",
    "armattempt",
}

# Scenario events that might be triggered by a Button
SCENARIO_EVENTS = {
    "relayonbyscenario": "scenario_triggered",
    "relayoffbyscenario": "scenario_triggered",
    "scenarioexecuted": "scenario_triggered",
}

# Video AI detection events (eventTag -> detection_type)
# These events are sent when cameras detect motion, humans, vehicles, or pets
VIDEO_EVENTS = {
    "videomotiondetected": "VIDEO_MOTION",
    "videohumandetected": "VIDEO_HUMAN",
    "videovehicledetected": "VIDEO_VEHICLE",
    "videopetdetected": "VIDEO_PET",
    # Alternative event tags (may vary by firmware)
    "videomotion": "VIDEO_MOTION",
    "videohuman": "VIDEO_HUMAN",
    "videovehicle": "VIDEO_VEHICLE",
    "videopet": "VIDEO_PET",
}

# eventTypeV2 values that indicate video AI detection
VIDEO_EVENT_TYPES = {
    "VIDEO_MOTION": "VIDEO_MOTION",
    "VIDEO_HUMAN": "VIDEO_HUMAN",
    "VIDEO_VEHICLE": "VIDEO_VEHICLE",
    "VIDEO_PET": "VIDEO_PET",
}

# Smart lock events (LockBridge Jeweller)
# Mapping event codes -> is_locked (more reliable than transition field)
LOCK_EVENT_CODE_STATES: dict[str, bool] = {
    "M_7E_20": False,  # Unlocked by knob (thumbturn)
    "M_7E_21": False,  # Unlocked by code
    "M_7E_23": False,  # Unlocked by user (app)
    "M_7E_27": True,  # Locked by knob (thumbturn)
    "M_7E_29": True,  # Locked automatically
    "M_7E_2A": True,  # Locked by user (app)
}

# Mapping event codes -> is_door_open
LOCK_DOOR_EVENT_CODE_STATES: dict[str, bool] = {
    "M_7E_2E": True,  # Door open
    "M_7E_2F": False,  # Door closed
    "M_7E_37": True,  # Door left open (malfunction warning)
}

# Event tags for routing (lowercased by _handle_event)
LOCK_EVENTS: set[str] = {
    "smartlockunlockedbyuser",
    "smartlockunlockedbycode",
    "smartlockunlockedbyknob",
    "smartlockmodulelockedautomatically",
}

LOCK_DOOR_EVENTS: set[str] = {
    "smartlockdooropen",
    "smartlockdoorleftopen",
    "smartlockdoorbellbuttonpressed",
}

# Map event tags to action keys for security events
SECURITY_EVENT_ACTIONS = {
    "arm": "armed",
    "disarm": "disarmed",
    "nightmodeon": "night_mode",
    "nightmodeoff": "night_mode_off",
    "partialarm": "armed",
    "grouparm": "group_armed",
    "groupdisarm": "group_disarmed",
}
