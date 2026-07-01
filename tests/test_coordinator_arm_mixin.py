"""Tests for AjaxArmServiceMixin.

The HA-action tracking is the linchpin between user-triggered arm/disarm
and the SSE/SQS race window: a pending HA action protects optimistic
updates from being overwritten by a stale REST poll. A bug here flips
the alarm panel state on user-triggered arm.

We exercise the mixin directly via object.__new__ to avoid pulling in
the full coordinator init.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ajax._coordinator_arm import AjaxArmServiceMixin
from custom_components.ajax.api import AjaxRestApiError


def _make_mixin() -> AjaxArmServiceMixin:
    mixin = object.__new__(AjaxArmServiceMixin)
    mixin._pending_ha_actions = {}
    mixin._arm_locks = {}
    mixin.api = MagicMock()
    return mixin


# ---------------------------------------------------------------------------
# HA action tracking
# ---------------------------------------------------------------------------


def test_register_ha_action_records_timestamp() -> None:
    mixin = _make_mixin()
    mixin._register_ha_action("hub1")
    assert "hub1" in mixin._pending_ha_actions
    assert mixin._pending_ha_actions["hub1"] > 0


def test_has_pending_ha_action_true_within_window() -> None:
    mixin = _make_mixin()
    mixin._pending_ha_actions["hub1"] = time.time() - 2  # 2s ago
    assert mixin.has_pending_ha_action("hub1") is True


def test_has_pending_ha_action_false_outside_window() -> None:
    """Past the 10-second protection window, REST polling is safe to overwrite."""
    mixin = _make_mixin()
    mixin._pending_ha_actions["hub1"] = time.time() - 15
    assert mixin.has_pending_ha_action("hub1") is False


def test_has_pending_ha_action_false_for_unknown_hub() -> None:
    assert _make_mixin().has_pending_ha_action("never_seen") is False


def test_has_pending_ha_action_does_not_consume_flag() -> None:
    """The non-consuming variant must stay True across repeated checks."""
    mixin = _make_mixin()
    mixin._pending_ha_actions["hub1"] = time.time()
    assert mixin.has_pending_ha_action("hub1") is True
    assert mixin.has_pending_ha_action("hub1") is True  # still true
    assert "hub1" in mixin._pending_ha_actions


def test_arm_lock_for_returns_same_lock_per_space() -> None:
    """Subsequent calls must hand back the SAME lock — otherwise concurrent
    arm calls race past each other.
    """
    mixin = _make_mixin()
    lock_a = mixin._arm_lock_for("space_a")
    lock_b = mixin._arm_lock_for("space_b")
    assert lock_a is not lock_b

    assert mixin._arm_lock_for("space_a") is lock_a  # identity preserved
    assert isinstance(lock_a, asyncio.Lock)


# ---------------------------------------------------------------------------
# async_arm_space / async_disarm_space
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_arm_space_calls_api_and_registers_action() -> None:
    mixin = _make_mixin()
    mixin.api.async_arm = AsyncMock()
    await mixin.async_arm_space("space_a", force=True)
    mixin.api.async_arm.assert_awaited_once_with("space_a", ignore_problems=True)
    assert mixin.has_pending_ha_action("space_a") is True


@pytest.mark.asyncio
async def test_async_arm_space_propagates_api_error() -> None:
    """Surface the error to the service caller — silently swallowing would
    leave the user thinking the arm succeeded.
    """
    mixin = _make_mixin()
    mixin.api.async_arm = AsyncMock(side_effect=AjaxRestApiError("api down"))
    with pytest.raises(AjaxRestApiError):
        await mixin.async_arm_space("space_a")


@pytest.mark.asyncio
async def test_async_disarm_space_calls_api_and_registers_action() -> None:
    mixin = _make_mixin()
    mixin.api.async_disarm = AsyncMock()
    await mixin.async_disarm_space("space_a")
    mixin.api.async_disarm.assert_awaited_once_with("space_a")
    assert mixin.has_pending_ha_action("space_a") is True
