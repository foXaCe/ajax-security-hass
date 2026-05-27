"""Unit tests for ``custom_components.ajax.models``.

The optimistic-update guard is the load-bearing invariant for switch
updates: a polling cycle that fires while a switch action is still in
flight must not roll back the local value. These tests pin that
contract.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from custom_components.ajax.models import AjaxDevice, DeviceType


def _make_device() -> AjaxDevice:
    """Build a minimal AjaxDevice for unit tests."""
    return AjaxDevice(
        id="dev1",
        name="Test device",
        type=DeviceType.SOCKET,
        space_id="space1",
        hub_id="hub1",
    )


def test_mark_optimistic_then_is_optimistic_returns_true() -> None:
    device = _make_device()
    device.mark_optimistic("night_mode_arm", ttl_seconds=15)
    assert device.is_optimistic("night_mode_arm") is True


def test_is_optimistic_false_for_unmarked_attribute() -> None:
    device = _make_device()
    assert device.is_optimistic("never_marked") is False


def test_is_optimistic_false_for_other_attribute() -> None:
    device = _make_device()
    device.mark_optimistic("always_active", ttl_seconds=15)
    # Marking always_active must not protect chimes_enabled — the guard
    # is strictly per-attribute, otherwise unrelated polled values would
    # be frozen by any switch action.
    assert device.is_optimistic("chimes_enabled") is False


def test_is_optimistic_expires_after_ttl() -> None:
    device = _make_device()
    now = 1_000_000.0
    with patch("custom_components.ajax.models.time", wraps=time) as mocked:
        mocked.time.return_value = now
        device.mark_optimistic("always_active", ttl_seconds=15)
        # Still inside the window.
        mocked.time.return_value = now + 14.0
        assert device.is_optimistic("always_active") is True
        # Window elapsed.
        mocked.time.return_value = now + 15.1
        assert device.is_optimistic("always_active") is False


def test_is_optimistic_garbage_collects_expired_entry() -> None:
    """Expired entries must be dropped so the guard dict cannot grow forever."""
    device = _make_device()
    now = 1_000_000.0
    with patch("custom_components.ajax.models.time", wraps=time) as mocked:
        mocked.time.return_value = now
        device.mark_optimistic("always_active", ttl_seconds=1)
        mocked.time.return_value = now + 5.0
        assert device.is_optimistic("always_active") is False
        assert "always_active" not in device.attributes.get("_optimistic_attrs", {})


def test_mark_optimistic_refreshes_existing_window() -> None:
    """A second call must extend the protection, not be ignored."""
    device = _make_device()
    now = 1_000_000.0
    with patch("custom_components.ajax.models.time", wraps=time) as mocked:
        mocked.time.return_value = now
        device.mark_optimistic("always_active", ttl_seconds=10)
        mocked.time.return_value = now + 8.0
        device.mark_optimistic("always_active", ttl_seconds=10)
        # Original window would have expired at now+10; the refresh pushes
        # the expiry to now+18, so 12s after the original call we should
        # still be protected.
        mocked.time.return_value = now + 12.0
        assert device.is_optimistic("always_active") is True


def test_has_battery() -> None:
    device = _make_device()
    assert device.has_battery is False
    device.battery_level = 50
    assert device.has_battery is True


def test_is_low_battery() -> None:
    device = _make_device()
    # No battery info → must NOT report low (would create false alarms).
    assert device.is_low_battery is False
    device.battery_level = 80
    assert device.is_low_battery is False
    device.battery_level = 15
    assert device.is_low_battery is True


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (None, False),
        (0, True),
        (19, True),
        (20, False),
        (100, False),
    ],
)
def test_is_low_battery_boundary(level: int | None, expected: bool) -> None:
    """The 20% threshold is the contract documented in models.py."""
    device = _make_device()
    device.battery_level = level
    assert device.is_low_battery is expected
