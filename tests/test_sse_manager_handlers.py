"""Tests for SSEManager device-resolution and event-handler plumbing.

The SSE manager's job is to translate per-event payloads ({eventTag,
hubId, source}) into device attribute mutations. Bugs surface as
silent state-drift: an SSE arrives but the entity never updates.

We test the bits that don't need a network round-trip:
- ``_find_device`` matching strategies (id, suffix for WireInput,
  fallback to name)
- ``is_state_protected`` window (REST polling must not overwrite
  fresh SSE state)
- ``_handle_door_event`` mutating ``device.attributes["door_opened"]``
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.ajax.models import AjaxDevice, AjaxSpace, DeviceType, SecurityState
from custom_components.ajax.sse_manager import SSEManager


def _make_manager() -> SSEManager:
    """Build a manager wired to a fake coordinator (no SSE client / no asyncio)."""
    mgr = object.__new__(SSEManager)
    mgr._last_state_update = {}
    mgr._recent_events = {}
    mgr._dedup_window = 5
    mgr._last_discovery_refresh = 0.0
    mgr.coordinator = SimpleNamespace(
        hass=MagicMock(),
        async_request_refresh=MagicMock(),
        stats={"discovery_refreshes": 0, "events_sse_received": 0},
    )
    return mgr


def _space_with(*devices: AjaxDevice) -> AjaxSpace:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    for d in devices:
        space.devices[d.id] = d
    return space


def _door(device_id: str = "d1", name: str = "Front Door") -> AjaxDevice:
    return AjaxDevice(id=device_id, name=name, type=DeviceType.DOOR_CONTACT, space_id="s1", hub_id="hub1")


# ---------------------------------------------------------------------------
# _find_device
# ---------------------------------------------------------------------------


def test_find_device_by_exact_id() -> None:
    mgr = _make_manager()
    dev = _door()
    space = _space_with(dev)
    assert mgr._find_device(space, source_name="", source_id="d1") is dev


def test_find_device_by_wire_input_suffix() -> None:
    """WireInputs ship a short (8-char) source_id that is the suffix of the full 16-char id."""
    mgr = _make_manager()
    dev = AjaxDevice(id="ABCDEFGH12345678", name="Wire 1", type=DeviceType.WIRE_INPUT, space_id="s1", hub_id="hub1")
    space = _space_with(dev)
    assert mgr._find_device(space, source_name="", source_id="12345678") is dev


def test_find_device_by_name_fallback() -> None:
    mgr = _make_manager()
    dev = _door(name="Garage Door")
    space = _space_with(dev)
    assert mgr._find_device(space, source_name="Garage Door", source_id="") is dev


def test_find_device_returns_none_and_triggers_discovery_refresh() -> None:
    """Unknown source_id → return None *and* nudge the coordinator to refresh."""
    mgr = _make_manager()
    space = _space_with()  # empty
    result = mgr._find_device(space, source_name="", source_id="unknown_id")
    assert result is None
    mgr.coordinator.hass.async_create_task.assert_called_once()


# ---------------------------------------------------------------------------
# is_state_protected
# ---------------------------------------------------------------------------


def test_is_state_protected_true_within_window() -> None:
    """Just-updated state must block REST overwrites for 5 seconds."""
    mgr = _make_manager()
    mgr._last_state_update["hub1"] = time.time() - 2  # 2s ago
    assert mgr.is_state_protected("hub1") is True


def test_is_state_protected_false_outside_window() -> None:
    mgr = _make_manager()
    mgr._last_state_update["hub1"] = time.time() - 30  # 30s ago
    assert mgr.is_state_protected("hub1") is False


def test_is_state_protected_false_when_never_updated() -> None:
    """No SSE event yet → REST is free to write."""
    assert _make_manager().is_state_protected("hub1") is False


# ---------------------------------------------------------------------------
# _handle_door_event
# ---------------------------------------------------------------------------


def test_handle_door_event_marks_attribute_as_opened() -> None:
    mgr = _make_manager()
    dev = _door()
    space = _space_with(dev)
    mgr._handle_door_event(space, event_tag="dooropened", source_name="Front Door", source_id="d1", transition="")
    assert dev.attributes["door_opened"] is True
    assert "door_opened_at" in dev.attributes


def test_handle_door_event_recovered_transition_clears_state() -> None:
    """transition='RECOVERED' must mark door closed regardless of the event tag."""
    mgr = _make_manager()
    dev = _door()
    dev.attributes["door_opened"] = True
    space = _space_with(dev)
    mgr._handle_door_event(
        space, event_tag="dooropened", source_name="Front Door", source_id="d1", transition="RECOVERED"
    )
    assert dev.attributes["door_opened"] is False


def test_handle_door_event_silent_on_missing_device() -> None:
    """Door event for a device we don't know → log debug, no crash."""
    mgr = _make_manager()
    space = _space_with()
    # Must not raise.
    mgr._handle_door_event(space, "dooropened", source_name="X", source_id="unknown", transition="")


# ---------------------------------------------------------------------------
# External-contact events (issue #151) — extcontact* drives the External
# Contact entity (external_contact_opened), NOT the reed Door entity.
# ---------------------------------------------------------------------------


def test_extcontact_opened_updates_external_contact_not_door() -> None:
    mgr = _make_manager()
    dev = _door()
    space = _space_with(dev)
    mgr._handle_door_event(space, event_tag="extcontactopened", source_name="Front Door", source_id="d1", transition="")
    assert dev.attributes["external_contact_opened"] is True
    assert "door_opened" not in dev.attributes  # the reed Door entity is left untouched


def test_extcontact_closed_clears_external_contact() -> None:
    mgr = _make_manager()
    dev = _door()
    dev.attributes["external_contact_opened"] = True
    space = _space_with(dev)
    mgr._handle_door_event(space, event_tag="extcontactclosed", source_name="Front Door", source_id="d1", transition="")
    assert dev.attributes["external_contact_opened"] is False


def test_door_event_does_not_touch_external_contact() -> None:
    mgr = _make_manager()
    dev = _door()
    space = _space_with(dev)
    mgr._handle_door_event(space, event_tag="dooropened", source_name="Front Door", source_id="d1", transition="")
    assert dev.attributes["door_opened"] is True
    assert "external_contact_opened" not in dev.attributes
