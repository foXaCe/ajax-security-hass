"""Coverage tests for SSEManager event dispatch and per-device handlers.

Complements ``test_sse_manager_handlers.py`` (which covers ``_find_device``
and the door handler) by exercising the full ``_handle_event`` dispatcher,
every per-type handler (motion/smoke/flood/glass/relay/button/wire-input/
device-status/lock/video/doorbell/scenario), deduplication, the alarm-history
helper, language setup, and start/stop lifecycle.

All tests use ``object.__new__(SSEManager)`` plus a hand-built fake
coordinator so no network round-trip or full HA harness is needed.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ajax.event_codes import DEFAULT_LANGUAGE
from custom_components.ajax.models import (
    AjaxDevice,
    AjaxSmartLock,
    AjaxSpace,
    AjaxVideoEdge,
    DeviceType,
    SecurityState,
)
from custom_components.ajax.sse_manager import SSEManager

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _make_manager() -> SSEManager:
    """Build a manager wired to a fully-mocked coordinator (no real asyncio)."""
    mgr = object.__new__(SSEManager)
    mgr._language = DEFAULT_LANGUAGE
    mgr._last_state_update = {}
    mgr._recent_events = {}
    mgr._dedup_window = 5
    mgr._last_discovery_refresh = 0.0
    mgr._pending_timers = set()
    mgr._background_tasks = set()
    mgr._security_event_lock = asyncio.Lock()

    hass = MagicMock()
    hass.config.language = "en"
    # call_later returns a fake handle that can be cancelled.
    hass.loop.call_later = MagicMock(return_value=MagicMock())
    # async_create_task swallows the coroutine so it does not warn.
    hass.async_create_task = MagicMock(side_effect=lambda coro: _consume(coro))

    coordinator = SimpleNamespace(
        hass=hass,
        account=None,
        stats={"events_sse_received": 0, "discovery_refreshes": 0},
        _event_entities={},
        _skipped_state_change_hubs=set(),
        _bypass_cache_next_refresh=False,
        has_pending_ha_action=MagicMock(return_value=False),
        async_set_updated_data=MagicMock(),
        async_request_refresh=AsyncMock(),
        async_force_metadata_refresh=AsyncMock(),
        _create_sqs_notification=AsyncMock(),
        _fire_security_state_event=MagicMock(),
        _update_polling_interval=MagicMock(),
        _async_save_smart_locks=AsyncMock(),
    )
    mgr.coordinator = coordinator
    return mgr


def _consume(coro: object) -> MagicMock:
    """Close a coroutine handed to async_create_task to avoid 'never awaited'."""
    if asyncio.iscoroutine(coro):
        coro.close()
    return MagicMock()


def _space(state: SecurityState = SecurityState.DISARMED) -> AjaxSpace:
    return AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=state)


def _device(device_id: str = "d1", name: str = "Dev", dtype: DeviceType = DeviceType.DOOR_CONTACT) -> AjaxDevice:
    return AjaxDevice(id=device_id, name=name, type=dtype, space_id="s1", hub_id="hub1")


def _attach(mgr: SSEManager, space: AjaxSpace) -> None:
    """Register a space on the coordinator's account so _handle_event resolves it."""
    mgr.coordinator.account = SimpleNamespace(spaces={space.id: space})


# ---------------------------------------------------------------------------
# set_language
# ---------------------------------------------------------------------------


def test_set_language_updates_attribute() -> None:
    mgr = _make_manager()
    mgr.set_language("fr")
    assert mgr._language == "fr"


# ---------------------------------------------------------------------------
# start / stop lifecycle
# ---------------------------------------------------------------------------


async def test_start_wires_callback_and_returns_success() -> None:
    mgr = _make_manager()
    mgr.sse_client = SimpleNamespace(_callback=None, start=AsyncMock(return_value=True))
    result = await mgr.start()
    assert result is True
    # Bound methods compare equal (==) even though identity (is) differs.
    assert mgr.sse_client._callback == mgr._handle_event


async def test_start_returns_false_on_client_failure() -> None:
    mgr = _make_manager()
    mgr.sse_client = SimpleNamespace(_callback=None, start=AsyncMock(return_value=False))
    assert await mgr.start() is False


async def test_stop_cancels_timers_and_awaits_background_tasks() -> None:
    mgr = _make_manager()
    mgr.sse_client = SimpleNamespace(stop=AsyncMock())
    handle = MagicMock()
    mgr._pending_timers.add(handle)

    async def _noop() -> None:
        return None

    task = asyncio.ensure_future(_noop())
    mgr._background_tasks.add(task)

    await mgr.stop()

    handle.cancel.assert_called_once()
    assert mgr._pending_timers == set()
    mgr.sse_client.stop.assert_awaited_once()


# ---------------------------------------------------------------------------
# _schedule_later / _spawn_background
# ---------------------------------------------------------------------------


def test_schedule_later_tracks_and_self_removes_handle() -> None:
    mgr = _make_manager()
    captured: dict[str, object] = {}

    def _fake_call_later(delay: float, cb: object) -> MagicMock:
        captured["cb"] = cb
        return MagicMock()

    mgr.coordinator.hass.loop.call_later = MagicMock(side_effect=_fake_call_later)
    called: list[bool] = []
    mgr._schedule_later(5.0, lambda: called.append(True))
    assert len(mgr._pending_timers) == 1
    # Fire the wrapped callback: it should discard the handle then call user cb.
    captured["cb"]()  # type: ignore[operator]
    assert called == [True]
    assert mgr._pending_timers == set()


