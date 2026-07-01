"""Coverage tests for misc Ajax platforms.

Targets the lower-traffic platforms that the existing suite only partially
exercises:

* ``logbook.py`` — the describe callbacks turn raw HA events into the
  localised one-liners shown in the logbook. They were untested (0%), so a
  typo in a translation table or a missing ``space_name`` fallback would
  silently ship.
* ``alarm_control_panel.py`` — the arm/disarm command methods do an
  optimistic state flip then roll it back on API failure; the rollback is
  the part that protects automations from a stuck wrong state.
* ``valve.py`` — the WaterStop open/close command + optimistic-guard
  rollback.
* ``event.py`` / ``lock.py`` / ``device_tracker.py`` — the ``device_info``
  builders that decide which Ajax device the entity attaches to.

Everything is built with ``object.__new__`` + lightweight mocks, per the
project convention — no full HA harness.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.ajax import logbook
from custom_components.ajax.alarm_control_panel import (
    AjaxAlarmControlPanel,
    AjaxGroupAlarmControlPanel,
)
from custom_components.ajax.device_tracker import AjaxHubTracker
from custom_components.ajax.event import AjaxEventEntity
from custom_components.ajax.lock import AjaxLock
from custom_components.ajax.models import (
    AjaxDevice,
    AjaxSmartLock,
    AjaxVideoEdge,
    DeviceType,
    GroupState,
    SecurityState,
    VideoEdgeType,
)
from custom_components.ajax.valve import AjaxValve

# ===========================================================================
# logbook.py
# ===========================================================================


def _hass(language: str = "en") -> MagicMock:
    hass = MagicMock()
    hass.config.language = language
    return hass


def _event(data: dict) -> SimpleNamespace:
    """A stand-in for homeassistant.core.Event with a `.data` mapping."""
    return SimpleNamespace(data=data)


def test_tr_returns_localised_string() -> None:
    assert logbook._tr(_hass("fr"), "armed") == "armé"
    assert logbook._tr(_hass("en"), "armed") == "armed"


def test_tr_falls_back_to_english_for_unknown_language() -> None:
    assert logbook._tr(_hass("it"), "armed") == "armed"


def test_tr_falls_back_to_key_for_unknown_key() -> None:
    assert logbook._tr(_hass("en"), "no_such_key") == "no_such_key"


def test_tr_formats_placeholders() -> None:
    assert logbook._tr(_hass("en"), "state_changed", old="A", new="B") == "changed from A to B"


def test_tr_returns_template_on_missing_placeholder() -> None:
    """A template expecting {old}/{new} but called without them must not crash."""
    # state_changed needs old+new; passing an unrelated kwarg triggers the KeyError branch.
    out = logbook._tr(_hass("en"), "state_changed", unrelated="x")
    assert out == "changed from {old} to {new}"


def test_tr_handles_none_language() -> None:
    hass = _hass(language=None)  # type: ignore[arg-type]
    assert logbook._tr(hass, "armed") == "armed"


def test_detection_label_localised_and_fallbacks() -> None:
    assert logbook._detection_label(_hass("fr"), "human") == "une personne"
    assert logbook._detection_label(_hass("en"), "vehicle") == "a vehicle"
    # Unknown event_type → returns the raw type verbatim.
    assert logbook._detection_label(_hass("en"), "weird") == "weird"
    # Known type, unknown language → English fallback.
    assert logbook._detection_label(_hass("it"), "pet") == "an animal"


def _describers() -> dict[str, object]:
    """Run async_describe_events and capture each registered callback by event type."""
    captured: dict[str, object] = {}
    hass = _hass("en")

    def _register(domain: str, event_type: str, cb: object) -> None:
        captured[event_type] = cb

    logbook.async_describe_events(hass, _register)  # type: ignore[arg-type]
    return captured


def test_async_describe_events_registers_all_event_types() -> None:
    captured = _describers()
    expected = {
        logbook.EVENT_AJAX_ARMED,
        logbook.EVENT_AJAX_DISARMED,
        logbook.EVENT_AJAX_ARMED_NIGHT,
        logbook.EVENT_AJAX_ARMED_HOME,
        logbook.EVENT_AJAX_SECURITY_STATE_CHANGED,
        logbook.EVENT_AJAX_BUTTON_PRESSED,
        logbook.EVENT_AJAX_DOORBELL_RING,
        logbook.EVENT_AJAX_SMART_LOCK_DOORBELL,
        logbook.EVENT_AJAX_SCENARIO_TRIGGERED,
        logbook.EVENT_AJAX_CAMERA_DETECTION,
    }
    assert expected.issubset(captured.keys())


def test_describe_armed_uses_space_name_and_icon() -> None:
    cb = _describers()[logbook.EVENT_AJAX_ARMED]
    out = cb(_event({"space_name": "Maison"}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_NAME] == "Maison"
    assert out[logbook.LOGBOOK_ENTRY_MESSAGE] == "armed"
    assert out[logbook.LOGBOOK_ENTRY_ICON] == "mdi:shield-lock"


def test_describe_armed_appends_source() -> None:
    """When the event carries a source_name, the message gets a `by <source>` suffix."""
    cb = _describers()[logbook.EVENT_AJAX_ARMED]
    out = cb(_event({"space_name": "Maison", "source_name": "Stéphane"}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_MESSAGE] == "armed by Stéphane"


def test_describe_armed_defaults_space_name() -> None:
    cb = _describers()[logbook.EVENT_AJAX_ARMED]
    out = cb(_event({}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_NAME] == "Ajax"


def test_describe_disarmed() -> None:
    cb = _describers()[logbook.EVENT_AJAX_DISARMED]
    out = cb(_event({"space_name": "Maison"}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_MESSAGE] == "disarmed"
    assert out[logbook.LOGBOOK_ENTRY_ICON] == "mdi:shield-off"


def test_describe_armed_night() -> None:
    cb = _describers()[logbook.EVENT_AJAX_ARMED_NIGHT]
    out = cb(_event({"space_name": "Maison"}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_MESSAGE] == "armed (night mode)"
    assert out[logbook.LOGBOOK_ENTRY_ICON] == "mdi:shield-moon"


def test_describe_armed_home() -> None:
    cb = _describers()[logbook.EVENT_AJAX_ARMED_HOME]
    out = cb(_event({"space_name": "Maison"}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_MESSAGE] == "armed (home)"
    assert out[logbook.LOGBOOK_ENTRY_ICON] == "mdi:shield-home"


def test_describe_state_changed() -> None:
    cb = _describers()[logbook.EVENT_AJAX_SECURITY_STATE_CHANGED]
    out = cb(_event({"space_name": "Maison", "old_state": "disarmed", "new_state": "armed"}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_MESSAGE] == "changed from disarmed to armed"
    assert out[logbook.LOGBOOK_ENTRY_ICON] == "mdi:shield-sync"


def test_describe_state_changed_defaults_unknown() -> None:
    cb = _describers()[logbook.EVENT_AJAX_SECURITY_STATE_CHANGED]
    out = cb(_event({}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_MESSAGE] == "changed from unknown to unknown"


def test_describe_button_uses_action() -> None:
    cb = _describers()[logbook.EVENT_AJAX_BUTTON_PRESSED]
    out = cb(_event({"device_name": "Remote", "action": "double_press"}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_NAME] == "Remote"
    assert out[logbook.LOGBOOK_ENTRY_MESSAGE] == "double_press"
    assert out[logbook.LOGBOOK_ENTRY_ICON] == "mdi:gesture-tap-button"


def test_describe_button_defaults() -> None:
    cb = _describers()[logbook.EVENT_AJAX_BUTTON_PRESSED]
    out = cb(_event({}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_NAME] == "Button"
    assert out[logbook.LOGBOOK_ENTRY_MESSAGE] == "pressed"


def test_describe_doorbell() -> None:
    cb = _describers()[logbook.EVENT_AJAX_DOORBELL_RING]
    out = cb(_event({"device_name": "Front Door"}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_NAME] == "Front Door"
    assert out[logbook.LOGBOOK_ENTRY_MESSAGE] == "rang"
    assert out[logbook.LOGBOOK_ENTRY_ICON] == "mdi:doorbell"


def test_describe_doorbell_default_name() -> None:
    cb = _describers()[logbook.EVENT_AJAX_DOORBELL_RING]
    out = cb(_event({}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_NAME] == "Doorbell"


def test_describe_smart_lock_doorbell() -> None:
    cb = _describers()[logbook.EVENT_AJAX_SMART_LOCK_DOORBELL]
    out = cb(_event({}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_NAME] == "Smart Lock"
    assert out[logbook.LOGBOOK_ENTRY_MESSAGE] == "rang"


def test_describe_scenario_with_target() -> None:
    cb = _describers()[logbook.EVENT_AJAX_SCENARIO_TRIGGERED]
    out = cb(_event({"scenario_name": "Night", "target_name": "Lights"}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_NAME] == "Night"
    assert out[logbook.LOGBOOK_ENTRY_MESSAGE] == "triggered on Lights"
    assert out[logbook.LOGBOOK_ENTRY_ICON] == "mdi:play-circle"


def test_describe_scenario_without_target() -> None:
    cb = _describers()[logbook.EVENT_AJAX_SCENARIO_TRIGGERED]
    out = cb(_event({"scenario_name": "Night"}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_MESSAGE] == "triggered"


def test_describe_scenario_default_name() -> None:
    cb = _describers()[logbook.EVENT_AJAX_SCENARIO_TRIGGERED]
    out = cb(_event({}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_NAME] == "Scenario"


def test_describe_camera_detection() -> None:
    cb = _describers()[logbook.EVENT_AJAX_CAMERA_DETECTION]
    out = cb(_event({"device_name": "Garden Cam", "event_type": "human"}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_NAME] == "Garden Cam"
    assert out[logbook.LOGBOOK_ENTRY_MESSAGE] == "detected a person"
    assert out[logbook.LOGBOOK_ENTRY_ICON] == "mdi:cctv"


def test_describe_camera_detection_default_name() -> None:
    cb = _describers()[logbook.EVENT_AJAX_CAMERA_DETECTION]
    out = cb(_event({}))  # type: ignore[operator]
    assert out[logbook.LOGBOOK_ENTRY_NAME] == "Camera"


# ===========================================================================
# alarm_control_panel.py — command methods + attributes/device_info
# ===========================================================================


def _space(security_state: SecurityState = SecurityState.DISARMED) -> SimpleNamespace:
    return SimpleNamespace(
        id="s1",
        name="Maison",
        security_state=security_state,
        hub_id="hub1",
        unread_notifications=0,
        devices={"d1": object()},
        notifications=[],
        rooms={},
        hub_details=None,
        group_mode_enabled=False,
        groups={},
        get_online_devices=lambda: [],
        get_devices_with_malfunctions=lambda: [],
        get_bypassed_devices=lambda: [],
    )


def _alarm_panel(space: SimpleNamespace | None, *, coordinator: MagicMock | None = None) -> AjaxAlarmControlPanel:
    panel = object.__new__(AjaxAlarmControlPanel)
    if coordinator is None:
        coordinator = MagicMock()
        coordinator.get_space = lambda sid: space
    panel.coordinator = coordinator
    panel._space_id = "s1"
    panel.async_write_ha_state = MagicMock()
    return panel


@pytest.mark.asyncio
async def test_alarm_arm_away_optimistic_then_calls_coordinator() -> None:
    space = _space()
    coordinator = MagicMock()
    coordinator.get_space = lambda sid: space
    coordinator.async_arm_space = AsyncMock()
    panel = _alarm_panel(space, coordinator=coordinator)

    await panel.async_alarm_arm_away()

    # Optimistic flip happened and stuck (no rollback on success).
    assert space.security_state is SecurityState.ARMED
    coordinator.async_arm_space.assert_awaited_once_with("s1")


@pytest.mark.asyncio
async def test_alarm_arm_away_rolls_back_on_failure() -> None:
    space = _space(SecurityState.DISARMED)
    coordinator = MagicMock()
    coordinator.get_space = lambda sid: space
    coordinator.async_arm_space = AsyncMock(side_effect=RuntimeError("boom"))
    coordinator.async_request_refresh_bypass_cache = AsyncMock()
    panel = _alarm_panel(space, coordinator=coordinator)

    with pytest.raises(HomeAssistantError):
        await panel.async_alarm_arm_away()

    # Pre-action state restored synchronously.
    assert space.security_state is SecurityState.DISARMED
    coordinator.async_request_refresh_bypass_cache.assert_awaited_once()


@pytest.mark.asyncio
async def test_alarm_arm_night_optimistic_and_rollback() -> None:
    space = _space(SecurityState.DISARMED)
    coordinator = MagicMock()
    coordinator.get_space = lambda sid: space
    coordinator.async_arm_night_mode = AsyncMock(side_effect=RuntimeError("boom"))
    coordinator.async_request_refresh_bypass_cache = AsyncMock()
    panel = _alarm_panel(space, coordinator=coordinator)

    with pytest.raises(HomeAssistantError):
        await panel.async_alarm_arm_night()
    assert space.security_state is SecurityState.DISARMED


@pytest.mark.asyncio
async def test_alarm_arm_night_success() -> None:
    space = _space(SecurityState.DISARMED)
    coordinator = MagicMock()
    coordinator.get_space = lambda sid: space
    coordinator.async_arm_night_mode = AsyncMock()
    panel = _alarm_panel(space, coordinator=coordinator)

    await panel.async_alarm_arm_night()
    assert space.security_state is SecurityState.NIGHT_MODE
    coordinator.async_arm_night_mode.assert_awaited_once_with("s1")


@pytest.mark.asyncio
async def test_alarm_disarm_success() -> None:
    space = _space(SecurityState.ARMED)
    coordinator = MagicMock()
    coordinator.get_space = lambda sid: space
    coordinator.async_disarm_space = AsyncMock()
    panel = _alarm_panel(space, coordinator=coordinator)

    await panel.async_alarm_disarm()
    assert space.security_state is SecurityState.DISARMED
    coordinator.async_disarm_space.assert_awaited_once_with("s1")


@pytest.mark.asyncio
async def test_alarm_disarm_rolls_back_on_failure() -> None:
    space = _space(SecurityState.ARMED)
    coordinator = MagicMock()
    coordinator.get_space = lambda sid: space
    coordinator.async_disarm_space = AsyncMock(side_effect=RuntimeError("boom"))
    coordinator.async_request_refresh_bypass_cache = AsyncMock()
    panel = _alarm_panel(space, coordinator=coordinator)

    with pytest.raises(HomeAssistantError):
        await panel.async_alarm_disarm()
    assert space.security_state is SecurityState.ARMED


@pytest.mark.asyncio
async def test_alarm_disarm_no_space_is_noop() -> None:
    """When the space vanished, the command still calls the coordinator without crashing."""
    coordinator = MagicMock()
    coordinator.get_space = lambda sid: None
    coordinator.async_disarm_space = AsyncMock()
    panel = _alarm_panel(None, coordinator=coordinator)

    await panel.async_alarm_disarm()
    coordinator.async_disarm_space.assert_awaited_once_with("s1")


def test_alarm_extra_state_attributes() -> None:
    space = _space()
    space.unread_notifications = 3
    panel = _alarm_panel(space)
    attrs = panel.extra_state_attributes
    assert attrs["space_id"] == "s1"
    assert attrs["space_name"] == "Maison"
    assert attrs["hub_id"] == "hub1"
    assert attrs["unread_notifications"] == 3
    assert attrs["total_devices"] == 1


def test_alarm_extra_state_attributes_empty_when_space_missing() -> None:
    panel = _alarm_panel(None)
    assert panel.extra_state_attributes == {}


def test_alarm_extra_state_attributes_changed_by_and_rooms() -> None:
    space = _space()
    space.notifications = [SimpleNamespace(title="armed", user_name="Alice")]
    space.rooms = {"r1": SimpleNamespace(name="Kitchen", device_ids=["d1", "d2"])}
    panel = _alarm_panel(space)
    attrs = panel.extra_state_attributes
    assert attrs["changed_by"] == "Alice"
    assert attrs["rooms"]["r1"] == {"name": "Kitchen", "device_count": 2}


def test_alarm_device_info_with_hub_details() -> None:
    space = _space()
    space.hub_details = {
        "hubSubtype": "HUB_2_PLUS",
        "color": "black",
        "firmware": {"version": "9.5"},
        "hardwareVersions": {"pcb": 12},
    }
    panel = _alarm_panel(space)
    info = panel.device_info
    assert info["model"] == "Hub 2 Plus (Black)"
    assert info["sw_version"] == "9.5"
    assert info["hw_version"] == "PCB rev.12"


def test_alarm_device_info_defaults_without_hub_details() -> None:
    space = _space()
    space.hub_details = None
    panel = _alarm_panel(space)
    info = panel.device_info
    assert info["model"] == "Security Hub"
    assert info["sw_version"] is None
    assert info["hw_version"] is None


def test_alarm_device_info_none_when_space_missing() -> None:
    panel = _alarm_panel(None)
    assert panel.device_info is None


def test_build_hub_info_none_when_space_missing() -> None:
    panel = _alarm_panel(None)
    assert panel._build_hub_info() is None


def test_handle_coordinator_update_refreshes_registry_once() -> None:
    """The registry refresh fires once on the first coordinator update, then is skipped."""
    space = _space()
    space.hub_details = {"hubSubtype": "HUB_2", "firmware": {"version": "9.0"}}
    panel = _alarm_panel(space)
    panel.hass = MagicMock()
    update_calls: list[int] = []
    panel._update_device_registry = lambda: update_calls.append(1)

    panel._handle_coordinator_update()
    panel._handle_coordinator_update()

    assert panel._device_info_updated is True
    assert len(update_calls) == 1  # only the first call refreshes the registry
    assert panel.async_write_ha_state.call_count == 2


def test_update_device_registry_noop_without_hub_info() -> None:
    """No space/hub details → bail out before touching the registry."""
    panel = _alarm_panel(None)
    panel.hass = MagicMock()
    # Must not raise even though dr.async_get is never reached.
    panel._update_device_registry()


def test_update_device_registry_updates_existing_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    space = _space()
    space.hub_details = {
        "hubSubtype": "HUB_2_PLUS",
        "color": "black",
        "firmware": {"version": "9.5"},
        "hardwareVersions": {"pcb": 7},
    }
    panel = _alarm_panel(space)
    panel.hass = MagicMock()

    registry = MagicMock()
    registry.async_get_device.return_value = SimpleNamespace(id="dev-entry-1")
    monkeypatch.setattr(
        "custom_components.ajax.alarm_control_panel.dr.async_get",
        lambda _hass: registry,
    )

    panel._update_device_registry()

    registry.async_update_device.assert_called_once()
    _, kwargs = registry.async_update_device.call_args
    assert kwargs["model"] == "Hub 2 Plus (Black)"
    assert kwargs["sw_version"] == "9.5"
    assert kwargs["hw_version"] == "PCB rev.7"


def test_update_device_registry_noop_when_entry_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    space = _space()
    space.hub_details = {"hubSubtype": "HUB_2", "firmware": {"version": "9.0"}}
    panel = _alarm_panel(space)
    panel.hass = MagicMock()

    registry = MagicMock()
    registry.async_get_device.return_value = None
    monkeypatch.setattr(
        "custom_components.ajax.alarm_control_panel.dr.async_get",
        lambda _hass: registry,
    )

    panel._update_device_registry()
    registry.async_update_device.assert_not_called()


# ---------------------------------------------------------------------------
# Group panel commands + attributes
# ---------------------------------------------------------------------------


def _group_panel(group: SimpleNamespace | None, *, coordinator: MagicMock | None = None) -> AjaxGroupAlarmControlPanel:
    panel = object.__new__(AjaxGroupAlarmControlPanel)
    if coordinator is None:
        coordinator = MagicMock()
        coordinator.get_group = lambda _s, _g: group
    coordinator.entry_id = "entry_test"
    panel.coordinator = coordinator
    panel._space_id = "s1"
    panel._group_id = "g1"
    panel.async_write_ha_state = MagicMock()
    return panel


@pytest.mark.asyncio
async def test_group_arm_away_success() -> None:
    group = SimpleNamespace(state=GroupState.DISARMED, name="LR", id="g1")
    coordinator = MagicMock()
    coordinator.get_group = lambda _s, _g: group
    coordinator.async_arm_group = AsyncMock()
    panel = _group_panel(group, coordinator=coordinator)

    await panel.async_alarm_arm_away()
    assert group.state is GroupState.ARMED
    coordinator.async_arm_group.assert_awaited_once_with("s1", "g1")


@pytest.mark.asyncio
async def test_group_arm_away_rolls_back_on_failure() -> None:
    group = SimpleNamespace(state=GroupState.DISARMED, name="LR", id="g1")
    coordinator = MagicMock()
    coordinator.get_group = lambda _s, _g: group
    coordinator.async_arm_group = AsyncMock(side_effect=RuntimeError("boom"))
    coordinator.async_request_refresh_bypass_cache = AsyncMock()
    panel = _group_panel(group, coordinator=coordinator)

    with pytest.raises(HomeAssistantError):
        await panel.async_alarm_arm_away()
    assert group.state is GroupState.DISARMED
    coordinator.async_request_refresh_bypass_cache.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_disarm_success() -> None:
    group = SimpleNamespace(state=GroupState.ARMED, name="LR", id="g1")
    coordinator = MagicMock()
    coordinator.get_group = lambda _s, _g: group
    coordinator.async_disarm_group = AsyncMock()
    panel = _group_panel(group, coordinator=coordinator)

    await panel.async_alarm_disarm()
    assert group.state is GroupState.DISARMED
    coordinator.async_disarm_group.assert_awaited_once_with("s1", "g1")


@pytest.mark.asyncio
async def test_group_disarm_rolls_back_on_failure() -> None:
    group = SimpleNamespace(state=GroupState.ARMED, name="LR", id="g1")
    coordinator = MagicMock()
    coordinator.get_group = lambda _s, _g: group
    coordinator.async_disarm_group = AsyncMock(side_effect=RuntimeError("boom"))
    coordinator.async_request_refresh_bypass_cache = AsyncMock()
    panel = _group_panel(group, coordinator=coordinator)

    with pytest.raises(HomeAssistantError):
        await panel.async_alarm_disarm()
    assert group.state is GroupState.ARMED


def test_group_extra_state_attributes() -> None:
    group = SimpleNamespace(
        state=GroupState.ARMED,
        name="LR",
        id="g1",
        bulk_arm_involved=True,
        bulk_disarm_involved=False,
    )
    panel = _group_panel(group)
    attrs = panel.extra_state_attributes
    assert attrs == {
        "group_id": "g1",
        "group_name": "LR",
        "space_id": "s1",
        "bulk_arm_involved": True,
        "bulk_disarm_involved": False,
    }


def test_group_extra_state_attributes_empty_when_missing() -> None:
    panel = _group_panel(None)
    assert panel.extra_state_attributes == {}


def test_group_device_info_links_to_hub() -> None:
    group = SimpleNamespace(state=GroupState.ARMED, name="LR", id="g1")
    panel = _group_panel(group)
    assert panel.device_info["identifiers"] == {("ajax", "entry_test_s1")}


# ===========================================================================
# valve.py — command + attributes/device_info
# ===========================================================================


def _waterstop(online: bool = True, attributes: dict | None = None) -> AjaxDevice:
    return AjaxDevice(
        id="d1",
        name="Water Stop",
        type=DeviceType.WATERSTOP,
        space_id="s1",
        hub_id="hub1",
        raw_type="WaterStop",
        online=online,
        room_name="Bathroom",
        attributes=attributes if attributes is not None else {},
    )


def _valve(device: AjaxDevice | None, *, hub_id: str = "hub1") -> AjaxValve:
    valve = object.__new__(AjaxValve)
    valve._space_id = "s1"
    valve._device_id = "d1"
    valve._valve_key = "valve"
    valve._valve_desc = {"key": "valve"}
    space = SimpleNamespace(devices={"d1": device} if device else {}, hub_id=hub_id)
    coordinator = MagicMock()
    coordinator.entry_id = "entry_test"
    coordinator.get_space = lambda sid: space
    coordinator.api.async_set_waterstop_state = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()
    valve.coordinator = coordinator
    valve.async_write_ha_state = MagicMock()
    return valve


@pytest.mark.asyncio
async def test_valve_open_calls_api_and_marks_optimistic() -> None:
    device = _waterstop()
    valve = _valve(device)
    await valve.async_open_valve()
    assert device.attributes["valveState"] == "OPEN"
    assert device.is_optimistic("valveState") is True
    valve.coordinator.api.async_set_waterstop_state.assert_awaited_once_with("hub1", "d1", True)


@pytest.mark.asyncio
async def test_valve_close_calls_api() -> None:
    device = _waterstop()
    valve = _valve(device)
    await valve.async_close_valve()
    assert device.attributes["valveState"] == "CLOSED"
    valve.coordinator.api.async_set_waterstop_state.assert_awaited_once_with("hub1", "d1", False)


@pytest.mark.asyncio
async def test_valve_error_reverts_to_previous_value() -> None:
    device = _waterstop(attributes={"valveState": "CLOSED"})
    valve = _valve(device)
    valve.coordinator.api.async_set_waterstop_state = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError):
        await valve.async_open_valve()
    # Reverted to the prior known value, optimistic guard cleared.
    assert device.attributes["valveState"] == "CLOSED"
    assert device.is_optimistic("valveState") is False
    valve.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_valve_error_drops_key_when_no_previous_value() -> None:
    """If valveState was never set, the failed optimistic write must be dropped, not left as OPEN."""
    device = _waterstop(attributes={})
    valve = _valve(device)
    valve.coordinator.api.async_set_waterstop_state = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError):
        await valve.async_open_valve()
    assert "valveState" not in device.attributes


@pytest.mark.asyncio
async def test_valve_raises_when_device_missing() -> None:
    valve = _valve(None)
    with pytest.raises(HomeAssistantError):
        await valve.async_open_valve()


@pytest.mark.asyncio
async def test_valve_raises_when_hub_missing() -> None:
    device = _waterstop()
    valve = _valve(device, hub_id=None)  # type: ignore[arg-type]
    with pytest.raises(HomeAssistantError):
        await valve.async_open_valve()


def test_valve_extra_state_attributes_includes_motor_state() -> None:
    device = _waterstop(attributes={"motorState": "OPENING"})
    valve = _valve(device)
    attrs = valve.extra_state_attributes
    assert attrs["device_type"] == "WaterStop"
    assert attrs["device_id"] == "d1"
    assert attrs["motor_state"] == "opening"


def test_valve_extra_state_attributes_without_motor_state() -> None:
    device = _waterstop(attributes={})
    valve = _valve(device)
    assert "motor_state" not in valve.extra_state_attributes


def test_valve_extra_state_attributes_empty_when_missing() -> None:
    assert _valve(None).extra_state_attributes == {}


def test_valve_device_info() -> None:
    device = _waterstop(attributes={"firmwareVersion": "1.2.3"})
    info = _valve(device).device_info
    assert info["identifiers"] == {("ajax", "entry_test_d1")}
    assert info["model"] == "WaterStop"
    assert info["sw_version"] == "1.2.3"
    assert info["suggested_area"] == "Bathroom"


def test_valve_device_info_none_when_missing() -> None:
    assert _valve(None).device_info is None


def test_valve_is_open_none_without_value_fn() -> None:
    """A descriptor without a value_fn yields an unknown (None) state."""
    device = _waterstop()
    valve = _valve(device)
    valve._valve_desc = {"key": "valve"}
    assert valve.is_open is None


def test_valve_get_device_none_when_space_missing() -> None:
    valve = object.__new__(AjaxValve)
    valve._space_id = "s1"
    valve._device_id = "d1"
    valve._valve_desc = {"key": "valve"}
    valve.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: None)
    assert valve._get_device() is None


# ===========================================================================
# event.py — device_info branches
# ===========================================================================


def _event_entity(*, account_spaces: dict | None) -> AjaxEventEntity:
    ent = object.__new__(AjaxEventEntity)
    ent._device_id = "x1"
    account = SimpleNamespace(spaces=account_spaces) if account_spaces is not None else None
    ent.coordinator = SimpleNamespace(last_update_success=True, account=account, entry_id="entry_test")
    return ent


def test_event_device_info_none_when_account_missing() -> None:
    assert _event_entity(account_spaces=None).device_info is None


def test_event_device_info_for_regular_device() -> None:
    device = AjaxDevice(id="x1", name="Button", type=DeviceType.BUTTON, space_id="s1", hub_id="hub1")
    space = SimpleNamespace(id="s1", devices={"x1": device}, video_edges={}, smart_locks={})
    info = _event_entity(account_spaces={"s1": space}).device_info
    assert info["identifiers"] == {("ajax", "entry_test_x1")}
    assert info["name"] == "Button"
    assert info["model"] == DeviceType.BUTTON.value


def test_event_device_info_for_video_edge() -> None:
    ve = AjaxVideoEdge(id="x1", name="Doorbell Cam", space_id="s1", video_edge_type=VideoEdgeType.DOORBELL)
    space = SimpleNamespace(id="s1", devices={}, video_edges={"x1": ve}, smart_locks={})
    info = _event_entity(account_spaces={"s1": space}).device_info
    assert info["identifiers"] == {("ajax", "entry_test_x1")}
    assert info["name"] == "Doorbell Cam"


def test_event_device_info_for_smart_lock() -> None:
    sl = AjaxSmartLock(id="x1", name="Front Lock", space_id="s1")
    space = SimpleNamespace(id="s1", devices={}, video_edges={}, smart_locks={"x1": sl})
    info = _event_entity(account_spaces={"s1": space}).device_info
    assert info["model"] == "LockBridge Jeweller"
    assert info["name"] == "Front Lock"


def test_event_device_info_none_when_device_not_found() -> None:
    space = SimpleNamespace(id="s1", devices={}, video_edges={}, smart_locks={})
    assert _event_entity(account_spaces={"s1": space}).device_info is None


def test_event_init_wires_attrs() -> None:
    coordinator = MagicMock()
    coordinator.entry_id = "entry_test"
    ent = AjaxEventEntity(
        coordinator=coordinator,
        space_id="s1",
        device_id="d1",
        event_key="doorbell_press",
        event_desc={
            "key": "doorbell_press",
            "translation_key": "doorbell_press",
            "event_types": ["ring"],
            "enabled_by_default": False,
        },
    )
    assert ent._attr_unique_id == "entry_test_d1_doorbell_press"
    assert ent._attr_translation_key == "doorbell_press"
    assert ent._attr_event_types == ["ring"]
    assert ent._attr_entity_registry_enabled_default is False


def test_event_fire_writes_state_when_hass_present() -> None:
    ent = object.__new__(AjaxEventEntity)
    ent._device_id = "d1"
    ent._attr_event_types = ["ring"]
    ent.hass = MagicMock()
    ent._trigger_event = MagicMock()
    ent.async_write_ha_state = MagicMock()
    ent.fire("ring")
    ent._trigger_event.assert_called_once_with("ring", None)
    ent.async_write_ha_state.assert_called_once()


# ===========================================================================
# lock.py — extra_state_attributes + device_info
# ===========================================================================


def _lock(smart_lock: AjaxSmartLock | None) -> AjaxLock:
    lock = object.__new__(AjaxLock)
    lock._space_id = "s1"
    lock._smart_lock_id = "sl1"
    space = SimpleNamespace(smart_locks={"sl1": smart_lock} if smart_lock else {})
    lock.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: space, entry_id="entry_test")
    return lock


def test_lock_extra_state_attributes_full() -> None:
    sl = AjaxSmartLock(id="sl1", name="Front Door", space_id="s1")
    sl.last_changed_by = "Alice"
    sl.last_event_tag = "manual_lock"
    sl.last_event_time = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)
    attrs = _lock(sl).extra_state_attributes
    assert attrs["last_changed_by"] == "Alice"
    assert attrs["last_event"] == "manual_lock"
    assert attrs["last_event_time"] == sl.last_event_time.isoformat()


def test_lock_extra_state_attributes_empty_fields() -> None:
    sl = AjaxSmartLock(id="sl1", name="Front Door", space_id="s1")
    assert _lock(sl).extra_state_attributes == {}


def test_lock_extra_state_attributes_empty_when_missing() -> None:
    assert _lock(None).extra_state_attributes == {}


def test_lock_device_info() -> None:
    sl = AjaxSmartLock(id="sl1", name="Front Door", space_id="s1")
    info = _lock(sl).device_info
    assert info["identifiers"] == {("ajax", "entry_test_sl1")}
    assert info["model"] == "LockBridge Jeweller"
    assert info["name"] == "Front Door"


def test_lock_device_info_none_when_missing() -> None:
    assert _lock(None).device_info is None


def test_lock_get_smart_lock_none_when_space_missing() -> None:
    lock = object.__new__(AjaxLock)
    lock._space_id = "s1"
    lock._smart_lock_id = "sl1"
    lock.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: None)
    assert lock._get_smart_lock() is None
    assert lock.is_locked is None
    assert lock.available is False


# ===========================================================================
# device_tracker.py — device_info
# ===========================================================================


def _tracker(space: SimpleNamespace | None) -> AjaxHubTracker:
    tracker = object.__new__(AjaxHubTracker)
    tracker._space_id = "s1"
    tracker.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: space, entry_id="entry_test")
    return tracker


def test_tracker_device_info_uses_space_name() -> None:
    info = _tracker(SimpleNamespace(name="Maison")).device_info
    assert info["identifiers"] == {("ajax", "entry_test_s1")}
    assert info["name"] == "Maison"


def test_tracker_device_info_renames_generic_hub() -> None:
    """A space literally named 'Hub' is shown as 'Ajax Hub' to avoid a bare label on the map."""
    info = _tracker(SimpleNamespace(name="Hub")).device_info
    assert info["name"] == "Ajax Hub"


def test_tracker_device_info_none_when_space_missing() -> None:
    assert _tracker(None).device_info is None


def _geofence_tracker(geofence: dict) -> AjaxHubTracker:
    space = SimpleNamespace(hub_details={"geoFence": geofence}, hub_id="hub1", name="Maison")
    tracker = object.__new__(AjaxHubTracker)
    tracker._space_id = "s1"
    tracker.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: space, entry_id="entry_test")
    return tracker


def test_tracker_longitude_invalid_value_returns_none() -> None:
    """Malformed longitude must be swallowed, mirroring the latitude guard."""
    assert _geofence_tracker({"longitude": "nope"}).longitude is None


def test_tracker_location_accuracy_invalid_value_returns_zero() -> None:
    assert _geofence_tracker({"radiusMeters": "nope"}).location_accuracy == 0
