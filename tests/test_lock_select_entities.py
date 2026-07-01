"""Tests for AjaxLock and select entity properties.

Both entities expose state derived from coordinator data — the lock from
``space.smart_locks[lock_id].is_locked`` and the selects from various
device attribute mappings. Bugs here surface as 'unknown' instead of the
actual state.

The lock is intentionally read-only (Ajax does not expose lock/unlock
endpoints) so calling lock/unlock must raise a translated HA error
instead of silently no-op'ing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.ajax.lock import AjaxLock
from custom_components.ajax.models import AjaxDevice, AjaxSmartLock, DeviceType, SecurityState
from custom_components.ajax.select import (
    SHOCK_SENSITIVITY_OPTIONS,
    AjaxShockSensitivitySelect,
)

# ---------------------------------------------------------------------------
# AjaxLock
# ---------------------------------------------------------------------------


def _make_lock(smart_lock: AjaxSmartLock | None, *, hub_id: str | None = "hub1") -> AjaxLock:
    lock = object.__new__(AjaxLock)
    lock._space_id = "s1"
    lock._smart_lock_id = "sl1"
    space = SimpleNamespace(smart_locks={"sl1": smart_lock} if smart_lock else {}, hub_id=hub_id)
    lock.coordinator = SimpleNamespace(
        last_update_success=True,
        get_space=lambda sid: space,
        api=SimpleNamespace(async_send_device_command=AsyncMock()),
        async_request_refresh=AsyncMock(),
    )
    return lock


def test_lock_is_locked_returns_state_from_smart_lock() -> None:
    sl = AjaxSmartLock(id="sl1", name="Front Door", space_id="s1")
    sl.is_locked = True
    assert _make_lock(sl).is_locked is True


def test_lock_is_locked_returns_none_before_first_event() -> None:
    """Smart locks ship `is_locked=None` until the first SSE/SQS event arrives."""
    sl = AjaxSmartLock(id="sl1", name="Front Door", space_id="s1")
    sl.is_locked = None
    assert _make_lock(sl).is_locked is None


def test_lock_is_locked_returns_none_when_smart_lock_missing() -> None:
    assert _make_lock(None).is_locked is None


def test_lock_available_tracks_smart_lock_presence() -> None:
    sl = AjaxSmartLock(id="sl1", name="Front Door", space_id="s1")
    assert _make_lock(sl).available is True
    assert _make_lock(None).available is False


@pytest.mark.asyncio
async def test_lock_async_lock_raises_translated_error() -> None:
    """Read-only entity — must reject lock command with a translated message."""
    with pytest.raises(HomeAssistantError):
        await _make_lock(AjaxSmartLock(id="sl1", name="X", space_id="s1")).async_lock()


@pytest.mark.asyncio
async def test_lock_async_unlock_sends_unlock_device_command() -> None:
    """Unlock issues the Ajax UNLOCK_DEVICE command and refreshes."""
    sl = AjaxSmartLock(id="sl1", name="Front Door", space_id="s1", raw_data={"deviceType": "SmartLockYale"})
    lock = _make_lock(sl)
    await lock.async_unlock()
    lock.coordinator.api.async_send_device_command.assert_awaited_once_with(
        "hub1", "sl1", "UNLOCK_DEVICE", "SmartLockYale"
    )
    lock.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_lock_async_unlock_defaults_device_type() -> None:
    """Falls back to a generic deviceType when the record has none."""
    lock = _make_lock(AjaxSmartLock(id="sl1", name="X", space_id="s1"))
    await lock.async_unlock()
    *_, device_type = lock.coordinator.api.async_send_device_command.await_args[0]
    assert device_type == "SmartLock"


@pytest.mark.asyncio
async def test_lock_async_unlock_no_hub_raises() -> None:
    lock = _make_lock(AjaxSmartLock(id="sl1", name="X", space_id="s1"), hub_id=None)
    with pytest.raises(HomeAssistantError):
        await lock.async_unlock()


@pytest.mark.asyncio
async def test_lock_async_unlock_api_error_raises() -> None:
    """A failed UNLOCK_DEVICE surfaces a translated HomeAssistantError."""
    lock = _make_lock(AjaxSmartLock(id="sl1", name="X", space_id="s1"))
    lock.coordinator.api.async_send_device_command = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError):
        await lock.async_unlock()


# ---------------------------------------------------------------------------
# AjaxShockSensitivitySelect
# ---------------------------------------------------------------------------


def _make_select(
    *, attribute_value: int | None = None, security_state: SecurityState = SecurityState.DISARMED, hub_id: str = "hub1"
) -> AjaxShockSensitivitySelect:
    device = AjaxDevice(
        id="d1",
        name="Door Test",
        type=DeviceType.DOOR_CONTACT,
        space_id="s1",
        hub_id=hub_id,
        attributes={"shock_sensor_sensitivity": attribute_value} if attribute_value is not None else {},
    )
    space = SimpleNamespace(devices={"d1": device}, security_state=security_state, hub_id=hub_id)

    select = object.__new__(AjaxShockSensitivitySelect)
    select._space_id = "s1"
    select._device_id = "d1"
    select.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: space)
    return select


def test_shock_select_current_option_maps_value_to_label() -> None:
    """API ships 0/4/7 — entity exposes low/normal/high."""
    for value, expected in SHOCK_SENSITIVITY_OPTIONS.items():
        assert _make_select(attribute_value=value).current_option == expected


def test_shock_select_current_option_returns_none_for_unmapped_value() -> None:
    """An out-of-range value must surface as `unknown`, never an arbitrary default."""
    assert _make_select(attribute_value=99).current_option is None


def test_shock_select_current_option_returns_none_when_attribute_missing() -> None:
    assert _make_select(attribute_value=None).current_option is None


@pytest.mark.asyncio
async def test_shock_select_refuses_change_when_system_is_armed() -> None:
    """Ajax requires the system disarmed to mutate device config — surface a translated error."""
    select = _make_select(attribute_value=0, security_state=SecurityState.ARMED)
    with pytest.raises(ServiceValidationError):
        await select.async_select_option("low")
