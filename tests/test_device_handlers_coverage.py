"""Exhaustive branch/value_fn coverage for every Ajax device handler.

``tests/test_device_handlers.py`` covers the headline shapes; this file
drills into each conditional branch (attribute-gated descriptors) and
*executes* the ``value_fn`` / ``api_transform`` closures so a wrong key,
broken cast or off-list enum fails fast.

The handlers are pure descriptor factories — no HA, no coordinator — so we
build a representative ``AjaxDevice`` per branch and assert on the returned
dicts directly.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.ajax.devices import (
    ButtonHandler,
    DimmerHandler,
    DoorbellHandler,
    DoorContactHandler,
    FloodDetectorHandler,
    GenericHandler,
    GlassBreakHandler,
    HubHandler,
    LifeQualityHandler,
    LightSwitchHandler,
    ManualCallPointHandler,
    MotionDetectorHandler,
    RepeaterHandler,
    SirenHandler,
    SmokeDetectorHandler,
    SocketHandler,
    TransmitterHandler,
    WaterStopHandler,
    WireInputHandler,
    get_device_handler,
    is_dimmer_device,
)
from custom_components.ajax.models import AjaxDevice, DeviceType


def _device(
    device_type: DeviceType,
    attributes: dict[str, Any] | None = None,
    **kwargs: Any,
) -> AjaxDevice:
    return AjaxDevice(
        id="dev1",
        name="Test device",
        type=device_type,
        space_id="space1",
        hub_id="hub1",
        attributes=attributes or {},
        **kwargs,
    )


def _keys(descriptors: list[dict[str, Any]]) -> set[str]:
    return {d["key"] for d in descriptors}


def _by_key(descriptors: list[dict[str, Any]], key: str) -> dict[str, Any]:
    return next(d for d in descriptors if d["key"] == key)


def _run_all_value_fns(descriptors: list[dict[str, Any]]) -> None:
    """Execute every callable in each descriptor — must never raise."""
    for desc in descriptors:
        for field in ("value_fn", "turn_on_fn", "turn_off_fn", "open_fn", "close_fn"):
            fn = desc.get(field)
            if callable(fn):
                fn()


# ---------------------------------------------------------------------------
# Routing (is_dimmer_device / get_device_handler)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_type,is_dimmer",
    [
        ("lightSwitchDimmer", True),
        ("light_switch_dimmer", True),
        ("SomeDimmerVariant", True),  # substring match
        ("LightSwitchTwoGang", False),
        (None, False),
    ],
)
def test_is_dimmer_device(raw_type: str | None, is_dimmer: bool) -> None:
    dev = _device(DeviceType.WALLSWITCH, raw_type=raw_type)
    assert is_dimmer_device(dev) is is_dimmer
    if is_dimmer:
        assert get_device_handler(dev) is DimmerHandler


def test_get_device_handler_falls_back_to_registry() -> None:
    assert get_device_handler(_device(DeviceType.SIREN)) is SirenHandler


# ---------------------------------------------------------------------------
# base.py helpers + common sensors
# ---------------------------------------------------------------------------


def test_base_common_sensors_room_branch() -> None:
    handler = RepeaterHandler(_device(DeviceType.REPEATER, room_name="Kitchen"))
    sensors = handler.get_common_sensors()
    assert _by_key(sensors, "room")["value_fn"]() == "Kitchen"

    no_room = RepeaterHandler(_device(DeviceType.REPEATER))
    assert no_room.get_common_sensors() == []


def test_base_helper_descriptors_value_fns() -> None:
    dev = _device(
        DeviceType.REPEATER,
        {"tampered": True},
        battery_level=88,
        signal_strength=70,
        firmware_version="1.2.3",
        malfunctions=[5],
    )
    handler = RepeaterHandler(dev)
    assert handler._battery_sensor()["value_fn"]() == 88
    assert handler._tamper_binary_sensor()["value_fn"]() is True
    assert handler._signal_strength_percent_sensor()["value_fn"]() == 70
    assert handler._firmware_version_sensor()["value_fn"]() == "1.2.3"
    assert handler._problem_binary_sensor()["value_fn"]() is True
    temp = handler._temperature_sensor()
    assert temp["value_fn"]() is None  # no temperature attribute


def test_base_default_collections_return_empty() -> None:
    """Handlers that don't override the optional getters fall back to []."""
    handler = RepeaterHandler(_device(DeviceType.REPEATER))
    assert handler.get_switches() == []
    assert handler.get_buttons() == []
    assert handler.get_events() == []


def test_base_resolve_entity_category() -> None:
    from homeassistant.helpers.entity import EntityCategory

    from custom_components.ajax.devices.base import resolve_entity_category

    assert resolve_entity_category(None) is None
    assert resolve_entity_category(EntityCategory.CONFIG) is EntityCategory.CONFIG
    assert resolve_entity_category("diagnostic") is EntityCategory.DIAGNOSTIC
    assert resolve_entity_category("CONFIG") is EntityCategory.CONFIG
    assert resolve_entity_category("nonsense") is None
    assert resolve_entity_category(123) is None


# ---------------------------------------------------------------------------
# DoorContact / WireInput
# ---------------------------------------------------------------------------


def test_door_contact_full_branches() -> None:
    handler = DoorContactHandler(
        _device(
            DeviceType.DOOR_CONTACT,
            {
                "extra_contact_aware": True,
                "external_contact_opened": True,
                "accelerometer_aware": True,
                "tilt_detected": True,
                "shock_sensor_aware": True,
                "shock_detected": True,
                "door_opened": True,
                "tampered": True,
            },
        )
    )
    binary = handler.get_binary_sensors()
    assert {"door", "external_contact", "tamper", "tilt", "shock"} <= _keys(binary)
    assert _by_key(binary, "tilt")["value_fn"]() is True
    assert _by_key(binary, "shock")["value_fn"]() is True
    assert _by_key(binary, "external_contact")["value_fn"]() is True
    _run_all_value_fns(binary)


def test_door_contact_tilt_shock_fallback_keys() -> None:
    """tilt/shock read the alt attribute name when the *_detected key is absent."""
    handler = DoorContactHandler(
        _device(
            DeviceType.DOOR_CONTACT,
            {
                "accelerometer_aware": True,
                "tilt": True,
                "shock_sensor_aware": True,
                "shock": True,
            },
        )
    )
    binary = handler.get_binary_sensors()
    assert _by_key(binary, "tilt")["value_fn"]() is True
    assert _by_key(binary, "shock")["value_fn"]() is True


