"""Smoke tests for every Ajax device handler.

The handlers are descriptor factories: they translate the raw Ajax
device attributes into entity descriptions consumed by the HA platforms.
Bugs here are silent — a missing key or a wrong device_class doesn't
crash, it just stops an entity from showing up.

These tests instantiate each handler with a representative set of
attributes and assert the basic shape of the returned descriptors so
typos or accidental key removals fail fast.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.ajax.devices import (
    DEVICE_HANDLERS,
    ButtonHandler,
    DimmerHandler,
    DoorbellHandler,
    DoorContactHandler,
    FloodDetectorHandler,
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


def _device(device_type: DeviceType, attributes: dict[str, Any] | None = None, **kwargs: Any) -> AjaxDevice:
    return AjaxDevice(
        id="dev1",
        name="Test device",
        type=device_type,
        space_id="space1",
        hub_id="hub1",
        attributes=attributes or {},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# get_device_handler / is_dimmer_device routing
# ---------------------------------------------------------------------------


def test_device_handler_routes_each_registered_device_type() -> None:
    """Every entry in DEVICE_HANDLERS must resolve via get_device_handler."""
    for device_type, expected in DEVICE_HANDLERS.items():
        assert get_device_handler(_device(device_type)) is expected, device_type


def test_dimmer_handler_short_circuits_when_raw_type_is_dimmer() -> None:
    """DimmerHandler must win even when DeviceType is the generic LIGHTSWITCH."""
    dev = _device(DeviceType.WALLSWITCH, raw_type="lightSwitchDimmer")
    assert is_dimmer_device(dev) is True
    assert get_device_handler(dev) is DimmerHandler


def test_get_device_handler_returns_none_for_unsupported_type() -> None:
    """Unknown device types degrade silently to None — the discovery loop drops them."""
    dev = _device(DeviceType.UNKNOWN)
    assert get_device_handler(dev) is None


# ---------------------------------------------------------------------------
# Per-handler descriptors
# ---------------------------------------------------------------------------


def _keys(descriptors: list[dict]) -> set[str]:
    return {d["key"] for d in descriptors}


def _by_key(descriptors: list[dict], key: str) -> dict:
    return next(d for d in descriptors if d["key"] == key)


def test_door_contact_handler_creates_door_sensor() -> None:
    handler = DoorContactHandler(_device(DeviceType.DOOR_CONTACT))
    sensors = handler.get_binary_sensors()
    assert "door" in _keys(sensors)


def test_door_contact_handler_opts_in_external_contact_when_attribute_set() -> None:
    base = DoorContactHandler(_device(DeviceType.DOOR_CONTACT))
    assert "external_contact" not in _keys(base.get_binary_sensors())

    enabled = DoorContactHandler(_device(DeviceType.DOOR_CONTACT, {"extra_contact_aware": True}))
    assert "external_contact" in _keys(enabled.get_binary_sensors())


def test_wire_input_handler_returns_door_descriptor() -> None:
    sensors = WireInputHandler(_device(DeviceType.WIRE_INPUT)).get_binary_sensors()
    assert _keys(sensors) >= {"door"}


def test_motion_detector_handler_returns_motion_descriptor() -> None:
    handler = MotionDetectorHandler(_device(DeviceType.MOTION_DETECTOR))
    sensors = handler.get_binary_sensors()
    assert "motion" in _keys(sensors)


def test_smoke_detector_handler_returns_smoke_descriptor() -> None:
    handler = SmokeDetectorHandler(_device(DeviceType.SMOKE_DETECTOR))
    keys = _keys(handler.get_binary_sensors())
    assert {"smoke", "tamper"} <= keys


def test_smoke_detector_handler_adds_co_for_fireprotect2_variants() -> None:
    """CO sensor is only created for FireProtect2/Plus raw types — guards against descriptor leak."""
    no_co = SmokeDetectorHandler(_device(DeviceType.SMOKE_DETECTOR, raw_type="FireProtect"))
    assert "co" not in _keys(no_co.get_binary_sensors())

    with_co = SmokeDetectorHandler(_device(DeviceType.SMOKE_DETECTOR, raw_type="FireProtect2Plus"))
    assert "co" in _keys(with_co.get_binary_sensors())


def test_flood_detector_handler_returns_moisture_descriptor() -> None:
    sensors = FloodDetectorHandler(_device(DeviceType.FLOOD_DETECTOR)).get_binary_sensors()
    assert "moisture" in _keys(sensors)


@pytest.mark.parametrize(
    "attributes",
    [
        {"leak_detected": True},  # SSE transport (sse_manager)
        {"flood_alarm": True},  # SQS transport (sqs_manager)
        {"state": "ALARM"},  # REST (legacy, kept for symmetry)
    ],
)
def test_flood_detector_moisture_fires_on_realtime_attributes(attributes: dict[str, Any]) -> None:
    """Regression: the moisture sensor read a camelCase key nothing ever wrote."""
    sensors = FloodDetectorHandler(_device(DeviceType.FLOOD_DETECTOR, attributes)).get_binary_sensors()
    assert _by_key(sensors, "moisture")["value_fn"]() is True


def test_flood_detector_moisture_off_without_alarm() -> None:
    sensors = FloodDetectorHandler(_device(DeviceType.FLOOD_DETECTOR, {"leak_detected": False})).get_binary_sensors()
    assert not _by_key(sensors, "moisture")["value_fn"]()


def test_glass_break_handler_returns_glass_break_descriptor() -> None:
    sensors = GlassBreakHandler(_device(DeviceType.GLASS_BREAK)).get_binary_sensors()
    assert "glass_break" in _keys(sensors)


@pytest.mark.parametrize(
    "attributes",
    [
        {"glass_break_detected": True},  # SSE transport
        {"glass_alarm": True},  # SQS transport
        {"state": "ALARM"},  # REST (legacy)
    ],
)
def test_glass_break_fires_on_realtime_attributes(attributes: dict[str, Any]) -> None:
    """Regression: the dedicated GlassProtect sensor only read a key nothing wrote."""
    sensors = GlassBreakHandler(_device(DeviceType.GLASS_BREAK, attributes)).get_binary_sensors()
    assert _by_key(sensors, "glass_break")["value_fn"]() is True


@pytest.mark.parametrize("attributes", [{"smoke_detected": True}, {"smoke_alarm": True}, {"state": "ALARM"}])
def test_smoke_detector_smoke_fires_on_realtime_attributes(attributes: dict[str, Any]) -> None:
    """Regression: the SQS smoke_alarm key was not read by the smoke sensor."""
    sensors = SmokeDetectorHandler(_device(DeviceType.SMOKE_DETECTOR, attributes)).get_binary_sensors()
    assert _by_key(sensors, "smoke")["value_fn"]() is True


def test_button_handler_returns_button_press_event() -> None:
    events = ButtonHandler(_device(DeviceType.BUTTON)).get_events()
    assert _keys(events) == {"button_press"}


def test_doorbell_handler_emits_doorbell_ring_event() -> None:
    events = DoorbellHandler(_device(DeviceType.DOORBELL)).get_events()
    assert _keys(events) == {"doorbell_press"}


def test_manual_call_point_handler_emits_binary_sensors() -> None:
    sensors = ManualCallPointHandler(_device(DeviceType.MANUAL_CALL_POINT)).get_binary_sensors()
    assert sensors, "MCP must surface at least one binary sensor"


def test_repeater_handler_returns_tamper_descriptor() -> None:
    sensors = RepeaterHandler(_device(DeviceType.REPEATER)).get_binary_sensors()
    assert "tamper" in _keys(sensors)


def test_siren_handler_exposes_external_power_only_when_attribute_present() -> None:
    no_attr = SirenHandler(_device(DeviceType.SIREN))
    assert "externally_powered" not in _keys(no_attr.get_binary_sensors())

    with_attr = SirenHandler(_device(DeviceType.SIREN, {"externally_powered": True}))
    assert "externally_powered" in _keys(with_attr.get_binary_sensors())


def test_hub_handler_returns_at_least_one_sensor() -> None:
    sensors = HubHandler(_device(DeviceType.HUB, {"externally_powered": True})).get_binary_sensors()
    assert sensors, "HubHandler should emit at least one binary sensor"


def test_socket_handler_skips_power_sensors_when_attributes_missing() -> None:
    handler = SocketHandler(_device(DeviceType.SOCKET))
    keys = _keys(handler.get_sensors())
    assert "power" not in keys
    assert "energy" not in keys
    assert "voltage" not in keys


def test_socket_handler_creates_power_sensors_when_attributes_present() -> None:
    handler = SocketHandler(_device(DeviceType.SOCKET, {"power": 50, "energy": 1000, "voltage": 230, "current": 200}))
    keys = _keys(handler.get_sensors())
    assert {"power", "energy", "voltage", "current"} <= keys


def test_socket_handler_accepts_raw_api_attribute_names() -> None:
    """The Ajax API sometimes ships the un-normalised attribute names."""
    handler = SocketHandler(
        _device(
            DeviceType.SOCKET,
            {
                "powerConsumptionWatts": 60,
                "powerConsumedWattsPerHour": 1500,
                "voltageVolts": 235,
                "currentMilliAmpere": 300,
            },
        )
    )
    keys = _keys(handler.get_sensors())
    assert {"power", "energy", "voltage", "current"} <= keys


def test_lightswitch_handler_returns_binary_sensors_when_attrs_present() -> None:
    """LightSwitch only emits descriptors for attributes it actually sees."""
    handler = LightSwitchHandler(
        _device(DeviceType.WALLSWITCH, {"voltage": 230, "current": 1500, "power": 25}),
    )
    sensors = handler.get_sensors()
    assert sensors, "LightSwitch with power attrs should expose sensors"


def test_transmitter_handler_emits_tamper_and_external_contact() -> None:
    handler = TransmitterHandler(_device(DeviceType.TRANSMITTER))
    sensors = handler.get_binary_sensors()
    assert {"tamper", "external_contact"} <= _keys(sensors)


def test_transmitter_night_mode_reads_normalized_attribute_key() -> None:
    """Regression (code-review HIGH): the night_mode switch must read the
    coordinator-normalized 'night_mode_arm' key, not the raw 'nightModeArm'
    (which is never stored), otherwise it permanently reads OFF.
    """
    handler = TransmitterHandler(_device(DeviceType.TRANSMITTER, {"night_mode_arm": True}))
    night = next(s for s in handler.get_switches() if s["key"] == "night_mode")
    assert night["value_fn"]() is True

    # And it stays OFF (not crashing / not reading the raw key) when normalized off.
    handler_off = TransmitterHandler(_device(DeviceType.TRANSMITTER, {"night_mode_arm": False}))
    night_off = next(s for s in handler_off.get_switches() if s["key"] == "night_mode")
    assert night_off["value_fn"]() is False


def test_waterstop_handler_returns_problem_descriptor() -> None:
    handler = WaterStopHandler(_device(DeviceType.WATERSTOP))
    sensors = handler.get_binary_sensors()
    assert "problem" in _keys(sensors)


@pytest.mark.parametrize(
    "api_value,expected",
    [
        ("OFF", "off"),
        ("ROTATE_TO_CLOSING", "rotate_to_closing"),
        ("ROTATE_TO_OPEN", "rotate_to_open"),
    ],
)
def test_waterstop_motor_state_value_is_always_in_options(api_value: str, expected: str) -> None:
    """Regression (code-review): the motor_state ENUM must emit a value that
    is one of its declared options for every API enum (OFF / ROTATE_TO_CLOSING
    / ROTATE_TO_OPEN); an off-list value makes HA reject the state.
    """
    handler = WaterStopHandler(_device(DeviceType.WATERSTOP, {"motorState": api_value}))
    spec = next(s for s in handler.get_sensors() if s["key"] == "motor_state")
    value = spec["value_fn"]()
    assert value == expected
    assert value in spec["options"]


@pytest.mark.parametrize(
    "api_value,expected",
    [
        ("SUPPLY", "supply"),
        ("NO_SUPPLY", "no_supply"),
    ],
)
def test_waterstop_external_power_value_is_always_in_options(api_value: str, expected: str) -> None:
    """Regression (code-review): extPower ENUM emits SUPPLY / NO_SUPPLY — both
    must be declared options so the diagnostic sensor never shows a raw key.
    """
    handler = WaterStopHandler(_device(DeviceType.WATERSTOP, {"extPower": api_value}))
    spec = next(s for s in handler.get_sensors() if s["key"] == "external_power")
    value = spec["value_fn"]()
    assert value == expected
    assert value in spec["options"]


@pytest.mark.parametrize(
    "api_minutes,expected",
    [
        (1, "1"),
        (3, "3"),  # real StreetSiren/HomeSiren payloads report 3 == 3 minutes
        (15, "15"),
        (6, "5"),  # off-list value snaps to the nearest option
        (11, "10"),  # snaps down to the nearest declared option
        (99, "15"),  # clamps to the largest option, never an invalid state
    ],
)
def test_siren_alarm_duration_reads_minutes_and_snaps_to_option(api_minutes: int, expected: str) -> None:
    """Regression (code-review): alarmDuration is reported in MINUTES, not
    seconds. An earlier /60 conversion produced "0" (off-list); the read must
    map 1:1 and snap any off-list value to the nearest declared option.
    """
    handler = SirenHandler(_device(DeviceType.SIREN, {"alarm_duration": api_minutes}))
    spec = next(s for s in handler.get_selects() if s["key"] == "alarm_duration")
    value = spec["value_fn"]()
    assert value == expected
    assert value in spec["options"]
    # api_transform round-trips the selected minutes straight back (no x60).
    assert spec["api_transform"](expected) == int(expected)


@pytest.mark.parametrize(
    "feature_attr,expected_key",
    [
        ("temperature", "temperature"),
        ("humidity", "humidity"),
        ("co2", "co2"),
    ],
)
def test_life_quality_handler_creates_sensor_per_supported_feature(feature_attr: str, expected_key: str) -> None:
    """LifeQuality only exposes the sensors whose attribute is reported by the device."""
    handler = LifeQualityHandler(_device(DeviceType.LIFE_QUALITY, {feature_attr: 42}))
    keys = _keys(handler.get_sensors())
    assert expected_key in keys
