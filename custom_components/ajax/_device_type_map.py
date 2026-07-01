"""Device-type alias table for Ajax REST payloads.

Pure data, no HA/transport imports — mirrors the ``event_maps.py``
pattern. Single source of truth used by
``_coordinator_state.AjaxStateUpdaterMixin._parse_device_type``.

NOTE: the fallback substring matching in ``_parse_device_type`` walks this
dict in insertion order — keep the more specific aliases before the generic
ones (e.g. ``motioncam`` before ``cam``).
"""

from __future__ import annotations

from .models import DeviceType

# Device-type aliases observed in Ajax REST payloads.
# Living at module level (not inside the parser) so reading the table
# does not allocate a fresh dict per call — `_parse_device_type` runs
# once per device per refresh, which adds up on big installations.
DEVICE_TYPE_MAP: dict[str, DeviceType] = {
    # Motion detectors
    "motion_protect": DeviceType.MOTION_DETECTOR,
    "motion": DeviceType.MOTION_DETECTOR,
    "pir": DeviceType.MOTION_DETECTOR,
    "motionprotect": DeviceType.MOTION_DETECTOR,
    "motioncam": DeviceType.MOTION_DETECTOR,
    "motion_cam": DeviceType.MOTION_DETECTOR,
    "motioncamoutdoor": DeviceType.MOTION_DETECTOR,
    "motion_cam_outdoor": DeviceType.MOTION_DETECTOR,
    "motioncamoutdoorphod": DeviceType.MOTION_DETECTOR,
    "motion_cam_outdoor_phod": DeviceType.MOTION_DETECTOR,
    "motionprotectcurtain": DeviceType.MOTION_DETECTOR,
    "motion_protect_curtain": DeviceType.MOTION_DETECTOR,
    "motionprotectoutdoor": DeviceType.MOTION_DETECTOR,
    "motion_protect_outdoor": DeviceType.MOTION_DETECTOR,
    "motioncamfibra": DeviceType.MOTION_DETECTOR,
    "motion_cam_fibra": DeviceType.MOTION_DETECTOR,
    "motioncamsphod": DeviceType.MOTION_DETECTOR,
    "motion_cam_s_phod": DeviceType.MOTION_DETECTOR,
    "curtainoutdoorjeweller": DeviceType.MOTION_DETECTOR,
    "curtain_outdoor_jeweller": DeviceType.MOTION_DETECTOR,
    # Combi detectors (motion + glass break)
    "combi_protect": DeviceType.COMBI_PROTECT,
    "combiprotect": DeviceType.COMBI_PROTECT,
    "combi": DeviceType.COMBI_PROTECT,
    # Door/Window contacts
    "door_protect": DeviceType.DOOR_CONTACT,
    "doorprotect": DeviceType.DOOR_CONTACT,
    "doorprotectplus": DeviceType.DOOR_CONTACT,
    "door_protect_plus": DeviceType.DOOR_CONTACT,
    "doorprotectplusfibra": DeviceType.DOOR_CONTACT,
    "door_protect_plus_fibra": DeviceType.DOOR_CONTACT,
    "doorprotectsplus": DeviceType.DOOR_CONTACT,
    "door_protect_s_plus": DeviceType.DOOR_CONTACT,
    "door": DeviceType.DOOR_CONTACT,
    "window": DeviceType.DOOR_CONTACT,
    "opening": DeviceType.DOOR_CONTACT,
    "magnet": DeviceType.DOOR_CONTACT,
    # Glass break
    "glass_protect": DeviceType.GLASS_BREAK,
    "glassprotect": DeviceType.GLASS_BREAK,
    "glassprotects": DeviceType.GLASS_BREAK,
    "glass_protect_s": DeviceType.GLASS_BREAK,
    "glass": DeviceType.GLASS_BREAK,
    # Smoke detectors
    "fire_protect": DeviceType.SMOKE_DETECTOR,
    "fireprotect": DeviceType.SMOKE_DETECTOR,
    "fireprotectplus": DeviceType.SMOKE_DETECTOR,
    "fire_protect_plus": DeviceType.SMOKE_DETECTOR,
    "fireprotect2plus": DeviceType.SMOKE_DETECTOR,
    "fire_protect_2_plus": DeviceType.SMOKE_DETECTOR,
    "fireprotect2": DeviceType.SMOKE_DETECTOR,
    "fire_protect_2": DeviceType.SMOKE_DETECTOR,
    "fire_protect_2_base": DeviceType.SMOKE_DETECTOR,
    "fireprotect2base": DeviceType.SMOKE_DETECTOR,
    "smoke": DeviceType.SMOKE_DETECTOR,
    "fire": DeviceType.SMOKE_DETECTOR,
    # Manual Call Point (fire alarm button)
    "switchbasemcpfire": DeviceType.MANUAL_CALL_POINT,
    "switch_base_mcp_fire": DeviceType.MANUAL_CALL_POINT,
    "manualcallpoint": DeviceType.MANUAL_CALL_POINT,
    "manual_call_point": DeviceType.MANUAL_CALL_POINT,
    "mcp": DeviceType.MANUAL_CALL_POINT,
    # Flood detectors
    "leak_protect": DeviceType.FLOOD_DETECTOR,
    "leakprotect": DeviceType.FLOOD_DETECTOR,
    "leaksprotect": DeviceType.FLOOD_DETECTOR,
    "leaks_protect": DeviceType.FLOOD_DETECTOR,
    "leak": DeviceType.FLOOD_DETECTOR,
    "water": DeviceType.FLOOD_DETECTOR,
    "flood": DeviceType.FLOOD_DETECTOR,
    # Temperature
    "temperature": DeviceType.TEMPERATURE_SENSOR,
    "temp": DeviceType.TEMPERATURE_SENSOR,
    # Controls - Keypads and Keyboards
    "keypad": DeviceType.KEYPAD,
    "keyboard": DeviceType.KEYPAD,
    "keypadplus": DeviceType.KEYPAD,
    "keypad_plus": DeviceType.KEYPAD,
    "keypadsplus": DeviceType.KEYPAD,
    "keypad_s_plus": DeviceType.KEYPAD,
    "keypadplusg3": DeviceType.KEYPAD,
    "keypad_plus_g3": DeviceType.KEYPAD,
    "keypadcombi": DeviceType.KEYPAD,
    "keypad_combi": DeviceType.KEYPAD,
    "keyboardfibra": DeviceType.KEYPAD,
    "keyboard_fibra": DeviceType.KEYPAD,
    "keypadtouchscreen": DeviceType.KEYPAD,
    "keypad_touchscreen": DeviceType.KEYPAD,
    "keypadtouchscreeng3": DeviceType.KEYPAD,
    "keypad_touchscreen_g3": DeviceType.KEYPAD,
    "keypadbeep": DeviceType.KEYPAD,
    "keypad_beep": DeviceType.KEYPAD,
    "keypadbase": DeviceType.KEYPAD,
    "keypad_base": DeviceType.KEYPAD,
    "keypadtouchscreenfibra": DeviceType.KEYPAD,
    "keypad_touchscreen_fibra": DeviceType.KEYPAD,
    "keypadoutdoorbase": DeviceType.KEYPAD,
    "keypad_outdoor_base": DeviceType.KEYPAD,
    "keypadoutdoor": DeviceType.KEYPAD,
    "keypad_outdoor": DeviceType.KEYPAD,
    "keypadoutdoorfibra": DeviceType.KEYPAD,
    "keypad_outdoor_fibra": DeviceType.KEYPAD,
    "keypadfibra": DeviceType.KEYPAD,
    "keypad_fibra": DeviceType.KEYPAD,
    # Remote controls
    "space_control": DeviceType.REMOTE_CONTROL,
    "spacecontrol": DeviceType.REMOTE_CONTROL,
    "remote": DeviceType.REMOTE_CONTROL,
    # Buttons
    "button": DeviceType.BUTTON,
    "double_button": DeviceType.BUTTON,
    "doublebutton": DeviceType.BUTTON,
    # Sirens
    "siren": DeviceType.SIREN,
    "alarm": DeviceType.SIREN,
    "homesiren": DeviceType.SIREN,
    "home_siren": DeviceType.SIREN,
    "streetsiren": DeviceType.SIREN,
    "street_siren": DeviceType.SIREN,
    "streetsirendoubledeck": DeviceType.SIREN,
    "street_siren_double_deck": DeviceType.SIREN,
    "streetsirensdoubledeck": DeviceType.SIREN,
    "street_siren_s_double_deck": DeviceType.SIREN,
    "homesirens": DeviceType.SIREN,
    "home_siren_s": DeviceType.SIREN,
    "streetsirefibra": DeviceType.SIREN,
    "street_siren_fibra": DeviceType.SIREN,
    "homesirefibra": DeviceType.SIREN,
    "home_siren_fibra": DeviceType.SIREN,
    # SpeakerPhone
    "speakerphone": DeviceType.SPEAKERPHONE,
    # Doorbell
    "doorbell": DeviceType.DOORBELL,
    "doorbellbutton": DeviceType.DOORBELL,
    "doorbell_button": DeviceType.DOORBELL,
    "motioncamvideodoorbell": DeviceType.DOORBELL,
    "motion_cam_video_doorbell": DeviceType.DOORBELL,
    # Transmitter
    "transmitter": DeviceType.TRANSMITTER,
    "transmitterfibra": DeviceType.TRANSMITTER,
    "transmitter_fibra": DeviceType.TRANSMITTER,
    "transmitterfibratwochannels": DeviceType.TRANSMITTER,
    "transmitter_fibra_two_channels": DeviceType.TRANSMITTER,
    "integration": DeviceType.TRANSMITTER,
    # MultiTransmitter (wired sensors hub)
    "multitransmitter": DeviceType.MULTI_TRANSMITTER,
    "multi_transmitter": DeviceType.MULTI_TRANSMITTER,
    "multitransmitterfibra": DeviceType.MULTI_TRANSMITTER,
    "multitransmitter_fibra": DeviceType.MULTI_TRANSMITTER,
    # MultiTransmitter wired inputs (treat as door contacts)
    "multitransmitterwireinput": DeviceType.WIRE_INPUT,
    "multitransmitter_wire_input": DeviceType.WIRE_INPUT,
    "multitransmitterfibrawireinput": DeviceType.WIRE_INPUT,
    "multitransmitter_fibra_wire_input": DeviceType.WIRE_INPUT,
    "multitransmitterwireinputrs": DeviceType.WIRE_INPUT,
    "multitransmitter_wire_input_rs": DeviceType.WIRE_INPUT,
    # Repeater / Range Extender
    "repeater": DeviceType.REPEATER,
    "rex": DeviceType.REPEATER,
    "range_extender": DeviceType.REPEATER,
    # Explicit raw Ajax types (Rex 1 / Rex 2) so they match exactly instead of
    # relying on the "extender" substring fallback — see issue #167.
    "rangeextender": DeviceType.REPEATER,
    "rangeextender2": DeviceType.REPEATER,
    "extender": DeviceType.REPEATER,
    # Wired Input Modules
    "wire_input_mt": DeviceType.WIRE_INPUT,
    "wireinputmt": DeviceType.WIRE_INPUT,
    "wire_input_rs": DeviceType.WIRE_INPUT,
    "wireinputrs": DeviceType.WIRE_INPUT,
    # Line Splitter
    "line_split_fibra": DeviceType.LINE_SPLITTER,
    "linesplitfibra": DeviceType.LINE_SPLITTER,
    "line_splitter": DeviceType.LINE_SPLITTER,
    "linesplitter": DeviceType.LINE_SPLITTER,
    # Smart devices
    "socket": DeviceType.SOCKET,
    "socketoutlet": DeviceType.SOCKET,
    "socket_outlet": DeviceType.SOCKET,
    "socketoutlettypee": DeviceType.SOCKET,
    "socket_outlet_type_e": DeviceType.SOCKET,
    "socketoutlettypef": DeviceType.SOCKET,
    "socket_outlet_type_f": DeviceType.SOCKET,
    "relay": DeviceType.RELAY,
    "wallswitch": DeviceType.WALLSWITCH,
    "wall_switch": DeviceType.WALLSWITCH,
    "wall_switch_jeweller": DeviceType.WALLSWITCH,
    "lightswitch": DeviceType.WALLSWITCH,
    "lightswitchonegang": DeviceType.WALLSWITCH,
    "lightswitchtwogang": DeviceType.WALLSWITCH,
    "lightswitchtwochanneltwoway": DeviceType.WALLSWITCH,
    "light_switch_two_channel_two_way": DeviceType.WALLSWITCH,
    "lightswitchdimmer": DeviceType.WALLSWITCH,
    "light_switch_dimmer": DeviceType.WALLSWITCH,
    "thermostat": DeviceType.THERMOSTAT,
    "life_quality": DeviceType.LIFE_QUALITY,
    "lifequality": DeviceType.LIFE_QUALITY,
    "waterstop": DeviceType.WATERSTOP,
    "water_stop": DeviceType.WATERSTOP,
    # Smart Locks (Yale cloud - handled separately, skip as normal device)
    "smartlockyale": DeviceType.SMART_LOCK,
    "smart_lock_yale": DeviceType.SMART_LOCK,
    "smartlock": DeviceType.SMART_LOCK,
    "smart_lock": DeviceType.SMART_LOCK,
    # Cameras
    "camera": DeviceType.CAMERA,
    "cam": DeviceType.CAMERA,
    # Hub
    "hub": DeviceType.HUB,
}
