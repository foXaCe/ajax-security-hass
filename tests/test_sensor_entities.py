"""Tests for AjaxSpaceSensor / AjaxDeviceSensor / AjaxVideoEdgeSensor.

We focus on the descriptor-driven plumbing: value_fn returns flow
through native_value, missing space/device returns None (HA = unknown),
buggy descriptors don't crash, and availability tracks both the
coordinator status and the per-device online flag.

We bypass CoordinatorEntity.__init__ (object.__new__) to avoid a full
HA fixture for what is essentially descriptor wiring.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.ajax.models import (
    AjaxDevice,
    AjaxSpace,
    AjaxVideoEdge,
    DeviceType,
    SecurityState,
    VideoEdgeType,
)
from custom_components.ajax.sensor import (
    AjaxDeviceSensor,
    AjaxHubSensor,
    AjaxSmartLockSensor,
    AjaxSpaceSensor,
    AjaxSpaceSensorDescription,
    AjaxVideoEdgeSensor,
    _get_hub_sensors,
)

# ---------------------------------------------------------------------------
# AjaxSpaceSensor
# ---------------------------------------------------------------------------


def _make_space_sensor(
    description: AjaxSpaceSensorDescription,
    *,
    space: AjaxSpace | None = None,
) -> AjaxSpaceSensor:
    sensor = object.__new__(AjaxSpaceSensor)
    sensor.entity_description = description
    sensor._space_id = "s1"
    sensor._entry = SimpleNamespace(entry_id="entry1")
    coordinator = SimpleNamespace(get_space=lambda sid: space)
    sensor.coordinator = coordinator
    return sensor


def test_space_sensor_native_value_calls_value_fn() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    space.devices["d1"] = AjaxDevice(id="d1", name="X", type=DeviceType.MOTION_DETECTOR, space_id="s1", hub_id="hub1")
    desc = AjaxSpaceSensorDescription(key="total_devices", value_fn=lambda s: len(s.devices))
    assert _make_space_sensor(desc, space=space).native_value == 1


def test_space_sensor_native_value_returns_none_when_space_missing() -> None:
    desc = AjaxSpaceSensorDescription(key="total_devices", value_fn=lambda s: 5)
    assert _make_space_sensor(desc, space=None).native_value is None


def test_space_sensor_native_value_returns_none_when_value_fn_missing() -> None:
    """Descriptor without value_fn — must NOT crash, must return None."""
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    desc = AjaxSpaceSensorDescription(key="placeholder", value_fn=None)
    assert _make_space_sensor(desc, space=space).native_value is None


def test_space_sensor_extra_attributes_only_on_recent_events_key() -> None:
    """Only the `recent_events` sensor gets the recent-history attrs payload."""
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    space.recent_events = []

    other = _make_space_sensor(AjaxSpaceSensorDescription(key="total_devices", value_fn=lambda s: 0), space=space)
    assert other.extra_state_attributes is None

    recent = _make_space_sensor(AjaxSpaceSensorDescription(key="recent_events", value_fn=lambda s: ""), space=space)
    assert recent.extra_state_attributes == {"events_count": 0}


def test_space_sensor_extra_attributes_handle_missing_space() -> None:
    """Recent-events sensor with no space returns None, not a crash."""
    sensor = _make_space_sensor(AjaxSpaceSensorDescription(key="recent_events", value_fn=lambda s: ""), space=None)
    assert sensor.extra_state_attributes is None


# ---------------------------------------------------------------------------
# AjaxDeviceSensor
# ---------------------------------------------------------------------------


def _device(online: bool = True) -> AjaxDevice:
    return AjaxDevice(
        id="d1",
        name="Sensor",
        type=DeviceType.MOTION_DETECTOR,
        space_id="s1",
        hub_id="hub1",
        online=online,
    )


def _make_device_sensor(
    sensor_desc: dict,
    *,
    device: AjaxDevice | None = None,
    update_success: bool = True,
) -> AjaxDeviceSensor:
    sensor = object.__new__(AjaxDeviceSensor)
    sensor._space_id = "s1"
    sensor._device_id = "d1"
    sensor._sensor_key = sensor_desc["key"]
    sensor._sensor_desc = sensor_desc
    space = SimpleNamespace(devices={"d1": device} if device else {})
    coordinator = SimpleNamespace(
        get_space=lambda sid: space,
        last_update_success=update_success,
    )
    sensor.coordinator = coordinator
    return sensor


def test_device_sensor_native_value_returns_value_fn() -> None:
    sensor = _make_device_sensor({"key": "battery", "value_fn": lambda: 85}, device=_device())
    assert sensor.native_value == 85


def test_device_sensor_native_value_returns_none_when_device_missing() -> None:
    sensor = _make_device_sensor({"key": "battery", "value_fn": lambda: 85}, device=None)
    assert sensor.native_value is None


def test_device_sensor_native_value_returns_none_when_value_fn_raises() -> None:
    """A buggy descriptor must NOT crash the platform."""

    def boom() -> int:
        raise ValueError("missing attribute")

    sensor = _make_device_sensor({"key": "battery", "value_fn": boom}, device=_device())
    assert sensor.native_value is None


def test_device_sensor_available_tracks_device_online_flag() -> None:
    """available=False the second a device goes offline."""
    online_sensor = _make_device_sensor({"key": "battery", "value_fn": lambda: 1}, device=_device(online=True))
    assert online_sensor.available is True

    offline_sensor = _make_device_sensor({"key": "battery", "value_fn": lambda: 1}, device=_device(online=False))
    assert offline_sensor.available is False


def test_device_sensor_available_false_when_coordinator_failed() -> None:
    sensor = _make_device_sensor(
        {"key": "battery", "value_fn": lambda: 1}, device=_device(online=True), update_success=False
    )
    assert sensor.available is False


def test_device_sensor_init_wires_descriptor_metadata() -> None:
    from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
    from homeassistant.const import UnitOfTemperature

    coord = MagicMock()
    coord.entry_id = "entry_test"
    sensor = AjaxDeviceSensor(
        coordinator=coord,
        space_id="s1",
        device_id="d1",
        sensor_key="temperature",
        sensor_desc={
            "key": "temperature",
            "device_class": SensorDeviceClass.TEMPERATURE,
            "native_unit_of_measurement": UnitOfTemperature.CELSIUS,
            "state_class": SensorStateClass.MEASUREMENT,
            "translation_key": "temperature",
            "value_fn": lambda: 21.5,
        },
    )
    assert sensor._attr_unique_id == "entry_test_d1_temperature"
    assert sensor._attr_device_class is SensorDeviceClass.TEMPERATURE
    assert sensor._attr_native_unit_of_measurement == UnitOfTemperature.CELSIUS
    assert sensor._attr_state_class is SensorStateClass.MEASUREMENT
    assert sensor._attr_translation_key == "temperature"


# ---------------------------------------------------------------------------
# AjaxVideoEdgeSensor
# ---------------------------------------------------------------------------


def _video_edge(online: bool = True) -> AjaxVideoEdge:
    # online is a read-only property derived from connection_state.
    return AjaxVideoEdge(
        id="ve1",
        name="Cam",
        space_id="s1",
        video_edge_type=VideoEdgeType.BULLET,
        connection_state="ONLINE" if online else "OFFLINE",
    )


def _make_ve_sensor(
    sensor_desc: dict,
    *,
    video_edge: AjaxVideoEdge | None = None,
    update_success: bool = True,
) -> AjaxVideoEdgeSensor:
    sensor = object.__new__(AjaxVideoEdgeSensor)
    sensor._space_id = "s1"
    sensor._video_edge_id = "ve1"
    sensor._sensor_key = sensor_desc["key"]
    sensor._sensor_desc = sensor_desc
    space = SimpleNamespace(video_edges={"ve1": video_edge} if video_edge else {})
    sensor.coordinator = SimpleNamespace(get_space=lambda sid: space, last_update_success=update_success)
    return sensor


def test_ve_sensor_native_value_and_missing() -> None:
    assert (
        _make_ve_sensor({"key": "storage", "value_fn": lambda: "ready"}, video_edge=_video_edge()).native_value
        == "ready"
    )
    assert _make_ve_sensor({"key": "storage", "value_fn": lambda: "ready"}, video_edge=None).native_value is None


def test_ve_sensor_native_value_swallows_value_fn_error() -> None:
    def boom() -> str:
        raise KeyError("x")

    assert _make_ve_sensor({"key": "storage", "value_fn": boom}, video_edge=_video_edge()).native_value is None


def test_ve_sensor_available_tracks_online_and_coordinator() -> None:
    assert _make_ve_sensor({"key": "s", "value_fn": lambda: 1}, video_edge=_video_edge(online=True)).available is True
    assert _make_ve_sensor({"key": "s", "value_fn": lambda: 1}, video_edge=_video_edge(online=False)).available is False
    assert _make_ve_sensor({"key": "s", "value_fn": lambda: 1}, video_edge=None).available is False
    assert (
        _make_ve_sensor({"key": "s", "value_fn": lambda: 1}, video_edge=_video_edge(), update_success=False).available
        is False
    )


def test_ve_sensor_extra_state_attributes() -> None:
    desc = {"key": "s", "value_fn": lambda: 1, "extra_state_attributes_fn": lambda: {"a": 1}}
    assert _make_ve_sensor(desc, video_edge=_video_edge()).extra_state_attributes == {"a": 1}
    # no fn → None
    assert _make_ve_sensor({"key": "s", "value_fn": lambda: 1}, video_edge=_video_edge()).extra_state_attributes is None


def test_ve_sensor_extra_state_attributes_swallows_error() -> None:
    def boom() -> dict:
        raise ValueError("nope")

    desc = {"key": "s", "value_fn": lambda: 1, "extra_state_attributes_fn": boom}
    assert _make_ve_sensor(desc, video_edge=_video_edge()).extra_state_attributes is None


# ---------------------------------------------------------------------------
# _get_hub_sensors (pure descriptor builder)
# ---------------------------------------------------------------------------


def _space_with_hub(hub_details: dict | None) -> AjaxSpace:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    space.hub_details = hub_details
    return space


def _hub_sensor_by_key(hub_details: dict) -> dict[str, dict]:
    return {s["key"]: s for s in _get_hub_sensors(_space_with_hub(hub_details))}


def test_get_hub_sensors_empty_when_no_details() -> None:
    assert _get_hub_sensors(_space_with_hub(None)) == []
    assert _get_hub_sensors(_space_with_hub({})) == []


def test_get_hub_sensors_battery() -> None:
    sensors = _hub_sensor_by_key({"battery": {"chargeLevelPercentage": 88}})
    assert "hub_battery" in sensors
    assert sensors["hub_battery"]["value_fn"]() == 88


def test_get_hub_sensors_gsm_signal_network_sim_lowercased() -> None:
    sensors = _hub_sensor_by_key({"gsm": {"signalLevel": "HIGH", "networkStatus": "LTE", "simCardState": "READY"}})
    assert sensors["gsm_signal"]["value_fn"]() == "high"
    assert sensors["gsm_network"]["value_fn"]() == "lte"
    assert sensors["sim_status"]["value_fn"]() == "ready"


def test_get_hub_sensors_active_channels_sorted() -> None:
    """Issue #76: channels arrive in random order — must be sorted to avoid churn."""
    sensors = _hub_sensor_by_key({"activeChannels": ["GSM", "ETHERNET"]})
    assert sensors["active_connection"]["value_fn"]() == "ETHERNET, GSM"


