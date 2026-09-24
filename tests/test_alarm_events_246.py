"""Issue #246: alarm events must never be dropped silently.

A DoorProtect Plus tilt alarm (``TiltDetected``, ``eventTypeV2=ALARM``)
fell through as "not handled": the panel stayed armed, and the tilt/shock
binary sensors, which nothing ever wrote, were permanently ``off``. These
tests pin the fix on both transports:
- tilt/shock set their sensor, auto-clear it, and trigger the panel;
- an unknown tag Ajax classes as ALARM still triggers the panel;
- every unhandled event is published as ``ajax_unhandled_event``.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ajax.const import EVENT_AJAX_UNHANDLED_EVENT
from custom_components.ajax.event_maps import ACCELEROMETER_RESET_SECONDS
from custom_components.ajax.models import SecurityState
from tests import test_sqs_manager_coverage as sqs_t, test_sse_manager_coverage as sse_t

# The payload from the issue, identifiers replaced.
_TILT = {
    "hubId": "hub1",
    "eventTag": "TiltDetected",
    "eventType": "ALARM",
    "eventTypeV2": "ALARM",
    "eventCode": "M_0F_31",
    "sourceObjectId": "d1",
    "sourceObjectName": "Window",
    "sourceObjectType": "DOOR_PROTECT_PLUS",
    "sourceRoomName": "WC",
    "transition": "IMPULSE",
    "additionalData": {"relatedGroupsInfo": [{"id": "00000002", "name": "G"}]},
    "additionalDataV2": None,
    "timestamp": 1790000000000,
}


def _sse(state: SecurityState):
    mgr = sse_t._make_manager()
    space = sse_t._space(state)
    dev = sse_t._device(name="Window")
    space.devices[dev.id] = dev
    sse_t._attach(mgr, space)
    return mgr, space, dev


def _sqs(state: SecurityState):
    mgr = sqs_t._make_manager()
    space = sqs_t._with_space(mgr, sqs_t._space(state))
    dev = sqs_t._device(name="Window")
    space.devices[dev.id] = dev
    return mgr, space, dev


def _fire_timers(mgr) -> None:
    for call in mgr.coordinator.hass.loop.call_later.call_args_list:
        delay, cb = call.args
        assert delay == ACCELEROMETER_RESET_SECONDS
        cb()


# ---------------------------------------------------------------- SSE -------


async def test_sse_tilt_alarm_triggers_panel_and_sensor() -> None:
    mgr, space, dev = _sse(SecurityState.NIGHT_MODE)
    await mgr._handle_event(dict(_TILT))

    assert space.security_state == SecurityState.TRIGGERED
    assert dev.attributes["tilt_detected"] is True
    assert space.recent_events[0]["action"] == "tilt_detected"
    mgr.coordinator._create_sqs_notification.assert_called_once()

    _fire_timers(mgr)
    assert dev.attributes["tilt_detected"] is False


async def test_sse_shock_routed_by_code_when_tag_is_unknown() -> None:
    mgr, space, dev = _sse(SecurityState.ARMED)
    await mgr._handle_event({**_TILT, "eventTag": "SomethingNew", "eventCode": "M_6F_30", "eventTypeV2": "SECURITY"})
    assert dev.attributes["shock_detected"] is True
    assert space.security_state == SecurityState.TRIGGERED  # armed


async def test_sse_tilt_while_disarmed_without_alarm_type_only_sets_sensor() -> None:
    mgr, space, dev = _sse(SecurityState.DISARMED)
    await mgr._handle_event({**_TILT, "eventType": "SECURITY", "eventTypeV2": "SECURITY"})
    assert dev.attributes["tilt_detected"] is True
    assert space.security_state == SecurityState.DISARMED
    mgr.coordinator._create_sqs_notification.assert_not_called()


async def test_sse_unknown_alarm_tag_triggers_panel(caplog: pytest.LogCaptureFixture) -> None:
    mgr, space, _ = _sse(SecurityState.ARMED)
    mgr.coordinator.hass.bus.async_fire = MagicMock()
    with caplog.at_level(logging.WARNING):
        await mgr._handle_event({**_TILT, "eventTag": "BrandNewAlarm", "eventCode": "M_99_99"})

    assert space.security_state == SecurityState.TRIGGERED
    assert any(r.levelno == logging.ERROR and "treated as a generic alarm" in r.message for r in caplog.records)
    name, data = mgr.coordinator.hass.bus.async_fire.call_args.args
    assert name == EVENT_AJAX_UNHANDLED_EVENT
    assert data["event_tag"] == "brandnewalarm"
    assert data["event_type"] == "ALARM"
    assert space.recent_events[0]["action"] == "alarm"


async def test_sse_unknown_alarm_falls_back_on_event_type(caplog: pytest.LogCaptureFixture) -> None:
    """Only ``eventType`` set (no V2): still an alarm."""
    mgr, space, _ = _sse(SecurityState.ARMED)
    event = {**_TILT, "eventTag": "BrandNewAlarm", "eventCode": ""}
    del event["eventTypeV2"]
    await mgr._handle_event(event)
    assert space.security_state == SecurityState.TRIGGERED


async def test_sse_unknown_non_alarm_is_published_but_changes_nothing(caplog: pytest.LogCaptureFixture) -> None:
    mgr, space, _ = _sse(SecurityState.ARMED)
    mgr.coordinator.hass.bus.async_fire = MagicMock()
    with caplog.at_level(logging.WARNING):
        await mgr._handle_event(
            {**_TILT, "eventTag": "Whatever", "eventCode": "", "eventType": "", "eventTypeV2": "INFO"}
        )
    assert space.security_state == SecurityState.ARMED
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert "not handled" in caplog.text
    assert mgr.coordinator.hass.bus.async_fire.call_args.args[0] == EVENT_AJAX_UNHANDLED_EVENT


# ---------------------------------------------------------------- SQS -------


async def test_sqs_tilt_alarm_triggers_panel_sensor_and_notification() -> None:
    mgr, space, dev = _sqs(SecurityState.NIGHT_MODE)
    mgr._create_alarm_notification = AsyncMock()
    await mgr._handle_event({"event": dict(_TILT)})

    assert space.security_state == SecurityState.TRIGGERED
    assert dev.attributes["tilt_detected"] is True
    mgr._create_alarm_notification.assert_awaited_once()
    _fire_timers(mgr)
    assert dev.attributes["tilt_detected"] is False


async def test_sqs_shock_while_armed_notifies_even_without_alarm_type() -> None:
    mgr, space, dev = _sqs(SecurityState.ARMED)
    mgr._create_alarm_notification = AsyncMock()
    await mgr._handle_event(
        {"event": {**_TILT, "eventTag": "ShockDetected", "eventCode": "M_0F_30", "eventType": "", "eventTypeV2": ""}}
    )
    assert dev.attributes["shock_detected"] is True
    assert space.security_state == SecurityState.TRIGGERED
    mgr._create_alarm_notification.assert_awaited_once()


async def test_sqs_unknown_alarm_tag_triggers_panel() -> None:
    mgr, space, _ = _sqs(SecurityState.ARMED)
    mgr._create_alarm_notification = AsyncMock()
    await mgr._handle_event({"event": {**_TILT, "eventTag": "BrandNewAlarm", "eventCode": ""}})
    assert space.security_state == SecurityState.TRIGGERED
    assert mgr.coordinator.hass.bus.async_fire.call_args.args[0] == EVENT_AJAX_UNHANDLED_EVENT
    mgr._create_alarm_notification.assert_awaited_once()


async def test_reset_is_a_noop_once_the_device_is_gone() -> None:
    mgr, space, dev = _sse(SecurityState.ARMED)
    await mgr._handle_event(dict(_TILT))
    del space.devices[dev.id]
    _fire_timers(mgr)  # must not raise


async def test_reset_follows_the_latest_event() -> None:
    """A second tilt within the delay keeps the sensor on after the first timer."""
    mgr, _, dev = _sse(SecurityState.ARMED)
    await mgr._handle_event(dict(_TILT))
    first_timer = mgr.coordinator.hass.loop.call_later.call_args_list[0].args[1]
    mgr._recent_events.clear()  # bypass dedup: a genuine second event
    dev.attributes["tilt_detected_at"] = "later"  # the second event re-stamps
    first_timer()
    assert dev.attributes["tilt_detected"] is True


def test_alarm_type_reads_either_field() -> None:
    from custom_components.ajax._event_helpers import EventHandlerMixin as M

    assert M._alarm_type({"eventTypeV2": "SECURITY", "eventType": "ALARM"}) == "ALARM"
    assert M._alarm_type({"eventTypeV2": "ALARM", "eventType": "SECURITY"}) == "ALARM"
    assert M._alarm_type({"eventTypeV2": "", "eventType": "INFO"}) == "INFO"
    assert M._alarm_type({}) == ""


async def test_sse_alarm_in_event_type_only_is_not_masked_by_v2() -> None:
    mgr, space, _ = _sse(SecurityState.ARMED)
    await mgr._handle_event({**_TILT, "eventTag": "BrandNewAlarm", "eventCode": "", "eventTypeV2": "SECURITY"})
    assert space.security_state == SecurityState.TRIGGERED


async def test_sqs_unmapped_alarm_history_is_marked_as_alarm() -> None:
    mgr, space, _ = _sqs(SecurityState.ARMED)
    mgr._create_alarm_notification = AsyncMock()
    await mgr._handle_event({"event": {**_TILT, "eventTag": "BrandNewAlarm", "eventCode": ""}})
    record = space.recent_events[0]
    assert record["is_alarm"] is True
    assert record["action"] == "alarm"


async def test_sqs_accelerometer_code_without_tag_is_processed() -> None:
    mgr, space, dev = _sqs(SecurityState.ARMED)
    mgr._create_alarm_notification = AsyncMock()
    await mgr._handle_event({"event": {**_TILT, "eventTag": ""}})
    assert dev.attributes["tilt_detected"] is True
    assert space.security_state == SecurityState.TRIGGERED