async def test_spawn_background_tracks_then_discards_task() -> None:
    mgr = _make_manager()

    async def _coro() -> None:
        return None

    task_holder: list[asyncio.Task[None]] = []

    def _create(coro: object) -> asyncio.Task[None]:
        t = asyncio.ensure_future(coro)  # type: ignore[arg-type]
        task_holder.append(t)
        return t

    mgr.coordinator.hass.async_create_task = MagicMock(side_effect=_create)
    mgr._spawn_background(_coro())
    assert len(mgr._background_tasks) == 1
    # Once the task completes, the done-callback discards it from the set.
    await asyncio.gather(*task_holder)
    assert mgr._background_tasks == set()


# ---------------------------------------------------------------------------
# _handle_event dispatch + guards
# ---------------------------------------------------------------------------


async def test_handle_event_increments_counter() -> None:
    mgr = _make_manager()
    await mgr._handle_event({})
    assert mgr.coordinator.stats["events_sse_received"] == 1


async def test_handle_event_missing_tag_or_hub_returns_early() -> None:
    mgr = _make_manager()
    await mgr._handle_event({"eventTag": "", "hubId": "hub1"})
    await mgr._handle_event({"eventTag": "dooropened"})  # no hubId
    # No account access happened.
    mgr.coordinator.async_set_updated_data.assert_not_called()


async def test_handle_event_warns_when_no_account() -> None:
    mgr = _make_manager()
    mgr.coordinator.account = None
    await mgr._handle_event({"eventTag": "dooropened", "hubId": "hub1"})
    mgr.coordinator.async_set_updated_data.assert_not_called()


async def test_handle_event_unknown_hub_returns_early() -> None:
    mgr = _make_manager()
    _attach(mgr, _space())  # hub_id = hub1
    await mgr._handle_event({"eventTag": "dooropened", "hubId": "OTHER"})
    mgr.coordinator.async_set_updated_data.assert_not_called()


async def test_handle_event_nested_format_dispatches_door() -> None:
    mgr = _make_manager()
    dev = _device()
    space = _space()
    space.devices[dev.id] = dev
    _attach(mgr, space)
    await mgr._handle_event(
        {
            "event": {
                "eventTag": "DoorOpened",
                "hubId": "hub1",
                "device": {"id": "d1", "name": "Dev", "type": "DoorProtect"},
            }
        }
    )
    assert dev.attributes["door_opened"] is True
    mgr.coordinator.async_set_updated_data.assert_called_once()


async def test_handle_event_dedup_drops_second_identical() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.MOTION_DETECTOR)
    space = _space()
    space.devices[dev.id] = dev
    _attach(mgr, space)
    payload = {"eventTag": "motiondetected", "hubId": "hub1", "deviceId": "d1"}
    await mgr._handle_event(payload)
    first_calls = mgr.coordinator.async_set_updated_data.call_count
    await mgr._handle_event(payload)
    # Second one is deduped → no further updated_data call.
    assert mgr.coordinator.async_set_updated_data.call_count == first_calls


async def test_handle_event_group_event_uses_group_id_in_dedup_key() -> None:
    mgr = _make_manager()
    space = _space(SecurityState.ARMED)
    _attach(mgr, space)
    payload = {
        "eventTag": "grouparm",
        "hubId": "hub1",
        "additionalData": {"relatedGroupsInfo": [{"id": "G1"}]},
    }
    await mgr._handle_event(payload)
    key = next(iter(mgr._recent_events))
    assert "G1" in key


async def test_handle_event_cleans_expired_dedup_entries() -> None:
    mgr = _make_manager()
    space = _space()
    space.devices["d1"] = _device(dtype=DeviceType.MOTION_DETECTOR)
    _attach(mgr, space)
    mgr._recent_events["stale"] = time.time() - 120  # well past 60s
    await mgr._handle_event({"eventTag": "motiondetected", "hubId": "hub1", "deviceId": "d1"})
    assert "stale" not in mgr._recent_events


async def test_handle_event_unhandled_tag_logs_and_still_updates() -> None:
    mgr = _make_manager()
    space = _space()
    _attach(mgr, space)
    await mgr._handle_event({"eventTag": "somethingweird", "hubId": "hub1", "deviceId": "x"})
    mgr.coordinator.async_set_updated_data.assert_called_once()


async def test_handle_event_hub_event_logs_info() -> None:
    mgr = _make_manager()
    space = _space()
    _attach(mgr, space)
    await mgr._handle_event({"eventTag": "hubonline", "hubId": "hub1", "deviceId": "hub1"})
    mgr.coordinator.async_set_updated_data.assert_called_once()


async def test_handle_event_swallows_handler_exception() -> None:
    mgr = _make_manager()
    space = _space()
    _attach(mgr, space)
    # Force an exception inside the dispatch by making async_set_updated_data
    # raise *after* a normal handler — actually make the account access blow up.
    mgr.coordinator.async_set_updated_data = MagicMock(side_effect=RuntimeError("boom"))
    # Should not propagate.
    await mgr._handle_event({"eventTag": "hubonline", "hubId": "hub1", "deviceId": "hub1"})


