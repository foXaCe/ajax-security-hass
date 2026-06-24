"""Tests for AjaxSwitch entity descriptor wiring.

AjaxSwitch wraps a switch_desc dict (key, value_fn, turn_on_fn,
turn_off_fn, translation_key, entity_category, icon) and exposes is_on /
available / turn_on / turn_off. Bugs at this layer fail closed (entity
stays unknown or unavailable).

We bypass CoordinatorEntity.__init__ to avoid a full HA fixture for what
is essentially descriptor wiring.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from homeassistant.helpers.entity import EntityCategory

from custom_components.ajax.models import AjaxDevice, DeviceType
from custom_components.ajax.switch import AjaxSwitch


def _device(online: bool = True) -> AjaxDevice:
    return AjaxDevice(
        id="d1",
        name="Relay Office",
        type=DeviceType.RELAY,
        space_id="s1",
        hub_id="hub1",
        online=online,
    )


def _make_switch(switch_desc: dict, *, device: AjaxDevice | None = None) -> AjaxSwitch:
    sw = object.__new__(AjaxSwitch)
    sw._space_id = "s1"
    sw._device_id = "d1"
    sw._switch_key = switch_desc["key"]
    sw._switch_desc = switch_desc
    space = SimpleNamespace(devices={"d1": device} if device else {})
    sw.coordinator = SimpleNamespace(get_space=lambda sid: space, last_update_success=True)
    return sw


# ---------------------------------------------------------------------------
# is_on
# ---------------------------------------------------------------------------


def test_is_on_returns_value_fn_result() -> None:
    sw = _make_switch({"key": "chimes", "value_fn": lambda: True}, device=_device())
    assert sw.is_on is True


def test_is_on_returns_none_when_device_missing() -> None:
    sw = _make_switch({"key": "chimes", "value_fn": lambda: True}, device=None)
    assert sw.is_on is None


def test_is_on_returns_none_when_value_fn_raises() -> None:
    """Buggy descriptor must NOT crash the platform — unknown is acceptable, ValueError is not."""

    def boom() -> bool:
        raise KeyError("attribute missing")

    sw = _make_switch({"key": "chimes", "value_fn": boom}, device=_device())
    assert sw.is_on is None


def test_is_on_returns_none_when_descriptor_has_no_value_fn() -> None:
    sw = _make_switch({"key": "chimes"}, device=_device())
    assert sw.is_on is None


# ---------------------------------------------------------------------------
# available
# ---------------------------------------------------------------------------


def test_available_true_for_online_device() -> None:
    assert _make_switch({"key": "x", "value_fn": lambda: True}, device=_device(online=True)).available is True


def test_available_false_for_offline_device() -> None:
    assert _make_switch({"key": "x", "value_fn": lambda: True}, device=_device(online=False)).available is False


def test_available_false_when_device_missing() -> None:
    """A removed device must surface as unavailable, not crash."""
    assert _make_switch({"key": "x", "value_fn": lambda: True}, device=None).available is False


def test_available_false_when_coordinator_update_failed() -> None:
    """A failed coordinator poll must surface even an online device as unavailable."""
    sw = _make_switch({"key": "x", "value_fn": lambda: True}, device=_device(online=True))
    sw.coordinator.last_update_success = False
    assert sw.available is False


def test_get_device_none_when_space_missing() -> None:
    """No space on the coordinator (e.g. hub removed) → _get_device returns None."""
    sw = object.__new__(AjaxSwitch)
    sw._space_id = "s1"
    sw._device_id = "d1"
    sw.coordinator = SimpleNamespace(get_space=lambda sid: None)
    assert sw._get_device() is None


# ---------------------------------------------------------------------------
# Descriptor wiring via __init__
# ---------------------------------------------------------------------------


def test_init_defaults_entity_category_to_config() -> None:
    """Switches are user-configurable — default to CONFIG, not DIAGNOSTIC."""
    coord = MagicMock()
    sw = AjaxSwitch(
        coordinator=coord,
        space_id="s1",
        device_id="d1",
        switch_key="chimes",
        switch_desc={"key": "chimes", "value_fn": lambda: True},
    )
    assert sw._attr_entity_category is EntityCategory.CONFIG


def test_init_accepts_diagnostic_string_for_entity_category() -> None:
    """Legacy descriptors used `entity_category="diagnostic"` as a string."""
    coord = MagicMock()
    sw = AjaxSwitch(
        coordinator=coord,
        space_id="s1",
        device_id="d1",
        switch_key="x",
        switch_desc={"key": "x", "value_fn": lambda: True, "entity_category": "diagnostic"},
    )
    assert sw._attr_entity_category is EntityCategory.DIAGNOSTIC


def test_init_custom_name_overrides_translation_key() -> None:
    """A descriptor with `name="ch1"` (multi-gang channel) suppresses the translation key."""
    coord = MagicMock()
    sw = AjaxSwitch(
        coordinator=coord,
        space_id="s1",
        device_id="d1",
        switch_key="channel",
        switch_desc={"key": "channel", "value_fn": lambda: True, "name": "Channel 1"},
    )
    assert sw._attr_name == "Channel 1"
    assert sw._attr_translation_key is None


def test_init_falls_back_to_switch_key_for_translation_key() -> None:
    coord = MagicMock()
    sw = AjaxSwitch(
        coordinator=coord,
        space_id="s1",
        device_id="d1",
        switch_key="chimes",
        switch_desc={"key": "chimes", "value_fn": lambda: True},
    )
    assert sw._attr_translation_key == "chimes"


def test_init_wires_icon_and_enabled_by_default() -> None:
    coord = MagicMock()
    sw = AjaxSwitch(
        coordinator=coord,
        space_id="s1",
        device_id="d1",
        switch_key="chimes",
        switch_desc={
            "key": "chimes",
            "value_fn": lambda: True,
            "icon": "mdi:bell-ring",
            "enabled_by_default": False,
        },
    )
    assert sw._attr_icon == "mdi:bell-ring"
    assert sw._attr_entity_registry_enabled_default is False