def test_get_hub_sensors_firmware() -> None:
    sensors = _hub_sensor_by_key({"firmware": {"version": "9.9"}})
    assert sensors["hub_firmware"]["value_fn"]() == "9.9"


# ---------------------------------------------------------------------------
# AjaxHubSensor
# ---------------------------------------------------------------------------


def _make_hub_sensor(sensor_desc: dict, *, hub_details: dict | None) -> AjaxHubSensor:
    sensor = object.__new__(AjaxHubSensor)
    sensor._space_id = "s1"
    sensor._sensor_key = sensor_desc["key"]
    sensor._sensor_desc = sensor_desc
    space = SimpleNamespace(hub_details=hub_details) if hub_details is not None else SimpleNamespace(hub_details=None)
    sensor.coordinator = SimpleNamespace(get_space=lambda sid: space)
    return sensor


def test_hub_sensor_native_value_passes_hub_details() -> None:
    desc = {"key": "hub_firmware", "value_fn": lambda hd: hd.get("firmware", {}).get("version")}
    assert _make_hub_sensor(desc, hub_details={"firmware": {"version": "9.0"}}).native_value == "9.0"


def test_hub_sensor_native_value_none_without_details() -> None:
    desc = {"key": "x", "value_fn": lambda hd: 1}
    assert _make_hub_sensor(desc, hub_details=None).native_value is None