async def test_handle_event_flat_source_fields_resolve() -> None:
    """When no device dict, sourceObjectName/sourceObjectId/sourceObjectType are used."""
    mgr = _make_manager()
    dev = _device(name="Kitchen Motion", dtype=DeviceType.MOTION_DETECTOR)
    space = _space()
    space.devices[dev.id] = dev
    _attach(mgr, space)
    await mgr._handle_event(
        {
            "eventTag": "motiondetected",
            "hubId": "hub1",
            "sourceObjectId": "d1",
            "sourceObjectName": "Kitchen Motion",
            "sourceObjectType": "MotionProtect",
        }
    )
    assert dev.attributes["motion_detected"] is True


# ---------------------------------------------------------------------------
# _handle_security_event
# ---------------------------------------------------------------------------


async def test_security_event_nightmodeoff_updates_state_immediately() -> None:
    mgr = _make_manager()
    space = _space(SecurityState.NIGHT_MODE)
    await mgr._handle_security_event(space, "nightmodeoff", "User", "USER")
    assert space.security_state == SecurityState.DISARMED
    mgr.coordinator._update_polling_interval.assert_called_once()
    mgr.coordinator._create_sqs_notification.assert_awaited_once()
    mgr.coordinator._fire_security_state_event.assert_called_once()


async def test_security_event_unknown_tag_returns_early() -> None:
    mgr = _make_manager()
    space = _space()
    await mgr._handle_security_event(space, "notamappedtag", "User")
    mgr.coordinator._create_sqs_notification.assert_not_awaited()


async def test_security_event_full_arm_triggers_refresh() -> None:
    mgr = _make_manager()
    space = _space(SecurityState.DISARMED)
    await mgr._handle_security_event(space, "arm", "User", "USER")
    mgr.coordinator.async_force_metadata_refresh.assert_awaited_once()
    assert mgr.coordinator._bypass_cache_next_refresh is True
    # Skip flag added then removed.
    assert "hub1" not in mgr.coordinator._skipped_state_change_hubs


async def test_security_event_refresh_failure_applies_fallback_state() -> None:
    mgr = _make_manager()
    space = _space(SecurityState.DISARMED)
    mgr.coordinator.async_force_metadata_refresh = AsyncMock(side_effect=RuntimeError("api down"))
    await mgr._handle_security_event(space, "arm", "User", "USER")
    # Fallback applied the new state because refresh failed and state changed.
    assert space.security_state == SecurityState.ARMED
    assert "hub1" in mgr._last_state_update


async def test_security_event_ha_pending_overrides_source() -> None:
    mgr = _make_manager()
    space = _space(SecurityState.NIGHT_MODE)
    mgr.coordinator.has_pending_ha_action = MagicMock(return_value=True)
    await mgr._handle_security_event(space, "nightmodeoff", "Keypad", "KEYPAD")
    # The fired event should carry the HA-attributed source.
    _, kwargs = mgr.coordinator._fire_security_state_event.call_args
    assert kwargs["source_name"] == "Home Assistant"
    assert kwargs["source_type"] == "HA"


# ---------------------------------------------------------------------------
# _record_alarm_event
# ---------------------------------------------------------------------------


def test_record_alarm_event_inserts_and_caps_history() -> None:
    mgr = _make_manager()
    space = _space()
    # Pre-fill with 12 entries to verify the cap at 10.
    space.recent_events = [{"action": f"old{i}"} for i in range(12)]
    mgr._record_alarm_event(space, "smoke_detected", "Smoke Dev", room_name="Hall")
    assert len(space.recent_events) == 10
    assert space.recent_events[0]["action"] == "smoke_detected"
    assert space.recent_events[0]["is_alarm"] is True
    assert space.recent_events[0]["room_name"] == "Hall"
    # Persistent notification spawned in background.
    mgr.coordinator.hass.async_create_task.assert_called()


def test_record_alarm_event_uses_french_when_ha_language_fr() -> None:
    mgr = _make_manager()
    mgr.coordinator.hass.config.language = "fr"
    space = _space()
    mgr._record_alarm_event(space, "smoke_detected", "Dev")
    assert space.recent_events[0]["message"] == "Fumée détectée"


def test_record_alarm_event_defaults_language_for_unknown() -> None:
    mgr = _make_manager()
    mgr.coordinator.hass.config.language = None
    space = _space()
    mgr._record_alarm_event(space, "smoke_detected", "Dev")
    assert space.recent_events[0]["action"] == "smoke_detected"


# ---------------------------------------------------------------------------
# _handle_motion_event
# ---------------------------------------------------------------------------


def test_motion_event_when_disarmed_sets_attribute_only() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.MOTION_DETECTOR)
    space = _space(SecurityState.DISARMED)
    space.devices[dev.id] = dev
    mgr._handle_motion_event(space, "motiondetected", "Dev", "d1")
    assert dev.attributes["motion_detected"] is True
    assert space.security_state == SecurityState.DISARMED


def test_motion_event_when_armed_triggers_alarm() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.MOTION_DETECTOR)
    space = _space(SecurityState.ARMED)
    space.devices[dev.id] = dev
    mgr._handle_motion_event(space, "motiondetected", "Dev", "d1")
    assert space.security_state == SecurityState.TRIGGERED
    assert space.recent_events  # alarm recorded


def test_motion_event_missing_device_no_crash() -> None:
    mgr = _make_manager()
    space = _space(SecurityState.ARMED)
    mgr._handle_motion_event(space, "motiondetected", "Nope", "unknown")
    assert space.security_state == SecurityState.ARMED


