"""Characterize the REST call budget on the event -> refresh path.

Three optimisations of the API call budget were merged recently (#192 skip
of smart-lock detail, #193 light refresh after a realtime event, #194
armed-aware cadence). They interact across the boundary between the
SSE/SQS managers and the coordinator, but no existing test exercises that
boundary end to end: each side mocks the other entirely
(``async_force_state_refresh = AsyncMock()`` on the manager side,
``async_refresh = AsyncMock()`` / ``_async_update_data`` mocked wholesale on
the coordinator side). These tests instead build a REAL
``AjaxDataCoordinator`` (via ``object.__new__``, no live ``hass`` /
``DataUpdateCoordinator`` machinery) and a REAL ``SQSManager`` wired to it,
and count the actual REST calls (or the real coordinator-tick methods)
produced by simulated events.

They are characterization tests: they pin today's behaviour, defects
included, with the defect spelled out in a comment on the assertion that
documents it. Plans 009 (light-refresh contract), 010 (bypass single-miss)
and 011 (burst coalescing) each tighten one of the assertions marked
``# BASELINE`` below — that is expected and will require updating this
file, not a regression in it.

The only mocked "bridge" in the coordinator builder is ``async_refresh``
itself, which stands in for ``DataUpdateCoordinator``'s real
``async_refresh`` (never initialised here, since ``__init__`` is bypassed)
but forwards straight into the REAL ``_async_update_data()`` — exactly what
``async_force_metadata_refresh`` / ``async_force_state_refresh`` do in
production (they just call ``self.async_refresh()``). Everything else
exercised here — throttle math, cycle counter, event dispatch, dedup — is
the genuine production code.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ajax.api import AjaxRestApi
from custom_components.ajax.const import UPDATE_INTERVAL
from custom_components.ajax.coordinator import AjaxDataCoordinator
from custom_components.ajax.models import (
    AjaxAccount,
    AjaxSmartLock,
    AjaxSpace,
    AjaxVideoEdge,
    SecurityState,
    VideoEdgeType,
)
from custom_components.ajax.sqs_manager import SQSManager

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _account_with_space(security_state: SecurityState) -> AjaxAccount:
    """An account with one space that already has a video edge + smart lock.

    Both devices are pre-existing (their ids match the canned API returns
    configured on the coordinator below) so a tick never hits the
    "new device" / "removed device" branches, which would need a real HA
    device registry — out of scope for a REST-call-budget test.
    """
    space = AjaxSpace(
        id="s1",
        name="Home",
        hub_id="hub1",
        real_space_id="rs1",
        security_state=security_state,
    )
    space.video_edges["ve1"] = AjaxVideoEdge(
        id="ve1",
        name="Cam",
        space_id="s1",
        video_edge_type=VideoEdgeType.BULLET,
        connection_state="ONLINE",
    )
    space.smart_locks["sl1"] = AjaxSmartLock(
        id="sl1",
        name="Lock",
        space_id="s1",
        raw_data={"id": "sl1", "name": "Lock"},
    )
    account = AjaxAccount(user_id="u1", name="user", email="u@example.com")
    account.spaces["s1"] = space
    return account


def _coordinator(security_state: SecurityState = SecurityState.ARMED) -> AjaxDataCoordinator:
    """Bare coordinator (``object.__new__``) with a REAL ``_async_update_data``.

    Modelled on ``tests/test_coordinator_core_coverage.py::_coordinator()``,
    extended with:
    - a pre-populated account (one armed-capable space with a video edge and
      a smart lock) so the light-tick fan-out branch has something to do;
    - canned ``coord.api.async_get_video_edges`` / ``async_get_smart_locks``
      so the REAL (unmocked) ``_async_update_video_edges`` /
      ``_async_update_smart_locks`` coordinator methods can run for real and
      the resulting REST-call counts are meaningful;
    - the extra attributes ``sqs_manager.py`` handlers touch directly
      (``_skipped_state_change_hubs``, ``_event_entities``, ...) and mocks
      for the coordinator hooks those handlers call
      (``has_pending_ha_action``, ``_create_sqs_notification``, ...) so a
      real ``SQSManager`` can be wired to this coordinator (see
      ``_make_manager`` below) without touching AWS or a real event loop.

    ``_async_update_spaces_from_hubs`` / ``_async_update_devices`` stay
    mocked at the coordinator-method level, exactly like the reference
    tests in ``test_coordinator_core_coverage.py`` — they are not part of
    the video/smart-lock REST budget this file measures, and running them
    for real would require reproducing the whole hubs/rooms/users/groups
    REST surface for no benefit here.
    """
    coord = object.__new__(AjaxDataCoordinator)
    coord.api = MagicMock()
    coord.hass = MagicMock()
    coord.config_entry = MagicMock()
    coord.account = _account_with_space(security_state)

    # Real-time managers (None by default — set per-test).
    coord.sqs_manager = None
    coord.sse_manager = None
    coord.onvif_manager = None

    # Polling state.
    coord._initial_load_done = True
    coord._force_metadata_refresh = False
    coord._bypass_cache_next_refresh = False
    coord._cycle_counter = 0
    coord._realtime_skip_factor = 3
    coord._last_metadata_refresh = 1e18  # far future -> no metadata refresh
    coord._door_sensor_poll_task = None
    coord._door_sensor_poll_security_state = SecurityState.DISARMED
    coord._door_sensor_fast_poll_enabled = False
    coord._sse_url = None
    coord._sqs_initialized = False
    coord._sqs_init_in_progress = False
    coord._aws_access_key_id = None
    coord._onvif_initialized = False
    coord._onvif_reconcile_in_progress = False
    coord._onvif_last_bootstrap_attempt = 0.0

    # Auth resilience.
    coord._consecutive_auth_errors = 0
    coord._max_auth_errors = 3

    # Diagnostics counters.
    coord.stats = {
        "events_sse_received": 0,
        "events_sqs_received": 0,
        "events_onvif_received": 0,
        "auth_errors": 0,
        "discovery_refreshes": 0,
    }

    # DataUpdateCoordinator-provided attributes _async_update_data reads.
    coord.last_update_success = True
    coord.update_interval = timedelta(seconds=UPDATE_INTERVAL)

    # api shape _async_update_data inspects.
    coord.api.is_proxy_mode = False
    coord.api.suggested_interval = None
    coord.api.bypass_cache_next = MagicMock()

    # Out of scope for this file's REST budget — mocked at the
    # coordinator-method level (see docstring above).
    coord._async_update_spaces_from_hubs = AsyncMock()
    coord._async_update_devices = AsyncMock()
    coord._async_update_notifications = AsyncMock()
    coord._async_restore_smart_locks = AsyncMock()
    coord._async_cleanup_stale_devices = MagicMock()
    coord._manage_door_sensor_polling = MagicMock()
    coord._reset_expired_motion_detections = MagicMock()

    # REAL (unmocked, inherited from AjaxStateUpdaterMixin) — these call
    # straight through to coord.api.async_get_video_edges /
    # async_get_smart_locks, the exact REST boundary this file measures.
    coord.api.async_get_video_edges = AsyncMock(
        return_value=[
            {
                "id": "ve1",
                "type": "BULLET",
                "name": "Cam",
                "networkInterface": {},
                "firmware": {},
                "connectionState": "ONLINE",
                "channels": [],
            }
        ]
    )
    coord.api.async_get_smart_locks = AsyncMock(return_value=[{"id": "sl1", "name": "Lock"}])

    # Attributes/hooks sqs_manager.py's handlers touch directly on the
    # coordinator, needed even though _async_update_spaces_from_hubs is
    # mocked (see tests/test_sqs_manager_coverage.py::_make_coordinator()).
    coord._skipped_state_change_hubs = set()
    coord._event_entities = {}
    coord.entry_id = "entry1"
    coord.has_pending_ha_action = MagicMock(return_value=False)
    coord._create_sqs_notification = AsyncMock()
    coord._async_save_smart_locks = AsyncMock()
    coord._fire_security_state_event = MagicMock()
    coord._update_polling_interval = MagicMock()
    coord.async_set_updated_data = MagicMock()
    coord._escape_markdown = lambda s: s
    coord.async_request_refresh = AsyncMock()
    coord.hass.loop.call_later = MagicMock(return_value=MagicMock())

    # Close any coroutine handed to async_create_task so it doesn't leak a
    # "coroutine was never awaited" warning when a handler spawns background
    # work (e.g. _async_save_smart_locks).
    create_task = MagicMock()

    def _consume(coro: Any = None, *args: Any, **kwargs: Any) -> MagicMock:
        if coro is not None and hasattr(coro, "close"):
            coro.close()
        return MagicMock()

    create_task.side_effect = _consume
    coord.hass.async_create_task = create_task
    coord.hass.bus.async_fire = MagicMock()

    # The one mocked "bridge": stands in for DataUpdateCoordinator's real
    # async_refresh() (never initialised — __init__ is bypassed) but
    # forwards into the REAL _async_update_data(), same as
    # async_force_metadata_refresh/async_force_state_refresh do in
    # production (coordinator.py:290-313).
    async def _fake_async_refresh() -> None:
        coord.account = await coord._async_update_data()

    coord.async_refresh = _fake_async_refresh

    return coord


def _make_manager(coordinator: AjaxDataCoordinator) -> SQSManager:
    """Build a REAL SQSManager (bypassing __init__) wired to ``coordinator``.

    Modelled on tests/test_sqs_manager_coverage.py::_make_manager(), except
    ``coordinator`` is a real AjaxDataCoordinator (see ``_coordinator()``
    above) instead of a MagicMock — the whole point of this file is to
    exercise the real manager -> real coordinator boundary.
    """
    mgr = object.__new__(SQSManager)
    mgr.coordinator = coordinator
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


def _sqs_event(
    event_tag: str, source_id: str = "grp", timestamp: int = 1700000000000, **overrides: Any
) -> dict[str, Any]:
    """Minimal SQS event_data envelope (modelled on the _event() helper in
    tests/test_sqs_manager_coverage.py).
    """
    event = {
        "eventTag": event_tag,
        "eventTypeV2": "SECURITY",
        "eventCode": "",
        "hubId": "hub1",
        "hubName": "Hub",
        "sourceObjectName": "Group",
        "sourceObjectType": "GROUP",
        "sourceObjectId": source_id,
        "sourceRoomName": "",
        "timestamp": timestamp,
        "transition": "",
        "additionalDataV2": [],
    }
    event.update(overrides)
    return {"event": event}


# ---------------------------------------------------------------------------
# Step 1 — smoke test: the real _async_update_data() runs end to end
# ---------------------------------------------------------------------------


async def test_periodic_tick_runs_real_update_data_pipeline() -> None:
    """A bare periodic tick runs the REAL _async_update_data() to completion."""
    coord = _coordinator(SecurityState.DISARMED)

    result = await coord._async_update_data()

    assert result is coord.account
    assert coord._cycle_counter == 1
    # No realtime manager + disarmed -> the light-tick optimisation never
    # applies (correct, existing behaviour — not a BASELINE finding): video
    # edges and smart locks are polled every cycle via the real REST calls.
    coord.api.async_get_video_edges.assert_awaited_once_with("rs1")
    coord.api.async_get_smart_locks.assert_awaited_once_with("rs1", known_ids={"sl1"})


# ---------------------------------------------------------------------------
# Step 2 — a real SQS event drives the real coordinator tick
# ---------------------------------------------------------------------------


async def test_sqs_arm_event_triggers_real_coordinator_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """An 'arm' event routed through the real manager runs a real coordinator tick."""
    monkeypatch.setattr("custom_components.ajax.sqs_manager.asyncio.sleep", AsyncMock())
    coord = _coordinator(SecurityState.DISARMED)
    mgr = _make_manager(coord)
    coord.sqs_manager = mgr
    space = coord.account.spaces["s1"]

    result = await mgr._handle_security_event(space, "arm", "John", "USER")

    assert result is True
    # Proof the REAL _async_update_data() ran (not a mocked bridge):
    # _async_update_devices is unconditional every tick, so one await means
    # one real pass through the coordinator's periodic-update branch.
    coord._async_update_devices.assert_awaited_once_with("s1")
    assert coord._cycle_counter == 1
    assert space.security_state == SecurityState.ARMED


# ---------------------------------------------------------------------------
# Step 3 — BASELINE: manager -> coordinator boundary defects
# ---------------------------------------------------------------------------


async def test_group_event_burst_runs_one_tick_per_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three distinct group-arm events in a row each run their own full tick."""
    monkeypatch.setattr("custom_components.ajax.sqs_manager.asyncio.sleep", AsyncMock())
    coord = _coordinator(SecurityState.ARMED)
    mgr = _make_manager(coord)
    coord.sqs_manager = mgr

    for idx, group_id in enumerate(("g1", "g2", "g3")):
        handled = await mgr._handle_event(_sqs_event("grouparm", source_id=group_id, timestamp=1700000000000 + idx))
        assert handled is True

    # BASELINE (plan 011): no coalescing across the burst — 3 independent
    # group-arm events produce 3 independent _async_update_data() ticks
    # (each with its own 1.0s internal sleep, here patched to no-op), even
    # though group states are already refetched on every tick (#150) and a
    # single coalesced refresh would have been enough.
    assert coord._async_update_devices.await_count == 3
    assert coord._cycle_counter == 3


