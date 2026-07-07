"""Core coverage for AjaxDataCoordinator (coordinator.py).

Exercises the polling-and-state pipeline directly via ``object.__new__`` so
we avoid the full DataUpdateCoordinator init (no real hass / event loop /
Debouncer). Every attribute the tested method reads is initialised by hand.

Covered:
- ``_async_update_data``: success (returns account), AjaxRestAuthError
  reauth counter → ConfigEntryAuthFailed at threshold, AjaxRestApiError →
  UpdateFailed.
- ``get_space`` / ``get_device`` / ``get_room`` / ``get_group`` helpers.
- ``_update_polling_interval`` adaptive interval + door-poll management.
- realtime_skip_factor throttle of video/smart-lock refresh, armed-aware
  (manager presence alone is not enough — SSE/SQS deliver nothing while
  every space is disarmed, so that state forces a refresh every cycle).
- ``async_shutdown`` (stop SQS / SSE / ONVIF / door poll + close api).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.ajax.api import AjaxRestApiError, AjaxRestAuthError
from custom_components.ajax.const import (
    UPDATE_INTERVAL,
    UPDATE_INTERVAL_ARMED,
)
from custom_components.ajax.coordinator import AjaxDataCoordinator
from custom_components.ajax.models import (
    AjaxAccount,
    AjaxDevice,
    AjaxGroup,
    AjaxRoom,
    AjaxSpace,
    DeviceType,
    SecurityState,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _account_with_space(security_state: SecurityState = SecurityState.DISARMED) -> AjaxAccount:
    """An account with a single space holding one device + room + group."""
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=security_state)
    space.devices["d1"] = AjaxDevice(id="d1", name="Relay", type=DeviceType.RELAY, space_id="s1", hub_id="hub1")
    space.rooms["r1"] = AjaxRoom(id="r1", name="Living", space_id="s1")
    space.groups["g1"] = AjaxGroup(id="g1", name="Perimeter", space_id="s1")
    account = AjaxAccount(user_id="u1", name="user", email="u@example.com")
    account.spaces["s1"] = space
    return account


def _coordinator() -> AjaxDataCoordinator:
    """Bare coordinator with the attributes the tested methods read."""
    coord = object.__new__(AjaxDataCoordinator)
    coord.api = MagicMock()
    coord.hass = MagicMock()
    coord.config_entry = MagicMock()
    coord.account = None

    # Real-time managers (None by default — set per-test).
    coord.sqs_manager = None
    coord.sse_manager = None
    coord.onvif_manager = None

    # Polling state.
    coord._initial_load_done = False
    coord._force_metadata_refresh = False
    coord._bypass_cache_next_refresh = False
    coord._cycle_counter = 0
    coord._realtime_skip_factor = 3
    coord._last_metadata_refresh = 0.0
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

    # DataUpdateCoordinator-provided attributes the methods read.
    coord.last_update_success = True
    coord.update_interval = timedelta(seconds=UPDATE_INTERVAL)

    # api shape the methods inspect.
    coord.api.is_proxy_mode = False
    coord.api.suggested_interval = None
    coord.api.bypass_cache_next = MagicMock()

    return coord


# ---------------------------------------------------------------------------
# get_space / get_device / get_room / get_group helpers
# ---------------------------------------------------------------------------


def test_helpers_return_none_when_account_missing() -> None:
    coord = _coordinator()
    coord.account = None
    assert coord.get_space("s1") is None
    assert coord.get_device("s1", "d1") is None
    assert coord.get_room("s1", "r1") is None
    assert coord.get_group("s1", "g1") is None


def test_helpers_return_objects_when_present() -> None:
    coord = _coordinator()
    coord.account = _account_with_space()
    space = coord.get_space("s1")
    assert space is not None and space.id == "s1"
    assert coord.get_device("s1", "d1").id == "d1"
    assert coord.get_room("s1", "r1").id == "r1"
    assert coord.get_group("s1", "g1").id == "g1"


def test_helpers_return_none_for_unknown_ids() -> None:
    coord = _coordinator()
    coord.account = _account_with_space()
    assert coord.get_space("nope") is None
    assert coord.get_device("s1", "nope") is None
    assert coord.get_device("nope", "d1") is None
    assert coord.get_room("s1", "nope") is None
    assert coord.get_group("s1", "nope") is None


# ---------------------------------------------------------------------------
# _async_update_data — initial load
# ---------------------------------------------------------------------------


async def test_update_data_initial_load_returns_account() -> None:
    coord = _coordinator()
    coord._initial_load_done = False
    coord.last_update_success = False  # exercises "connection restored" log line

    async def _init_account() -> None:
        coord.account = _account_with_space(SecurityState.ARMED)

    coord._async_init_account = AsyncMock(side_effect=_init_account)
    coord._async_update_spaces_from_hubs = AsyncMock()
    coord._async_update_devices = AsyncMock()
    coord._async_update_video_edges = AsyncMock()
    coord._async_update_smart_locks = AsyncMock()
    coord._async_update_notifications = AsyncMock()
    coord._async_restore_smart_locks = AsyncMock()
    coord._async_cleanup_stale_devices = MagicMock()
    coord._manage_door_sensor_polling = MagicMock()
    # No SSE/SQS/ONVIF bootstrap on this path.
    coord._sse_url = None
    coord._aws_access_key_id = None
    coord._sse_initialized = True
    coord._sqs_initialized = True
    coord._onvif_initialized = True
    coord.config_entry = None  # skip the background-task bootstrap block

    result = await coord._async_update_data()

    assert result is coord.account
    assert coord._initial_load_done is True
    assert coord._consecutive_auth_errors == 0
    coord._async_init_account.assert_awaited_once()
    coord._async_update_devices.assert_awaited()


async def test_update_data_initial_load_proxy_mode_sequential() -> None:
    """Proxy mode runs the per-space loads sequentially (not via gather)."""
    coord = _coordinator()
    coord.api.is_proxy_mode = True
    coord._initial_load_done = False

    async def _init_account() -> None:
        coord.account = _account_with_space(SecurityState.DISARMED)

    coord._async_init_account = AsyncMock(side_effect=_init_account)
    coord._async_update_spaces_from_hubs = AsyncMock()
    coord._async_update_devices = AsyncMock()
    coord._async_update_video_edges = AsyncMock()
    coord._async_update_smart_locks = AsyncMock()
    coord._async_update_notifications = AsyncMock()
    coord._async_restore_smart_locks = AsyncMock()
    coord._async_cleanup_stale_devices = MagicMock()
    coord._manage_door_sensor_polling = MagicMock()
    coord._sse_url = None
    coord._aws_access_key_id = None
    coord._sse_initialized = True
    coord._sqs_initialized = True
    coord._onvif_initialized = True
    coord.config_entry = None

    await coord._async_update_data()

    # Disarmed space → door sensor polling started for that space.
    coord._manage_door_sensor_polling.assert_called_once_with(True, SecurityState.DISARMED)
    coord._async_update_video_edges.assert_awaited_once_with("s1")


async def test_update_data_bypass_cache_flag_consumed() -> None:
    coord = _coordinator()
    coord._bypass_cache_next_refresh = True
    coord._initial_load_done = True
    coord.account = _account_with_space()
    coord._async_update_spaces_from_hubs = AsyncMock()
    coord._async_update_devices = AsyncMock()
    coord._async_update_video_edges = AsyncMock()
    coord._async_update_smart_locks = AsyncMock()
    coord._reset_expired_motion_detections = MagicMock()

    await coord._async_update_data()

    coord.api.bypass_cache_next.assert_called_once()
    assert coord._bypass_cache_next_refresh is False


# ---------------------------------------------------------------------------
# _async_update_data — periodic update + realtime throttle
# ---------------------------------------------------------------------------


async def test_periodic_update_no_realtime_always_refreshes_video_smart() -> None:
    """Without SSE/SQS, video edges + smart locks refresh every cycle."""
    coord = _coordinator()
    coord._initial_load_done = True
    coord.sse_manager = None
    coord.sqs_manager = None
    coord._last_metadata_refresh = 1e18  # far future → no metadata refresh
    coord.account = _account_with_space()
    # Give the space a video edge + smart lock so the refresh branch runs.
    coord.account.spaces["s1"].video_edges["ve1"] = MagicMock()
    coord.account.spaces["s1"].smart_locks["sl1"] = MagicMock()

    coord._async_update_spaces_from_hubs = AsyncMock()
    coord._async_update_devices = AsyncMock()
    coord._async_update_video_edges = AsyncMock()
    coord._async_update_smart_locks = AsyncMock()
    coord._reset_expired_motion_detections = MagicMock()

    await coord._async_update_data()

    coord._async_update_video_edges.assert_awaited_once_with("s1")
    coord._async_update_smart_locks.assert_awaited_once_with("s1")


async def test_periodic_update_realtime_throttles_video_smart() -> None:
    """Manager present + an armed space + cycle not on the Nth tick → skip.

    Realtime freshness (SSE/SQS) is only trustworthy while the alarm is
    armed, so this test uses an ARMED space — a disarmed one would force
    the refresh regardless of the cycle counter (see the dedicated
    all-disarmed test below).
    """
    coord = _coordinator()
    coord._initial_load_done = True
    coord.sse_manager = MagicMock()  # realtime active
    coord._realtime_skip_factor = 3
    coord._cycle_counter = 0  # becomes 1 after increment → 1 % 3 != 0 → skip
    coord._last_metadata_refresh = 1e18
    coord.account = _account_with_space(SecurityState.ARMED)
    coord.account.spaces["s1"].video_edges["ve1"] = MagicMock()
    coord.account.spaces["s1"].smart_locks["sl1"] = MagicMock()

    coord._async_update_spaces_from_hubs = AsyncMock()
    coord._async_update_devices = AsyncMock()
    coord._async_update_video_edges = AsyncMock()
    coord._async_update_smart_locks = AsyncMock()
    coord._reset_expired_motion_detections = MagicMock()

    await coord._async_update_data()

    coord._async_update_video_edges.assert_not_awaited()
    coord._async_update_smart_locks.assert_not_awaited()
    assert coord._cycle_counter == 1


async def test_periodic_update_realtime_refreshes_on_nth_cycle() -> None:
    """Manager present + an armed space: the Nth cycle still does the full sync."""
    coord = _coordinator()
    coord._initial_load_done = True
    coord.sqs_manager = MagicMock()  # realtime active
    coord._realtime_skip_factor = 3
    coord._cycle_counter = 2  # becomes 3 after increment → 3 % 3 == 0 → refresh
    coord._last_metadata_refresh = 1e18
    coord.account = _account_with_space(SecurityState.ARMED)
    coord.account.spaces["s1"].video_edges["ve1"] = MagicMock()
    coord.account.spaces["s1"].smart_locks["sl1"] = MagicMock()

    coord._async_update_spaces_from_hubs = AsyncMock()
    coord._async_update_devices = AsyncMock()
    coord._async_update_video_edges = AsyncMock()
    coord._async_update_smart_locks = AsyncMock()
    coord._reset_expired_motion_detections = MagicMock()

    await coord._async_update_data()

    coord._async_update_video_edges.assert_awaited_once_with("s1")
    coord._async_update_smart_locks.assert_awaited_once_with("s1")


async def test_periodic_update_realtime_manager_but_all_disarmed_always_refreshes() -> None:
    """Manager present but every space disarmed → SSE/SQS deliver nothing.

    Ajax never pushes real-time events while disarmed, so a manager being
    instantiated is not enough on its own: the throttle must fall back to
    polling every cycle, same as the no-manager case, even off the Nth tick.
    """
    coord = _coordinator()
    coord._initial_load_done = True
    coord.sse_manager = MagicMock()  # manager present...
    coord._realtime_skip_factor = 3
    coord._cycle_counter = 0  # becomes 1 after increment → 1 % 3 != 0
    coord._last_metadata_refresh = 1e18
    coord.account = _account_with_space(SecurityState.DISARMED)  # ...but disarmed
    coord.account.spaces["s1"].video_edges["ve1"] = MagicMock()
    coord.account.spaces["s1"].smart_locks["sl1"] = MagicMock()

    coord._async_update_spaces_from_hubs = AsyncMock()
    coord._async_update_devices = AsyncMock()
    coord._async_update_video_edges = AsyncMock()
    coord._async_update_smart_locks = AsyncMock()
    coord._reset_expired_motion_detections = MagicMock()

    await coord._async_update_data()

    coord._async_update_video_edges.assert_awaited_once_with("s1")
    coord._async_update_smart_locks.assert_awaited_once_with("s1")


async def test_periodic_update_forced_metadata_refresh_clears_flag() -> None:
    coord = _coordinator()
    coord._initial_load_done = True
    coord._force_metadata_refresh = True
    coord.account = _account_with_space()
    coord._async_update_spaces_from_hubs = AsyncMock()
    coord._async_update_devices = AsyncMock()
    coord._async_update_video_edges = AsyncMock()
    coord._async_update_smart_locks = AsyncMock()
    coord._reset_expired_motion_detections = MagicMock()

    await coord._async_update_data()

    assert coord._force_metadata_refresh is False
    # full_refresh=True passed when metadata refresh is needed
    coord._async_update_spaces_from_hubs.assert_awaited_once_with(full_refresh=True)


# ---------------------------------------------------------------------------
# _async_update_data — error handling
# ---------------------------------------------------------------------------


async def test_update_data_auth_error_below_threshold_raises_update_failed() -> None:
    coord = _coordinator()
    coord._initial_load_done = True
    coord.account = _account_with_space()
    coord._max_auth_errors = 3
    coord._consecutive_auth_errors = 0
    coord._async_update_spaces_from_hubs = AsyncMock(side_effect=AjaxRestAuthError("bad token"))

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._consecutive_auth_errors == 1
    assert coord.stats["auth_errors"] == 1


async def test_update_data_auth_error_at_threshold_raises_reauth() -> None:
    coord = _coordinator()
    coord._initial_load_done = True
    coord.account = _account_with_space()
    coord._max_auth_errors = 3
    coord._consecutive_auth_errors = 2  # next failure hits the threshold
    coord._async_update_spaces_from_hubs = AsyncMock(side_effect=AjaxRestAuthError("expired"))

    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()

    assert coord._consecutive_auth_errors == 3
    assert coord.stats["auth_errors"] == 1


async def test_update_data_api_error_raises_update_failed() -> None:
    coord = _coordinator()
    coord._initial_load_done = True
    coord.account = _account_with_space()
    coord._async_update_spaces_from_hubs = AsyncMock(side_effect=AjaxRestApiError("503"))

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


async def test_update_data_api_error_resets_none_account() -> None:
    """If init never populates the account, raise a clear UpdateFailed."""
    coord = _coordinator()
    coord._initial_load_done = False
    coord.account = None
    coord._async_init_account = AsyncMock()  # leaves account None

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


# ---------------------------------------------------------------------------
# _update_polling_interval — adaptive interval
# ---------------------------------------------------------------------------


def test_update_polling_interval_armed_uses_armed_interval() -> None:
    coord = _coordinator()
    coord.account = _account_with_space(SecurityState.ARMED)
    coord._manage_door_sensor_polling = MagicMock()
    coord.update_interval = timedelta(seconds=UPDATE_INTERVAL)
    coord.api.suggested_interval = None

    coord._update_polling_interval(SecurityState.ARMED)

    assert coord.update_interval.total_seconds() == UPDATE_INTERVAL_ARMED
    # Armed → door polling should be turned off.
    coord._manage_door_sensor_polling.assert_called_once_with(False, SecurityState.ARMED)


def test_update_polling_interval_disarmed_uses_base_interval() -> None:
    coord = _coordinator()
    coord.account = _account_with_space(SecurityState.DISARMED)
    coord._manage_door_sensor_polling = MagicMock()
    coord.update_interval = timedelta(seconds=UPDATE_INTERVAL_ARMED)  # start armed
    coord.api.suggested_interval = None

    coord._update_polling_interval(SecurityState.DISARMED)

    assert coord.update_interval.total_seconds() == UPDATE_INTERVAL
    coord._manage_door_sensor_polling.assert_called_once_with(True, SecurityState.DISARMED)


def test_update_polling_interval_respects_higher_proxy_suggestion() -> None:
    coord = _coordinator()
    coord.account = _account_with_space(SecurityState.ARMED)
    coord._manage_door_sensor_polling = MagicMock()
    coord.update_interval = timedelta(seconds=UPDATE_INTERVAL)
    coord.api.suggested_interval = 120  # higher than armed base (60)

    coord._update_polling_interval(SecurityState.ARMED)

    assert coord.update_interval.total_seconds() == 120


def test_update_polling_interval_ignores_lower_proxy_suggestion() -> None:
    coord = _coordinator()
    coord.account = _account_with_space(SecurityState.ARMED)
    coord._manage_door_sensor_polling = MagicMock()
    coord.update_interval = timedelta(seconds=UPDATE_INTERVAL_ARMED)
    coord.api.suggested_interval = 10  # lower than base → ignored

    coord._update_polling_interval(SecurityState.ARMED)

    assert coord.update_interval.total_seconds() == UPDATE_INTERVAL_ARMED


def test_update_polling_interval_night_mode_polls_door_sensors() -> None:
    coord = _coordinator()
    coord.account = _account_with_space(SecurityState.NIGHT_MODE)
    coord._manage_door_sensor_polling = MagicMock()
    coord.update_interval = timedelta(seconds=UPDATE_INTERVAL)
    coord.api.suggested_interval = None

    coord._update_polling_interval(SecurityState.NIGHT_MODE)

    # Night mode → armed interval, but door polling still enabled.
    assert coord.update_interval.total_seconds() == UPDATE_INTERVAL_ARMED
    coord._manage_door_sensor_polling.assert_called_once_with(True, SecurityState.NIGHT_MODE)


# ---------------------------------------------------------------------------
# _manage_door_sensor_polling — start / stop logic
# ---------------------------------------------------------------------------


def test_manage_door_poll_disabled_when_fast_poll_off() -> None:
    coord = _coordinator()
    coord.account = _account_with_space(SecurityState.DISARMED)
    coord._door_sensor_fast_poll_enabled = False  # disabled
    coord._door_sensor_poll_task = None

    coord._manage_door_sensor_polling(True, SecurityState.DISARMED)

    # Stays off — no task created.
    assert coord._door_sensor_poll_task is None


def test_manage_door_poll_disabled_in_proxy_mode() -> None:
    coord = _coordinator()
    coord.account = _account_with_space(SecurityState.DISARMED)
    coord._door_sensor_fast_poll_enabled = True
    coord._sse_url = "https://proxy/sse"  # proxy mode → forced off
    coord._door_sensor_poll_task = None

    coord._manage_door_sensor_polling(True, SecurityState.DISARMED)

    assert coord._door_sensor_poll_task is None


def test_manage_door_poll_starts_task_when_enabled() -> None:
    coord = _coordinator()
    coord.account = _account_with_space(SecurityState.DISARMED)
    coord._door_sensor_fast_poll_enabled = True
    coord._sse_url = None
    coord._door_sensor_poll_task = None
    sentinel = MagicMock()

    def _capture_task(hass, coro, name):  # type: ignore[no-untyped-def]
        # Close the un-awaited coroutine to avoid a RuntimeWarning.
        coro.close()
        return sentinel

    coord.config_entry.async_create_background_task = MagicMock(side_effect=_capture_task)

    coord._manage_door_sensor_polling(True, SecurityState.DISARMED)

    assert coord._door_sensor_poll_task is sentinel
    coord.config_entry.async_create_background_task.assert_called_once()


def test_manage_door_poll_stops_task_when_no_space_needs_it() -> None:
    coord = _coordinator()
    coord.account = _account_with_space(SecurityState.ARMED)  # no disarmed/night space
    coord._door_sensor_fast_poll_enabled = True
    coord._sse_url = None
    task = MagicMock()
    coord._door_sensor_poll_task = task

    coord._manage_door_sensor_polling(False, SecurityState.ARMED)

    task.cancel.assert_called_once()
    assert coord._door_sensor_poll_task is None


# ---------------------------------------------------------------------------
# _should_refresh_metadata
# ---------------------------------------------------------------------------


def test_should_refresh_metadata_true_when_stale() -> None:
    coord = _coordinator()
    coord._last_metadata_refresh = 0.0  # epoch → very stale
    assert coord._should_refresh_metadata() is True


def test_should_refresh_metadata_false_when_recent() -> None:
    import time as _time

    coord = _coordinator()
    coord._last_metadata_refresh = _time.time()
    assert coord._should_refresh_metadata() is False


# ---------------------------------------------------------------------------
# _parse_door_state_from_wiring (static)
# ---------------------------------------------------------------------------


def test_parse_door_state_top_level_ok_is_closed() -> None:
    assert AjaxDataCoordinator._parse_door_state_from_wiring("OK", None) is False


def test_parse_door_state_top_level_not_ok_is_open() -> None:
    assert AjaxDataCoordinator._parse_door_state_from_wiring("ALARM", None) is True


def test_parse_door_state_none_external_is_closed() -> None:
    assert AjaxDataCoordinator._parse_door_state_from_wiring(None, None) is False


def test_parse_door_state_non_dict_wiring_falls_back() -> None:
    assert AjaxDataCoordinator._parse_door_state_from_wiring("ALARM", "not-a-dict") is True


def test_parse_door_state_two_eol() -> None:
    wiring = {
        "wiringSchemeType": "TWO_EOL",
        "contactTwoDetails": {"contactState": "ALARM"},
    }
    assert AjaxDataCoordinator._parse_door_state_from_wiring("OK", wiring) is True


def test_parse_door_state_one_eol() -> None:
    wiring = {
        "wiringSchemeType": "ONE_EOL",
        "contactDetails": {"contactState": "ALARM"},
    }
    assert AjaxDataCoordinator._parse_door_state_from_wiring("OK", wiring) is True


def test_parse_door_state_no_eol() -> None:
    wiring = {"wiringSchemeType": "NO_EOL", "contactState": "ALARM"}
    assert AjaxDataCoordinator._parse_door_state_from_wiring("OK", wiring) is True


def test_parse_door_state_no_eol_ok_stays_closed() -> None:
    wiring = {"wiringSchemeType": "NO_EOL", "contactState": "OK"}
    assert AjaxDataCoordinator._parse_door_state_from_wiring("OK", wiring) is False


# ---------------------------------------------------------------------------
# async_force_metadata_refresh / async_request_refresh_bypass_cache
# ---------------------------------------------------------------------------


async def test_async_force_metadata_refresh_sets_flag_and_refreshes() -> None:
    coord = _coordinator()
    coord._force_metadata_refresh = False
    coord.async_refresh = AsyncMock()

    await coord.async_force_metadata_refresh()

    assert coord._force_metadata_refresh is True
    coord.async_refresh.assert_awaited_once()


async def test_async_force_state_refresh_refreshes_without_full_flag() -> None:
    coord = _coordinator()
    coord._force_metadata_refresh = False
    coord.async_refresh = AsyncMock()

    await coord.async_force_state_refresh()

    # Light refresh must NOT force the account-wide metadata pass.
    assert coord._force_metadata_refresh is False
    coord.async_refresh.assert_awaited_once()


async def test_async_request_refresh_bypass_cache_sets_flag() -> None:
    coord = _coordinator()
    coord._bypass_cache_next_refresh = False
    coord.async_request_refresh = AsyncMock()

    await coord.async_request_refresh_bypass_cache()

    assert coord._bypass_cache_next_refresh is True
    coord.async_request_refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# _async_update_data — realtime bootstrap block (SSE / SQS / ONVIF)
# ---------------------------------------------------------------------------


async def test_initial_load_starts_sse_and_onvif_background_tasks() -> None:
    """Proxy mode (sse_url set) → SSE + ONVIF background tasks created."""
    coord = _coordinator()
    coord._initial_load_done = False

    async def _init_account() -> None:
        coord.account = _account_with_space(SecurityState.ARMED)

    coord._async_init_account = AsyncMock(side_effect=_init_account)
    coord._async_update_spaces_from_hubs = AsyncMock()
    coord._async_update_devices = AsyncMock()
    coord._async_update_video_edges = AsyncMock()
    coord._async_update_smart_locks = AsyncMock()
    coord._async_update_notifications = AsyncMock()
    coord._async_restore_smart_locks = AsyncMock()
    coord._async_cleanup_stale_devices = MagicMock()
    coord._manage_door_sensor_polling = MagicMock()
    coord._async_init_sse = MagicMock()
    coord._async_init_sqs = MagicMock()
    coord._async_init_onvif = MagicMock()

    coord._sse_url = "https://proxy/sse"
    coord._aws_access_key_id = None
    coord._sse_initialized = False
    coord._sqs_initialized = False
    coord._onvif_initialized = False

    created: list[str] = []

    def _create(hass, coro, name):  # type: ignore[no-untyped-def]
        coro.close()  # avoid un-awaited coroutine warning
        created.append(name)
        return MagicMock()

    coord.config_entry.async_create_background_task = MagicMock(side_effect=_create)

    await coord._async_update_data()

    assert "ajax_init_sse" in created
    assert "ajax_init_onvif" in created
    assert "ajax_init_sqs" not in created


async def test_initial_load_starts_sqs_when_no_sse() -> None:
    """Direct mode (aws key set, no sse_url) → SQS + ONVIF background tasks."""
    coord = _coordinator()
    coord._initial_load_done = False

    async def _init_account() -> None:
        coord.account = _account_with_space(SecurityState.ARMED)

    coord._async_init_account = AsyncMock(side_effect=_init_account)
    coord._async_update_spaces_from_hubs = AsyncMock()
    coord._async_update_devices = AsyncMock()
    coord._async_update_video_edges = AsyncMock()
    coord._async_update_smart_locks = AsyncMock()
    coord._async_update_notifications = AsyncMock()
    coord._async_restore_smart_locks = AsyncMock()
    coord._async_cleanup_stale_devices = MagicMock()
    coord._manage_door_sensor_polling = MagicMock()
    coord._async_init_sse = MagicMock()
    coord._async_init_sqs = MagicMock()
    coord._async_init_onvif = MagicMock()

    coord._sse_url = None
    coord._aws_access_key_id = "AKIA"
    coord._sse_initialized = False
    coord._sqs_initialized = False
    coord._onvif_initialized = False

    created: list[str] = []

    def _create(hass, coro, name):  # type: ignore[no-untyped-def]
        coro.close()
        created.append(name)
        return MagicMock()

    coord.config_entry.async_create_background_task = MagicMock(side_effect=_create)

    await coord._async_update_data()

    assert "ajax_init_sqs" in created
    assert "ajax_init_onvif" in created
    assert "ajax_init_sse" not in created


# ---------------------------------------------------------------------------
# _async_poll_door_sensors_loop
# ---------------------------------------------------------------------------


def _sleep_then_cancel():  # type: ignore[no-untyped-def]
    """asyncio.sleep replacement: run once, then break the while True loop."""
    calls = {"n": 0}

    async def _sleep(_seconds):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] >= 2:
            import asyncio as _a

            raise _a.CancelledError
        return None

    return _sleep


async def test_door_poll_loop_updates_door_contact_state() -> None:
    coord = _coordinator()
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    door = AjaxDevice(id="d1", name="Door", type=DeviceType.DOOR_CONTACT, space_id="s1", hub_id="hub1")
    door.attributes["door_opened"] = False
    space.devices["d1"] = door
    coord.account = AjaxAccount(user_id="u1", name="u", email="u@e.com")
    coord.account.spaces["s1"] = space

    # API reports the reed switch now open (reedClosed=False → door_opened=True).
    coord.api.async_get_devices = AsyncMock(return_value=[{"id": "d1", "reedClosed": False}])
    coord.async_set_updated_data = MagicMock()

    import asyncio as _a

    with pytest.raises(_a.CancelledError):  # noqa: SIM117
        import unittest.mock as _m

        with _m.patch(
            "custom_components.ajax.coordinator.asyncio.sleep",
            new=_sleep_then_cancel(),
        ):
            await coord._async_poll_door_sensors_loop()

    assert door.attributes["door_opened"] is True
    coord.async_set_updated_data.assert_called_once_with(coord.account)


async def test_door_poll_loop_no_account_skips() -> None:
    coord = _coordinator()
    coord.account = None
    coord.api.async_get_devices = AsyncMock()
    coord.async_set_updated_data = MagicMock()

    import asyncio as _a

    with pytest.raises(_a.CancelledError):  # noqa: SIM117
        import unittest.mock as _m

        with _m.patch(
            "custom_components.ajax.coordinator.asyncio.sleep",
            new=_sleep_then_cancel(),
        ):
            await coord._async_poll_door_sensors_loop()

    coord.api.async_get_devices.assert_not_called()
    coord.async_set_updated_data.assert_not_called()


async def test_door_poll_loop_transmitter_and_wire_input() -> None:
    coord = _coordinator()
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    trans = AjaxDevice(id="t1", name="Trans", type=DeviceType.TRANSMITTER, space_id="s1", hub_id="hub1")
    trans.attributes["externalContactTriggered"] = False
    wire = AjaxDevice(id="w1", name="Wire", type=DeviceType.WIRE_INPUT, space_id="s1", hub_id="hub1")
    wire.attributes["door_opened"] = False
    space.devices["t1"] = trans
    space.devices["w1"] = wire
    coord.account = AjaxAccount(user_id="u1", name="u", email="u@e.com")
    coord.account.spaces["s1"] = space

    coord.api.async_get_devices = AsyncMock(
        return_value=[
            {"id": "t1", "externalContactTriggered": True},
            {"id": "w1", "externalContactState": "ALARM"},
        ]
    )
    coord.async_set_updated_data = MagicMock()

    import asyncio as _a

    with pytest.raises(_a.CancelledError):  # noqa: SIM117
        import unittest.mock as _m

        with _m.patch(
            "custom_components.ajax.coordinator.asyncio.sleep",
            new=_sleep_then_cancel(),
        ):
            await coord._async_poll_door_sensors_loop()

    assert trans.attributes["externalContactTriggered"] is True
    assert wire.attributes["door_opened"] is True
    coord.async_set_updated_data.assert_called_once()


async def test_door_poll_loop_night_mode_filters_excluded_sensors() -> None:
    """Night mode only polls sensors NOT armed in night mode."""
    coord = _coordinator()
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.NIGHT_MODE)
    # This sensor is armed in night mode → must be filtered out → no poll → no update.
    door = AjaxDevice(id="d1", name="Door", type=DeviceType.DOOR_CONTACT, space_id="s1", hub_id="hub1")
    door.attributes["door_opened"] = False
    door.attributes["night_mode_arm"] = True
    space.devices["d1"] = door
    coord.account = AjaxAccount(user_id="u1", name="u", email="u@e.com")
    coord.account.spaces["s1"] = space

    coord.api.async_get_devices = AsyncMock(return_value=[{"id": "d1", "reedClosed": False}])
    coord.async_set_updated_data = MagicMock()

    import asyncio as _a

    with pytest.raises(_a.CancelledError):  # noqa: SIM117
        import unittest.mock as _m

        with _m.patch(
            "custom_components.ajax.coordinator.asyncio.sleep",
            new=_sleep_then_cancel(),
        ):
            await coord._async_poll_door_sensors_loop()

    # Sensor filtered out → contact_sensors empty → API never queried.
    coord.api.async_get_devices.assert_not_called()
    coord.async_set_updated_data.assert_not_called()


async def test_door_poll_loop_door_contact_via_wiring_external_state() -> None:
    """No reedClosed → fall back to externalContactState + wiring parser."""
    coord = _coordinator()
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    door = AjaxDevice(id="d1", name="Door", type=DeviceType.DOOR_CONTACT, space_id="s1", hub_id="hub1")
    door.attributes["door_opened"] = False
    space.devices["d1"] = door
    coord.account = AjaxAccount(user_id="u1", name="u", email="u@e.com")
    coord.account.spaces["s1"] = space

    coord.api.async_get_devices = AsyncMock(return_value=[{"id": "d1", "externalContactState": "ALARM"}])
    coord.async_set_updated_data = MagicMock()

    import asyncio as _a

    with pytest.raises(_a.CancelledError):  # noqa: SIM117
        import unittest.mock as _m

        with _m.patch(
            "custom_components.ajax.coordinator.asyncio.sleep",
            new=_sleep_then_cancel(),
        ):
            await coord._async_poll_door_sensors_loop()

    assert door.attributes["door_opened"] is True


async def test_door_poll_loop_handles_api_error() -> None:
    coord = _coordinator()
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    space.devices["d1"] = AjaxDevice(id="d1", name="Door", type=DeviceType.DOOR_CONTACT, space_id="s1", hub_id="hub1")
    coord.account = AjaxAccount(user_id="u1", name="u", email="u@e.com")
    coord.account.spaces["s1"] = space

    coord.api.async_get_devices = AsyncMock(side_effect=AjaxRestApiError("503"))
    coord.async_set_updated_data = MagicMock()

    import asyncio as _a

    with pytest.raises(_a.CancelledError):  # noqa: SIM117
        import unittest.mock as _m

        with _m.patch(
            "custom_components.ajax.coordinator.asyncio.sleep",
            new=_sleep_then_cancel(),
        ):
            await coord._async_poll_door_sensors_loop()

    # API error is swallowed → no data update emitted.
    coord.async_set_updated_data.assert_not_called()


# ---------------------------------------------------------------------------
# async_shutdown
# ---------------------------------------------------------------------------


async def test_async_shutdown_stops_all_managers_and_closes_api() -> None:
    coord = _coordinator()
    coord.sqs_manager = MagicMock()
    coord.sqs_manager.stop = AsyncMock()
    coord.sse_manager = MagicMock()
    coord.sse_manager.stop = AsyncMock()
    coord.onvif_manager = MagicMock()
    coord.onvif_manager.async_stop = AsyncMock()
    coord.api.close = AsyncMock()

    # The task is cancelled then awaited under suppress(CancelledError), so it
    # must be a real awaitable. Use a tiny stub recording cancel().
    class _FakeTask:
        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

        def __await__(self):  # type: ignore[no-untyped-def]
            async def _raise() -> None:
                raise _CancelledError

            return _raise().__await__()

    import asyncio as _asyncio

    _CancelledError = _asyncio.CancelledError
    task = _FakeTask()
    coord._door_sensor_poll_task = task

    await coord.async_shutdown()

    coord.sqs_manager.stop.assert_awaited_once()
    coord.sse_manager.stop.assert_awaited_once()
    coord.onvif_manager.async_stop.assert_awaited_once()
    assert task.cancelled is True
    assert coord._door_sensor_poll_task is None
    coord.api.close.assert_awaited_once()


async def test_async_shutdown_handles_manager_stop_errors() -> None:
    """A failing manager.stop() must not prevent api.close()."""
    coord = _coordinator()
    coord.sqs_manager = MagicMock()
    coord.sqs_manager.stop = AsyncMock(side_effect=RuntimeError("boom"))
    coord.sse_manager = MagicMock()
    coord.sse_manager.stop = AsyncMock(side_effect=RuntimeError("boom"))
    coord.onvif_manager = MagicMock()
    coord.onvif_manager.async_stop = AsyncMock(side_effect=RuntimeError("boom"))
    coord._door_sensor_poll_task = None
    coord.api.close = AsyncMock()

    await coord.async_shutdown()

    coord.api.close.assert_awaited_once()


async def test_async_shutdown_no_managers() -> None:
    """Shutdown with everything None just closes the API."""
    coord = _coordinator()
    coord.sqs_manager = None
    coord.sse_manager = None
    coord.onvif_manager = None
    coord._door_sensor_poll_task = None
    coord.api.close = AsyncMock()

    await coord.async_shutdown()

    coord.api.close.assert_awaited_once()


async def test_update_data_initial_load_gather_isolates_failures() -> None:
    """One failing space must not leave sibling loads orphaned.

    ``gather(return_exceptions=True)`` lets every per-space coroutine finish,
    then the first failure is re-raised so error handling stays unchanged.
    """
    coord = _coordinator()
    coord._initial_load_done = False

    async def _init_account() -> None:
        account = _account_with_space(SecurityState.ARMED)
        # Second space so the gather schedules two device loads.
        second = AjaxSpace(id="s2", name="Garage", hub_id="hub2")
        account.spaces["s2"] = second
        coord.account = account

    coord._async_init_account = AsyncMock(side_effect=_init_account)
    coord._async_update_spaces_from_hubs = AsyncMock()
    boom = AjaxRestApiError("space s1 exploded")

    async def _update_devices(space_id: str) -> None:
        if space_id != "s2":
            raise boom

    coord._async_update_devices = AsyncMock(side_effect=_update_devices)
    coord._async_update_video_edges = AsyncMock()
    coord._async_update_smart_locks = AsyncMock()
    coord._async_update_notifications = AsyncMock()
    coord._async_restore_smart_locks = AsyncMock()
    coord._async_cleanup_stale_devices = MagicMock()
    coord._manage_door_sensor_polling = MagicMock()
    coord._sse_url = None
    coord._aws_access_key_id = None
    coord._sse_initialized = True
    coord._sqs_initialized = True
    coord._onvif_initialized = True
    coord.config_entry = None
    coord.api.is_proxy_mode = False

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    # BOTH spaces were attempted — the failure did not cancel the sibling.
    called_spaces = {c.args[0] for c in coord._async_update_devices.await_args_list}
    assert called_spaces == {"s1", "s2"}


async def test_door_poll_loop_refreshes_smart_lock_state() -> None:
    """A bridged smart lock (no realtime events) must follow the 5 s fast
    poll instead of lagging on the 30-60 s main poll (#88)."""
    from custom_components.ajax.models import AjaxSmartLock

    coord = _coordinator()
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    lock = AjaxSmartLock(id="sl1", name="Doorman", space_id="s1")
    lock.is_locked = True
    lock.is_door_open = False
    space.smart_locks["sl1"] = lock
    coord.account = AjaxAccount(user_id="u1", name="u", email="u@e.com")
    coord.account.spaces["s1"] = space

    coord.api.async_get_devices = AsyncMock(
        return_value=[{"id": "sl1", "model": {"lockStatus": "UNLOCKED", "doorStatus": "OPEN"}}]
    )
    coord.async_set_updated_data = MagicMock()

    import asyncio as _a

    with pytest.raises(_a.CancelledError):  # noqa: SIM117
        import unittest.mock as _m

        with _m.patch(
            "custom_components.ajax.coordinator.asyncio.sleep",
            new=_sleep_then_cancel(),
        ):
            await coord._async_poll_door_sensors_loop()

    assert lock.is_locked is False
    assert lock.is_door_open is True
    coord.async_set_updated_data.assert_called_once_with(coord.account)