# ---------------------------------------------------------------------------
# _handle_smoke_event
# ---------------------------------------------------------------------------


def test_smoke_event_sets_smoke_attribute_and_triggers() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.SMOKE_DETECTOR)
    space = _space(SecurityState.DISARMED)
    space.devices[dev.id] = dev
    mgr._handle_smoke_event(space, "smokedetected", "Dev", "d1")
    assert dev.attributes["smoke_detected"] is True
    assert space.security_state == SecurityState.TRIGGERED


def test_smoke_event_temperature_branch() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.SMOKE_DETECTOR)
    space = _space(SecurityState.DISARMED)
    space.devices[dev.id] = dev
    mgr._handle_smoke_event(space, "temperatureabovethreshold", "Dev", "d1")
    assert dev.attributes["temperature_alert"] is True


def test_smoke_event_co_branch() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.SMOKE_DETECTOR)
    space = _space(SecurityState.DISARMED)
    space.devices[dev.id] = dev
    mgr._handle_smoke_event(space, "codetected", "Dev", "d1")
    assert dev.attributes["co_detected"] is True


def test_smoke_event_cleared_does_not_trigger() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.SMOKE_DETECTOR)
    space = _space(SecurityState.DISARMED)
    space.devices[dev.id] = dev
    mgr._handle_smoke_event(space, "nosmokedetected", "Dev", "d1")
    assert dev.attributes["smoke_detected"] is False
    assert space.security_state == SecurityState.DISARMED


def test_smoke_event_missing_device_no_crash() -> None:
    mgr = _make_manager()
    space = _space()
    mgr._handle_smoke_event(space, "smokedetected", "Nope", "unknown")


# ---------------------------------------------------------------------------
# _handle_flood_event
# ---------------------------------------------------------------------------


def test_flood_event_triggers_alarm() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.FLOOD_DETECTOR)
    space = _space(SecurityState.DISARMED)
    space.devices[dev.id] = dev
    mgr._handle_flood_event(space, "leakagedetected", "Dev", "d1")
    assert dev.attributes["leak_detected"] is True
    assert space.security_state == SecurityState.TRIGGERED


def test_flood_event_cleared_no_trigger() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.FLOOD_DETECTOR)
    space = _space(SecurityState.DISARMED)
    space.devices[dev.id] = dev
    mgr._handle_flood_event(space, "noleakagedetected", "Dev", "d1")
    assert dev.attributes["leak_detected"] is False
    assert space.security_state == SecurityState.DISARMED


def test_flood_event_missing_device_no_crash() -> None:
    mgr = _make_manager()
    mgr._handle_flood_event(_space(), "leakagedetected", "Nope", "unknown")


# ---------------------------------------------------------------------------
# _handle_glass_event
# ---------------------------------------------------------------------------


def test_glass_event_when_armed_triggers() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.GLASS_BREAK)
    space = _space(SecurityState.ARMED)
    space.devices[dev.id] = dev
    mgr._handle_glass_event(space, "glassbreakdetected", "Dev", "d1")
    assert dev.attributes["glass_break_detected"] is True
    assert space.security_state == SecurityState.TRIGGERED


def test_glass_event_when_disarmed_no_trigger() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.GLASS_BREAK)
    space = _space(SecurityState.DISARMED)
    space.devices[dev.id] = dev
    mgr._handle_glass_event(space, "glassbreakdetected", "Dev", "d1")
    assert dev.attributes["glass_break_detected"] is True
    assert space.security_state == SecurityState.DISARMED


def test_glass_event_missing_device_no_crash() -> None:
    mgr = _make_manager()
    mgr._handle_glass_event(_space(SecurityState.ARMED), "glassbreakdetected", "Nope", "unknown")


# ---------------------------------------------------------------------------
# _handle_device_status_event
# ---------------------------------------------------------------------------


def test_device_status_offline_sets_online_false() -> None:
    mgr = _make_manager()
    dev = _device()
    dev.online = True
    space = _space()
    space.devices[dev.id] = dev
    mgr._handle_device_status_event(space, "offline", "Dev", "d1")
    assert dev.online is False


def test_device_status_online_sets_online_true() -> None:
    mgr = _make_manager()
    dev = _device()
    dev.online = False
    space = _space()
    space.devices[dev.id] = dev
    mgr._handle_device_status_event(space, "online", "Dev", "d1")
    assert dev.online is True


def test_device_status_low_battery_branch() -> None:
    mgr = _make_manager()
    dev = _device()
    space = _space()
    space.devices[dev.id] = dev
    mgr._handle_device_status_event(space, "lowbattery", "Dev", "d1")
    assert dev.attributes["low_battery"] is True


def test_device_status_power_branch() -> None:
    mgr = _make_manager()
    dev = _device()
    space = _space()
    space.devices[dev.id] = dev
    mgr._handle_device_status_event(space, "externalpowerdisconnected", "Dev", "d1")
    # Power lost -> externally_powered is False (the key the socket sensor reads).
    assert dev.attributes["externally_powered"] is False


def test_device_status_missing_device_no_crash() -> None:
    mgr = _make_manager()
    mgr._handle_device_status_event(_space(), "offline", "Nope", "unknown")


# ---------------------------------------------------------------------------
# _handle_relay_event
# ---------------------------------------------------------------------------


