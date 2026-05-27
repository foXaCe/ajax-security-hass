"""Unit tests for the shared SSE/SQS event mixin.

The mixin owns logic that the two managers must NOT diverge on:
detection-type → HA-event-type mapping, video-edge entity firing, and
the throttled discovery refresh triggered by unknown device ids.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.ajax._event_helpers import (
    VIDEO_DETECTION_EVENT_TYPES,
    EventHandlerMixin,
)


class _StubManager(EventHandlerMixin):
    """Concrete subclass with a fake coordinator/hass for the tests."""

    def __init__(self, hass) -> None:
        self.coordinator = SimpleNamespace(
            hass=hass,
            _event_entities={},
            async_request_refresh=MagicMock(),
        )


def _hass_with_create_task() -> SimpleNamespace:
    """Build a minimal fake hass with the few hooks the mixin touches."""
    hass = SimpleNamespace(
        async_create_task=MagicMock(),
        bus=SimpleNamespace(async_fire=MagicMock()),
    )
    return hass


def test_video_detection_event_types_covers_all_onvif_events() -> None:
    """Every ONVIF VIDEO_* event reported by the cameras must map.

    Missing one would leave the `event.<camera>_detection` entity
    silent for that detection type — exactly the regression #133 we
    fixed before. Pin the contract here.
    """
    assert VIDEO_DETECTION_EVENT_TYPES == {
        "VIDEO_MOTION": "motion",
        "VIDEO_HUMAN": "human",
        "VIDEO_VEHICLE": "vehicle",
        "VIDEO_PET": "pet",
        "VIDEO_LINE_CROSSING": "line_crossing",
    }


def test_fire_video_detection_event_fires_matching_entity() -> None:
    mgr = _StubManager(_hass_with_create_task())
    event_entity = MagicMock()
    mgr.coordinator._event_entities["ve1_detection"] = event_entity

    video_edge = SimpleNamespace(id="ve1", name="Cam")
    mgr._fire_video_detection_event(video_edge, "VIDEO_HUMAN")

    event_entity.fire.assert_called_once_with("human")


def test_fire_video_detection_event_silent_when_entity_missing() -> None:
    """Missing entity must not crash — entities are registered lazily."""
    mgr = _StubManager(_hass_with_create_task())
    video_edge = SimpleNamespace(id="ve1", name="Cam")
    # Registry empty: must not raise.
    mgr._fire_video_detection_event(video_edge, "VIDEO_HUMAN")


def test_fire_video_detection_event_ignores_unknown_detection_type() -> None:
    mgr = _StubManager(_hass_with_create_task())
    event_entity = MagicMock()
    mgr.coordinator._event_entities["ve1_detection"] = event_entity

    video_edge = SimpleNamespace(id="ve1", name="Cam")
    mgr._fire_video_detection_event(video_edge, "VIDEO_NOT_A_REAL_TYPE")

    event_entity.fire.assert_not_called()


def test_request_discovery_refresh_triggers_coordinator_refresh() -> None:
    mgr = _StubManager(_hass_with_create_task())
    mgr._request_discovery_refresh("source-id")
    mgr.coordinator.hass.async_create_task.assert_called_once()


def test_request_discovery_refresh_throttled_within_window() -> None:
    """A burst of events for the same unknown id must trigger one refresh."""
    mgr = _StubManager(_hass_with_create_task())
    mgr._request_discovery_refresh("source-id")
    mgr._request_discovery_refresh("source-id")
    mgr._request_discovery_refresh("another-id")
    # Three calls within the 60 s throttle window → only the first fires.
    assert mgr.coordinator.hass.async_create_task.call_count == 1


def test_request_discovery_refresh_fires_again_after_throttle() -> None:
    mgr = _StubManager(_hass_with_create_task())
    mgr._request_discovery_refresh("source-id")
    # Pretend 61 s elapsed without touching the real clock.
    mgr._last_discovery_refresh = time.time() - 61
    mgr._request_discovery_refresh("source-id")
    assert mgr.coordinator.hass.async_create_task.call_count == 2


@pytest.mark.parametrize("empty", ["", None])
def test_request_discovery_refresh_noop_on_empty_id(empty: str | None) -> None:
    """Empty source_id (Hub-level events) must NOT trigger a refresh."""
    mgr = _StubManager(_hass_with_create_task())
    mgr._request_discovery_refresh(empty or "")
    mgr.coordinator.hass.async_create_task.assert_not_called()


def test_update_video_detection_adds_new_state_entry() -> None:
    mgr = _StubManager(_hass_with_create_task())
    video_edge = SimpleNamespace(
        id="ve1",
        name="Cam",
        channels=[{"id": "0", "state": []}],
    )
    mgr._update_video_detection(video_edge, channel_id="0", detection_type="VIDEO_HUMAN", active=True)
    assert video_edge.channels[0]["state"] == [{"type": "VIDEO_HUMAN", "active": True}]


def test_update_video_detection_updates_existing_entry() -> None:
    mgr = _StubManager(_hass_with_create_task())
    video_edge = SimpleNamespace(
        id="ve1",
        name="Cam",
        channels=[{"id": "0", "state": [{"type": "VIDEO_HUMAN", "active": True}]}],
    )
    mgr._update_video_detection(video_edge, channel_id="0", detection_type="VIDEO_HUMAN", active=False)
    assert video_edge.channels[0]["state"] == [{"type": "VIDEO_HUMAN", "active": False}]


def test_update_video_detection_noop_for_non_list_channels() -> None:
    """Defensive: malformed payloads from the proxy must not crash."""
    mgr = _StubManager(_hass_with_create_task())
    video_edge = SimpleNamespace(id="ve1", name="Cam", channels="not-a-list")
    mgr._update_video_detection(video_edge, channel_id=None, detection_type="VIDEO_HUMAN", active=True)
    # Channels left untouched, no exception.
    assert video_edge.channels == "not-a-list"
