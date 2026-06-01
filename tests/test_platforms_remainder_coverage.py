"""Remainder coverage for the lighter entity platforms.

Covers the bits not exercised by the focused per-entity tests:

* ``valve.async_setup_entry`` / ``_build_valves`` discovery plus the
  optimistic-write + rollback machinery of ``_set_valve_state``.
* ``lock.async_setup_entry`` / ``_build_lock`` discovery and the
  read-only ``async_lock`` / ``async_unlock`` guards.
* ``device_tracker.async_setup_entry`` geofence filtering.
* ``alarm_control_panel.async_setup_entry`` (space + group panels) and
  the ``AjaxGroupAlarmControlPanel.__init__`` name fallback.
* ``update.async_setup_entry`` (hub + video-edge entities) and
  ``_build_update`` discovery.

All entities are built without a running Home Assistant: the coordinator
and API are mocks, ``async_write_ha_state`` is stubbed, and
``connect_new_entity_signal`` is patched out (or replaced by a capture
shim to grab the discovery builder closure).
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.ajax import (
    alarm_control_panel as acp_mod,
    device_tracker as dt_mod,
    lock as lock_mod,
    update as update_mod,
    valve as valve_mod,
)
from custom_components.ajax.alarm_control_panel import AjaxGroupAlarmControlPanel
from custom_components.ajax.lock import AjaxLock
from custom_components.ajax.models import (
    AjaxAccount,
    AjaxDevice,
    AjaxGroup,
    AjaxSmartLock,
    AjaxSpace,
    AjaxVideoEdge,
    DeviceType,
    VideoEdgeType,
)
from custom_components.ajax.valve import AjaxValve

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _account(*spaces: AjaxSpace) -> AjaxAccount:
    account = AjaxAccount(user_id="u1", name="U", email="u@e.com")
    for space in spaces:
        account.spaces[space.id] = space
    return account


def _waterstop_space() -> AjaxSpace:
    device = AjaxDevice(
        id="d1",
        name="Water Stop",
        type=DeviceType.WATERSTOP,
        space_id="s1",
        hub_id="hub1",
        attributes={"valveState": "OPEN"},
    )
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    space.devices[device.id] = device
    return space


# ===========================================================================
# valve
# ===========================================================================


@pytest.mark.asyncio
async def test_valve_setup_entry_no_account_returns_early() -> None:
    entry = SimpleNamespace(runtime_data=SimpleNamespace(account=None))
    add = MagicMock()
    await valve_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


@pytest.mark.asyncio
async def test_valve_setup_entry_creates_valve() -> None:
    space = _waterstop_space()
    coordinator = SimpleNamespace(account=_account(space), get_space=lambda sid: None, entry_id="entry_test")
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    with patch("custom_components.ajax.valve.connect_new_entity_signal"):
        await valve_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_called_once()
    created = add.call_args[0][0]
    assert any(isinstance(e, AjaxValve) for e in created)


@pytest.mark.asyncio
async def test_valve_setup_entry_no_handler_skips_add() -> None:
    device = AjaxDevice(id="d1", name="Mystery", type=DeviceType.UNKNOWN, space_id="s1", hub_id="hub1")
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    space.devices[device.id] = device
    coordinator = SimpleNamespace(account=_account(space), get_space=lambda sid: None)
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    with patch("custom_components.ajax.valve.connect_new_entity_signal"):
        await valve_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


async def _capture_valve_builder(space: AjaxSpace):
    coordinator = SimpleNamespace(
        account=_account(space),
        get_space=lambda sid: space if sid == space.id else None,
        entry_id="entry_test",
    )
    entry = SimpleNamespace(runtime_data=coordinator)
    captured: dict = {}

    def _fake_connect(hass, entry_, signal, domain, add, builder, label):
        captured["builder"] = builder

    with patch("custom_components.ajax.valve.connect_new_entity_signal", _fake_connect):
        await valve_mod.async_setup_entry(MagicMock(), entry, MagicMock())
    return captured["builder"]


@pytest.mark.asyncio
async def test_valve_build_valves_pairs() -> None:
    builder = await _capture_valve_builder(_waterstop_space())
    pairs = builder("s1", "d1")
    assert pairs
    uid, entity = pairs[0]
    # The builder's first tuple element is the bare dedup key, not the
    # namespaced unique_id (which lives on entity._attr_unique_id).
    assert uid == "d1_valve"
    assert entity._attr_unique_id == "entry_test_d1_valve"
    assert isinstance(entity, AjaxValve)


@pytest.mark.asyncio
async def test_valve_build_valves_empty_when_space_missing() -> None:
    builder = await _capture_valve_builder(_waterstop_space())
    assert builder("ghost", "d1") == []


@pytest.mark.asyncio
async def test_valve_build_valves_empty_when_device_missing() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    builder = await _capture_valve_builder(space)
    assert builder("s1", "ghost") == []


@pytest.mark.asyncio
async def test_valve_build_valves_empty_when_no_handler() -> None:
    device = AjaxDevice(id="d1", name="Mystery", type=DeviceType.UNKNOWN, space_id="s1", hub_id="hub1")
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    space.devices[device.id] = device
    builder = await _capture_valve_builder(space)
    assert builder("s1", "d1") == []


def _valve_with_device(device: AjaxDevice | None, *, hub_id: str | None = "hub1") -> AjaxValve:
    valve = object.__new__(AjaxValve)
    valve._space_id = "s1"
    valve._device_id = "d1"
    valve._valve_key = "valve"
    valve._valve_desc = {"key": "valve"}
    space = SimpleNamespace(devices={"d1": device} if device else {}, hub_id=hub_id)
    api = SimpleNamespace(async_set_waterstop_state=AsyncMock())
    valve.coordinator = SimpleNamespace(
        get_space=lambda sid: space,
        api=api,
        async_request_refresh=AsyncMock(),
    )
    valve.async_write_ha_state = MagicMock()
    return valve


@pytest.mark.asyncio
async def test_valve_open_marks_optimistic_and_calls_api() -> None:
    device = AjaxDevice(
        id="d1", name="WS", type=DeviceType.WATERSTOP, space_id="s1", hub_id="hub1", attributes={"valveState": "CLOSED"}
    )
    valve = _valve_with_device(device)
    await valve.async_open_valve()
    assert device.attributes["valveState"] == "OPEN"
    # Optimistic guard reserved against the next poll.
    assert device.is_optimistic("valveState") is True
    valve.coordinator.api.async_set_waterstop_state.assert_awaited_once_with("hub1", "d1", True)
    valve.async_write_ha_state.assert_called()


@pytest.mark.asyncio
async def test_valve_close_calls_api_with_false() -> None:
    device = AjaxDevice(
        id="d1", name="WS", type=DeviceType.WATERSTOP, space_id="s1", hub_id="hub1", attributes={"valveState": "OPEN"}
    )
    valve = _valve_with_device(device)
    await valve.async_close_valve()
    assert device.attributes["valveState"] == "CLOSED"
    valve.coordinator.api.async_set_waterstop_state.assert_awaited_once_with("hub1", "d1", False)


@pytest.mark.asyncio
async def test_valve_set_state_raises_when_device_missing() -> None:
    valve = _valve_with_device(None)
    with pytest.raises(HomeAssistantError) as err:
        await valve.async_open_valve()
    assert err.value.translation_key == "device_not_found"


@pytest.mark.asyncio
async def test_valve_set_state_raises_when_hub_missing() -> None:
    device = AjaxDevice(id="d1", name="WS", type=DeviceType.WATERSTOP, space_id="s1", hub_id="hub1")
    valve = _valve_with_device(device, hub_id=None)
    with pytest.raises(HomeAssistantError) as err:
        await valve.async_open_valve()
    assert err.value.translation_key == "hub_not_found"


@pytest.mark.asyncio
async def test_valve_rollback_restores_previous_value_on_error() -> None:
    device = AjaxDevice(
        id="d1", name="WS", type=DeviceType.WATERSTOP, space_id="s1", hub_id="hub1", attributes={"valveState": "CLOSED"}
    )
    valve = _valve_with_device(device)
    valve.coordinator.api.async_set_waterstop_state = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError) as err:
        await valve.async_open_valve()
    assert err.value.translation_key == "failed_to_change"
    # Reverted to the previous concrete value and the guard was cleared.
    assert device.attributes["valveState"] == "CLOSED"
    assert "valveState" not in device.attributes.get("_optimistic_attrs", {})
    valve.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_valve_rollback_drops_key_when_previously_unset() -> None:
    device = AjaxDevice(id="d1", name="WS", type=DeviceType.WATERSTOP, space_id="s1", hub_id="hub1", attributes={})
    valve = _valve_with_device(device)
    valve.coordinator.api.async_set_waterstop_state = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError):
        await valve.async_close_valve()
    # The key never existed before, so the rollback drops it entirely.
    assert "valveState" not in device.attributes


# ===========================================================================
# lock
# ===========================================================================


def _lock_space() -> AjaxSpace:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    space.smart_locks["lk1"] = AjaxSmartLock(id="lk1", name="Front Door", space_id="s1")
    return space


@pytest.mark.asyncio
async def test_lock_setup_entry_no_account_returns_early() -> None:
    entry = SimpleNamespace(runtime_data=SimpleNamespace(account=None))
    add = MagicMock()
    await lock_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


@pytest.mark.asyncio
async def test_lock_setup_entry_creates_lock() -> None:
    coordinator = SimpleNamespace(account=_account(_lock_space()), entry_id="entry_test")
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    with patch("custom_components.ajax.lock.connect_new_entity_signal"):
        await lock_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_called_once()
    created = add.call_args[0][0]
    assert any(isinstance(e, AjaxLock) for e in created)


@pytest.mark.asyncio
async def test_lock_setup_entry_no_locks_skips_add() -> None:
    coordinator = SimpleNamespace(account=_account(AjaxSpace(id="s1", name="Home", hub_id="hub1")))
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    with patch("custom_components.ajax.lock.connect_new_entity_signal"):
        await lock_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


@pytest.mark.asyncio
async def test_lock_build_lock_pairs() -> None:
    coordinator = SimpleNamespace(account=_account(_lock_space()), entry_id="entry_test")
    entry = SimpleNamespace(runtime_data=coordinator)
    captured: dict = {}

    def _fake_connect(hass, entry_, signal, domain, add, builder, label):
        captured["builder"] = builder

    with patch("custom_components.ajax.lock.connect_new_entity_signal", _fake_connect):
        await lock_mod.async_setup_entry(MagicMock(), entry, MagicMock())
    pairs = captured["builder"]("s1", "lk1")
    assert pairs
    uid, entity = pairs[0]
    # Bare dedup key here; the namespaced id lives on _attr_unique_id.
    assert uid == "lk1_lock"
    assert entity._attr_unique_id == "entry_test_lk1_lock"
    assert isinstance(entity, AjaxLock)


def _lock_entity() -> AjaxLock:
    lk = object.__new__(AjaxLock)
    lk._space_id = "s1"
    lk._smart_lock_id = "lk1"
    return lk


@pytest.mark.asyncio
async def test_lock_async_lock_not_supported() -> None:
    with pytest.raises(HomeAssistantError) as err:
        await _lock_entity().async_lock()
    assert err.value.translation_key == "lock_not_supported"


@pytest.mark.asyncio
async def test_lock_async_unlock_not_supported() -> None:
    with pytest.raises(HomeAssistantError) as err:
        await _lock_entity().async_unlock()
    assert err.value.translation_key == "lock_not_supported"


# ===========================================================================
# device_tracker
# ===========================================================================


def _hub_space(*, geofence: dict | None) -> AjaxSpace:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    if geofence is not None:
        space.hub_details = {"geoFence": geofence}
    return space


@pytest.mark.asyncio
async def test_tracker_setup_entry_no_account_returns_early() -> None:
    entry = SimpleNamespace(runtime_data=SimpleNamespace(account=None))
    add = MagicMock()
    await dt_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


@pytest.mark.asyncio
async def test_tracker_setup_entry_creates_tracker_with_geofence() -> None:
    space = _hub_space(geofence={"latitude": 48.85, "longitude": 2.35})
    coordinator = SimpleNamespace(account=_account(space), entry_id="entry_test")
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    await dt_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_called_once()
    created = add.call_args[0][0]
    assert len(created) == 1
    assert isinstance(created[0], dt_mod.AjaxHubTracker)


@pytest.mark.asyncio
async def test_tracker_setup_entry_skips_space_without_coordinates() -> None:
    # hub_details present but geoFence has no lat/lon -> no tracker.
    space = _hub_space(geofence={"radiusMeters": 100})
    coordinator = SimpleNamespace(account=_account(space))
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    await dt_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


@pytest.mark.asyncio
async def test_tracker_setup_entry_skips_space_without_hub_details() -> None:
    space = _hub_space(geofence=None)
    coordinator = SimpleNamespace(account=_account(space))
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    await dt_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


# ===========================================================================
# alarm_control_panel
# ===========================================================================


@pytest.mark.asyncio
async def test_acp_setup_entry_no_account_logs_and_skips() -> None:
    coordinator = SimpleNamespace(account=None)
    entry = SimpleNamespace(runtime_data=coordinator, entry_id="e1")
    add = MagicMock()
    with patch("custom_components.ajax.alarm_control_panel.connect_new_entity_signal"):
        await acp_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


@pytest.mark.asyncio
async def test_acp_setup_entry_creates_space_panel_only() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    coordinator = SimpleNamespace(account=_account(space))
    entry = SimpleNamespace(runtime_data=coordinator, entry_id="e1")
    add = MagicMock()
    with patch("custom_components.ajax.alarm_control_panel.connect_new_entity_signal"):
        await acp_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_called_once()
    created = add.call_args[0][0]
    assert len(created) == 1
    assert isinstance(created[0], acp_mod.AjaxAlarmControlPanel)


@pytest.mark.asyncio
async def test_acp_setup_entry_creates_group_panels() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", group_mode_enabled=True)
    space.groups["g1"] = AjaxGroup(id="g1", name="Living Room", space_id="s1")
    space.groups["g2"] = AjaxGroup(id="g2", name="Garage", space_id="s1")
    coordinator = SimpleNamespace(
        account=_account(space),
        get_group=lambda sid, gid: space.groups.get(gid),
    )
    entry = SimpleNamespace(runtime_data=coordinator, entry_id="e1")
    add = MagicMock()
    with patch("custom_components.ajax.alarm_control_panel.connect_new_entity_signal"):
        await acp_mod.async_setup_entry(MagicMock(), entry, add)
    created = add.call_args[0][0]
    # 1 space panel + 2 group panels.
    assert len(created) == 3
    group_panels = [e for e in created if isinstance(e, AjaxGroupAlarmControlPanel)]
    assert len(group_panels) == 2


def test_acp_group_panel_init_name_fallback() -> None:
    coordinator = SimpleNamespace(get_group=lambda sid, gid: None)
    entry = SimpleNamespace(entry_id="e1")
    with patch.object(AjaxGroupAlarmControlPanel, "__init__", AjaxGroupAlarmControlPanel.__init__):
        panel = object.__new__(AjaxGroupAlarmControlPanel)
        # Drive the real __init__ body without CoordinatorEntity wiring.
        with patch(
            "custom_components.ajax.alarm_control_panel.CoordinatorEntity.__init__",
            return_value=None,
        ):
            AjaxGroupAlarmControlPanel.__init__(panel, coordinator, entry, "s1", "g1")
    # No group found -> fallback name.
    assert panel._attr_name == "Group"
    assert panel._attr_unique_id == "e1_group_alarm_g1"


def test_acp_group_panel_init_uses_group_name() -> None:
    group = AjaxGroup(id="g1", name="Living Room", space_id="s1")
    coordinator = SimpleNamespace(get_group=lambda sid, gid: group)
    entry = SimpleNamespace(entry_id="e1")
    panel = object.__new__(AjaxGroupAlarmControlPanel)
    with patch(
        "custom_components.ajax.alarm_control_panel.CoordinatorEntity.__init__",
        return_value=None,
    ):
        AjaxGroupAlarmControlPanel.__init__(panel, coordinator, entry, "s1", "g1")
    assert panel._attr_name == "Living Room"


# ===========================================================================
# update
# ===========================================================================


def _coordinator_with_spaces(*spaces: AjaxSpace):
    data = SimpleNamespace(spaces={s.id: s for s in spaces})
    return SimpleNamespace(
        data=data,
        get_space=lambda sid: data.spaces.get(sid),
        last_update_success=True,
        entry_id="entry_test",
    )


@pytest.mark.asyncio
async def test_update_setup_entry_creates_hub_and_video_edge() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    space.hub_details = {"firmware": {"version": "1.0"}, "hubSubtype": "HUB_2"}
    space.video_edges["ve1"] = AjaxVideoEdge(
        id="ve1", name="Cam", space_id="s1", video_edge_type=VideoEdgeType.TURRET, firmware_version="2.0"
    )
    coordinator = _coordinator_with_spaces(space)
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    with patch("custom_components.ajax.update.connect_new_entity_signal"):
        await update_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_called_once()
    created = add.call_args[0][0]
    assert any(isinstance(e, update_mod.AjaxHubFirmwareUpdate) for e in created)
    assert any(isinstance(e, update_mod.AjaxVideoEdgeFirmwareUpdate) for e in created)


@pytest.mark.asyncio
async def test_update_setup_entry_no_firmware_no_hub_entity() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")  # hub_details empty -> no hub entity
    coordinator = _coordinator_with_spaces(space)
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    with patch("custom_components.ajax.update.connect_new_entity_signal"):
        await update_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


@pytest.mark.asyncio
async def test_update_build_update_pairs() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    space.video_edges["ve1"] = AjaxVideoEdge(
        id="ve1", name="Cam", space_id="s1", video_edge_type=VideoEdgeType.BULLET, firmware_version="2.0"
    )
    coordinator = _coordinator_with_spaces(space)
    entry = SimpleNamespace(runtime_data=coordinator)
    captured: dict = {}

    def _fake_connect(hass, entry_, signal, domain, add, builder, label):
        captured["builder"] = builder

    with patch("custom_components.ajax.update.connect_new_entity_signal", _fake_connect):
        await update_mod.async_setup_entry(MagicMock(), entry, MagicMock())
    builder = captured["builder"]
    pairs = builder("s1", "ve1")
    assert pairs
    uid, entity = pairs[0]
    # Bare dedup key here; the namespaced id lives on _attr_unique_id.
    assert uid == "ve1_firmware_update"
    assert entity._attr_unique_id == "entry_test_ve1_firmware_update"
    assert isinstance(entity, update_mod.AjaxVideoEdgeFirmwareUpdate)
    # Unknown space / video edge -> empty.
    assert builder("ghost", "ve1") == []
    assert builder("s1", "ghost") == []


def test_optimistic_guard_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity check the guard the valve relies on actually expires."""
    device = AjaxDevice(id="d1", name="WS", type=DeviceType.WATERSTOP, space_id="s1", hub_id="hub1")
    device.mark_optimistic("valveState", 0.0)
    monkeypatch.setattr(time, "time", lambda: time.time.__self__ if False else 1e18)  # far future
    assert device.is_optimistic("valveState") is False