def test_relay_event_switched_on() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.RELAY)
    space = _space()
    space.devices[dev.id] = dev
    mgr._handle_relay_event(space, "switchedon", "Dev", "d1")
    assert dev.attributes["is_on"] is True


def test_relay_event_switched_off() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.RELAY)
    space = _space()
    space.devices[dev.id] = dev
    mgr._handle_relay_event(space, "switchedoff", "Dev", "d1")
    assert dev.attributes["is_on"] is False


def test_relay_event_missing_device_no_crash() -> None:
    mgr = _make_manager()
    mgr._handle_relay_event(_space(), "switchedon", "Nope", "unknown")


# ---------------------------------------------------------------------------
# _handle_button_event
# ---------------------------------------------------------------------------


def test_button_event_fires_bus_and_event_entity() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.BUTTON)
    space = _space()
    space.devices[dev.id] = dev
    entity = MagicMock()
    mgr.coordinator._event_entities["d1_button_press"] = entity
    mgr._handle_button_event(space, "buttonsinglepress", "Dev", "d1")
    assert dev.attributes["last_action"] == "single_press"
    mgr.coordinator.hass.bus.async_fire.assert_called_once()
    entity.fire.assert_called_once_with("single_press")


def test_button_event_unknown_tag_returns() -> None:
    mgr = _make_manager()
    space = _space()
    # Not in BUTTON_EVENTS — guarded by .get() None check.
    mgr._handle_button_event(space, "notabutton", "Dev", "d1")
    mgr.coordinator.hass.bus.async_fire.assert_not_called()


def test_button_event_missing_device_returns() -> None:
    mgr = _make_manager()
    mgr._handle_button_event(_space(), "buttonsinglepress", "Nope", "unknown")
    mgr.coordinator.hass.bus.async_fire.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_wire_input_event
# ---------------------------------------------------------------------------


def test_wire_input_triggered_sets_door_opened() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.WIRE_INPUT)
    space = _space()
    space.devices[dev.id] = dev
    mgr._handle_wire_input_event(space, "intrusionalarm", "Dev", "d1", transition="TRIGGERED")
    assert dev.attributes["door_opened"] is True
    assert dev.last_trigger_time is not None


def test_wire_input_recovered_clears() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.WIRE_INPUT)
    space = _space()
    space.devices[dev.id] = dev
    mgr._handle_wire_input_event(space, "intrusionalarm", "Dev", "d1", transition="RECOVERED")
    assert dev.attributes["door_opened"] is False
    assert dev.last_trigger_time is None


def test_wire_input_unknown_tag_returns() -> None:
    mgr = _make_manager()
    space = _space()
    space.devices["d1"] = _device(dtype=DeviceType.WIRE_INPUT)
    mgr._handle_wire_input_event(space, "notawire", "Dev", "d1", transition="")
    assert "door_opened" not in space.devices["d1"].attributes


def test_wire_input_missing_device_returns() -> None:
    mgr = _make_manager()
    mgr._handle_wire_input_event(_space(), "intrusionalarm", "Nope", "unknown", transition="TRIGGERED")


# ---------------------------------------------------------------------------
# _handle_doorbell_event
# ---------------------------------------------------------------------------


def test_doorbell_event_on_regular_device_fires_bus() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.DOORBELL)
    space = _space()
    space.devices[dev.id] = dev
    entity = MagicMock()
    mgr.coordinator._event_entities["d1_doorbell_press"] = entity
    mgr._handle_doorbell_event(space, "Dev", "d1")
    assert dev.attributes["doorbell_ring"] is True
    assert "last_ring" in dev.attributes
    mgr.coordinator.hass.bus.async_fire.assert_called_once()
    entity.fire.assert_called_once_with("ring")
    # A reset timer was scheduled.
    assert len(mgr._pending_timers) == 1


def test_doorbell_event_on_video_edge() -> None:
    mgr = _make_manager()
    space = _space()
    ve = AjaxVideoEdge(id="ve1", name="Cam", space_id="s1")
    space.video_edges[ve.id] = ve
    mgr._handle_doorbell_event(space, "Cam", "ve1")
    assert ve.detections["doorbell_ring"] is True
    mgr.coordinator.hass.bus.async_fire.assert_called_once()


def test_doorbell_event_unknown_device_no_fire() -> None:
    mgr = _make_manager()
    space = _space()
    mgr._handle_doorbell_event(space, "Ghost", "unknown")
    mgr.coordinator.hass.bus.async_fire.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_scenario_event
# ---------------------------------------------------------------------------


def test_scenario_event_with_initiator_fires_bus() -> None:
    mgr = _make_manager()
    space = _space()
    event = {
        "sourceObjectName": "Relay",
        "additionalDataV2": [
            {"additionalDataV2Type": "INITIATOR_INFO", "objectName": "Button A", "objectType": "BUTTON"}
        ],
    }
    mgr._handle_scenario_event(space, event, "relayonbyscenario")
    mgr.coordinator.hass.bus.async_fire.assert_called_once()
    _, payload = mgr.coordinator.hass.bus.async_fire.call_args[0]
    assert payload["scenario_name"] == "Button A"
    assert payload["initiator_type"] == "BUTTON"


def test_scenario_event_without_initiator_returns() -> None:
    mgr = _make_manager()
    space = _space()
    mgr._handle_scenario_event(space, {"additionalDataV2": []}, "relayonbyscenario")
    mgr.coordinator.hass.bus.async_fire.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_lock_event
