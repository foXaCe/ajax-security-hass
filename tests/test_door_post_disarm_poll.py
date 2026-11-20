"""Door contacts are polled for a while right after a disarm (#285).

Realtime events can be missing then even though the devices keep reporting,
so a bounded polling window runs whatever the fast-polling option or the
proxy mode says.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from custom_components.ajax import _coordinator_door_poll as door_poll
from custom_components.ajax.const import POST_DISARM_POLL_SECONDS
from custom_components.ajax.coordinator import AjaxDataCoordinator
from custom_components.ajax.models import SecurityState


def _coordinator(*, proxy: bool = True, fast: bool = False) -> AjaxDataCoordinator:
    coord = object.__new__(AjaxDataCoordinator)
    coord.hass = MagicMock()
    coord.config_entry = MagicMock()
    # The polling coroutine is not run here: close it so it is not left unawaited.
    coord.config_entry.async_create_background_task.side_effect = lambda _hass, coro, _name: coro.close()
    coord.account = None
    coord._initial_load_done = True
    coord._door_sensor_poll_task = None
    coord._door_sensor_poll_security_state = SecurityState.ARMED
    coord._door_sensor_fast_poll_enabled = fast
    coord._sse_url = "https://proxy.example/sse" if proxy else None
    coord._door_post_disarm_until = 0.0
    return coord


def test_disarm_in_proxy_mode_starts_a_bounded_window() -> None:
    coord = _coordinator(proxy=True)
    with patch.object(door_poll.time, "monotonic", return_value=1000.0):
        coord._manage_door_sensor_polling(True, SecurityState.DISARMED)
    assert coord._door_post_disarm_until == 1000.0 + POST_DISARM_POLL_SECONDS
    coord.config_entry.async_create_background_task.assert_called_once()


def test_no_polling_in_proxy_mode_once_the_window_is_over() -> None:
    coord = _coordinator(proxy=True)
    coord._door_sensor_poll_security_state = SecurityState.DISARMED  # no new transition
    coord._door_post_disarm_until = 1000.0
    with patch.object(door_poll.time, "monotonic", return_value=1000.0 + 1):
        coord._manage_door_sensor_polling(True, SecurityState.DISARMED)
    coord.config_entry.async_create_background_task.assert_not_called()


def test_staying_disarmed_does_not_reopen_the_window() -> None:
    coord = _coordinator(proxy=True)
    coord._door_sensor_poll_security_state = SecurityState.DISARMED
    with patch.object(door_poll.time, "monotonic", return_value=5000.0):
        coord._manage_door_sensor_polling(True, SecurityState.DISARMED)
    assert coord._door_post_disarm_until == 0.0


def test_no_window_before_the_initial_load() -> None:
    coord = _coordinator(proxy=True)
    coord._initial_load_done = False
    with patch.object(door_poll.time, "monotonic", return_value=1000.0):
        coord._manage_door_sensor_polling(True, SecurityState.DISARMED)
    assert coord._door_post_disarm_until == 0.0
    coord.config_entry.async_create_background_task.assert_not_called()


def test_direct_mode_with_fast_polling_is_unchanged() -> None:
    coord = _coordinator(proxy=False, fast=True)
    coord._door_sensor_poll_security_state = SecurityState.DISARMED
    coord._manage_door_sensor_polling(True, SecurityState.DISARMED)
    coord.config_entry.async_create_background_task.assert_called_once()


async def test_loop_stops_by_itself_when_the_window_ends() -> None:
    coord = _coordinator(proxy=True)
    coord._door_post_disarm_until = 1000.0
    coord._door_sensor_poll_task = MagicMock()

    async def _no_sleep(_delay: float) -> None:
        return None

    with (
        patch.object(door_poll.asyncio, "sleep", _no_sleep),
        patch.object(door_poll.time, "monotonic", return_value=1000.0 + 1),
    ):
        await coord._async_poll_door_sensors_loop()
    assert coord._door_sensor_poll_task is None