def test_door_contact_sensors_branches() -> None:
    handler = DoorContactHandler(
        _device(
            DeviceType.DOOR_CONTACT,
            {
                "temperature": 21,
                "connection_type": "JEWELLER",
                "operating_mode": "BIDIRECTIONAL",
            },
            battery_level=50,
            signal_strength=60,
            battery_state="OK",
        )
    )
    sensors = handler.get_sensors()
    assert {"battery", "signal_strength", "temperature", "connection_type", "operating_mode", "battery_state"} <= _keys(
        sensors
    )
    assert _by_key(sensors, "connection_type")["value_fn"]() == "JEWELLER"
    assert _by_key(sensors, "operating_mode")["value_fn"]() == "BIDIRECTIONAL"
    assert _by_key(sensors, "battery_state")["value_fn"]() == "OK"
    _run_all_value_fns(sensors)


def test_door_contact_switches_standard_and_plus() -> None:
    base = DoorContactHandler(
        _device(DeviceType.DOOR_CONTACT, {"indicatorLightMode": "STANDARD", "night_mode_arm": True})
    )
    switches = base.get_switches()
    keys = _keys(switches)
    assert {"always_active", "indicator_light", "night_mode"} <= keys
    assert "external_contact_enabled" not in keys  # not a Plus
    assert _by_key(switches, "indicator_light")["value_fn"]() is True
    assert _by_key(switches, "night_mode")["value_fn"]() is True
    _run_all_value_fns(switches)

    plus = DoorContactHandler(
        _device(
            DeviceType.DOOR_CONTACT,
            {
                "extra_contact_aware": True,
                "shock_sensor_aware": True,
                "ignore_simple_impact": True,
                "accelerometer_aware": True,
                "siren_triggers": ["REED", "SHOCK", "TILT"],
            },
            raw_type="DoorProtectPlus",
        )
    )
    plus_switches = plus.get_switches()
    assert {
        "external_contact_enabled",
        "shock_sensor",
        "ignore_impact",
        "tilt_sensor",
        "siren_trigger_reed",
        "siren_trigger_shock",
        "siren_trigger_tilt",
    } <= _keys(plus_switches)
    assert _by_key(plus_switches, "siren_trigger_reed")["value_fn"]() is True
    assert _by_key(plus_switches, "siren_trigger_shock")["value_fn"]() is True
    assert _by_key(plus_switches, "siren_trigger_tilt")["value_fn"]() is True
    _run_all_value_fns(plus_switches)


def test_wire_input_handler_branches() -> None:
    handler = WireInputHandler(
        _device(
            DeviceType.WIRE_INPUT,
            {
                "wiring_type": "TWO_EOL",
                "temperature": 18,
                "tampered": True,
                "door_opened": True,
                "night_mode_arm": True,
            },
        )
    )
    binary = handler.get_binary_sensors()
    assert {"door", "tamper"} <= _keys(binary)
    assert _by_key(binary, "tamper")["value_fn"]() is True

    sensors = handler.get_sensors()
    assert _keys(sensors) == {"temperature"}
    assert _by_key(sensors, "temperature")["value_fn"]() == 18

    switches = handler.get_switches()
    assert {"always_active", "night_mode"} <= _keys(switches)
    assert _by_key(switches, "night_mode")["value_fn"]() is True
    _run_all_value_fns(switches)

    # No tamper when wiring scheme not TWO_EOL, no temperature sensor when absent
    plain = WireInputHandler(_device(DeviceType.WIRE_INPUT))
    assert "tamper" not in _keys(plain.get_binary_sensors())
    assert plain.get_sensors() == []


# ---------------------------------------------------------------------------
# MotionDetector
# ---------------------------------------------------------------------------


def test_motion_detector_combi_glass_break_branch() -> None:
    handler = MotionDetectorHandler(
        _device(DeviceType.MOTION_DETECTOR, {"glass_break_detected": True, "motion_detected": True, "tampered": True})
    )
    binary = handler.get_binary_sensors()
    assert {"motion", "tamper", "glass_break"} <= _keys(binary)
    assert _by_key(binary, "motion")["value_fn"]() is True
    assert _by_key(binary, "glass_break")["value_fn"]() is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, "low"),
        (1, "normal"),
        (2, "high"),
        (5, "5"),  # unknown int -> str
        ("weird", "weird"),  # non-int -> str
    ],
)
def test_motion_detector_sensitivity_label(raw: Any, expected: str) -> None:
    handler = MotionDetectorHandler(_device(DeviceType.MOTION_DETECTOR, {"sensitivity": raw}))
    sensors = handler.get_sensors()
    assert _by_key(sensors, "sensitivity")["value_fn"]() == expected


def test_motion_detector_sensors_and_switches() -> None:
    handler = MotionDetectorHandler(
        _device(
            DeviceType.MOTION_DETECTOR,
            {
                "temperature": 20,
                "sensitivity": 1,
                "indicatorLightMode": "STANDARD",
                "night_mode_arm": True,
                "siren_triggers": ["MOTION"],
            },
            battery_level=42,
            signal_strength=55,
        )
    )
    sensors = handler.get_sensors()
    assert {"battery", "signal_strength", "temperature", "sensitivity"} <= _keys(sensors)
    _run_all_value_fns(sensors)

    switches = handler.get_switches()
    assert {"always_active", "indicator_light", "night_mode", "siren_trigger_motion"} <= _keys(switches)
    assert _by_key(switches, "siren_trigger_motion")["value_fn"]() is True
    assert _by_key(switches, "indicator_light")["value_fn"]() is True
    _run_all_value_fns(switches)


# ---------------------------------------------------------------------------
# GlassBreak
# ---------------------------------------------------------------------------


def test_glass_break_external_contact_and_sensors() -> None:
    handler = GlassBreakHandler(
        _device(
            DeviceType.GLASS_BREAK,
            {
                "extra_contact_aware": True,
                "external_contact_opened": True,
                "temperature": 19,
                "sensitivity": 2,
            },
            battery_level=40,
            signal_strength=33,
        )
    )
    binary = handler.get_binary_sensors()
    assert {"glass_break", "external_contact", "tamper"} <= _keys(binary)
    assert _by_key(binary, "external_contact")["value_fn"]() is True

    sensors = handler.get_sensors()
    assert {"battery", "signal_strength", "temperature", "sensitivity"} <= _keys(sensors)
    assert _by_key(sensors, "sensitivity")["value_fn"]() == "high"
    _run_all_value_fns(sensors)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, "low"),
        (1, "normal"),
        (2, "high"),
        (9, "9"),  # unknown int -> str
        ("weird", "weird"),  # non-castable -> except branch -> str
    ],
)
def test_glass_break_sensitivity_label(raw: Any, expected: str) -> None:
    handler = GlassBreakHandler(_device(DeviceType.GLASS_BREAK, {"sensitivity": raw}))
    assert handler._get_sensitivity_label() == expected


def test_glass_break_switches() -> None:
    handler = GlassBreakHandler(
        _device(
            DeviceType.GLASS_BREAK,
            {"indicatorLightMode": "STANDARD", "night_mode_arm": True, "siren_triggers": ["GLASS"]},
        )
    )
    switches = handler.get_switches()
    assert {
        "always_active",
        "indicator_light",
        "night_mode",
        "external_contact_enabled",
        "siren_trigger_glass",
    } <= _keys(switches)
    assert _by_key(switches, "siren_trigger_glass")["value_fn"]() is True
    _run_all_value_fns(switches)