# ---------------------------------------------------------------------------


def test_lock_event_locks_existing_lock_by_code() -> None:
    mgr = _make_manager()
    space = _space()
    lock = AjaxSmartLock(id="lock1", name="Door Lock", space_id="s1")
    space.smart_locks[lock.id] = lock
    # M_7E_27 → is_locked True per LOCK_EVENT_CODE_STATES.
    mgr._handle_lock_event(
        space, "smartlockunlockedbyuser", "Door Lock", "lock1", "M_7E_27", {"additionalData": {"sourceUserName": "Bob"}}
    )
    assert lock.is_locked is True
    assert lock.last_changed_by == "Bob"
    assert lock.last_event_tag == "smartlockunlockedbyuser"


def test_lock_event_auto_discovers_new_lock() -> None:
    mgr = _make_manager()
    space = _space()
    mgr._handle_lock_event(space, "smartlockunlockedbyuser", "New Lock", "newlock", "M_7E_20", {})
    assert "newlock" in space.smart_locks
    assert space.smart_locks["newlock"].is_locked is False
    # Signal + storage save spawned.
    mgr.coordinator.hass.async_create_task.assert_called()


def test_lock_event_without_source_id_returns() -> None:
    mgr = _make_manager()
    space = _space()
    mgr._handle_lock_event(space, "smartlockunlockedbyuser", "", "", "M_7E_20", {})
    assert space.smart_locks == {}


def test_lock_event_door_open_updates_door_state() -> None:
    mgr = _make_manager()
    space = _space()
    lock = AjaxSmartLock(id="lock1", name="Lock", space_id="s1")
    space.smart_locks[lock.id] = lock
    # M_7E_2E → door open True.
    mgr._handle_lock_event(space, "smartlockdooropen", "Lock", "lock1", "M_7E_2E", {})
    assert lock.is_door_open is True


def test_lock_event_door_left_open_fires_entity() -> None:
    mgr = _make_manager()
    space = _space()
    lock = AjaxSmartLock(id="lock1", name="Lock", space_id="s1")
    space.smart_locks[lock.id] = lock
    entity = MagicMock()
    mgr.coordinator._event_entities["lock1_smart_lock_event"] = entity
    mgr._handle_lock_event(space, "smartlockdoorleftopen", "Lock", "lock1", "", {})
    entity.fire.assert_called_once_with("door_left_open")


def test_lock_event_doorbell_button_fires_bus_and_entity() -> None:
    mgr = _make_manager()
    space = _space()
    lock = AjaxSmartLock(id="lock1", name="Lock", space_id="s1")
    space.smart_locks[lock.id] = lock
    entity = MagicMock()
    mgr.coordinator._event_entities["lock1_smart_lock_event"] = entity
    mgr._handle_lock_event(space, "smartlockdoorbellbuttonpressed", "Lock", "lock1", "", {})
    mgr.coordinator.hass.bus.async_fire.assert_called_once()
    entity.fire.assert_called_once_with("doorbell_pressed")


def test_lock_event_found_by_name() -> None:
    mgr = _make_manager()
    space = _space()
    lock = AjaxSmartLock(id="lock1", name="Named Lock", space_id="s1")
    space.smart_locks[lock.id] = lock
    mgr._handle_lock_event(space, "smartlockunlockedbyuser", "Named Lock", "", "M_7E_20", {})
    assert lock.is_locked is False
    assert lock.last_event_tag == "smartlockunlockedbyuser"


# ---------------------------------------------------------------------------
# _handle_video_event
# ---------------------------------------------------------------------------


def test_video_event_updates_detection_and_schedules_reset() -> None:
    mgr = _make_manager()
    space = _space()
    ve = AjaxVideoEdge(id="ve1", name="Cam", space_id="s1", channels=[{"id": "0", "state": []}])
    space.video_edges[ve.id] = ve
    mgr._handle_video_event(space, "videomotiondetected", "", "Cam", "ve1")
    state = ve.channels[0]["state"]
    assert any(e["type"] == "VIDEO_MOTION" and e["active"] for e in state)
    # Auto-reset timer scheduled.
    assert len(mgr._pending_timers) == 1


def test_video_event_via_event_type_v2() -> None:
    mgr = _make_manager()
    space = _space()
    ve = AjaxVideoEdge(id="ve1", name="Cam", space_id="s1", channels=[{"id": "0", "state": []}])
    space.video_edges[ve.id] = ve
    mgr._handle_video_event(space, "someothertag", "VIDEO_HUMAN", "Cam", "ve1")
    state = ve.channels[0]["state"]
    assert any(e["type"] == "VIDEO_HUMAN" and e["active"] for e in state)


def test_video_event_unknown_detection_type_returns() -> None:
    mgr = _make_manager()
    space = _space()
    ve = AjaxVideoEdge(id="ve1", name="Cam", space_id="s1", channels=[{"id": "0", "state": []}])
    space.video_edges[ve.id] = ve
    mgr._handle_video_event(space, "nope", "NOPE", "Cam", "ve1")
    assert ve.channels[0]["state"] == []
    assert mgr._pending_timers == set()


def test_video_event_unknown_device_returns() -> None:
    mgr = _make_manager()
    space = _space()
    mgr._handle_video_event(space, "videomotiondetected", "", "Ghost", "unknown")
    assert mgr._pending_timers == set()


