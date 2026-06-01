"""Tests for the AjaxBinarySensor entity wrapper.

The class is thin: it wraps a descriptor dict (key, value_fn, device_class,
translation_key, enabled_by_default) and exposes the result via the
`is_on` / `available` / `device_info` properties HA reads. Bugs surface
silently — a missing key returns None instead of the actual sensor value
(entity stays "unknown" forever).

We bypass CoordinatorEntity.__init__ to avoid spinning up a full HA
instance for what's essentially descriptor wiring.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.helpers.entity import EntityCategory

from custom_components.ajax.binary_sensor import AjaxBinarySensor
from custom_components.ajax.models import AjaxDevice, DeviceType


def _device(online: bool = True, **extra) -> AjaxDevice:
    return AjaxDevice(
        id="d1",
        name="Motion Living",
        type=DeviceType.MOTION_DETECTOR,
        space_id="s1",
        hub_id="hub1",
        online=online,
        **extra,
    )


def _make_sensor(
    sensor_desc: dict,
    *,
    device: AjaxDevice | None = None,
    update_success: bool = True,
) -> AjaxBinarySensor:
    """Build a sensor wired to a fake coordinator/space/device chain."""
    sensor = object.__new__(AjaxBinarySensor)
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


# ---------------------------------------------------------------------------
# is_on
# ---------------------------------------------------------------------------


def test_is_on_returns_value_fn_result() -> None:
    desc = {"key": "motion", "value_fn": lambda: True}
    assert _make_sensor(desc, device=_device()).is_on is True


def test_is_on_returns_none_when_device_missing() -> None:
    """Device removed from Ajax → sensor goes to unknown, not False."""
    desc = {"key": "motion", "value_fn": lambda: True}
    assert _make_sensor(desc, device=None).is_on is None


def test_is_on_returns_none_when_value_fn_raises() -> None:
    """A buggy descriptor must NOT crash the entire entity platform."""

    def boom() -> bool:
        raise RuntimeError("attribute missing")

    desc = {"key": "motion", "value_fn": boom}
    assert _make_sensor(desc, device=_device()).is_on is None


def test_is_on_returns_none_when_no_value_fn() -> None:
    """Descriptor without value_fn (rare but possible) — surface unknown, not False."""
    desc = {"key": "motion"}
    assert _make_sensor(desc, device=_device()).is_on is None


# ---------------------------------------------------------------------------
# available
# ---------------------------------------------------------------------------


def test_available_true_when_device_online_and_coordinator_success() -> None:
    sensor = _make_sensor({"key": "motion", "value_fn": lambda: True}, device=_device(online=True))
    assert sensor.available is True


def test_available_false_when_device_offline() -> None:
    sensor = _make_sensor({"key": "motion", "value_fn": lambda: True}, device=_device(online=False))
    assert sensor.available is False


def test_available_false_when_coordinator_last_update_failed() -> None:
    """Coordinator failed — propagate as unavailable so automations don't latch a stale state."""
    sensor = _make_sensor(
        {"key": "motion", "value_fn": lambda: True}, device=_device(online=True), update_success=False
    )
    assert sensor.available is False


def test_available_false_when_device_removed() -> None:
    sensor = _make_sensor({"key": "motion", "value_fn": lambda: True}, device=None)
    assert sensor.available is False


# ---------------------------------------------------------------------------
# __init__: descriptor wiring
# ---------------------------------------------------------------------------


def test_init_wires_device_class_and_translation_key() -> None:
    coord = MagicMock()
    coord.last_update_success = True
    coord.entry_id = "entry_test"
    sensor = AjaxBinarySensor(
        coordinator=coord,
        space_id="s1",
        device_id="d1",
        sensor_key="door",
        sensor_desc={
            "key": "door",
            "device_class": BinarySensorDeviceClass.OPENING,
            "translation_key": "door_state",
            "enabled_by_default": False,
            "entity_category": "diagnostic",
        },
    )
    assert sensor._attr_unique_id == "entry_test_d1_door"
    assert sensor._attr_device_class is BinarySensorDeviceClass.OPENING
    assert sensor._attr_translation_key == "door_state"
    assert sensor._attr_entity_registry_enabled_default is False
    assert sensor._attr_entity_category is EntityCategory.DIAGNOSTIC


def test_init_falls_back_to_sensor_key_when_no_device_class() -> None:
    """Without device_class, HA needs a translation_key — default to the sensor key."""
    coord = MagicMock()
    coord.last_update_success = True
    coord.entry_id = "entry_test"
    sensor = AjaxBinarySensor(
        coordinator=coord,
        space_id="s1",
        device_id="d1",
        sensor_key="bypass",
        sensor_desc={"key": "bypass", "value_fn": lambda: False},
    )
    assert sensor._attr_translation_key == "bypass"


def test_init_keeps_explicit_name_when_provided() -> None:
    """A descriptor with `name=None` deliberately suppresses the entity name."""
    coord = MagicMock()
    coord.last_update_success = True
    coord.entry_id = "entry_test"
    sensor = AjaxBinarySensor(
        coordinator=coord,
        space_id="s1",
        device_id="d1",
        sensor_key="door",
        sensor_desc={"key": "door", "device_class": BinarySensorDeviceClass.OPENING, "name": None},
    )
    assert sensor._attr_name is None