# ---------------------------------------------------------------------------
# FloodDetector
# ---------------------------------------------------------------------------


def test_flood_detector_sensors_with_malfunctions_and_firmware() -> None:
    handler = FloodDetectorHandler(
        _device(
            DeviceType.FLOOD_DETECTOR,
            {"temperature": 17},
            battery_level=20,
            signal_strength=10,
            firmware_version="2.0.0",
            malfunctions=[1, 2],
        )
    )
    sensors = handler.get_sensors()
    assert {"battery", "signal_strength", "temperature", "malfunctions", "firmware_version"} <= _keys(sensors)
    assert _by_key(sensors, "malfunctions")["value_fn"]() == [1, 2]
    assert _by_key(sensors, "firmware_version")["value_fn"]() == "2.0.0"


@pytest.mark.parametrize(
    "attributes",
    [
        {"state": "ALARM"},
        {"leak_detected": True},
        {"flood_alarm": True},
    ],
)
def test_flood_detector_moisture_value_fn_executes(attributes: dict[str, Any]) -> None:
    handler = FloodDetectorHandler(_device(DeviceType.FLOOD_DETECTOR, attributes))
    binary = handler.get_binary_sensors()
    assert _by_key(binary, "moisture")["value_fn"]() is True
    assert _by_key(binary, "tamper")["value_fn"]() is False


def test_flood_detector_switches() -> None:
    handler = FloodDetectorHandler(
        _device(DeviceType.FLOOD_DETECTOR, {"indicatorLightMode": "STANDARD", "siren_triggers": ["LEAK"]})
    )
    switches = handler.get_switches()
    assert {"always_active", "indicator_light", "siren_on_leak"} <= _keys(switches)
    assert _by_key(switches, "indicator_light")["value_fn"]() is True
    assert _by_key(switches, "siren_on_leak")["value_fn"]() is True
    _run_all_value_fns(switches)


# ---------------------------------------------------------------------------
# SmokeDetector
# ---------------------------------------------------------------------------


def test_smoke_detector_all_optional_binary_sensors() -> None:
    handler = SmokeDetectorHandler(
        _device(
            DeviceType.SMOKE_DETECTOR,
            {
                "coAlarm": "CO_ALARM_DETECTED",
                "steamAlarm": "STEAM_ALARM_DETECTED",
                "temperatureAlarmDetected": True,
                "highTemperatureDiffDetected": True,
            },
        )
    )
    binary = handler.get_binary_sensors()
    assert {"smoke", "tamper", "co", "steam", "high_temperature", "rapid_temperature_rise"} <= _keys(binary)
    assert _by_key(binary, "co")["value_fn"]() is True
    assert _by_key(binary, "steam")["value_fn"]() is True
    assert _by_key(binary, "high_temperature")["value_fn"]() is True
    assert _by_key(binary, "rapid_temperature_rise")["value_fn"]() is True


def test_smoke_detector_high_temp_via_sse_alert_key() -> None:
    handler = SmokeDetectorHandler(
        _device(DeviceType.SMOKE_DETECTOR, {"temperatureAlarmDetected": False, "temperature_alert": True})
    )
    binary = handler.get_binary_sensors()
    assert _by_key(binary, "high_temperature")["value_fn"]() is True


def test_smoke_detector_co_via_alternate_keys() -> None:
    # The CO sensor is only created when has_co is True (raw_type / FireProtectPlus
    # / coAlarm present). The SSE-normalized ``co_alarm`` key is then read by value_fn.
    via_alarm = SmokeDetectorHandler(
        _device(DeviceType.SMOKE_DETECTOR, raw_type="FireProtectPlus", attributes={"co_alarm": True})
    )
    assert _by_key(via_alarm.get_binary_sensors(), "co")["value_fn"]() is True

    via_detected = SmokeDetectorHandler(
        _device(DeviceType.SMOKE_DETECTOR, raw_type="FireProtect2", attributes={"co_detected": True})
    )
    assert _by_key(via_detected.get_binary_sensors(), "co")["value_fn"]() is True

    # has_co also triggers on the FIRE_PROTECT_2_BASE raw type.
    base = SmokeDetectorHandler(
        _device(DeviceType.SMOKE_DETECTOR, raw_type="FIRE_PROTECT_2_BASE", attributes={"co_detected": True})
    )
    assert "co" in _keys(base.get_binary_sensors())


def test_smoke_detector_steam_via_sse_key() -> None:
    handler = SmokeDetectorHandler(_device(DeviceType.SMOKE_DETECTOR, {"steamAlarm": "OFF", "steam_detected": True}))
    assert _by_key(handler.get_binary_sensors(), "steam")["value_fn"]() is True


def test_smoke_detector_sensors_branches() -> None:
    handler = SmokeDetectorHandler(
        _device(
            DeviceType.SMOKE_DETECTOR,
            {"temperature": 22, "co_level": 5},
            battery_level=70,
            signal_strength=80,
            firmware_version="3.1",
            malfunctions=[7],
        )
    )
    sensors = handler.get_sensors()
    assert {"battery", "signal_strength", "temperature", "co_level", "malfunctions", "firmware_version"} <= _keys(
        sensors
    )
    assert _by_key(sensors, "co_level")["value_fn"]() == 5
    assert _by_key(sensors, "malfunctions")["value_fn"]() == "7"


def test_smoke_detector_malfunctions_non_list() -> None:
    handler = SmokeDetectorHandler(_device(DeviceType.SMOKE_DETECTOR, malfunctions=3))
    sensors = handler.get_sensors()
    assert _by_key(sensors, "malfunctions")["value_fn"]() == "3"


def test_smoke_detector_switches_all_branches() -> None:
    handler = SmokeDetectorHandler(
        _device(
            DeviceType.SMOKE_DETECTOR,
            {
                "indicatorLightMode": "STANDARD",
                "coAlarmEnable": "CO_ALARM_ENABLED",
                "tempAlarmEnable": "TEMP_ALARM_ENABLED",
                "tempDiffAlarmEnable": "TEMP_DIFF_ALARM_ENABLED",
                "siren_triggers": ["SMOKE", "CO", "TEMPERATURE", "TEMPERATURE_DIFF"],
            },
        )
    )
    switches = handler.get_switches()
    assert {
        "indicator_light",
        "co_alarm_enabled",
        "temp_alarm_enabled",
        "temp_diff_alarm_enabled",
        "siren_trigger_smoke",
        "siren_trigger_co",
        "siren_trigger_temperature",
        "siren_trigger_temp_diff",
    } <= _keys(switches)
    assert _by_key(switches, "co_alarm_enabled")["value_fn"]() is True
    assert _by_key(switches, "temp_alarm_enabled")["value_fn"]() is True
    assert _by_key(switches, "temp_diff_alarm_enabled")["value_fn"]() is True
    assert _by_key(switches, "siren_trigger_smoke")["value_fn"]() is True
    assert _by_key(switches, "siren_trigger_co")["value_fn"]() is True
    assert _by_key(switches, "siren_trigger_temperature")["value_fn"]() is True
    assert _by_key(switches, "siren_trigger_temp_diff")["value_fn"]() is True