def test_reset_video_detection_clears_state() -> None:
    mgr = _make_manager()
    space = _space()
    ve = AjaxVideoEdge(
        id="ve1",
        name="Cam",
        space_id="s1",
        channels=[{"id": "0", "state": [{"type": "VIDEO_MOTION", "active": True}]}],
    )
    space.video_edges[ve.id] = ve
    _attach(mgr, space)
    mgr._reset_video_detection("s1", "ve1", "0", "VIDEO_MOTION")
    assert ve.channels[0]["state"][0]["active"] is False
    mgr.coordinator.async_set_updated_data.assert_called_once()


def test_reset_video_detection_no_account_returns() -> None:
    mgr = _make_manager()
    mgr.coordinator.account = None
    # Must not raise.
    mgr._reset_video_detection("s1", "ve1", "0", "VIDEO_MOTION")


def test_reset_video_detection_unknown_space_returns() -> None:
    mgr = _make_manager()
    _attach(mgr, _space())
    mgr._reset_video_detection("nope", "ve1", "0", "VIDEO_MOTION")
    mgr.coordinator.async_set_updated_data.assert_not_called()


def test_reset_video_detection_unknown_edge_returns() -> None:
    mgr = _make_manager()
    space = _space()
    _attach(mgr, space)
    mgr._reset_video_detection("s1", "missing", "0", "VIDEO_MOTION")
    mgr.coordinator.async_set_updated_data.assert_not_called()


# ---------------------------------------------------------------------------
# is_state_protected (extra branch coverage)
# ---------------------------------------------------------------------------


def test_is_state_protected_just_updated() -> None:
    mgr = _make_manager()
    mgr._last_state_update["hub1"] = time.time()
    assert mgr.is_state_protected("hub1") is True


# ---------------------------------------------------------------------------
# _handle_tamper_event
# ---------------------------------------------------------------------------


def test_tamper_event_lid_open_sets_tampered() -> None:
    mgr = _make_manager()
    dev = _device()
    space = _space()
    space.devices[dev.id] = dev
    mgr._handle_tamper_event(space, "lidopen", "Dev", "d1", transition="TRIGGERED")
    assert dev.attributes["tampered"] is True


def test_tamper_event_recovered_clears() -> None:
    mgr = _make_manager()
    dev = _device()
    dev.attributes["tampered"] = True
    space = _space()
    space.devices[dev.id] = dev
    mgr._handle_tamper_event(space, "lidopen", "Dev", "d1", transition="RECOVERED")
    assert dev.attributes["tampered"] is False


def test_tamper_event_missing_device_no_crash() -> None:
    mgr = _make_manager()
    mgr._handle_tamper_event(_space(), "lidopen", "Nope", "unknown", transition="TRIGGERED")


# ---------------------------------------------------------------------------
# _find_device extra strategies
# ---------------------------------------------------------------------------


def test_find_device_wire_input_suffix_match() -> None:
    """8-char source_id matches a device whose 16-char id ends with it."""
    mgr = _make_manager()
    dev = AjaxDevice(id="AAAAAAAA12345678", name="Wire", type=DeviceType.WIRE_INPUT, space_id="s1", hub_id="hub1")
    space = _space()
    space.devices[dev.id] = dev
    assert mgr._find_device(space, source_name="", source_id="12345678") is dev


def test_find_device_name_fallback() -> None:
    mgr = _make_manager()
    dev = _device(name="Special Name")
    space = _space()
    space.devices[dev.id] = dev
    assert mgr._find_device(space, source_name="Special Name", source_id="") is dev


# ---------------------------------------------------------------------------
# _handle_event door alarm path (armed → alarm history) + dispatch fan-out
# ---------------------------------------------------------------------------


async def test_handle_event_door_when_armed_records_alarm() -> None:
    mgr = _make_manager()
    dev = _device()
    space = _space(SecurityState.ARMED)
    space.devices[dev.id] = dev
    _attach(mgr, space)
    # No eventCode → transition defaults to TRIGGERED.
    await mgr._handle_event({"eventTag": "dooropened", "hubId": "hub1", "deviceId": "d1"})
    assert dev.attributes["door_opened"] is True
    # Door opening while armed is an alarm-class event → recorded in history.
    assert space.recent_events
    assert space.recent_events[0]["action"] == "door_opened"


async def test_handle_event_dispatches_smoke() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.SMOKE_DETECTOR)
    space = _space()
    space.devices[dev.id] = dev
    _attach(mgr, space)
    await mgr._handle_event({"eventTag": "smokedetected", "hubId": "hub1", "deviceId": "d1"})
    assert dev.attributes["smoke_detected"] is True


async def test_handle_event_dispatches_flood() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.FLOOD_DETECTOR)
    space = _space()
    space.devices[dev.id] = dev
    _attach(mgr, space)
    await mgr._handle_event({"eventTag": "leakagedetected", "hubId": "hub1", "deviceId": "d1"})
    assert dev.attributes["leak_detected"] is True


async def test_handle_event_dispatches_glass() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.GLASS_BREAK)
    space = _space(SecurityState.ARMED)
    space.devices[dev.id] = dev
    _attach(mgr, space)
    await mgr._handle_event({"eventTag": "glassbreakdetected", "hubId": "hub1", "deviceId": "d1"})
    assert dev.attributes["glass_break_detected"] is True