def test_hub_sensor_native_value_swallows_error() -> None:
    def boom(hd: dict) -> int:
        raise RuntimeError("x")

    assert _make_hub_sensor({"key": "x", "value_fn": boom}, hub_details={"a": 1}).native_value is None


def test_hub_sensor_available() -> None:
    assert _make_hub_sensor({"key": "x", "value_fn": lambda hd: 1}, hub_details={"a": 1}).available is True
    assert _make_hub_sensor({"key": "x", "value_fn": lambda hd: 1}, hub_details=None).available is False


# ---------------------------------------------------------------------------
# AjaxSmartLockSensor
# ---------------------------------------------------------------------------


def _make_smart_lock_sensor(lock: object | None) -> AjaxSmartLockSensor:
    sensor = object.__new__(AjaxSmartLockSensor)
    sensor._space_id = "s1"
    sensor._smart_lock_id = "lock1"
    space = SimpleNamespace(smart_locks={"lock1": lock} if lock else {})
    sensor.coordinator = SimpleNamespace(get_space=lambda sid: space)
    return sensor


def test_smart_lock_sensor_native_value() -> None:
    lock = SimpleNamespace(last_changed_by="Alice", name="Front Door")
    assert _make_smart_lock_sensor(lock).native_value == "Alice"


def test_smart_lock_sensor_native_value_none_when_missing() -> None:
    assert _make_smart_lock_sensor(None).native_value is None


def test_smart_lock_sensor_available() -> None:
    lock = SimpleNamespace(last_changed_by="Bob", name="Door")
    assert _make_smart_lock_sensor(lock).available is True
    assert _make_smart_lock_sensor(None).available is False