def test_smoke_detector_siren_trigger_co_binds_to_cco_token() -> None:
    """When only CCO is reported, the CO switch binds and reads the CCO token."""
    handler = SmokeDetectorHandler(_device(DeviceType.SMOKE_DETECTOR, {"siren_triggers": ["CCO"]}))
    switches = handler.get_switches()
    co_switch = _by_key(switches, "siren_trigger_co")
    assert co_switch["trigger_key"] == "CCO"
    assert co_switch["value_fn"]() is True


def test_smoke_detector_siren_trigger_smoke_via_smoke_alarm_attr() -> None:
    handler = SmokeDetectorHandler(
        _device(DeviceType.SMOKE_DETECTOR, {"siren_triggers": ["X"], "smokeAlarm": "OFF", "coAlarm": "OFF"})
    )
    keys = _keys(handler.get_switches())
    assert "siren_trigger_smoke" in keys
    assert "siren_trigger_co" in keys


# ---------------------------------------------------------------------------
# Socket
# ---------------------------------------------------------------------------


def test_socket_binary_sensors_external_power() -> None:
    handler = SocketHandler(
        _device(DeviceType.SOCKET, {"externally_powered": True}, malfunctions=[1]),
    )
    binary = handler.get_binary_sensors()
    assert {"problem", "external_power"} <= _keys(binary)
    assert _by_key(binary, "problem")["value_fn"]() is True
    assert _by_key(binary, "external_power")["value_fn"]() is True


def test_socket_power_sensors_value_fns() -> None:
    handler = SocketHandler(
        _device(
            DeviceType.SOCKET,
            {"power": 50, "energy": 1.5, "voltage": 230, "current": 2.0, "temperature": 30},
            signal_strength=90,
            firmware_version="1.0",
            malfunctions=[2, 3],
        )
    )
    sensors = handler.get_sensors()
    assert {
        "signal_strength",
        "temperature",
        "power",
        "energy",
        "voltage",
        "current",
        "malfunctions",
        "firmware_version",
    } <= _keys(sensors)
    assert _by_key(sensors, "power")["value_fn"]() == 50
    assert _by_key(sensors, "energy")["value_fn"]() == 1.5
    assert _by_key(sensors, "voltage")["value_fn"]() == 230
    assert _by_key(sensors, "current")["value_fn"]() == 2.0
    assert _by_key(sensors, "malfunctions")["value_fn"]() == "2, 3"


def test_socket_power_sensors_raw_attribute_value_fns() -> None:
    handler = SocketHandler(
        _device(
            DeviceType.SOCKET,
            {
                "powerConsumptionWatts": 60,
                "powerConsumedWattsPerHour": 1500,
                "voltageVolts": 235,
                "currentMilliAmpers": 300,
            },
        )
    )
    sensors = handler.get_sensors()
    assert _by_key(sensors, "power")["value_fn"]() == 60
    assert _by_key(sensors, "energy")["value_fn"]() == 1500
    assert _by_key(sensors, "voltage")["value_fn"]() == 235
    assert _by_key(sensors, "current")["value_fn"]() == 300


def test_socket_malfunctions_non_list() -> None:
    handler = SocketHandler(_device(DeviceType.SOCKET, malfunctions=4))
    assert _by_key(handler.get_sensors(), "malfunctions")["value_fn"]() == "4"


def test_socket_standard_switches_and_actions() -> None:
    handler = SocketHandler(
        _device(
            DeviceType.SOCKET,
            {
                "is_on": True,
                "indicationEnabled": True,
                "currentProtectionEnabled": True,
                "voltageProtectionEnabled": True,
            },
        )
    )
    switches = handler.get_switches()
    assert {"socket", "indication_enabled", "current_protection", "voltage_protection"} <= _keys(switches)
    socket = _by_key(switches, "socket")
    assert socket["value_fn"]() is True
    assert socket["turn_on_fn"]() == {"action": "turn_on"}
    assert socket["turn_off_fn"]() == {"action": "turn_off"}
    assert _by_key(switches, "indication_enabled")["value_fn"]() is True
    assert _by_key(switches, "current_protection")["value_fn"]() is True
    assert _by_key(switches, "voltage_protection")["value_fn"]() is True


def test_socket_multi_gang_switches() -> None:
    handler = SocketHandler(
        _device(
            DeviceType.WALLSWITCH,
            {
                "has_channel_1": True,
                "has_channel_2": True,
                "channel_1_name": "Lampe",
                "channel_2_name": "Spot",
                "channel_1_on": True,
                "channel_2_on": False,
            },
        )
    )
    switches = handler.get_switches()
    assert {"channel_1", "channel_2"} == _keys(switches)
    c1 = _by_key(switches, "channel_1")
    assert c1["name"] == "Lampe"
    assert c1["value_fn"]() is True
    assert c1["channel"] == 0
    c2 = _by_key(switches, "channel_2")
    assert c2["channel"] == 1
    assert c2["value_fn"]() is False


def test_socket_multi_gang_single_channel_only() -> None:
    handler = SocketHandler(_device(DeviceType.WALLSWITCH, {"has_channel_1": True}))
    assert _keys(handler.get_switches()) == {"channel_1"}


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------


def test_hub_binary_sensors_all_branches() -> None:
    handler = HubHandler(
        _device(
            DeviceType.HUB,
            {
                "online": True,
                "tampered": True,
                "externally_powered": True,
                "battery_connected": True,
                "gsm_antenna": True,
                "jeweller_radio": True,
                "wings_radio": True,
            },
            malfunctions=[1],
        )
    )
    binary = handler.get_binary_sensors()
    assert {
        "connection",
        "problem",
        "tamper",
        "external_power",
        "battery_connected",
        "gsm_antenna",
        "jeweller_radio",
        "wings_radio",
    } <= _keys(binary)
    _run_all_value_fns(binary)
    assert _by_key(binary, "connection")["value_fn"]() is True
    assert _by_key(binary, "problem")["value_fn"]() is True