async def test_handle_event_dispatches_tamper() -> None:
    mgr = _make_manager()
    dev = _device()
    space = _space()
    space.devices[dev.id] = dev
    _attach(mgr, space)
    await mgr._handle_event({"eventTag": "lidopen", "hubId": "hub1", "deviceId": "d1"})
    assert dev.attributes["tampered"] is True


async def test_handle_event_dispatches_device_status() -> None:
    mgr = _make_manager()
    dev = _device()
    dev.online = True
    space = _space()
    space.devices[dev.id] = dev
    _attach(mgr, space)
    await mgr._handle_event({"eventTag": "offline", "hubId": "hub1", "deviceId": "d1"})
    assert dev.online is False


async def test_handle_event_dispatches_relay() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.RELAY)
    space = _space()
    space.devices[dev.id] = dev
    _attach(mgr, space)
    await mgr._handle_event({"eventTag": "switchedon", "hubId": "hub1", "deviceId": "d1"})
    assert dev.attributes["is_on"] is True


async def test_handle_event_dispatches_button() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.BUTTON)
    space = _space()
    space.devices[dev.id] = dev
    _attach(mgr, space)
    await mgr._handle_event({"eventTag": "buttonsinglepress", "hubId": "hub1", "deviceId": "d1"})
    assert dev.attributes["last_action"] == "single_press"


async def test_handle_event_dispatches_wire_input() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.WIRE_INPUT)
    space = _space()
    space.devices[dev.id] = dev
    _attach(mgr, space)
    # No eventCode → transition defaults to TRIGGERED.
    await mgr._handle_event({"eventTag": "intrusionalarm", "hubId": "hub1", "deviceId": "d1"})
    assert dev.attributes["door_opened"] is True


async def test_handle_event_dispatches_scenario() -> None:
    mgr = _make_manager()
    space = _space()
    _attach(mgr, space)
    await mgr._handle_event(
        {
            "eventTag": "relayonbyscenario",
            "hubId": "hub1",
            "deviceId": "d1",
            "additionalDataV2": [
                {"additionalDataV2Type": "INITIATOR_INFO", "objectName": "Btn", "objectType": "BUTTON"}
            ],
        }
    )
    # The scenario bus event is fired (plus the final updated_data signal).
    fired = [c.args[0] for c in mgr.coordinator.hass.bus.async_fire.call_args_list]
    assert "ajax_scenario_triggered" in fired


async def test_handle_event_dispatches_video_via_type_v2() -> None:
    mgr = _make_manager()
    space = _space()
    ve = AjaxVideoEdge(id="ve1", name="Cam", space_id="s1", channels=[{"id": "0", "state": []}])
    space.video_edges[ve.id] = ve
    _attach(mgr, space)
    await mgr._handle_event(
        {"eventTag": "unrelated", "hubId": "hub1", "sourceObjectId": "ve1", "eventTypeV2": "VIDEO_HUMAN"}
    )
    assert any(e["type"] == "VIDEO_HUMAN" for e in ve.channels[0]["state"])


async def test_handle_event_dispatches_doorbell() -> None:
    mgr = _make_manager()
    dev = _device(dtype=DeviceType.DOORBELL)
    space = _space()
    space.devices[dev.id] = dev
    _attach(mgr, space)
    await mgr._handle_event({"eventTag": "doorbellpressed", "hubId": "hub1", "deviceId": "d1"})
    assert dev.attributes["doorbell_ring"] is True


async def test_handle_event_dispatches_lock() -> None:
    mgr = _make_manager()
    space = _space()
    lock = AjaxSmartLock(id="lock1", name="Lock", space_id="s1")
    space.smart_locks[lock.id] = lock
    _attach(mgr, space)
    await mgr._handle_event(
        {"eventTag": "smartlockunlockedbyuser", "hubId": "hub1", "deviceId": "lock1", "eventCode": "M_7E_20"}
    )
    assert lock.is_locked is False


async def test_handle_event_dispatches_security() -> None:
    mgr = _make_manager()
    space = _space(SecurityState.ARMED)
    _attach(mgr, space)
    await mgr._handle_event({"eventTag": "nightmodeoff", "hubId": "hub1", "sourceName": "User"})
    assert space.security_state == SecurityState.DISARMED


# ---------------------------------------------------------------------------
# _reset_video_detection exception path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _handle_door_event transition / missing-device branches (direct)
# ---------------------------------------------------------------------------


def test_door_event_recovered_transition_marks_closed() -> None:
    mgr = _make_manager()
    dev = _device()
    dev.attributes["door_opened"] = True
    space = _space()
    space.devices[dev.id] = dev
    mgr._handle_door_event(space, "dooropened", "Dev", "d1", transition="RECOVERED")
    assert dev.attributes["door_opened"] is False


def test_door_event_missing_device_no_crash() -> None:
    mgr = _make_manager()
    mgr._handle_door_event(_space(), "dooropened", "Ghost", "unknown", transition="TRIGGERED")


def test_reset_video_detection_swallows_exception() -> None:
    mgr = _make_manager()
    space = _space()
    ve = AjaxVideoEdge(id="ve1", name="Cam", space_id="s1", channels=[{"id": "0", "state": []}])
    space.video_edges[ve.id] = ve
    _attach(mgr, space)
    # Make the final updated-data call blow up; the handler must swallow it.
    mgr.coordinator.async_set_updated_data = MagicMock(side_effect=RuntimeError("boom"))
    mgr._reset_video_detection("s1", "ve1", "0", "VIDEO_MOTION")  # must not raise


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
