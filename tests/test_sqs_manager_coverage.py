"""Tests for SQSManager event dispatch and per-handler state mutations.

The SQS manager translates raw SQS event payloads into in-memory coordinator
state changes (security mode, door/motion/alarm/relay/button/doorbell/lock
attributes, video detections). Bugs here surface as silent state drift: an SQS
event arrives but the matching entity never updates.

These tests build a manager via ``object.__new__`` and wire a fake coordinator
(no AWS, no asyncio scheduling beyond what we patch). They exercise:
- ``_handle_event`` dispatcher routing + dedup + missing-field short-circuit
- each ``_handle_*`` handler's happy path and missing-device path
- ``_create_event_record`` (parsed code + tag-fallback)
- ``_create_alarm_notification`` filter gating
- ``_find_space`` / ``_find_device`` / ``_reset_video_detection``
- ``is_state_protected`` window
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ajax.models import (
    AjaxDevice,
    AjaxSmartLock,
    AjaxSpace,
    AjaxVideoEdge,
    DeviceType,
    SecurityState,
)
from custom_components.ajax.sqs_manager import SQSManager

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _make_coordinator() -> MagicMock:
    """Build a fake coordinator with the attributes/methods handlers touch."""
    coord = MagicMock()
    coord.account = MagicMock()
    coord.account.spaces = {}
    coord.stats = {"events_sqs_received": 0, "discovery_refreshes": 0}
    coord._skipped_state_change_hubs = set()
    coord._event_entities = {}
    coord.config_entry = SimpleNamespace(options={})
    # Async coordinator hooks
    coord.has_pending_ha_action = MagicMock(return_value=False)
    coord.async_force_metadata_refresh = AsyncMock()
    coord._create_sqs_notification = AsyncMock()
    coord._async_save_smart_locks = AsyncMock()
    coord._fire_security_state_event = MagicMock()
    coord._update_polling_interval = MagicMock()
    coord.async_set_updated_data = MagicMock()
    coord._escape_markdown = lambda s: s
    coord.async_request_refresh = AsyncMock()
    # hass loop / bus / task plumbing
    coord.hass = MagicMock()
    coord.hass.loop.call_later = MagicMock(return_value=MagicMock())

    # Close any coroutine handed to async_create_task so it doesn't leak a
    # "coroutine was never awaited" RuntimeWarning when handlers spawn
    # background work (e.g. _async_save_smart_locks / async_request_refresh).
    create_task = MagicMock()

    def _consume(coro=None, *args, **kwargs):
        if coro is not None and hasattr(coro, "close"):
            coro.close()
        return MagicMock()

    create_task.side_effect = _consume
    coord.hass.async_create_task = create_task
    coord.hass.bus.async_fire = MagicMock()
    return coord


def _make_manager() -> SQSManager:
    """Build an SQSManager bypassing __init__ (no AWS / no asyncio loop)."""
    mgr = object.__new__(SQSManager)
    mgr.coordinator = _make_coordinator()
    mgr.sqs_client = MagicMock()
    mgr._enabled = True
    mgr._last_event_time = 0.0
    mgr._last_state_update = {}
    mgr._recent_event_ids = {}
    mgr._language = "en"
    mgr._last_discovery_refresh = 0.0
    mgr._pending_timers = set()
    mgr._background_tasks = set()
    mgr._security_event_lock = asyncio.Lock()
    return mgr


def _space(state: SecurityState = SecurityState.DISARMED) -> AjaxSpace:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=state)
    return space


def _with_space(mgr: SQSManager, space: AjaxSpace) -> AjaxSpace:
    """Register a space on the coordinator account so _find_space sees it."""
    mgr.coordinator.account.spaces = {space.id: space}
    return space


def _device(
    device_id: str = "d1",
    name: str = "Front Door",
    dtype: DeviceType = DeviceType.DOOR_CONTACT,
) -> AjaxDevice:
    return AjaxDevice(id=device_id, name=name, type=dtype, space_id="s1", hub_id="hub1")


def _event(**overrides) -> dict:
    """Build a minimal SQS event_data envelope."""
    event = {
        "eventTag": "DoorOpened",
        "eventTypeV2": "SECURITY",
        "eventCode": "",
        "hubId": "hub1",
        "hubName": "Hub",
        "sourceObjectName": "Front Door",
        "sourceObjectType": "DOOR_PROTECT",
        "sourceObjectId": "d1",
        "sourceRoomName": "Hall",
        "timestamp": 1700000000000,
        "transition": "",
        "additionalDataV2": [],
    }
    event.update(overrides)
    return {"event": event}


# ---------------------------------------------------------------------------
# _find_space
# ---------------------------------------------------------------------------


def test_find_space_by_hub_id() -> None:
    mgr = _make_manager()
    space = _with_space(mgr, _space())
    assert mgr._find_space("hub1") is space


def test_find_space_by_space_id() -> None:
    mgr = _make_manager()
    space = _space()
    space.hub_id = None
    _with_space(mgr, space)
    assert mgr._find_space("s1") is space


def test_find_space_not_found() -> None:
    mgr = _make_manager()
    _with_space(mgr, _space())
    assert mgr._find_space("nope") is None


def test_find_space_no_account() -> None:
    mgr = _make_manager()
    mgr.coordinator.account = None
    assert mgr._find_space("hub1") is None


# ---------------------------------------------------------------------------
# _is_duplicate_event
# ---------------------------------------------------------------------------


def test_is_duplicate_event_first_then_duplicate() -> None:
    mgr = _make_manager()
    assert mgr._is_duplicate_event("k1") is False
    assert mgr._is_duplicate_event("k1") is True


def test_is_duplicate_event_purges_old_entries() -> None:
    mgr = _make_manager()
    mgr._recent_event_ids = {"old": time.time() - 100}
    assert mgr._is_duplicate_event("new") is False
    # Stale entry must have been purged during the cleanup pass.
    assert "old" not in mgr._recent_event_ids


# ---------------------------------------------------------------------------
# _find_device
# ---------------------------------------------------------------------------


def test_find_device_by_exact_id() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device()
    space.devices["d1"] = dev
    assert mgr._find_device(space, "", "d1") is dev


def test_find_device_by_suffix() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device(device_id="ABCDEFGH12345678", dtype=DeviceType.WIRE_INPUT)
    space.devices[dev.id] = dev
    assert mgr._find_device(space, "", "12345678") is dev


def test_find_device_by_prefix() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device(device_id="12345678ABCDEFGH", dtype=DeviceType.WIRE_INPUT)
    space.devices[dev.id] = dev
    assert mgr._find_device(space, "", "12345678") is dev


def test_find_device_by_name_fallback() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device(name="Garage")
    space.devices[dev.id] = dev
    assert mgr._find_device(space, "Garage", "") is dev


def test_find_device_not_found_triggers_discovery() -> None:
    mgr = _make_manager()
    space = _space()
    assert mgr._find_device(space, "Ghost", "zzzzzzzz") is None
    mgr.coordinator.hass.async_create_task.assert_called_once()


# ---------------------------------------------------------------------------
# is_state_protected
# ---------------------------------------------------------------------------


def test_is_state_protected_true_within_window() -> None:
    mgr = _make_manager()
    mgr._last_state_update["hub1"] = time.time() - 2
    assert mgr.is_state_protected("hub1") is True


def test_is_state_protected_false_outside_window() -> None:
    mgr = _make_manager()
    mgr._last_state_update["hub1"] = time.time() - 100
    assert mgr.is_state_protected("hub1") is False


def test_is_state_protected_false_never_updated() -> None:
    assert _make_manager().is_state_protected("hub1") is False


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


def test_is_enabled_property() -> None:
    mgr = _make_manager()
    assert mgr.is_enabled is True
    mgr._enabled = False
    assert mgr.is_enabled is False


def test_last_event_time_property() -> None:
    mgr = _make_manager()
    mgr._last_event_time = 42.0
    assert mgr.last_event_time == 42.0


# ---------------------------------------------------------------------------
# _create_event_record
# ---------------------------------------------------------------------------


def test_create_event_record_with_parsed_code() -> None:
    """A valid M_XX_YY code drives action/message/is_alarm/category."""
    mgr = _make_manager()
    rec = mgr._create_event_record(
        event_tag="dooropened",
        event_type="SECURITY",
        event_code="M_01_20",
        source_name="Front Door",
        source_type="DOOR_PROTECT",
        source_id="d1",
        room_name="Hall",
        hub_name="Hub",
        timestamp=1700000000000,
        transition="TRIGGERED",
    )
    assert rec["event_code"] == "M_01_20"
    assert rec["source_name"] == "Front Door"
    # Parsed code path populates a non-default category/action.
    assert "action" in rec and rec["action"]


def test_create_event_record_tag_fallback() -> None:
    """No event code → fall back to event-tag dict mapping (door_opened, alarm)."""
    mgr = _make_manager()
    rec = mgr._create_event_record(
        event_tag="dooropened",
        event_type="",
        event_code="",
        source_name="Front Door",
        source_type="DOOR_PROTECT",
        source_id="d1",
        room_name="",
        hub_name="",
        timestamp=0,
        transition="",
    )
    assert rec["action"] == "door_opened"
    assert rec["is_alarm"] is True
    # timestamp=0 → falls back to "now" (a datetime), not epoch.
    assert rec["timestamp"] is not None


def test_create_event_record_smoke_alarm_flag() -> None:
    mgr = _make_manager()
    rec = mgr._create_event_record(
        event_tag="smokedetected",
        event_type="ALARM",
        event_code="",
        source_name="Kitchen",
        source_type="FIRE_PROTECT",
        source_id="d2",
        room_name="Kitchen",
        hub_name="Hub",
        timestamp=1700000000000,
        transition="TRIGGERED",
    )
    assert rec["action"] == "smoke_detected"
    assert rec["is_alarm"] is True


# ---------------------------------------------------------------------------
# _handle_security_event
# ---------------------------------------------------------------------------


async def test_handle_security_event_arm_refreshes_and_fires(monkeypatch) -> None:
    mgr = _make_manager()
    # Bypass the 1.0s sleep inside the lock.
    monkeypatch.setattr("custom_components.ajax.sqs_manager.asyncio.sleep", AsyncMock())
    space = _space(SecurityState.DISARMED)
    result = await mgr._handle_security_event(space, "arm", "John", "USER")
    assert result is True
    assert space.security_state == SecurityState.ARMED
    mgr.coordinator.async_force_metadata_refresh.assert_awaited_once()
    mgr.coordinator._create_sqs_notification.assert_awaited_once()
    mgr.coordinator._fire_security_state_event.assert_called_once()


async def test_handle_security_event_disarm_clears_skip_flag(monkeypatch) -> None:
    mgr = _make_manager()
    monkeypatch.setattr("custom_components.ajax.sqs_manager.asyncio.sleep", AsyncMock())
    space = _space(SecurityState.ARMED)
    await mgr._handle_security_event(space, "disarm", "John", "USER")
    assert space.security_state == SecurityState.DISARMED
    # Skip flag must be cleared in the finally block.
    assert "hub1" not in mgr.coordinator._skipped_state_change_hubs


async def test_handle_security_event_unknown_tag_returns_false() -> None:
    mgr = _make_manager()
    space = _space()
    assert await mgr._handle_security_event(space, "bogus", "John") is False


async def test_handle_security_event_ha_pending_skips_state_update(monkeypatch) -> None:
    """When HA initiated the action, the optimistic state must not be overwritten."""
    mgr = _make_manager()
    monkeypatch.setattr("custom_components.ajax.sqs_manager.asyncio.sleep", AsyncMock())
    mgr.coordinator.has_pending_ha_action = MagicMock(return_value=True)
    space = _space(SecurityState.DISARMED)
    space.security_state = SecurityState.ARMED  # already optimistically armed
    await mgr._handle_security_event(space, "arm", "John", "USER")
    # State stays as optimistically set, notification still fires.
    mgr.coordinator._create_sqs_notification.assert_awaited_once()


async def test_handle_security_event_night_mode_no_refresh() -> None:
    """night mode is neither group nor full arm/disarm → no metadata refresh."""
    mgr = _make_manager()
    space = _space(SecurityState.DISARMED)
    result = await mgr._handle_security_event(space, "nightmodeon", "John", "USER")
    assert result is True
    assert space.security_state == SecurityState.NIGHT_MODE
    mgr.coordinator.async_force_metadata_refresh.assert_not_awaited()


# ---------------------------------------------------------------------------
# _handle_door_event
# ---------------------------------------------------------------------------


async def test_handle_door_event_opened() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device()
    space.devices["d1"] = dev
    assert await mgr._handle_door_event(space, "dooropened", "Front Door", "d1", "") is True
    assert dev.attributes["door_opened"] is True
    assert dev.last_trigger_time is not None


async def test_handle_extcontact_opened_updates_external_contact_not_door() -> None:
    """extcontact* drives the External Contact entity, not the reed Door (issue #151)."""
    mgr = _make_manager()
    space = _space()
    dev = _device()
    space.devices["d1"] = dev
    assert await mgr._handle_door_event(space, "extcontactopened", "Front Door", "d1", "") is True
    assert dev.attributes["external_contact_opened"] is True
    assert "door_opened" not in dev.attributes


async def test_handle_extcontact_closed_clears_external_contact() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device()
    dev.attributes["external_contact_opened"] = True
    space.devices["d1"] = dev
    await mgr._handle_door_event(space, "extcontactclosed", "Front Door", "d1", "")
    assert dev.attributes["external_contact_opened"] is False


async def test_handle_door_event_recovered_transition_closes() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device()
    dev.attributes["door_opened"] = True
    space.devices["d1"] = dev
    await mgr._handle_door_event(space, "dooropened", "Front Door", "d1", "RECOVERED")
    assert dev.attributes["door_opened"] is False
    assert dev.last_trigger_time is None


async def test_handle_door_event_triggered_transition_opens() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device()
    space.devices["d1"] = dev
    await mgr._handle_door_event(space, "doorclosed", "Front Door", "d1", "TRIGGERED")
    assert dev.attributes["door_opened"] is True


async def test_handle_door_event_unknown_tag_returns_false() -> None:
    mgr = _make_manager()
    space = _space()
    assert await mgr._handle_door_event(space, "bogus", "X", "d1", "") is False


async def test_handle_door_event_missing_device() -> None:
    mgr = _make_manager()
    space = _space()
    assert await mgr._handle_door_event(space, "dooropened", "Ghost", "zzzzzzzz", "") is False


# ---------------------------------------------------------------------------
# _handle_motion_event
# ---------------------------------------------------------------------------


async def test_handle_motion_event_detected_no_alarm_when_disarmed() -> None:
    mgr = _make_manager()
    space = _space(SecurityState.DISARMED)
    dev = _device(dtype=DeviceType.MOTION_DETECTOR)
    space.devices["d1"] = dev
    assert await mgr._handle_motion_event(space, "motiondetected", "Front Door", "d1") is True
    assert dev.attributes["motion_detected"] is True
    assert space.security_state == SecurityState.DISARMED


async def test_handle_motion_event_triggers_alarm_when_armed() -> None:
    mgr = _make_manager()
    space = _space(SecurityState.ARMED)
    dev = _device(dtype=DeviceType.MOTION_DETECTOR)
    space.devices["d1"] = dev
    await mgr._handle_motion_event(space, "motiondetected", "Front Door", "d1")
    assert space.security_state == SecurityState.TRIGGERED


async def test_handle_motion_event_cleared() -> None:
    mgr = _make_manager()
    space = _space(SecurityState.DISARMED)
    dev = _device(dtype=DeviceType.MOTION_DETECTOR)
    space.devices["d1"] = dev
    await mgr._handle_motion_event(space, "nomotiondetected", "Front Door", "d1")
    assert dev.attributes["motion_detected"] is False
    assert dev.last_trigger_time is None


async def test_handle_motion_event_unknown_tag() -> None:
    mgr = _make_manager()
    assert await mgr._handle_motion_event(_space(), "bogus", "X", "d1") is False


async def test_handle_motion_event_missing_device() -> None:
    mgr = _make_manager()
    assert await mgr._handle_motion_event(_space(), "motiondetected", "Ghost", "zzzzzzzz") is False


# ---------------------------------------------------------------------------
# _handle_alarm_event
# ---------------------------------------------------------------------------


async def test_handle_alarm_event_smoke_triggers_always() -> None:
    mgr = _make_manager()
    space = _space(SecurityState.DISARMED)
    dev = _device(dtype=DeviceType.SMOKE_DETECTOR)
    space.devices["d1"] = dev
    await mgr._handle_alarm_event(space, "smoke", "smokedetected", "Front Door", "d1")
    assert dev.attributes["smoke_alarm"] is True
    assert space.security_state == SecurityState.TRIGGERED


async def test_handle_alarm_event_flood_triggers_always() -> None:
    mgr = _make_manager()
    space = _space(SecurityState.DISARMED)
    dev = _device(dtype=DeviceType.FLOOD_DETECTOR)
    space.devices["d1"] = dev
    await mgr._handle_alarm_event(space, "flood", "leakagedetected", "Front Door", "d1")
    assert dev.attributes["flood_alarm"] is True
    assert space.security_state == SecurityState.TRIGGERED


async def test_handle_alarm_event_glass_only_when_armed() -> None:
    mgr = _make_manager()
    space = _space(SecurityState.DISARMED)
    dev = _device(dtype=DeviceType.GLASS_BREAK)
    space.devices["d1"] = dev
    await mgr._handle_alarm_event(space, "glass", "glassbreakdetected", "Front Door", "d1")
    # Disarmed → glass break does NOT trigger.
    assert space.security_state == SecurityState.DISARMED
    assert dev.attributes["glass_alarm"] is True


async def test_handle_alarm_event_glass_triggers_when_armed() -> None:
    mgr = _make_manager()
    space = _space(SecurityState.ARMED)
    dev = _device(dtype=DeviceType.GLASS_BREAK)
    space.devices["d1"] = dev
    await mgr._handle_alarm_event(space, "glass", "glassbreakdetected", "Front Door", "d1")
    assert space.security_state == SecurityState.TRIGGERED


async def test_handle_alarm_event_clear_resets_trigger_time() -> None:
    mgr = _make_manager()
    space = _space(SecurityState.DISARMED)
    dev = _device(dtype=DeviceType.SMOKE_DETECTOR)
    space.devices["d1"] = dev
    await mgr._handle_alarm_event(space, "smoke", "nosmokedetected", "Front Door", "d1")
    assert dev.attributes["smoke_alarm"] is False
    assert dev.last_trigger_time is None


async def test_handle_alarm_event_missing_device() -> None:
    mgr = _make_manager()
    assert await mgr._handle_alarm_event(_space(), "smoke", "smokedetected", "Ghost", "zzzzzzzz") is False


# ---------------------------------------------------------------------------
# _handle_relay_event
# ---------------------------------------------------------------------------


async def test_handle_relay_event_on() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device(dtype=DeviceType.RELAY)
    space.devices["d1"] = dev
    assert await mgr._handle_relay_event(space, "switchedon", "Front Door", "d1") is True
    assert dev.attributes["is_on"] is True


async def test_handle_relay_event_off() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device(dtype=DeviceType.RELAY)
    space.devices["d1"] = dev
    await mgr._handle_relay_event(space, "switchedoff", "Front Door", "d1")
    assert dev.attributes["is_on"] is False


async def test_handle_relay_event_unknown_tag() -> None:
    mgr = _make_manager()
    assert await mgr._handle_relay_event(_space(), "bogus", "X", "d1") is False


async def test_handle_relay_event_missing_device() -> None:
    mgr = _make_manager()
    assert await mgr._handle_relay_event(_space(), "switchedon", "Ghost", "zzzzzzzz") is False


# ---------------------------------------------------------------------------
# _handle_wire_input_event
# ---------------------------------------------------------------------------


async def test_handle_wire_input_event_triggered() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device(dtype=DeviceType.WIRE_INPUT)
    space.devices["d1"] = dev
    assert await mgr._handle_wire_input_event(space, "intrusionalarm", "Front Door", "d1", "TRIGGERED") is True
    assert dev.attributes["door_opened"] is True
    assert dev.last_trigger_time is not None


async def test_handle_wire_input_event_recovered() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device(dtype=DeviceType.WIRE_INPUT)
    space.devices["d1"] = dev
    await mgr._handle_wire_input_event(space, "s1alarm", "Front Door", "d1", "RECOVERED")
    assert dev.attributes["door_opened"] is False
    assert dev.last_trigger_time is None


async def test_handle_wire_input_event_unknown_tag() -> None:
    mgr = _make_manager()
    assert await mgr._handle_wire_input_event(_space(), "bogus", "X", "d1", "") is False


async def test_handle_wire_input_event_missing_device() -> None:
    mgr = _make_manager()
    assert await mgr._handle_wire_input_event(_space(), "intrusionalarm", "Ghost", "zzzzzzzz", "") is False


# ---------------------------------------------------------------------------
# _handle_button_event
# ---------------------------------------------------------------------------


async def test_handle_button_event_fires_bus_and_entity() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device(dtype=DeviceType.BUTTON)
    space.devices["d1"] = dev
    event_entity = MagicMock()
    mgr.coordinator._event_entities = {"d1_button_press": event_entity}
    assert await mgr._handle_button_event(space, "buttonpressed", "Front Door", "d1") is True
    assert dev.attributes["last_action"] == "single_press"
    mgr.coordinator.hass.bus.async_fire.assert_called_once()
    event_entity.fire.assert_called_once_with("single_press")


async def test_handle_button_event_no_entity() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device(dtype=DeviceType.BUTTON)
    space.devices["d1"] = dev
    assert await mgr._handle_button_event(space, "panicbuttonpressed", "Front Door", "d1") is True
    assert dev.attributes["last_action"] == "panic"


async def test_handle_button_event_unknown_tag() -> None:
    mgr = _make_manager()
    assert await mgr._handle_button_event(_space(), "bogus", "X", "d1") is False


async def test_handle_button_event_missing_device() -> None:
    mgr = _make_manager()
    assert await mgr._handle_button_event(_space(), "buttonpressed", "Ghost", "zzzzzzzz") is False


# ---------------------------------------------------------------------------
# _handle_doorbell_event
# ---------------------------------------------------------------------------


async def test_handle_doorbell_event_on_regular_device() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device(name="Bell", dtype=DeviceType.DOORBELL)
    space.devices["d1"] = dev
    event_entity = MagicMock()
    mgr.coordinator._event_entities = {"d1_doorbell_press": event_entity}
    assert await mgr._handle_doorbell_event(space, "doorbellpressed", "Bell", "d1") is True
    assert dev.attributes["doorbell_ring"] is True
    mgr.coordinator.hass.bus.async_fire.assert_called_once()
    event_entity.fire.assert_called_once_with("ring")
    # An auto-reset timer must have been scheduled.
    mgr.coordinator.hass.loop.call_later.assert_called_once()


async def test_handle_doorbell_event_on_video_edge() -> None:
    mgr = _make_manager()
    space = _space()
    ve = AjaxVideoEdge(id="ve1", name="Door Cam", space_id="s1")
    space.video_edges["ve1"] = ve
    assert await mgr._handle_doorbell_event(space, "doorbellpressed", "Door Cam", "ve1") is True
    assert ve.detections["doorbell_ring"] is True
    mgr.coordinator.hass.bus.async_fire.assert_called_once()


async def test_handle_doorbell_event_missing_device() -> None:
    mgr = _make_manager()
    assert await mgr._handle_doorbell_event(_space(), "doorbellpressed", "Ghost", "zzzzzzzz") is False


# ---------------------------------------------------------------------------
# _handle_scenario_event
# ---------------------------------------------------------------------------


async def test_handle_scenario_event_fires_with_initiator() -> None:
    mgr = _make_manager()
    space = _space()
    additional = [
        {
            "additionalDataV2Type": "INITIATOR_INFO",
            "objectName": "Living Button",
            "objectType": "BUTTON",
        }
    ]
    assert await mgr._handle_scenario_event(space, "scenarioexecuted", "Relay", additional) is True
    mgr.coordinator.hass.bus.async_fire.assert_called_once()
    args = mgr.coordinator.hass.bus.async_fire.call_args
    assert args.args[0] == "ajax_scenario_triggered"
    assert args.args[1]["scenario_name"] == "Living Button"


async def test_handle_scenario_event_no_initiator() -> None:
    mgr = _make_manager()
    assert await mgr._handle_scenario_event(_space(), "scenarioexecuted", "Relay", []) is False
    mgr.coordinator.hass.bus.async_fire.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_device_status_event
# ---------------------------------------------------------------------------


async def test_handle_device_status_offline() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device()
    dev.online = True
    space.devices["d1"] = dev
    assert await mgr._handle_device_status_event(space, "offline", "Front Door", "d1") is True
    assert dev.online is False


async def test_handle_device_status_online() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device()
    dev.online = False
    space.devices["d1"] = dev
    await mgr._handle_device_status_event(space, "online", "Front Door", "d1")
    assert dev.online is True


async def test_handle_device_status_low_battery() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device()
    space.devices["d1"] = dev
    await mgr._handle_device_status_event(space, "lowbattery", "Front Door", "d1")
    assert dev.attributes["low_battery"] is True


async def test_handle_device_status_battery_charged() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device()
    dev.attributes["low_battery"] = True
    space.devices["d1"] = dev
    await mgr._handle_device_status_event(space, "batterycharged", "Front Door", "d1")
    assert dev.attributes["low_battery"] is False


async def test_handle_device_status_tamper() -> None:
    mgr = _make_manager()
    space = _space()
    dev = _device()
    space.devices["d1"] = dev
    await mgr._handle_device_status_event(space, "lidopen", "Front Door", "d1")
    assert dev.attributes["tampered"] is True


async def test_handle_device_status_missing_device() -> None:
    mgr = _make_manager()
    assert await mgr._handle_device_status_event(_space(), "offline", "Ghost", "zzzzzzzz") is False


# ---------------------------------------------------------------------------
# _handle_video_event
# ---------------------------------------------------------------------------


async def test_handle_video_event_by_tag() -> None:
    mgr = _make_manager()
    space = _space()
    ve = AjaxVideoEdge(id="ve1", name="Cam", space_id="s1")
    space.video_edges["ve1"] = ve
    assert await mgr._handle_video_event(space, "videohumandetected", "", "Cam", "ve1") is True
    # Detection recorded on the channel state.
    state = ve.channels[0]["state"]
    assert {"type": "VIDEO_HUMAN", "active": True} in state
    # Auto-reset timer scheduled.
    mgr.coordinator.hass.loop.call_later.assert_called_once()


async def test_handle_video_event_by_event_type() -> None:
    mgr = _make_manager()
    space = _space()
    ve = AjaxVideoEdge(id="ve1", name="Cam", space_id="s1")
    space.video_edges["ve1"] = ve
    assert await mgr._handle_video_event(space, "unknowntag", "VIDEO_MOTION", "Cam", "ve1") is True


async def test_handle_video_event_unknown_detection() -> None:
    mgr = _make_manager()
    space = _space()
    assert await mgr._handle_video_event(space, "bogus", "BOGUS", "Cam", "ve1") is False


async def test_handle_video_event_missing_video_edge() -> None:
    mgr = _make_manager()
    space = _space()
    assert await mgr._handle_video_event(space, "videohumandetected", "", "Ghost", "nope") is False


# ---------------------------------------------------------------------------
# _handle_lock_event
# ---------------------------------------------------------------------------


async def test_handle_lock_event_unlock_existing() -> None:
    mgr = _make_manager()
    space = _space()
    lock = AjaxSmartLock(id="lock1", name="Front Lock", space_id="s1", is_locked=True)
    space.smart_locks["lock1"] = lock
    event = {"additionalData": {"sourceUserName": "Alice"}}
    assert (
        await mgr._handle_lock_event(space, "smartlockunlockedbyuser", "Front Lock", "lock1", "M_7E_23", event) is True
    )
    assert lock.is_locked is False
    assert lock.last_changed_by == "Alice"
    assert lock.last_event_tag == "smartlockunlockedbyuser"


async def test_handle_lock_event_lock_by_code() -> None:
    mgr = _make_manager()
    space = _space()
    lock = AjaxSmartLock(id="lock1", name="Front Lock", space_id="s1", is_locked=False)
    space.smart_locks["lock1"] = lock
    await mgr._handle_lock_event(space, "smartlockmodulelockedautomatically", "Front Lock", "lock1", "M_7E_29", {})
    assert lock.is_locked is True


async def test_handle_lock_event_door_open() -> None:
    mgr = _make_manager()
    space = _space()
    lock = AjaxSmartLock(id="lock1", name="Front Lock", space_id="s1")
    space.smart_locks["lock1"] = lock
    await mgr._handle_lock_event(space, "smartlockdooropen", "Front Lock", "lock1", "M_7E_2E", {})
    assert lock.is_door_open is True


async def test_handle_lock_event_door_left_open_fires_entity() -> None:
    mgr = _make_manager()
    space = _space()
    lock = AjaxSmartLock(id="lock1", name="Front Lock", space_id="s1")
    space.smart_locks["lock1"] = lock
    event_entity = MagicMock()
    mgr.coordinator._event_entities = {"lock1_smart_lock_event": event_entity}
    await mgr._handle_lock_event(space, "smartlockdoorleftopen", "Front Lock", "lock1", "M_7E_37", {})
    event_entity.fire.assert_called_once_with("door_left_open")


async def test_handle_lock_event_doorbell_pressed() -> None:
    mgr = _make_manager()
    space = _space()
    lock = AjaxSmartLock(id="lock1", name="Front Lock", space_id="s1")
    space.smart_locks["lock1"] = lock
    event_entity = MagicMock()
    mgr.coordinator._event_entities = {"lock1_smart_lock_event": event_entity}
    await mgr._handle_lock_event(space, "smartlockdoorbellbuttonpressed", "Front Lock", "lock1", "", {})
    mgr.coordinator.hass.bus.async_fire.assert_called_once()
    event_entity.fire.assert_called_once_with("doorbell_pressed")


async def test_handle_lock_event_autocreate_from_event() -> None:
    """An unknown smart lock with a source_id is auto-discovered and dispatched."""
    mgr = _make_manager()
    space = _space()
    result = await mgr._handle_lock_event(space, "smartlockunlockedbycode", "New Lock", "newlock", "M_7E_21", {})
    assert result is True
    assert "newlock" in space.smart_locks
    mgr.coordinator.hass.async_create_task.assert_called_once()


async def test_handle_lock_event_no_source_id_returns_false() -> None:
    mgr = _make_manager()
    space = _space()
    assert await mgr._handle_lock_event(space, "smartlockunlockedbyuser", "Lock", "", "", {}) is False


async def test_handle_lock_event_match_by_name() -> None:
    mgr = _make_manager()
    space = _space()
    lock = AjaxSmartLock(id="lock1", name="Front Lock", space_id="s1", is_locked=True)
    space.smart_locks["lock1"] = lock
    # source_id empty → must fall back to name match.
    await mgr._handle_lock_event(space, "smartlockunlockedbyknob", "Front Lock", "", "M_7E_20", {})
    assert lock.is_locked is False


# ---------------------------------------------------------------------------
# _reset_video_detection
# ---------------------------------------------------------------------------


def test_reset_video_detection_clears_state() -> None:
    mgr = _make_manager()
    space = _space()
    ve = AjaxVideoEdge(id="ve1", name="Cam", space_id="s1")
    ve.channels = [{"id": "0", "state": [{"type": "VIDEO_HUMAN", "active": True}]}]
    space.video_edges["ve1"] = ve
    mgr.coordinator.account.spaces = {"s1": space}
    mgr._reset_video_detection("s1", "ve1", "0", "VIDEO_HUMAN")
    assert ve.channels[0]["state"][0]["active"] is False
    mgr.coordinator.async_set_updated_data.assert_called_once()


def test_reset_video_detection_no_account() -> None:
    mgr = _make_manager()
    mgr.coordinator.account = None
    # Must not raise.
    mgr._reset_video_detection("s1", "ve1", "0", "VIDEO_HUMAN")


def test_reset_video_detection_unknown_space() -> None:
    mgr = _make_manager()
    mgr.coordinator.account.spaces = {}
    mgr._reset_video_detection("nope", "ve1", "0", "VIDEO_HUMAN")


def test_reset_video_detection_unknown_video_edge() -> None:
    mgr = _make_manager()
    space = _space()
    mgr.coordinator.account.spaces = {"s1": space}
    mgr._reset_video_detection("s1", "nope", "0", "VIDEO_HUMAN")


# ---------------------------------------------------------------------------
# _create_alarm_notification
# ---------------------------------------------------------------------------


async def test_create_alarm_notification_default(monkeypatch) -> None:
    mgr = _make_manager()
    space = _space()
    created = MagicMock()
    monkeypatch.setattr(
        "homeassistant.components.persistent_notification.async_create",
        created,
    )
    rec = {
        "source_name": "Kitchen",
        "room_name": "Kitchen",
        "message": "Smoke detected",
        "event_code": "M_03_20",
        "action": "smoke_detected",
    }
    await mgr._create_alarm_notification(space, rec)
    created.assert_called_once()


async def test_create_alarm_notification_persistent_disabled(monkeypatch) -> None:
    mgr = _make_manager()
    mgr.coordinator.config_entry = SimpleNamespace(options={"persistent_notification": False})
    space = _space()
    created = MagicMock()
    monkeypatch.setattr(
        "homeassistant.components.persistent_notification.async_create",
        created,
    )
    await mgr._create_alarm_notification(space, {"message": "X"})
    created.assert_not_called()


async def test_create_alarm_notification_filter_none(monkeypatch) -> None:
    mgr = _make_manager()
    mgr.coordinator.config_entry = SimpleNamespace(options={"notification_filter": "none"})
    space = _space()
    created = MagicMock()
    monkeypatch.setattr(
        "homeassistant.components.persistent_notification.async_create",
        created,
    )
    await mgr._create_alarm_notification(space, {"message": "X"})
    created.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_event dispatcher
# ---------------------------------------------------------------------------


async def test_handle_event_disabled_returns_false() -> None:
    mgr = _make_manager()
    mgr._enabled = False
    assert await mgr._handle_event(_event()) is False


async def test_handle_event_missing_hub_or_tag() -> None:
    mgr = _make_manager()
    assert await mgr._handle_event(_event(hubId="", eventTag="")) is True


async def test_handle_event_duplicate_ignored() -> None:
    mgr = _make_manager()
    _with_space(mgr, _space())
    ev = _event()
    assert await mgr._handle_event(ev) is True
    # Second identical event (same tag/source/timestamp) is deduplicated.
    assert await mgr._handle_event(ev) is True
    assert mgr.coordinator.stats["events_sqs_received"] == 2


async def test_handle_event_unmanaged_hub_returns_false() -> None:
    mgr = _make_manager()
    _with_space(mgr, _space())
    assert await mgr._handle_event(_event(hubId="other_hub")) is False


async def test_handle_event_routes_door_event() -> None:
    mgr = _make_manager()
    space = _with_space(mgr, _space())
    dev = _device()
    space.devices["d1"] = dev
    assert await mgr._handle_event(_event(eventTag="DoorOpened")) is True
    assert dev.attributes["door_opened"] is True
    # Event recorded in history and UI updated.
    assert len(space.recent_events) == 1
    mgr.coordinator.async_set_updated_data.assert_called()


async def test_handle_event_alarm_creates_notification(monkeypatch) -> None:
    mgr = _make_manager()
    space = _with_space(mgr, _space(SecurityState.DISARMED))
    dev = _device(dtype=DeviceType.SMOKE_DETECTOR)
    space.devices["d1"] = dev
    created = MagicMock()
    monkeypatch.setattr(
        "homeassistant.components.persistent_notification.async_create",
        created,
    )
    await mgr._handle_event(_event(eventTag="SmokeDetected", eventTypeV2="ALARM", sourceObjectId="d1"))
    created.assert_called_once()


async def test_handle_event_unhandled_tag_logged() -> None:
    """Unknown event tag must not raise and still returns True."""
    mgr = _make_manager()
    _with_space(mgr, _space())
    assert await mgr._handle_event(_event(eventTag="TotallyUnknownTag")) is True


async def test_handle_event_hub_event_logged() -> None:
    mgr = _make_manager()
    _with_space(mgr, _space())
    assert await mgr._handle_event(_event(eventTag="HubOnline")) is True


async def test_handle_event_swallows_handler_exception() -> None:
    """A handler raising must be caught — _handle_event returns True (ack)."""
    mgr = _make_manager()
    space = _with_space(mgr, _space())
    dev = _device()
    space.devices["d1"] = dev
    # Force _add_event_to_history to blow up mid-flight.
    mgr._add_event_to_history = MagicMock(side_effect=RuntimeError("boom"))
    assert await mgr._handle_event(_event()) is True


# ---------------------------------------------------------------------------
# _handle_event dispatcher — full routing coverage
# ---------------------------------------------------------------------------


async def test_handle_event_routes_security(monkeypatch) -> None:
    mgr = _make_manager()
    space = _with_space(mgr, _space(SecurityState.DISARMED))
    monkeypatch.setattr("custom_components.ajax.sqs_manager.asyncio.sleep", AsyncMock())
    await mgr._handle_event(_event(eventTag="arm", sourceObjectId="user1"))
    assert space.security_state == SecurityState.ARMED


async def test_handle_event_routes_motion() -> None:
    mgr = _make_manager()
    space = _with_space(mgr, _space(SecurityState.ARMED))
    dev = _device(dtype=DeviceType.MOTION_DETECTOR)
    space.devices["d1"] = dev
    await mgr._handle_event(_event(eventTag="MotionDetected", sourceObjectId="d1"))
    assert space.security_state == SecurityState.TRIGGERED


async def test_handle_event_routes_flood() -> None:
    mgr = _make_manager()
    space = _with_space(mgr, _space())
    dev = _device(dtype=DeviceType.FLOOD_DETECTOR)
    space.devices["d1"] = dev
    await mgr._handle_event(_event(eventTag="LeakageDetected", sourceObjectId="d1"))
    assert dev.attributes["flood_alarm"] is True


async def test_handle_event_routes_glass() -> None:
    mgr = _make_manager()
    space = _with_space(mgr, _space())
    dev = _device(dtype=DeviceType.GLASS_BREAK)
    space.devices["d1"] = dev
    await mgr._handle_event(_event(eventTag="GlassBreakDetected", sourceObjectId="d1"))
    assert dev.attributes["glass_alarm"] is True


async def test_handle_event_routes_relay() -> None:
    mgr = _make_manager()
    space = _with_space(mgr, _space())
    dev = _device(dtype=DeviceType.RELAY)
    space.devices["d1"] = dev
    await mgr._handle_event(_event(eventTag="SwitchedOn", sourceObjectId="d1"))
    assert dev.attributes["is_on"] is True


async def test_handle_event_routes_button() -> None:
    mgr = _make_manager()
    space = _with_space(mgr, _space())
    dev = _device(dtype=DeviceType.BUTTON)
    space.devices["d1"] = dev
    await mgr._handle_event(_event(eventTag="ButtonPressed", sourceObjectId="d1"))
    assert dev.attributes["last_action"] == "single_press"


async def test_handle_event_routes_doorbell() -> None:
    mgr = _make_manager()
    space = _with_space(mgr, _space())
    dev = _device(name="Bell", dtype=DeviceType.DOORBELL)
    space.devices["d1"] = dev
    await mgr._handle_event(_event(eventTag="DoorbellPressed", sourceObjectName="Bell", sourceObjectId="d1"))
    assert dev.attributes["doorbell_ring"] is True


async def test_handle_event_routes_scenario() -> None:
    mgr = _make_manager()
    _with_space(mgr, _space())
    additional = [{"additionalDataV2Type": "INITIATOR_INFO", "objectName": "Btn", "objectType": "BUTTON"}]
    await mgr._handle_event(_event(eventTag="ScenarioExecuted", additionalDataV2=additional))
    # The scenario bus event must have been fired.
    fired = [c.args[0] for c in mgr.coordinator.hass.bus.async_fire.call_args_list]
    assert "ajax_scenario_triggered" in fired


async def test_handle_event_routes_wire_input() -> None:
    mgr = _make_manager()
    space = _with_space(mgr, _space())
    dev = _device(dtype=DeviceType.WIRE_INPUT)
    space.devices["d1"] = dev
    await mgr._handle_event(_event(eventTag="IntrusionAlarm", sourceObjectId="d1", transition="TRIGGERED"))
    assert dev.attributes["door_opened"] is True


async def test_handle_event_routes_device_status() -> None:
    mgr = _make_manager()
    space = _with_space(mgr, _space())
    dev = _device()
    dev.online = True
    space.devices["d1"] = dev
    await mgr._handle_event(_event(eventTag="Offline", sourceObjectId="d1"))
    assert dev.online is False


async def test_handle_event_routes_video() -> None:
    mgr = _make_manager()
    space = _with_space(mgr, _space())
    ve = AjaxVideoEdge(id="ve1", name="Cam", space_id="s1")
    space.video_edges["ve1"] = ve
    await mgr._handle_event(_event(eventTag="VideoHumanDetected", sourceObjectName="Cam", sourceObjectId="ve1"))
    assert ve.channels[0]["state"][0]["type"] == "VIDEO_HUMAN"


async def test_handle_event_routes_lock() -> None:
    mgr = _make_manager()
    space = _with_space(mgr, _space())
    lock = AjaxSmartLock(id="lock1", name="Lock", space_id="s1", is_locked=True)
    space.smart_locks["lock1"] = lock
    await mgr._handle_event(_event(eventTag="SmartLockUnlockedByUser", sourceObjectId="lock1", eventCode="M_7E_23"))
    assert lock.is_locked is False


# ---------------------------------------------------------------------------
# _add_event_to_history — truncation
# ---------------------------------------------------------------------------


def test_add_event_to_history_truncates() -> None:
    mgr = _make_manager()
    space = _space()
    for i in range(SQSManager.MAX_EVENTS_HISTORY + 5):
        mgr._add_event_to_history(space, {"i": i})
    assert len(space.recent_events) == SQSManager.MAX_EVENTS_HISTORY
    # Most recent first.
    assert space.recent_events[0]["i"] == SQSManager.MAX_EVENTS_HISTORY + 4


# ---------------------------------------------------------------------------
# _create_event_record — security tag fallback (SECURITY_EVENT_ACTIONS branch)
# ---------------------------------------------------------------------------


def test_create_event_record_security_tag_fallback() -> None:
    mgr = _make_manager()
    rec = mgr._create_event_record(
        event_tag="arm",
        event_type="SECURITY",
        event_code="",
        source_name="John",
        source_type="USER",
        source_id="u1",
        room_name="",
        hub_name="Hub",
        timestamp=0,
        transition="",
    )
    assert rec["action"] == "armed"


# ---------------------------------------------------------------------------
# _handle_security_event — refresh failure path
# ---------------------------------------------------------------------------


async def test_handle_security_event_refresh_failure_clears_flag(monkeypatch) -> None:
    mgr = _make_manager()
    monkeypatch.setattr("custom_components.ajax.sqs_manager.asyncio.sleep", AsyncMock())
    mgr.coordinator.async_force_metadata_refresh = AsyncMock(side_effect=RuntimeError("boom"))
    space = _space(SecurityState.DISARMED)
    result = await mgr._handle_security_event(space, "arm", "John", "USER")
    assert result is True
    # Even on refresh failure, the skip flag is cleared and notification fires.
    assert "hub1" not in mgr.coordinator._skipped_state_change_hubs
    mgr.coordinator._create_sqs_notification.assert_awaited_once()


# ---------------------------------------------------------------------------
# _reset_video_detection — exception swallowed
# ---------------------------------------------------------------------------


def test_reset_video_detection_swallows_exception() -> None:
    mgr = _make_manager()
    # account.spaces.get raises → caught by the broad except.
    mgr.coordinator.account.spaces = MagicMock()
    mgr.coordinator.account.spaces.get = MagicMock(side_effect=RuntimeError("boom"))
    # Must not raise.
    mgr._reset_video_detection("s1", "ve1", "0", "VIDEO_HUMAN")


# ---------------------------------------------------------------------------
# Lifecycle helpers: set_language / _schedule_later / _spawn_background / start / stop
# ---------------------------------------------------------------------------


def test_set_language_valid_and_fallback() -> None:
    mgr = _make_manager()
    mgr.set_language("fr")
    assert mgr._language == "fr"
    mgr.set_language("zz")
    # Unknown language falls back to default.
    assert mgr._language in ("en", "fr", "es")


def test_schedule_later_tracks_and_releases_handle() -> None:
    mgr = _make_manager()
    captured = {}

    def _call_later(delay, cb):
        captured["cb"] = cb
        return "handle"

    mgr.coordinator.hass.loop.call_later = MagicMock(side_effect=_call_later)
    flag = {"fired": False}
    mgr._schedule_later(5.0, lambda: flag.__setitem__("fired", True))
    assert "handle" in mgr._pending_timers
    # Firing the wrapped callback removes the handle and runs the real callback.
    captured["cb"]()
    assert flag["fired"] is True
    assert "handle" not in mgr._pending_timers


def test_spawn_background_tracks_task() -> None:
    mgr = _make_manager()
    task = MagicMock()
    mgr.coordinator.hass.async_create_task = MagicMock(return_value=task)

    async def _coro():
        return None

    coro = _coro()
    mgr._spawn_background(coro)
    coro.close()
    assert task in mgr._background_tasks
    task.add_done_callback.assert_called_once()


async def test_start_success() -> None:
    mgr = _make_manager()
    mgr.sqs_client.connect = AsyncMock(return_value=True)
    mgr.sqs_client.start_receiving = AsyncMock()
    mgr._enabled = False
    assert await mgr.start() is True
    assert mgr._enabled is True
    assert mgr.sqs_client.event_callback == mgr._handle_event


async def test_start_connect_fails() -> None:
    mgr = _make_manager()
    mgr.sqs_client.connect = AsyncMock(return_value=False)
    assert await mgr.start() is False


async def test_start_exception_returns_false() -> None:
    mgr = _make_manager()
    mgr.sqs_client.connect = AsyncMock(side_effect=RuntimeError("boom"))
    assert await mgr.start() is False


async def test_stop_cancels_timers_and_closes() -> None:
    mgr = _make_manager()
    handle = MagicMock()
    mgr._pending_timers = {handle}
    mgr.sqs_client.stop_receiving = AsyncMock()
    mgr.sqs_client.close = AsyncMock()
    await mgr.stop()
    assert mgr._enabled is False
    handle.cancel.assert_called_once()
    assert mgr._pending_timers == set()
    mgr.sqs_client.close.assert_awaited_once()


async def test_stop_swallows_client_error() -> None:
    mgr = _make_manager()
    mgr.sqs_client.stop_receiving = AsyncMock(side_effect=RuntimeError("boom"))
    mgr.sqs_client.close = AsyncMock()
    # Must not raise.
    await mgr.stop()
    assert mgr._enabled is False


# ---------------------------------------------------------------------------
# __init__ — full constructor wiring
# ---------------------------------------------------------------------------


def test_init_wires_defaults() -> None:
    coord = MagicMock()
    client = MagicMock()
    mgr = SQSManager(coord, client)
    assert mgr.coordinator is coord
    assert mgr.sqs_client is client
    assert mgr._enabled is False
    assert mgr._last_event_time == 0.0
    assert mgr._last_state_update == {}
    assert mgr._recent_event_ids == {}
    assert mgr._pending_timers == set()
    assert mgr._background_tasks == set()
    assert isinstance(mgr._security_event_lock, asyncio.Lock)


# ---------------------------------------------------------------------------
# stop — awaits in-flight background tasks
# ---------------------------------------------------------------------------


async def test_stop_gathers_background_tasks() -> None:
    mgr = _make_manager()
    mgr.sqs_client.stop_receiving = AsyncMock()
    mgr.sqs_client.close = AsyncMock()

    async def _bg():
        return None

    task = asyncio.ensure_future(_bg())
    mgr._background_tasks = {task}
    await mgr.stop()
    assert task.done()


# ---------------------------------------------------------------------------
# _handle_event — CancelledError propagates (not swallowed)
# ---------------------------------------------------------------------------


async def test_handle_event_reraises_cancelled() -> None:
    mgr = _make_manager()
    space = _with_space(mgr, _space())
    dev = _device()
    space.devices["d1"] = dev
    mgr._add_event_to_history = MagicMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await mgr._handle_event(_event())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