def test_hub_sensors_all_branches() -> None:
    handler = HubHandler(
        _device(
            DeviceType.HUB,
            {
                "gsm_signal_level": -70,
                "wifi_signal_level": -50,
                "active_connection": ["WIFI", "ETHERNET"],
                "network_status": "ONLINE",
                "gsm_type": "4G",
                "total_devices": 10,
                "online_devices": 9,
                "devices_with_malfunctions": 1,
                "unread_notifications": 2,
                "sim_status": "ACTIVE",
            },
            battery_level=100,
            firmware_version="OS Malevich 2.0",
            malfunctions=[3, 4],
        )
    )
    sensors = handler.get_sensors()
    assert {
        "battery",
        "gsm_signal_level",
        "wifi_signal_level",
        "active_connection",
        "network_status",
        "gsm_type",
        "total_devices",
        "online_devices",
        "devices_with_malfunctions",
        "unread_notifications",
        "sim_status",
        "malfunctions",
        "firmware_version",
    } <= _keys(sensors)
    # active_connection list -> sorted, comma-joined
    assert _by_key(sensors, "active_connection")["value_fn"]() == "ETHERNET, WIFI"
    assert _by_key(sensors, "gsm_type")["value_fn"]() == "4g"
    assert _by_key(sensors, "sim_status")["value_fn"]() == "ACTIVE"
    assert _by_key(sensors, "malfunctions")["value_fn"]() == "3, 4"


def test_hub_active_connection_string_and_gsm_type_empty() -> None:
    handler = HubHandler(
        _device(DeviceType.HUB, {"active_connection": "WIFI", "gsm_type": ""}, battery_level=80),
    )
    sensors = handler.get_sensors()
    assert _by_key(sensors, "active_connection")["value_fn"]() == "WIFI"
    assert _by_key(sensors, "gsm_type")["value_fn"]() is None


def test_hub_malfunctions_non_list() -> None:
    handler = HubHandler(_device(DeviceType.HUB, battery_level=50, malfunctions=2))
    assert _by_key(handler.get_sensors(), "malfunctions")["value_fn"]() == "2"


# ---------------------------------------------------------------------------
# LifeQuality
# ---------------------------------------------------------------------------


def test_life_quality_binary_sensors_value_fns() -> None:
    handler = LifeQualityHandler(
        _device(
            DeviceType.LIFE_QUALITY,
            {
                "tampered": True,
                "actualCO2": 1500,
                "maxComfortCO2": 1000,
                "actualTemperature": 280,  # 28.0 C
                "minComfortTemperature": 18,
                "maxComfortTemperature": 25,
                "actualHumidity": 800,  # 80.0%
                "minComfortHumidity": 30,
                "maxComfortHumidity": 60,
            },
        )
    )
    binary = handler.get_binary_sensors()
    assert {"tamper", "co2_problem", "temperature_problem", "humidity_problem"} <= _keys(binary)
    assert _by_key(binary, "co2_problem")["value_fn"]() is True
    assert _by_key(binary, "temperature_problem")["value_fn"]() is True
    assert _by_key(binary, "humidity_problem")["value_fn"]() is True
    assert _by_key(binary, "tamper")["value_fn"]() is True


def test_life_quality_problems_false_when_within_range_or_missing() -> None:
    ok = LifeQualityHandler(
        _device(
            DeviceType.LIFE_QUALITY,
            {
                "actualCO2": 500,
                "maxComfortCO2": 1000,
                "actualTemperature": 210,  # 21.0 C
                "minComfortTemperature": 18,
                "maxComfortTemperature": 25,
                "actualHumidity": 450,  # 45.0%
                "minComfortHumidity": 30,
                "maxComfortHumidity": 60,
            },
        )
    )
    binary = ok.get_binary_sensors()
    assert _by_key(binary, "co2_problem")["value_fn"]() is False
    assert _by_key(binary, "temperature_problem")["value_fn"]() is False
    assert _by_key(binary, "humidity_problem")["value_fn"]() is False

    # No data at all -> all problems False (None guards)
    empty = LifeQualityHandler(_device(DeviceType.LIFE_QUALITY))
    empty_binary = empty.get_binary_sensors()
    assert _by_key(empty_binary, "co2_problem")["value_fn"]() is False
    assert _by_key(empty_binary, "temperature_problem")["value_fn"]() is False
    assert _by_key(empty_binary, "humidity_problem")["value_fn"]() is False


def test_life_quality_sensors_value_fns() -> None:
    handler = LifeQualityHandler(
        _device(
            DeviceType.LIFE_QUALITY,
            {"actualCO2": 600, "actualTemperature": 215, "actualHumidity": 470, "calibrationState": "IN_PROGRESS"},
            battery_level=90,
            signal_strength=77,
        )
    )
    sensors = handler.get_sensors()
    assert {"co2", "temperature", "humidity", "battery", "signal_strength", "calibration_state"} <= _keys(sensors)
    assert _by_key(sensors, "co2")["value_fn"]() == 600
    assert _by_key(sensors, "temperature")["value_fn"]() == 21.5
    assert _by_key(sensors, "humidity")["value_fn"]() == 47.0
    assert _by_key(sensors, "calibration_state")["value_fn"]() == "in progress"


def test_life_quality_temperature_fallback_and_humidity_none() -> None:
    # No actualTemperature -> fall back to integer "temperature"
    fallback = LifeQualityHandler(_device(DeviceType.LIFE_QUALITY, {"temperature": 23}))
    assert fallback._get_temperature() == 23
    # No temperature at all -> None
    none_handler = LifeQualityHandler(_device(DeviceType.LIFE_QUALITY))
    assert none_handler._get_temperature() is None
    assert none_handler._get_humidity() is None


def test_life_quality_indicator_switch() -> None:
    on = LifeQualityHandler(_device(DeviceType.LIFE_QUALITY, {"indication": "CO2_ON"}))
    switch = _by_key(on.get_switches(), "indicator_light")
    assert switch["value_fn"]() is True

    off = LifeQualityHandler(_device(DeviceType.LIFE_QUALITY, {"indication": "CO2_OFF"}))
    assert _by_key(off.get_switches(), "indicator_light")["value_fn"]() is False

    none = LifeQualityHandler(_device(DeviceType.LIFE_QUALITY))
    assert none.get_switches() == []


# ---------------------------------------------------------------------------
# LightSwitch
# ---------------------------------------------------------------------------


def test_lightswitch_binary_sensors_branches() -> None:
    handler = LightSwitchHandler(
        _device(
            DeviceType.WALLSWITCH,
            {
                "protectStatuses": ["CURRENT_LIMIT_ON", "TEMPERATURE_LIMIT_ON"],
                "dataChannelOk": "OK",
            },
            malfunctions=[1],
        )
    )
    binary = handler.get_binary_sensors()
    assert {"problem", "current_limit", "temperature_limit", "data_channel_ok"} <= _keys(binary)
    assert _by_key(binary, "current_limit")["value_fn"]() is True
    assert _by_key(binary, "temperature_limit")["value_fn"]() is True
    assert _by_key(binary, "data_channel_ok")["value_fn"]() is True
    assert _by_key(binary, "problem")["value_fn"]() is True


