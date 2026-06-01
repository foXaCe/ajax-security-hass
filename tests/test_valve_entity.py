"""Tests for AjaxValve (WaterStop) entity.

The valve is descriptor-driven like binary_sensor/switch but adds the
`is_closed` mirror property (HA convention: both is_open and is_closed
must be tri-state — True/False/None — so unknown states stay unknown
on both sides).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.ajax.models import AjaxDevice, DeviceType
from custom_components.ajax.valve import AjaxValve


def _device(online: bool = True) -> AjaxDevice:
    return AjaxDevice(
        id="d1",
        name="Water Stop",
        type=DeviceType.WATERSTOP,
        space_id="s1",
        hub_id="hub1",
        online=online,
    )


def _make_valve(valve_desc: dict, *, device: AjaxDevice | None = None) -> AjaxValve:
    valve = object.__new__(AjaxValve)
    valve._space_id = "s1"
    valve._device_id = "d1"
    valve._valve_key = valve_desc["key"]
    valve._valve_desc = valve_desc
    space = SimpleNamespace(devices={"d1": device} if device else {})
    valve.coordinator = SimpleNamespace(get_space=lambda sid: space)
    return valve


def test_is_open_returns_value_fn_result() -> None:
    assert _make_valve({"key": "valve", "value_fn": lambda: True}, device=_device()).is_open is True


def test_is_open_returns_none_when_device_missing() -> None:
    assert _make_valve({"key": "valve", "value_fn": lambda: True}, device=None).is_open is None


def test_is_open_returns_none_when_value_fn_raises() -> None:
    def boom() -> bool:
        raise RuntimeError("crash")

    assert _make_valve({"key": "valve", "value_fn": boom}, device=_device()).is_open is None


def test_is_closed_mirrors_is_open_when_known() -> None:
    valve_open = _make_valve({"key": "valve", "value_fn": lambda: True}, device=_device())
    assert valve_open.is_closed is False

    valve_closed = _make_valve({"key": "valve", "value_fn": lambda: False}, device=_device())
    assert valve_closed.is_closed is True


def test_is_closed_returns_none_when_is_open_is_unknown() -> None:
    """HA convention — both tri-states must stay None together."""
    valve_unknown = _make_valve({"key": "valve", "value_fn": lambda: None}, device=_device())
    assert valve_unknown.is_closed is None
    assert valve_unknown.is_open is None


def test_available_tracks_device_online_flag() -> None:
    assert _make_valve({"key": "valve", "value_fn": lambda: True}, device=_device(online=True)).available is True
    assert _make_valve({"key": "valve", "value_fn": lambda: True}, device=_device(online=False)).available is False


def test_available_false_when_device_missing() -> None:
    assert _make_valve({"key": "valve", "value_fn": lambda: True}, device=None).available is False


def test_init_wires_translation_key_and_default_enabled() -> None:
    coord = MagicMock()
    coord.entry_id = "entry_test"
    valve = AjaxValve(
        coordinator=coord,
        space_id="s1",
        device_id="d1",
        valve_key="valve",
        valve_desc={
            "key": "valve",
            "value_fn": lambda: True,
            "translation_key": "water_valve",
            "enabled_by_default": False,
        },
    )
    assert valve._attr_unique_id == "entry_test_d1_valve"
    assert valve._attr_translation_key == "water_valve"
    assert valve._attr_entity_registry_enabled_default is False
