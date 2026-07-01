"""Tests for AjaxPanicButton.

The panic button triggers a real Ajax emergency alarm — pressing it
twice in quick succession would double-page the monitoring centre, so
the entity owns a per-instance cooldown. We pin both branches: the
first press calls the coordinator, the second within the cooldown
window raises a translated HomeAssistantError.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.ajax.button import AjaxPanicButton


def _make_panic() -> AjaxPanicButton:
    button = object.__new__(AjaxPanicButton)
    button._space_id = "s1"
    button._entry = SimpleNamespace(entry_id="entry1")
    button._last_press_ts = 0.0
    button.coordinator = SimpleNamespace(async_press_panic_button=AsyncMock())
    return button


@pytest.mark.asyncio
async def test_panic_button_invokes_coordinator() -> None:
    button = _make_panic()
    await button.async_press()
    button.coordinator.async_press_panic_button.assert_awaited_once_with("s1")


@pytest.mark.asyncio
async def test_panic_button_cooldown_rejects_double_tap() -> None:
    """A second press inside the cooldown window must raise — no silent no-op
    that would mislead the user, and no second API call.
    """
    button = _make_panic()
    await button.async_press()  # first press → ok
    with pytest.raises(HomeAssistantError):
        await button.async_press()  # second press inside cooldown → rejected
    assert button.coordinator.async_press_panic_button.await_count == 1


@pytest.mark.asyncio
async def test_panic_button_propagates_coordinator_failure() -> None:
    """If the coordinator's panic call fails, surface the error to the user."""
    button = _make_panic()
    button.coordinator.async_press_panic_button = AsyncMock(side_effect=RuntimeError("api down"))
    with pytest.raises(HomeAssistantError):
        await button.async_press()


@pytest.mark.asyncio
async def test_panic_button_failed_press_does_not_consume_cooldown() -> None:
    """A failed panic must NOT burn the cooldown — an emergency retry has to be
    possible immediately after a transient API failure.
    """
    button = _make_panic()
    button.coordinator.async_press_panic_button = AsyncMock(side_effect=RuntimeError("api down"))
    with pytest.raises(HomeAssistantError):
        await button.async_press()  # first press fails

    # The cooldown timestamp was restored, so a retry reaches the coordinator
    # again instead of being rejected by the cooldown guard.
    button.coordinator.async_press_panic_button = AsyncMock()
    await button.async_press()
    button.coordinator.async_press_panic_button.assert_awaited_once_with("s1")