def test_lightswitch_sensors_branches() -> None:
    handler = LightSwitchHandler(
        _device(
            DeviceType.WALLSWITCH,
            {"temperature": 40, "dataChannelSignalQuality": "VERY_GOOD"},
            signal_strength=66,
            firmware_version="1.5",
        )
    )
    sensors = handler.get_sensors()
    assert {"signal_strength", "temperature", "data_channel_quality", "firmware_version"} <= _keys(sensors)
    assert _by_key(sensors, "data_channel_quality")["value_fn"]() == "very good"


def test_lightswitch_switches_settings_and_night_mode() -> None:
    handler = LightSwitchHandler(
        _device(
            DeviceType.WALLSWITCH,
            {
                "settingsSwitch": [
                    "LED_INDICATOR_ENABLED",
                    "CHILD_LOCK_ENABLED",
                    "STATE_MEMORY_ENABLED",
                    "CURRENT_THRESHOLD_ENABLED",
                ],
                "night_mode_arm": True,
            },
        )
    )
    switches = handler.get_switches()
    assert {"led_indicator", "child_lock", "state_memory", "current_threshold", "night_mode"} <= _keys(switches)
    assert _by_key(switches, "led_indicator")["value_fn"]() is True
    assert _by_key(switches, "child_lock")["value_fn"]() is True
    assert _by_key(switches, "state_memory")["value_fn"]() is True
    assert _by_key(switches, "current_threshold")["value_fn"]() is True
    assert _by_key(switches, "night_mode")["value_fn"]() is True


def test_lightswitch_numbers_and_selects() -> None:
    handler = LightSwitchHandler(
        _device(DeviceType.WALLSWITCH, {"touchSensitivity": 5, "touchMode": "TOUCH_MODE_TOGGLE"})
    )
    numbers = handler.get_numbers()
    assert _by_key(numbers, "touch_sensitivity")["value_fn"]() == 5

    selects = handler.get_selects()
    touch = _by_key(selects, "touch_mode")
    assert touch["value_fn"]() == "touch_mode_toggle"
    assert touch["value_fn"]() in touch["options"]


def test_lightswitch_empty_optional_collections() -> None:
    handler = LightSwitchHandler(_device(DeviceType.WALLSWITCH))
    assert handler.get_numbers() == []
    assert handler.get_selects() == []
    assert handler.get_switches() == []


# ---------------------------------------------------------------------------
# Dimmer
# ---------------------------------------------------------------------------


def test_dimmer_binary_sensors_and_sensors() -> None:
    handler = DimmerHandler(
        _device(
            DeviceType.WALLSWITCH,
            {
                "protectStatuses": ["CURRENT_LIMIT_ON", "TEMPERATURE_LIMIT_ON"],
                "dataChannelOk": "OK",
                "temperature": 35,
                "actualBrightnessCh1": 75,
                "dataChannelSignalQuality": "GOOD_SIGNAL",
            },
            raw_type="lightSwitchDimmer",
            signal_strength=88,
            firmware_version="2.2",
            malfunctions=[9],
        )
    )
    binary = handler.get_binary_sensors()
    assert {"problem", "current_limit", "temperature_limit", "data_channel_ok"} <= _keys(binary)
    assert _by_key(binary, "current_limit")["value_fn"]() is True
    assert _by_key(binary, "temperature_limit")["value_fn"]() is True
    assert _by_key(binary, "data_channel_ok")["value_fn"]() is True

    sensors = handler.get_sensors()
    assert {
        "signal_strength",
        "temperature",
        "current_brightness",
        "data_channel_quality",
        "firmware_version",
    } <= _keys(sensors)
    assert _by_key(sensors, "current_brightness")["value_fn"]() == 75
    assert _by_key(sensors, "data_channel_quality")["value_fn"]() == "good signal"


def test_dimmer_minimal_device() -> None:
    handler = DimmerHandler(_device(DeviceType.WALLSWITCH, raw_type="lightSwitchDimmer", signal_strength=10))
    binary = handler.get_binary_sensors()
    # current_limit/temperature_limit are always present; value_fn reads empty list
    assert _by_key(binary, "current_limit")["value_fn"]() is False
    assert _by_key(binary, "temperature_limit")["value_fn"]() is False
    sensors = handler.get_sensors()
    assert _by_key(sensors, "current_brightness")["value_fn"]() is None


# ---------------------------------------------------------------------------
# Transmitter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "alarm_type",
    ["INTRUSION", "OPENING", "FIRE", "FLOOD", "GAS", "CO", "PANIC", "MEDICAL", "UNKNOWN_TYPE"],
)
def test_transmitter_alarm_type_device_class(alarm_type: str) -> None:
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass

    handler = TransmitterHandler(_device(DeviceType.TRANSMITTER, {"customAlarmType": alarm_type}))
    ext = _by_key(handler.get_binary_sensors(), "external_contact")
    expected = TransmitterHandler.ALARM_TYPE_DEVICE_CLASS.get(alarm_type, BinarySensorDeviceClass.OPENING)
    assert ext["device_class"] == expected


def test_transmitter_external_contact_value_fns() -> None:
    triggered = TransmitterHandler(_device(DeviceType.TRANSMITTER, {"externalContactTriggered": True}))
    assert _by_key(triggered.get_binary_sensors(), "external_contact")["value_fn"]() is True

    fallback = TransmitterHandler(_device(DeviceType.TRANSMITTER, {"door_opened": True}))
    assert _by_key(fallback.get_binary_sensors(), "external_contact")["value_fn"]() is True


def test_transmitter_sensors_all_branches() -> None:
    handler = TransmitterHandler(
        _device(
            DeviceType.TRANSMITTER,
            {
                "temperature": 20,
                "externalContactStateMode": "nc",
                "customAlarmType": "OPENING",
                "externalContactAlarmMode": "IMPULSE",
                "externalDevicePowerSupplyMode": "POWER_SAVING",
                "armDelaySeconds": 30,
                "alarmDelaySeconds": 15,
            },
            battery_level=44,
            signal_strength=55,
        )
    )
    sensors = handler.get_sensors()
    assert {
        "battery",
        "signal_strength",
        "temperature",
        "contact_mode",
        "alarm_type",
        "alarm_mode",
        "power_supply_mode",
        "arm_delay",
        "alarm_delay",
    } <= _keys(sensors)
    assert _by_key(sensors, "contact_mode")["value_fn"]() == "NC"
    assert _by_key(sensors, "alarm_type")["value_fn"]() == "opening"
    assert _by_key(sensors, "alarm_mode")["value_fn"]() == "impulse"
    assert _by_key(sensors, "power_supply_mode")["value_fn"]() == "power saving"
    assert _by_key(sensors, "arm_delay")["value_fn"]() == 30
    assert _by_key(sensors, "alarm_delay")["value_fn"]() == 15


