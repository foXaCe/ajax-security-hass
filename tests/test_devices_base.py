"""Unit tests for the device handler base helpers.

These descriptors are now consumed by ~15 handlers. A regression on a
helper would silently change the shape of dozens of entities, so the
expected dict structure is pinned here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.helpers.entity import EntityCategory

from custom_components.ajax.devices import base


class _StubHandler(base.AjaxDeviceHandler):
    """Concrete subclass so we can instantiate the abstract base."""

    def get_binary_sensors(self) -> list[dict]:
        return []

    def get_sensors(self) -> list[dict]:
        return []


def _make_handler(**device_overrides) -> _StubHandler:
    """Build a handler whose ``self.device`` is a SimpleNamespace stub.

    ``device_overrides`` wins over the defaults so individual tests can
    set ``attributes={}``, ``room_name=None``, etc.
    """
    fields: dict = {
        "battery_level": 80,
        "signal_strength": 75,
        "firmware_version": "2.0.1",
        "room_name": "Living room",
        "attributes": {"tampered": False, "temperature": 21.5},
        "malfunctions": 0,
    }
    fields.update(device_overrides)
    return _StubHandler(SimpleNamespace(**fields))


# ---------------------------------------------------------------------------
# resolve_entity_category
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("diagnostic", EntityCategory.DIAGNOSTIC),
        ("DIAGNOSTIC", EntityCategory.DIAGNOSTIC),  # case insensitive
        ("config", EntityCategory.CONFIG),
        (EntityCategory.DIAGNOSTIC, EntityCategory.DIAGNOSTIC),
        (None, None),
        ("not-a-category", None),
        (42, None),
    ],
)
def test_resolve_entity_category(raw, expected) -> None:
    """All legacy string forms must coerce, garbage must yield None."""
    assert base.resolve_entity_category(raw) is expected


# ---------------------------------------------------------------------------
# Common entity descriptors
# ---------------------------------------------------------------------------


def test_battery_sensor_descriptor_shape() -> None:
    desc = _make_handler()._battery_sensor()
    assert desc["key"] == "battery"
    assert desc["device_class"] is SensorDeviceClass.BATTERY
    assert desc["native_unit_of_measurement"] == PERCENTAGE
    assert desc["state_class"] is SensorStateClass.MEASUREMENT
    assert desc["value_fn"]() == 80
    assert desc["enabled_by_default"] is True


def test_battery_sensor_can_be_disabled_by_default() -> None:
    desc = _make_handler()._battery_sensor(enabled_by_default=False)
    assert desc["enabled_by_default"] is False


def test_tamper_binary_sensor_reads_attributes_with_default_false() -> None:
    handler = _make_handler(attributes={})  # attribute may not be present yet
    desc = handler._tamper_binary_sensor()
    assert desc["key"] == "tamper"
    assert desc["device_class"] is BinarySensorDeviceClass.TAMPER
    assert desc["value_fn"]() is False  # absent → default False, NOT crash


def test_tamper_binary_sensor_returns_real_value() -> None:
    handler = _make_handler(attributes={"tampered": True})
    assert handler._tamper_binary_sensor()["value_fn"]() is True


def test_temperature_sensor_defaults_to_attributes_temperature() -> None:
    desc = _make_handler()._temperature_sensor()
    assert desc["key"] == "temperature"
    assert desc["device_class"] is SensorDeviceClass.TEMPERATURE
    assert desc["native_unit_of_measurement"] == UnitOfTemperature.CELSIUS
    assert desc["value_fn"]() == 21.5


def test_temperature_sensor_accepts_custom_attr() -> None:
    """LifeQuality and friends store the temperature under custom keys."""
    handler = _make_handler(attributes={"ambient_temperature": 19.0})
    desc = handler._temperature_sensor(attr="ambient_temperature")
    assert desc["value_fn"]() == 19.0


def test_signal_strength_percent_sensor() -> None:
    desc = _make_handler()._signal_strength_percent_sensor()
    assert desc["key"] == "signal_strength"
    assert desc["translation_key"] == "signal_strength"
    assert desc["native_unit_of_measurement"] == PERCENTAGE
    assert desc["value_fn"]() == 75


def test_firmware_version_sensor_is_diagnostic_and_disabled_by_default() -> None:
    """Firmware is debug info — entity_category must be diagnostic and
    enabled_by_default False so it does not clutter dashboards."""
    desc = _make_handler()._firmware_version_sensor()
    assert desc["key"] == "firmware_version"
    assert desc["translation_key"] == "firmware_version"
    assert desc["entity_category"] == "diagnostic"
    assert desc["enabled_by_default"] is False
    assert desc["value_fn"]() == "2.0.1"


def test_problem_binary_sensor_reflects_malfunctions_count() -> None:
    handler = _make_handler(malfunctions=0)
    desc = handler._problem_binary_sensor()
    assert desc["device_class"] is BinarySensorDeviceClass.PROBLEM
    assert desc["value_fn"]() is False

    handler.device.malfunctions = 3
    assert handler._problem_binary_sensor()["value_fn"]() is True


def test_get_common_sensors_includes_room_when_known() -> None:
    handler = _make_handler(room_name="Kitchen")
    common = handler.get_common_sensors()
    assert len(common) == 1
    room_desc = common[0]
    assert room_desc["key"] == "room"
    assert room_desc["translation_key"] == "room"
    assert room_desc["value_fn"]() == "Kitchen"


def test_get_common_sensors_omits_room_when_unknown() -> None:
    """An unrouted device must not get a dangling 'room: None' sensor."""
    handler = _make_handler(room_name=None)
    assert handler.get_common_sensors() == []
