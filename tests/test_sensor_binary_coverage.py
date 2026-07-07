"""Extra coverage for sensor.py + binary_sensor.py.

This file complements ``test_sensor_entities.py`` and
``test_binary_sensor_entity.py`` by exercising the parts those files leave
uncovered:

- the pure formatting helpers in ``sensor.py`` (``format_*``,
  ``get_last_event_*``, ``_format_time_ago``);
- the ``device_info`` properties of every entity class (model naming,
  via_device / NVR linking, hub firmware);
- the hub/smart-lock binary sensors (``AjaxHubBinarySensor`` value_key vs
  value_fn, ``AjaxSmartLockBinarySensor`` door state);
- the registry-update path of ``AjaxBinarySensor``;
- ``async_setup_entry`` discovery for both platforms, driven with light
  mocks (no full HA harness).

Entities are built with ``object.__new__`` to bypass
``CoordinatorEntity.__init__``, mirroring the project convention.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.binary_sensor import BinarySensorDeviceClass

from custom_components.ajax.binary_sensor import (
    AjaxBinarySensor,
    AjaxHubBinarySensor,
    AjaxSmartLockBinarySensor,
    AjaxVideoEdgeBinarySensor,
    async_setup_entry as binary_async_setup_entry,
)
from custom_components.ajax.models import (
    AjaxDevice,
    AjaxSmartLock,
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
    _format_time_ago,
    async_setup_entry as sensor_async_setup_entry,
    format_event_text,
    format_hub_type,
    format_signal_level,
    format_timezone,
    get_last_event_attributes,
    get_last_event_text,
)

# ===========================================================================
# sensor.py — pure formatting helpers
# ===========================================================================


def test_format_timezone_variants() -> None:
    assert format_timezone(None) is None
    assert format_timezone("") is None
    # Single token (no underscore) returned unchanged.
    assert format_timezone("UTC") == "UTC"
    # region/city — city keeps underscores between words.
    assert format_timezone("europe_paris") == "Europe/Paris"
    assert format_timezone("america_new_york") == "America/New_York"


def test_format_hub_type_variants() -> None:
    assert format_hub_type(None) is None
    assert format_hub_type("") is None
    assert format_hub_type("hub_2_plus") == "Hub 2 Plus"


def test_format_signal_level() -> None:
    assert format_signal_level(None) is None
    assert format_signal_level("") is None
    assert format_signal_level("HIGH") == "high"


def test_format_event_text_uses_explicit_message() -> None:
    event = {
        "message": "Door opened",
        "device_name": " Front Door ",
        "room_name": " Hallway ",
        "source_name": "John",
    }
    text = format_event_text(event)
    assert text == "Door opened - Front Door (Hallway) by John"


def test_format_event_text_falls_back_to_action_map() -> None:
    # No message → action map (case-insensitive) supplies the text.
    assert format_event_text({"action": "DISARM"}) == "Disarmed"
    assert format_event_text({"action": "motion_detected"}) == "Motion detected"


def test_format_event_text_unknown_action_falls_back_to_raw() -> None:
    # Unknown action → uses the action itself, then event_type, then "Event".
    assert format_event_text({"action": "weird_thing"}) == "weird_thing"
    assert format_event_text({"event_type": "SOMETHING"}) == "SOMETHING"
    assert format_event_text({}) == "Event"


def test_format_event_text_french_uses_par() -> None:
    # A French-looking message switches the connector from "by" to "par".
    text = format_event_text({"message": "Armé", "user_name": "Marie"})
    assert text == "Armé par Marie"


def test_get_last_event_text_no_events() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    space.recent_events = []
    assert get_last_event_text(space) == "no_event"


def test_get_last_event_text_formats_first_event() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    space.recent_events = [{"message": "Armed", "user_name": "Bob"}]
    assert get_last_event_text(space) == "Armed by Bob"


def test_get_last_event_attributes_empty() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    space.recent_events = []
    assert get_last_event_attributes(space) == {"events_count": 0}


def test_get_last_event_attributes_with_datetime_timestamp() -> None:
    ts = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    space.recent_events = [
        {
            "event_type": "ALARM",
            "action": "motion_detected",
            "source_name": "PIR",
            "room_name": "Living",
            "message": "Motion detected",
            "timestamp": ts,
        }
    ]
    attrs = get_last_event_attributes(space)
    assert attrs["event_type"] == "ALARM"
    assert attrs["events_count"] == 1
    assert attrs["timestamp"] == ts.isoformat()
    assert "time_ago" in attrs
    # recent_history carries the formatted HH:MM:SS time.
    assert attrs["recent_history"][0]["time"] == ts.strftime("%H:%M:%S")
    assert attrs["recent_history"][0]["source"] == "PIR"


def test_get_last_event_attributes_with_string_timestamp() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    space.recent_events = [{"action": "arm", "timestamp": "2026-01-01T00:00:00"}]
    attrs = get_last_event_attributes(space)
    # Non-datetime timestamp is stringified, no time_ago.
    assert attrs["timestamp"] == "2026-01-01T00:00:00"
    assert "time_ago" not in attrs


def test_format_time_ago_buckets() -> None:
    now = datetime.now(UTC)
    assert _format_time_ago(now) == "Just now"
    assert _format_time_ago(now - timedelta(minutes=5)) == "5 min ago"
    assert _format_time_ago(now - timedelta(hours=3)) == "3h ago"
    assert _format_time_ago(now - timedelta(days=2)) == "2d ago"


def test_format_time_ago_naive_timestamp_treated_as_utc() -> None:
    # A tz-naive timestamp must not raise (offset-aware subtraction).
    naive = datetime.now(UTC).replace(tzinfo=None)
    assert _format_time_ago(naive) == "Just now"


# ===========================================================================
# sensor.py — device_info properties
# ===========================================================================


def _space_for_info(**kwargs) -> AjaxSpace:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    for key, value in kwargs.items():
        setattr(space, key, value)
    return space


def test_space_sensor_device_info_with_firmware_and_subtype() -> None:
    space = _space_for_info(
        hub_details={"firmware": {"version": "2.3"}, "hubSubtype": "hub_2"},
    )
    sensor = object.__new__(AjaxSpaceSensor)
    sensor._space_id = "s1"
    sensor.entity_description = AjaxSpaceSensorDescription(key="x", value_fn=lambda s: 0)
    sensor.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: space, entry_id="entry_test")

    info = sensor.device_info
    assert info["identifiers"] == {("ajax", "entry_test_s1")}
    assert info["sw_version"] == "2.3"
    assert info["model"] == "Hub 2"
    assert info["name"] == "Home"


def test_space_sensor_device_info_renames_bare_hub_and_defaults_model() -> None:
    space = AjaxSpace(id="s1", name="Hub", hub_id="hub1", security_state=SecurityState.DISARMED)
    space.hub_details = None
    sensor = object.__new__(AjaxSpaceSensor)
    sensor._space_id = "s1"
    sensor.entity_description = AjaxSpaceSensorDescription(key="x", value_fn=lambda s: 0)
    sensor.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: space, entry_id="entry_test")

    info = sensor.device_info
    assert info["name"] == "Ajax Hub"
    assert info["model"] == "Security Hub"
    assert info["sw_version"] is None


def test_space_sensor_device_info_none_when_space_missing() -> None:
    sensor = object.__new__(AjaxSpaceSensor)
    sensor._space_id = "s1"
    sensor.entity_description = AjaxSpaceSensorDescription(key="x", value_fn=lambda s: 0)
    sensor.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: None)
    assert sensor.device_info is None


def _device(online: bool = True, **extra) -> AjaxDevice:
    return AjaxDevice(
        id="d1",
        name="Sensor",
        type=DeviceType.MOTION_DETECTOR,
        space_id="s1",
        hub_id="hub1",
        online=online,
        **extra,
    )


def _device_sensor(device: AjaxDevice | None) -> AjaxDeviceSensor:
    sensor = object.__new__(AjaxDeviceSensor)
    sensor._space_id = "s1"
    sensor._device_id = "d1"
    sensor._sensor_key = "battery"
    sensor._sensor_desc = {"key": "battery"}
    space = SimpleNamespace(devices={"d1": device} if device else {})
    sensor.coordinator = SimpleNamespace(get_space=lambda sid: space, last_update_success=True, entry_id="entry_test")
    return sensor


def test_device_sensor_device_info() -> None:
    device = _device(
        raw_type="MotionProtect",
        firmware_version="1.2",
        hardware_version="HW3",
        room_name="Kitchen",
    )
    info = _device_sensor(device).device_info
    assert info["identifiers"] == {("ajax", "entry_test_d1")}
    assert info["model"] == "MotionProtect"
    assert info["via_device"] == ("ajax", "entry_test_s1")
    assert info["sw_version"] == "1.2"
    assert info["hw_version"] == "HW3"
    assert info["suggested_area"] == "Kitchen"


def test_device_sensor_device_info_none_when_missing() -> None:
    assert _device_sensor(None).device_info is None


def test_device_sensor_native_value_none_without_value_fn() -> None:
    sensor = _device_sensor(_device())
    sensor._sensor_desc = {"key": "battery"}
    assert sensor.native_value is None


def _video_edge(
    *,
    online: bool = True,
    ve_type: VideoEdgeType = VideoEdgeType.BULLET,
    color: str | None = None,
    **extra,
) -> AjaxVideoEdge:
    return AjaxVideoEdge(
        id="ve1",
        name="Cam",
        space_id="s1",
        video_edge_type=ve_type,
        color=color,
        connection_state="ONLINE" if online else "OFFLINE",
        **extra,
    )


def _ve_sensor(video_edge: AjaxVideoEdge | None, *, nvr_id: str | None = None) -> AjaxVideoEdgeSensor:
    sensor = object.__new__(AjaxVideoEdgeSensor)
    sensor._space_id = "s1"
    sensor._video_edge_id = "ve1"
    sensor._sensor_key = "storage"
    sensor._sensor_desc = {"key": "storage"}
    space = SimpleNamespace(
        video_edges={"ve1": video_edge} if video_edge else {},
        get_recording_nvr_id=lambda cam_id: nvr_id,
    )
    sensor.coordinator = SimpleNamespace(get_space=lambda sid: space, last_update_success=True, entry_id="entry_test")
    return sensor


def test_ve_sensor_device_info_standalone_links_to_hub() -> None:
    info = _ve_sensor(_video_edge(ve_type=VideoEdgeType.BULLET, color="white")).device_info
    assert info["identifiers"] == {("ajax", "entry_test_ve1")}
    # Human-readable model name plus colour suffix.
    assert info["model"] == "BulletCam (White)"
    assert info["via_device"] == ("ajax", "entry_test_s1")


def test_ve_sensor_device_info_links_to_nvr_when_recorded() -> None:
    info = _ve_sensor(_video_edge(), nvr_id="nvr99").device_info
    assert info["via_device"] == ("ajax", "entry_test_nvr99")


def test_ve_sensor_device_info_none_when_missing() -> None:
    assert _ve_sensor(None).device_info is None


def test_ve_sensor_native_value_none_without_value_fn() -> None:
    sensor = _ve_sensor(_video_edge())
    sensor._sensor_desc = {"key": "storage"}
    assert sensor.native_value is None


# ===========================================================================
# sensor.py — AjaxHubSensor (__init__ hub_id, native_value, device_info)
# ===========================================================================


def _hub_space(hub_id: str | None = "hub1", hub_details: dict | None = None) -> AjaxSpace:
    space = AjaxSpace(id="s1", name="Home", hub_id=hub_id, security_state=SecurityState.DISARMED)
    space.hub_details = hub_details if hub_details is not None else {}
    return space


def test_hub_sensor_init_uses_hub_id_for_unique_id() -> None:
    space = _hub_space(hub_id="hubABC")
    coord = MagicMock()
    coord.entry_id = "entry_test"
    coord.get_space.return_value = space
    sensor = AjaxHubSensor(
        coordinator=coord,
        space_id="s1",
        sensor_key="gsm_signal",
        sensor_desc={
            "key": "gsm_signal",
            "translation_key": "gsm_signal_level",
            "native_unit_of_measurement": "dBm",
            "state_class": "measurement",
            "enabled_by_default": False,
        },
    )
    assert sensor._attr_unique_id == "entry_test_hubABC_gsm_signal"
    assert sensor._attr_translation_key == "gsm_signal_level"
    assert sensor._attr_native_unit_of_measurement == "dBm"
    assert sensor._attr_entity_registry_enabled_default is False


def test_hub_sensor_init_falls_back_to_space_id_and_sensor_key() -> None:
    coord = MagicMock()
    coord.entry_id = "entry_test"
    coord.get_space.return_value = None
    sensor = AjaxHubSensor(
        coordinator=coord,
        space_id="s1",
        sensor_key="firmware",
        sensor_desc={"key": "firmware"},
    )
    # No space → hub_id falls back to space_id; no translation_key/device_class
    # → translation_key defaults to the sensor key.
    assert sensor._attr_unique_id == "entry_test_s1_firmware"
    assert sensor._attr_translation_key == "firmware"


def test_hub_sensor_device_info_links_to_space() -> None:
    space = _hub_space()
    sensor = object.__new__(AjaxHubSensor)
    sensor._space_id = "s1"
    sensor.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: space, entry_id="entry_test")
    assert sensor.device_info["identifiers"] == {("ajax", "entry_test_s1")}


def test_hub_sensor_device_info_none_when_space_missing() -> None:
    sensor = object.__new__(AjaxHubSensor)
    sensor._space_id = "s1"
    sensor.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: None)
    assert sensor.device_info is None


# ===========================================================================
# sensor.py — AjaxSmartLockSensor device_info
# ===========================================================================


def test_smart_lock_sensor_device_info() -> None:
    lock = AjaxSmartLock(id="lock1", name="Front Door", space_id="s1")
    sensor = object.__new__(AjaxSmartLockSensor)
    sensor._space_id = "s1"
    sensor._smart_lock_id = "lock1"
    space = SimpleNamespace(smart_locks={"lock1": lock})
    sensor.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: space, entry_id="entry_test")
    info = sensor.device_info
    assert info["identifiers"] == {("ajax", "entry_test_lock1")}
    assert info["model"] == "LockBridge Jeweller"
    assert info["via_device"] == ("ajax", "entry_test_s1")


def test_smart_lock_sensor_device_info_none_when_missing() -> None:
    sensor = object.__new__(AjaxSmartLockSensor)
    sensor._space_id = "s1"
    sensor._smart_lock_id = "lock1"
    sensor.coordinator = SimpleNamespace(
        last_update_success=True, get_space=lambda sid: SimpleNamespace(smart_locks={})
    )
    assert sensor.device_info is None


# ===========================================================================
# binary_sensor.py — AjaxBinarySensor device_info + registry update
# ===========================================================================


def _binary_sensor(device: AjaxDevice | None) -> AjaxBinarySensor:
    sensor = object.__new__(AjaxBinarySensor)
    sensor._space_id = "s1"
    sensor._device_id = "d1"
    sensor._sensor_key = "motion"
    sensor._sensor_desc = {"key": "motion"}
    space = SimpleNamespace(devices={"d1": device} if device else {})
    sensor.coordinator = SimpleNamespace(get_space=lambda sid: space, last_update_success=True, entry_id="entry_test")
    return sensor


def test_binary_sensor_device_info_with_color() -> None:
    device = _device(
        raw_type="DoorProtect Plus",
        device_color="WHITE",
        firmware_version="1.0",
        hardware_version="HW1",
        room_name="Garage",
    )
    info = _binary_sensor(device).device_info
    assert info["model"] == "DoorProtect Plus (White)"
    assert info["via_device"] == ("ajax", "entry_test_s1")
    assert info["suggested_area"] == "Garage"


def test_binary_sensor_device_info_default_model_from_type() -> None:
    # No raw_type → model derived from the DeviceType enum value.
    device = _device(raw_type=None)
    info = _binary_sensor(device).device_info
    assert info["model"] == "Motion Detector"


def test_binary_sensor_device_info_none_when_missing() -> None:
    assert _binary_sensor(None).device_info is None


def test_binary_sensor_update_device_registry_updates_entry() -> None:
    device = _device(raw_type="DoorProtect", device_color="BLACK", firmware_version="2.0", hardware_version="HW2")
    sensor = _binary_sensor(device)
    sensor.hass = MagicMock()

    device_entry = SimpleNamespace(id="entry-id")
    registry = MagicMock()
    registry.async_get_device.return_value = device_entry

    with patch("custom_components.ajax._binary_sensor_entities.dr.async_get", return_value=registry):
        sensor._update_device_registry()

    registry.async_update_device.assert_called_once()
    _, kwargs = registry.async_update_device.call_args
    assert kwargs["model"] == "DoorProtect (Black)"
    assert kwargs["sw_version"] == "2.0"
    assert kwargs["hw_version"] == "HW2"


def test_binary_sensor_update_device_registry_noop_without_device() -> None:
    sensor = _binary_sensor(None)
    sensor.hass = MagicMock()
    with patch("custom_components.ajax._binary_sensor_entities.dr.async_get") as get:
        sensor._update_device_registry()
        get.assert_not_called()


def test_binary_sensor_update_device_registry_noop_when_entry_missing() -> None:
    sensor = _binary_sensor(_device())
    sensor.hass = MagicMock()
    registry = MagicMock()
    registry.async_get_device.return_value = None
    with patch("custom_components.ajax._binary_sensor_entities.dr.async_get", return_value=registry):
        sensor._update_device_registry()
    registry.async_update_device.assert_not_called()


def test_binary_sensor_handle_coordinator_update_runs_registry_once() -> None:
    sensor = _binary_sensor(_device())
    sensor.async_write_ha_state = MagicMock()
    calls: list[int] = []
    sensor._update_device_registry = lambda: calls.append(1)  # type: ignore[method-assign]

    sensor._handle_coordinator_update()
    sensor._handle_coordinator_update()

    # Registry update fires only on the first coordinator push.
    assert calls == [1]
    assert sensor.async_write_ha_state.call_count == 2


# ===========================================================================
# binary_sensor.py — AjaxVideoEdgeBinarySensor
# ===========================================================================


def _ve_binary(video_edge: AjaxVideoEdge | None, *, nvr_id: str | None = None) -> AjaxVideoEdgeBinarySensor:
    sensor = object.__new__(AjaxVideoEdgeBinarySensor)
    sensor._space_id = "s1"
    sensor._video_edge_id = "ve1"
    sensor._sensor_key = "motion"
    sensor._sensor_desc = {"key": "motion"}
    space = SimpleNamespace(
        video_edges={"ve1": video_edge} if video_edge else {},
        get_recording_nvr_id=lambda cam_id: nvr_id,
    )
    sensor.coordinator = SimpleNamespace(get_space=lambda sid: space, last_update_success=True, entry_id="entry_test")
    return sensor


def test_ve_binary_is_on_value_fn_and_errors() -> None:
    on = _ve_binary(_video_edge())
    on._sensor_desc = {"key": "motion", "value_fn": lambda: True}
    assert on.is_on is True

    no_fn = _ve_binary(_video_edge())
    no_fn._sensor_desc = {"key": "motion"}
    assert no_fn.is_on is None

    missing = _ve_binary(None)
    missing._sensor_desc = {"key": "motion", "value_fn": lambda: True}
    assert missing.is_on is None

    def boom() -> bool:
        raise ValueError("x")

    err = _ve_binary(_video_edge())
    err._sensor_desc = {"key": "motion", "value_fn": boom}
    assert err.is_on is None


def test_ve_binary_available() -> None:
    assert _ve_binary(_video_edge(online=True)).available is True
    assert _ve_binary(_video_edge(online=False)).available is False
    assert _ve_binary(None).available is False


def test_ve_binary_extra_state_attributes() -> None:
    with_attrs = _ve_binary(_video_edge())
    with_attrs._sensor_desc = {"key": "motion", "extra_state_attributes": {"linked_nvr": "nvr1"}}
    assert with_attrs.extra_state_attributes == {"linked_nvr": "nvr1"}

    without = _ve_binary(_video_edge())
    without._sensor_desc = {"key": "motion"}
    assert without.extra_state_attributes is None


def test_ve_binary_device_info_links_to_nvr() -> None:
    info = _ve_binary(_video_edge(ve_type=VideoEdgeType.MINIDOME, color="black"), nvr_id="nvr1").device_info
    assert info["model"] == "MiniDome (Black)"
    assert info["via_device"] == ("ajax", "entry_test_nvr1")


def test_ve_binary_device_info_none_when_missing() -> None:
    assert _ve_binary(None).device_info is None


# ===========================================================================
# binary_sensor.py — AjaxHubBinarySensor
# ===========================================================================


def _hub_binary(sensor_key: str, hub_details: dict | None) -> AjaxHubBinarySensor:
    space = SimpleNamespace(hub_details=hub_details)
    sensor = object.__new__(AjaxHubBinarySensor)
    sensor._space_id = "s1"
    sensor._sensor_key = sensor_key
    sensor._sensor_config = AjaxHubBinarySensor.HUB_BINARY_SENSORS.get(sensor_key, {})
    sensor.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: space, entry_id="entry_test")
    return sensor


def test_hub_binary_init_wires_tamper_and_external_power() -> None:
    space = _hub_space(hub_id="hubXYZ")
    coord = MagicMock()
    coord.entry_id = "entry_test"
    coord.get_space.return_value = space

    tamper = AjaxHubBinarySensor(coordinator=coord, space_id="s1", sensor_key="tamper")
    assert tamper._attr_unique_id == "entry_test_hubXYZ_tamper"
    assert tamper._attr_device_class is BinarySensorDeviceClass.TAMPER

    power = AjaxHubBinarySensor(coordinator=coord, space_id="s1", sensor_key="external_power")
    assert power._attr_device_class is BinarySensorDeviceClass.PLUG
    assert power._attr_translation_key == "external_power"


def test_hub_binary_init_falls_back_to_space_id() -> None:
    coord = MagicMock()
    coord.entry_id = "entry_test"
    coord.get_space.return_value = None
    sensor = AjaxHubBinarySensor(coordinator=coord, space_id="s1", sensor_key="tamper")
    assert sensor._attr_unique_id == "entry_test_s1_tamper"


def test_hub_binary_is_on_value_key() -> None:
    on = _hub_binary("tamper", {"tampered": True})
    assert on.is_on is True
    off = _hub_binary("tamper", {"tampered": False})
    assert off.is_on is False
    # value_key absent (but hub_details non-empty) → default False.
    default = _hub_binary("tamper", {"other": 1})
    assert default.is_on is False


def test_hub_binary_is_on_value_fn() -> None:
    on = _hub_binary("external_power", {"externallyPowered": True})
    assert on.is_on is True


def test_hub_binary_is_on_value_fn_swallows_error() -> None:
    sensor = _hub_binary("external_power", {"externallyPowered": True})
    sensor._sensor_config = {"value_fn": lambda hd: hd["missing_key"]}
    assert sensor.is_on is None


def test_hub_binary_is_on_none_without_config() -> None:
    sensor = _hub_binary("unknown", {"a": 1})
    sensor._sensor_config = {}
    assert sensor.is_on is None


def test_hub_binary_is_on_none_without_hub_details() -> None:
    assert _hub_binary("tamper", None).is_on is None


def test_hub_binary_available_and_device_info() -> None:
    present = _hub_binary("tamper", {"tampered": False})
    assert present.available is True
    assert present.device_info["identifiers"] == {("ajax", "entry_test_s1")}

    absent = _hub_binary("tamper", None)
    assert absent.available is False


def test_hub_binary_device_info_none_when_space_missing() -> None:
    sensor = object.__new__(AjaxHubBinarySensor)
    sensor._space_id = "s1"
    sensor.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: None)
    assert sensor.device_info is None


# ===========================================================================
# binary_sensor.py — AjaxSmartLockBinarySensor
# ===========================================================================


def _sl_binary(lock: object | None) -> AjaxSmartLockBinarySensor:
    sensor = object.__new__(AjaxSmartLockBinarySensor)
    sensor._space_id = "s1"
    sensor._smart_lock_id = "lock1"
    space = SimpleNamespace(smart_locks={"lock1": lock} if lock else {})
    sensor.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: space, entry_id="entry_test")
    return sensor


def test_smart_lock_binary_is_on_reports_door_state() -> None:
    open_lock = AjaxSmartLock(id="lock1", name="Door", space_id="s1", is_door_open=True)
    assert _sl_binary(open_lock).is_on is True
    closed = AjaxSmartLock(id="lock1", name="Door", space_id="s1", is_door_open=False)
    assert _sl_binary(closed).is_on is False
    assert _sl_binary(None).is_on is None


def test_smart_lock_binary_available() -> None:
    lock = AjaxSmartLock(id="lock1", name="Door", space_id="s1")
    assert _sl_binary(lock).available is True
    assert _sl_binary(None).available is False


def test_smart_lock_binary_device_info() -> None:
    lock = AjaxSmartLock(id="lock1", name="Front", space_id="s1")
    info = _sl_binary(lock).device_info
    assert info["identifiers"] == {("ajax", "entry_test_lock1")}
    assert info["model"] == "LockBridge Jeweller"
    assert info["via_device"] == ("ajax", "entry_test_s1")


def test_smart_lock_binary_device_info_none_when_missing() -> None:
    assert _sl_binary(None).device_info is None


# ===========================================================================
# async_setup_entry — discovery wiring for both platforms
# ===========================================================================


def _full_space() -> AjaxSpace:
    """A space populated with one device, one camera, one smart lock and a hub."""
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    space.devices["d1"] = AjaxDevice(
        id="d1",
        name="Motion",
        type=DeviceType.MOTION_DETECTOR,
        space_id="s1",
        hub_id="hub1",
    )
    space.video_edges["ve1"] = AjaxVideoEdge(
        id="ve1",
        name="Cam",
        space_id="s1",
        video_edge_type=VideoEdgeType.BULLET,
        connection_state="ONLINE",
    )
    space.smart_locks["lock1"] = AjaxSmartLock(id="lock1", name="Lock", space_id="s1")
    space.hub_details = {"battery": {"chargeLevelPercentage": 90}, "firmware": {"version": "1.0"}}
    return space


def _coordinator_with_space(space: AjaxSpace) -> MagicMock:
    coordinator = MagicMock()
    coordinator.account.spaces = {"s1": space}
    coordinator.get_space.side_effect = lambda sid: space if sid == "s1" else None
    return coordinator


async def test_sensor_async_setup_entry_creates_entities() -> None:
    space = _full_space()
    coordinator = _coordinator_with_space(space)
    entry = MagicMock()
    entry.runtime_data = coordinator
    added: list = []

    def _add(entities, *args, **kwargs):
        added.extend(entities)

    with patch("custom_components.ajax.sensor.connect_new_entity_signal") as connect:
        await sensor_async_setup_entry(MagicMock(), entry, _add)

    # Space, device, video-edge, hub and smart-lock sensors all created.
    assert added, "expected at least one sensor entity"
    class_names = {type(e).__name__ for e in added}
    assert "AjaxSpaceSensor" in class_names
    assert "AjaxSmartLockSensor" in class_names
    # Discovery signals are connected once per signal type.
    assert connect.call_count == 4


async def test_sensor_async_setup_entry_no_account_warns_and_returns() -> None:
    coordinator = MagicMock()
    coordinator.account = None
    entry = MagicMock()
    entry.runtime_data = coordinator
    add = MagicMock()
    await sensor_async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


async def test_binary_async_setup_entry_creates_entities() -> None:
    space = _full_space()
    coordinator = _coordinator_with_space(space)
    entry = MagicMock()
    entry.runtime_data = coordinator
    added: list = []

    def _add(entities, *args, **kwargs):
        added.extend(entities)

    with patch("custom_components.ajax.binary_sensor.connect_new_entity_signal") as connect:
        await binary_async_setup_entry(MagicMock(), entry, _add)

    class_names = {type(e).__name__ for e in added}
    assert "AjaxSmartLockBinarySensor" in class_names
    assert "AjaxHubBinarySensor" in class_names
    assert connect.call_count == 4


async def test_binary_async_setup_entry_no_account_returns() -> None:
    coordinator = MagicMock()
    coordinator.account = None
    entry = MagicMock()
    entry.runtime_data = coordinator
    add = MagicMock()
    await binary_async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


# ===========================================================================
# __init__ wiring not exercised by the object.__new__ helpers
# ===========================================================================


def test_device_sensor_init_options_enabled_and_entity_category() -> None:
    coord = MagicMock()
    sensor = AjaxDeviceSensor(
        coordinator=coord,
        space_id="s1",
        device_id="d1",
        sensor_key="state",
        sensor_desc={
            "key": "state",
            "options": ["open", "closed"],
            "enabled_by_default": False,
            "entity_category": "diagnostic",
        },
    )
    assert sensor._attr_options == ["open", "closed"]
    assert sensor._attr_entity_registry_enabled_default is False
    # "diagnostic" string is resolved to the HA enum.
    assert sensor._attr_entity_category is not None


def test_device_sensor_init_device_class_without_translation_key() -> None:
    from homeassistant.components.sensor import SensorDeviceClass

    coord = MagicMock()
    sensor = AjaxDeviceSensor(
        coordinator=coord,
        space_id="s1",
        device_id="d1",
        sensor_key="temperature",
        sensor_desc={"key": "temperature", "device_class": SensorDeviceClass.TEMPERATURE},
    )
    # device_class present, no translation_key → HA auto-naming, no fallback set.
    assert not hasattr(sensor, "_attr_translation_key") or sensor._attr_translation_key is None


def test_ve_sensor_init_full_wiring() -> None:
    from homeassistant.components.sensor import SensorDeviceClass

    coord = MagicMock()
    coord.entry_id = "entry_test"
    sensor = AjaxVideoEdgeSensor(
        coordinator=coord,
        space_id="s1",
        video_edge_id="ve1",
        sensor_key="storage",
        sensor_desc={
            "key": "storage",
            "translation_key": "storage_state",
            "entity_category": "diagnostic",
            "enabled_by_default": False,
            "device_class": SensorDeviceClass.ENUM,
            "options": ["ok", "full"],
            "native_unit_of_measurement": "GB",
        },
    )
    assert sensor._attr_unique_id == "entry_test_ve1_storage"
    assert sensor._attr_translation_key == "storage_state"
    assert sensor._attr_entity_registry_enabled_default is False
    assert sensor._attr_device_class is SensorDeviceClass.ENUM
    assert sensor._attr_options == ["ok", "full"]
    assert sensor._attr_native_unit_of_measurement == "GB"


def test_ve_binary_init_full_wiring() -> None:
    coord = MagicMock()
    coord.entry_id = "entry_test"
    sensor = AjaxVideoEdgeBinarySensor(
        coordinator=coord,
        space_id="s1",
        video_edge_id="ve1",
        sensor_key="motion",
        sensor_desc={
            "key": "motion",
            "translation_key": "video_motion",
            "device_class": BinarySensorDeviceClass.MOTION,
            "enabled_by_default": False,
        },
    )
    assert sensor._attr_unique_id == "entry_test_ve1_motion"
    assert sensor._attr_translation_key == "video_motion"
    assert sensor._attr_device_class is BinarySensorDeviceClass.MOTION
    assert sensor._attr_entity_registry_enabled_default is False


async def test_binary_sensor_async_added_to_hass_updates_registry() -> None:
    sensor = _binary_sensor(_device())
    sensor.hass = MagicMock()
    called: list[int] = []
    sensor._update_device_registry = lambda: called.append(1)  # type: ignore[method-assign]

    with patch(
        "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
        new=AsyncMock(),
    ):
        await sensor.async_added_to_hass()

    assert called == [1]


# ===========================================================================
# async_setup_entry — dynamic discovery builder closures
# ===========================================================================


def _capture_builders(connect_mock: MagicMock) -> dict[str, object]:
    """Map each builder callable by its ``label`` kwarg."""
    builders: dict[str, object] = {}
    for call in connect_mock.call_args_list:
        builder = call.args[5]
        label = call.kwargs["label"]
        builders[label] = builder
    return builders


async def test_sensor_discovery_builders() -> None:
    space = _full_space()
    coordinator = _coordinator_with_space(space)
    entry = MagicMock()
    entry.runtime_data = coordinator

    with patch("custom_components.ajax.sensor.connect_new_entity_signal") as connect:
        await sensor_async_setup_entry(MagicMock(), entry, lambda *a, **k: None)

    builders = _capture_builders(connect)

    device_pairs = builders["sensor(s)"]("s1", "d1")
    assert device_pairs and all(uid.startswith("d1_") for uid, _ in device_pairs)
    # Unknown device id → empty list.
    assert builders["sensor(s)"]("s1", "missing") == []

    ve_pairs = builders["Video Edge sensor(s)"]("s1", "ve1")
    assert ve_pairs and all(uid.startswith("ve1_") for uid, _ in ve_pairs)
    assert builders["Video Edge sensor(s)"]("s1", "missing") == []

    lock_pairs = builders["smart lock sensor(s)"]("s1", "lock1")
    assert lock_pairs[0][0] == "lock1_last_changed_by"


async def test_binary_discovery_builders() -> None:
    space = _full_space()
    coordinator = _coordinator_with_space(space)
    entry = MagicMock()
    entry.runtime_data = coordinator

    with patch("custom_components.ajax.binary_sensor.connect_new_entity_signal") as connect:
        await binary_async_setup_entry(MagicMock(), entry, lambda *a, **k: None)

    builders = _capture_builders(connect)

    device_pairs = builders["binary sensor(s)"]("s1", "d1")
    assert all(uid.startswith("d1_") for uid, _ in device_pairs)
    assert builders["binary sensor(s)"]("s1", "missing") == []

    ve_pairs = builders["Video Edge binary sensor(s)"]("s1", "ve1")
    assert all(isinstance(uid, str) for uid, _ in ve_pairs)
    assert builders["Video Edge binary sensor(s)"]("s1", "missing") == []

    lock_pairs = builders["smart lock door sensor(s)"]("s1", "lock1")
    assert lock_pairs[0][0] == "lock1_door"


# ===========================================================================
# _get_* helpers — "no space" branch returns None
# ===========================================================================


def _no_space_coordinator() -> SimpleNamespace:
    return SimpleNamespace(get_space=lambda sid: None, last_update_success=True)


def test_device_sensor_get_device_none_when_no_space() -> None:
    sensor = object.__new__(AjaxDeviceSensor)
    sensor._space_id = "s1"
    sensor._device_id = "d1"
    sensor._sensor_desc = {"key": "k"}
    sensor.coordinator = _no_space_coordinator()
    assert sensor._get_device() is None
    assert sensor.available is False


def test_ve_sensor_get_video_edge_none_when_no_space() -> None:
    sensor = object.__new__(AjaxVideoEdgeSensor)
    sensor._space_id = "s1"
    sensor._video_edge_id = "ve1"
    sensor.coordinator = _no_space_coordinator()
    assert sensor._get_video_edge() is None
    assert sensor._get_recording_nvr_id() is None


def test_binary_sensor_get_device_none_when_no_space() -> None:
    sensor = object.__new__(AjaxBinarySensor)
    sensor._space_id = "s1"
    sensor._device_id = "d1"
    sensor.coordinator = _no_space_coordinator()
    assert sensor._get_device() is None


def test_ve_binary_get_helpers_none_when_no_space() -> None:
    sensor = object.__new__(AjaxVideoEdgeBinarySensor)
    sensor._space_id = "s1"
    sensor._video_edge_id = "ve1"
    sensor.coordinator = _no_space_coordinator()
    assert sensor._get_video_edge() is None
    assert sensor._get_recording_nvr_id() is None


def test_smart_lock_sensor_get_none_when_no_space() -> None:
    sensor = object.__new__(AjaxSmartLockSensor)
    sensor._space_id = "s1"
    sensor._smart_lock_id = "lock1"
    sensor.coordinator = _no_space_coordinator()
    assert sensor._get_smart_lock() is None


def test_smart_lock_binary_get_none_when_no_space() -> None:
    sensor = object.__new__(AjaxSmartLockBinarySensor)
    sensor._space_id = "s1"
    sensor._smart_lock_id = "lock1"
    sensor.coordinator = _no_space_coordinator()
    assert sensor._get_smart_lock() is None