def test_transmitter_switches_with_accelerometer() -> None:
    handler = TransmitterHandler(
        _device(
            DeviceType.TRANSMITTER,
            {
                "externalContactAlwaysActive": True,
                "night_mode_arm": True,
                "accelerometerAware": True,
                "siren_triggers": ["EXTRA_CONTACT", "ACCELERATION"],
            },
        )
    )
    switches = handler.get_switches()
    assert {
        "always_active",
        "night_mode",
        "accelerometer",
        "siren_trigger_contact",
        "siren_trigger_acceleration",
    } <= _keys(switches)
    assert _by_key(switches, "always_active")["value_fn"]() is True
    assert _by_key(switches, "accelerometer")["value_fn"]() is True
    assert _by_key(switches, "siren_trigger_contact")["value_fn"]() is True
    assert _by_key(switches, "siren_trigger_acceleration")["value_fn"]() is True


def test_transmitter_switches_without_accelerometer() -> None:
    handler = TransmitterHandler(_device(DeviceType.TRANSMITTER))
    keys = _keys(handler.get_switches())
    assert {"always_active", "night_mode", "siren_trigger_contact"} <= keys
    assert "accelerometer" not in keys
    assert "siren_trigger_acceleration" not in keys


# ---------------------------------------------------------------------------
# ManualCallPoint
# ---------------------------------------------------------------------------


def test_manual_call_point_binary_sensors_value_fns() -> None:
    handler = ManualCallPointHandler(
        _device(DeviceType.MANUAL_CALL_POINT, {"switchState": "button_pressed", "tampered": True})
    )
    binary = handler.get_binary_sensors()
    assert {"fire_alarm", "tamper"} <= _keys(binary)
    assert _by_key(binary, "fire_alarm")["value_fn"]() is True
    assert _by_key(binary, "tamper")["value_fn"]() is True


def test_manual_call_point_sensors_branches() -> None:
    handler = ManualCallPointHandler(
        _device(
            DeviceType.MANUAL_CALL_POINT,
            {"temperature": 21, "switchState": "BUTTON_PRESSED", "color": "RED"},
            battery_level=60,
            signal_strength=40,
        )
    )
    sensors = handler.get_sensors()
    assert {"battery", "signal_strength", "temperature", "switch_state", "device_color"} <= _keys(sensors)
    assert _by_key(sensors, "switch_state")["value_fn"]() == "button_pressed"
    assert _by_key(sensors, "device_color")["value_fn"]() == "red"


def test_manual_call_point_sensors_minimal() -> None:
    handler = ManualCallPointHandler(_device(DeviceType.MANUAL_CALL_POINT, battery_level=10, signal_strength=20))
    sensors = handler.get_sensors()
    keys = _keys(sensors)
    assert "temperature" not in keys
    assert "device_color" not in keys
    # default switch_state value
    assert _by_key(sensors, "switch_state")["value_fn"]() == "button_unpressed"
    assert handler.get_switches() == []


# ---------------------------------------------------------------------------
# WaterStop
# ---------------------------------------------------------------------------


def test_waterstop_binary_sensors_temp_protect() -> None:
    handler = WaterStopHandler(
        _device(DeviceType.WATERSTOP, {"tampered": True, "tempProtectState": "ON"}, malfunctions=[1])
    )
    binary = handler.get_binary_sensors()
    assert {"tamper", "problem", "temp_protect"} <= _keys(binary)
    assert _by_key(binary, "temp_protect")["value_fn"]() is True
    assert _by_key(binary, "problem")["value_fn"]() is True


def test_waterstop_sensors_all_branches() -> None:
    handler = WaterStopHandler(
        _device(
            DeviceType.WATERSTOP,
            {
                "temperature": 12,
                "motorState": "OFF",
                "extPower": "SUPPLY",
                "preventionEnable": "ENABLED",
                "preventionDaysPeriod": 7,
            },
            battery_level=95,
            signal_strength=85,
            firmware_version="4.0",
        )
    )
    sensors = handler.get_sensors()
    assert {
        "battery",
        "temperature",
        "signal_strength",
        "motor_state",
        "external_power",
        "prevention_status",
        "prevention_period",
        "firmware_version",
    } <= _keys(sensors)
    assert _by_key(sensors, "motor_state")["value_fn"]() == "off"
    assert _by_key(sensors, "external_power")["value_fn"]() == "supply"
    assert _by_key(sensors, "prevention_status")["value_fn"]() == "enabled"
    assert _by_key(sensors, "prevention_period")["value_fn"]() == 7
    assert _by_key(sensors, "firmware_version")["value_fn"]() == "4.0"


def test_waterstop_valve_actions() -> None:
    handler = WaterStopHandler(_device(DeviceType.WATERSTOP, {"valveState": "OPEN"}))
    valve = _by_key(handler.get_valves(), "valve")
    assert valve["value_fn"]() is True
    assert valve["open_fn"]() == {"action": "open_valve"}
    assert valve["close_fn"]() == {"action": "close_valve"}

    closed = WaterStopHandler(_device(DeviceType.WATERSTOP, {"valveState": "CLOSED"}))
    assert _by_key(closed.get_valves(), "valve")["value_fn"]() is False


# ---------------------------------------------------------------------------
# Siren
# ---------------------------------------------------------------------------


def test_siren_binary_sensors_tamper_and_external_power() -> None:
    handler = SirenHandler(_device(DeviceType.SIREN, {"tampered": False, "externallyPowered": True}))
    binary = handler.get_binary_sensors()
    assert {"tamper", "externally_powered"} <= _keys(binary)
    assert _by_key(binary, "externally_powered")["value_fn"]() is True

    # tampered None -> no tamper sensor
    no_tamper = SirenHandler(_device(DeviceType.SIREN, {"tampered": None}))
    assert "tamper" not in _keys(no_tamper.get_binary_sensors())


def test_siren_sensors_branches() -> None:
    handler = SirenHandler(
        _device(DeviceType.SIREN, {"temperature": 24}, battery_level=33, signal_strength=44),
    )
    sensors = handler.get_sensors()
    assert {"battery", "signal_strength", "temperature"} <= _keys(sensors)
    _run_all_value_fns(sensors)

    # No battery/signal -> only what is present
    minimal = SirenHandler(_device(DeviceType.SIREN))
    assert minimal.get_sensors() == []


def test_siren_selects_volume_and_beep() -> None:
    handler = SirenHandler(
        _device(
            DeviceType.SIREN,
            {
                "siren_volume_level": "LOUD",
                "beep_volume_level": "QUIET",
                "alarm_duration": 5,
            },
        )
    )
    selects = handler.get_selects()
    assert {"siren_volume", "beep_volume", "alarm_duration"} <= _keys(selects)
    sv = _by_key(selects, "siren_volume")
    assert sv["value_fn"]() == "loud"
    assert sv["api_transform"]("loud") == "LOUD"
    bv = _by_key(selects, "beep_volume")
    assert bv["value_fn"]() == "quiet"
    assert bv["api_transform"]("quiet") == "QUIET"