async def test_disarm_event_light_refresh_fetches_video_and_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A realtime-triggered 'light' refresh can still do the full REST fan-out."""
    monkeypatch.setattr("custom_components.ajax.sqs_manager.asyncio.sleep", AsyncMock())
    coord = _coordinator(SecurityState.ARMED)
    # Land the upcoming tick on the Nth cycle of the #194 throttle.
    coord._cycle_counter = coord._realtime_skip_factor - 1
    mgr = _make_manager(coord)
    coord.sqs_manager = mgr
    space = coord.account.spaces["s1"]

    result = await mgr._handle_security_event(space, "disarm", "John", "USER")

    assert result is True
    # sqs_manager._handle_security_event calls async_force_state_refresh()
    # *before* it mutates space.security_state (the "if state_changed..."
    # assignment is textually after the "if is_group_event or
    # is_full_arm_disarm" block, sqs_manager.py:459-499) — so at the moment
    # _async_update_data() actually runs, the space still reports ARMED and
    # realtime_active is still True. What forces the fetch below is the
    # Nth-cycle sync cadence set up above, not "disarmed -> poll every
    # cycle" (that path is covered by test_periodic_tick_... instead).
    #
    # BASELINE (plan 009): async_force_state_refresh's docstring promises a
    # refresh "WITHOUT ... rooms/users/video re-fetches across all hubs" —
    # but it only calls self.async_refresh(), i.e. the SAME
    # _async_update_data() as an ordinary poll tick, so it inherits the
    # #194 throttle verbatim and can still trigger the full video/smart-lock
    # fan-out it claims to avoid.
    assert coord.api.async_get_video_edges.await_count == 1
    assert coord.api.async_get_smart_locks.await_count == 1
    assert space.security_state == SecurityState.DISARMED  # applied AFTER the refresh


async def test_force_state_refresh_increments_cycle_counter() -> None:
    """An out-of-band forced refresh is not free: it consumes a throttle cycle."""
    coord = _coordinator(SecurityState.ARMED)
    assert coord._cycle_counter == 0

    await coord.async_force_state_refresh()

    # BASELINE (plan 009): async_force_state_refresh() drives the same
    # _cycle_counter used by the #194 armed-aware throttle, so a realtime
    # event shifts the phase of the ordinary polling cadence instead of
    # being a side-channel that leaves it alone.
    assert coord._cycle_counter == 1


# ---------------------------------------------------------------------------
# Step 4 — BASELINE: the proxy-cache bypass window at the API layer
# ---------------------------------------------------------------------------


def _space_payload() -> dict[str, Any]:
    return {
        "devices": [
            {"id": "v1", "type": "VIDEO_EDGE"},
            {"id": "l1", "type": "SMART_LOCK"},
        ]
    }


async def test_bypass_window_double_space_fetch() -> None:
    """Inside a bypass_cache_next() window, the space payload is fetched twice."""
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    api.user_id = "u1"
    api._request = AsyncMock(return_value=_space_payload())  # type: ignore[method-assign]

    api.bypass_cache_next()
    await api.async_get_video_edges("s1")
    await api.async_get_smart_locks("s1", known_ids={"l1"})

    space_fetches = [call for call in api._request.await_args_list if call.args[:2] == ("GET", "user/u1/spaces/s1")]
    # BASELINE (plan 010): bypass_cache_next() opens a blanket 2s window
    # during which _cache_bypass_active() is True for every caller — both
    # async_get_video_edges and async_get_smart_locks re-fetch the same
    # space payload instead of the second one reusing the first's result.
    assert len(space_fetches) == 2


async def test_no_bypass_window_single_space_fetch() -> None:
    """Twin of the bypass test: outside a bypass window, the cache coalesces.

    This is nominal, already-correct behaviour — it must keep holding once
    plan 010 tightens the bypass-window test above.
    """
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    api.user_id = "u1"
    api._request = AsyncMock(return_value=_space_payload())  # type: ignore[method-assign]

    await api.async_get_video_edges("s1")
    await api.async_get_smart_locks("s1", known_ids={"l1"})

    space_fetches = [call for call in api._request.await_args_list if call.args[:2] == ("GET", "user/u1/spaces/s1")]
    assert len(space_fetches) == 1