def test_siren_select_volume_defaults_when_none() -> None:
    handler = SirenHandler(
        _device(DeviceType.SIREN, {"siren_volume_level": None, "beep_volume_level": None}),
    )
    selects = handler.get_selects()
    assert _by_key(selects, "siren_volume")["value_fn"]() == "very_loud"
    assert _by_key(selects, "beep_volume")["value_fn"]() == "loud"


def test_siren_alarm_duration_default_on_invalid() -> None:
    handler = SirenHandler(_device(DeviceType.SIREN, {"alarm_duration": "oops"}))
    spec = _by_key(handler.get_selects(), "alarm_duration")
    assert spec["value_fn"]() == "3"
    assert spec["api_transform"]("not_an_int") == 3


def test_siren_switches_all_branches() -> None:
    handler = SirenHandler(
        _device(
            DeviceType.SIREN,
            {
                "night_mode_arm": True,
                "beep_on_arm_disarm": True,
                "beep_on_delay": True,
                "led_indication": "BLINK_WHILE_ARMED",
                "chimes_enabled": True,
                "alert_if_moved": True,
            },
        )
    )
    switches = handler.get_switches()
    assert {
        "night_mode",
        "beep_on_arm_disarm",
        "beep_on_delay",
        "blink_while_armed",
        "chimes",
        "alert_if_moved",
    } <= _keys(switches)
    assert _by_key(switches, "blink_while_armed")["value_fn"]() is True
    assert _by_key(switches, "chimes")["value_fn"]() is True
    assert _by_key(switches, "alert_if_moved")["value_fn"]() is True


def test_siren_blink_state_variants() -> None:
    # bool led_indication
    bool_led = SirenHandler(_device(DeviceType.SIREN, {"led_indication": True}))
    assert _by_key(bool_led.get_switches(), "blink_while_armed")["value_fn"]() is True

    # string led_indication not matching
    str_led = SirenHandler(_device(DeviceType.SIREN, {"led_indication": "DISABLED"}))
    assert _by_key(str_led.get_switches(), "blink_while_armed")["value_fn"]() is False

    # fall back to blink_while_armed attribute
    fallback = SirenHandler(_device(DeviceType.SIREN, {"blink_while_armed": True}))
    assert _by_key(fallback.get_switches(), "blink_while_armed")["value_fn"]() is True


# ---------------------------------------------------------------------------
# Button / Doorbell / Repeater
# ---------------------------------------------------------------------------


def test_button_sensors_all_enum_branches() -> None:
    handler = ButtonHandler(
        _device(
            DeviceType.BUTTON,
            {
                "last_action": "single_press",
                "button_mode": "PANIC_BUTTON",
                "brightness": "HIGH",
                "false_press_filter": "LONG_PUSH",
            },
            battery_level=70,
            signal_strength=80,
        )
    )
    sensors = handler.get_sensors()
    assert {
        "battery",
        "signal_strength",
        "last_action",
        "button_mode",
        "button_brightness",
        "false_press_filter",
    } <= _keys(sensors)
    assert _by_key(sensors, "last_action")["value_fn"]() == "single_press"
    assert _by_key(sensors, "button_mode")["value_fn"]() == "panic_button"
    assert _by_key(sensors, "button_brightness")["value_fn"]() == "high"
    assert _by_key(sensors, "false_press_filter")["value_fn"]() == "long_push"

    binary = handler.get_binary_sensors()
    assert _keys(binary) == {"tamper"}
    assert handler.get_switches() == []

    events = handler.get_events()
    assert _by_key(events, "button_press")["event_types"]


def test_button_sensors_minimal() -> None:
    handler = ButtonHandler(_device(DeviceType.BUTTON, battery_level=50, signal_strength=50))
    sensors = handler.get_sensors()
    keys = _keys(sensors)
    assert {"battery", "signal_strength", "last_action"} <= keys
    assert "button_mode" not in keys
    assert _by_key(sensors, "last_action")["value_fn"]() is None


def test_doorbell_sensors_and_events() -> None:
    handler = DoorbellHandler(
        _device(
            DeviceType.DOORBELL,
            {"last_ring": "2026-05-31T10:00:00Z", "tampered": True},
            battery_level=40,
            signal_strength=30,
        )
    )
    sensors = handler.get_sensors()
    assert {"battery", "signal_strength", "last_ring"} <= _keys(sensors)
    # TIMESTAMP sensor must return a datetime parsed from the stored ISO string.
    ring = _by_key(sensors, "last_ring")["value_fn"]()
    assert ring is not None and ring.isoformat() == "2026-05-31T10:00:00+00:00"
    assert _by_key(handler.get_binary_sensors(), "tamper")["value_fn"]() is True
    assert _by_key(handler.get_events(), "doorbell_press")["event_types"] == ["ring"]


def test_repeater_sensors_and_binary() -> None:
    handler = RepeaterHandler(_device(DeviceType.REPEATER, {"tampered": False}, battery_level=99, signal_strength=99))
    sensors = handler.get_sensors()
    assert _keys(sensors) == {"battery", "signal_strength"}
    assert _by_key(sensors, "battery")["value_fn"]() == 99
    binary = handler.get_binary_sensors()
    assert _by_key(binary, "tamper")["value_fn"]() is False


# ---------------------------------------------------------------------------
# GenericHandler (fallback for recognised types without a dedicated module)
# ---------------------------------------------------------------------------


def test_generic_handler_mapped_for_orphan_types() -> None:
    """THERMOSTAT / TEMPERATURE_SENSOR / LINE_SPLITTER must not be silently skipped."""
    for device_type in (DeviceType.THERMOSTAT, DeviceType.TEMPERATURE_SENSOR, DeviceType.LINE_SPLITTER):
        handler_class = get_device_handler(_device(device_type))
        assert handler_class is GenericHandler, device_type


def test_generic_handler_standard_diagnostics() -> None:
    handler = GenericHandler(
        _device(DeviceType.LINE_SPLITTER, {"tampered": True}, battery_level=88, signal_strength=70)
    )
    sensors = handler.get_sensors()
    assert _keys(sensors) == {"battery", "signal_strength", "firmware_version"}
    assert _by_key(sensors, "battery")["value_fn"]() == 88
    binary = handler.get_binary_sensors()
    assert _keys(binary) == {"tamper", "problem"}
    assert _by_key(binary, "tamper")["value_fn"]() is True
    _run_all_value_fns(sensors + binary)


def test_generic_handler_temperature_when_reported() -> None:
    handler = GenericHandler(_device(DeviceType.TEMPERATURE_SENSOR, {"temperature": 21.5}))
    sensors = handler.get_sensors()
    assert "temperature" in _keys(sensors)
    assert _by_key(sensors, "temperature")["value_fn"]() == 21.5
