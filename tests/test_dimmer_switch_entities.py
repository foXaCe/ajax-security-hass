"""Tests for the LightSwitchDimmer switch entities.

These three switches (settings-list membership, a plain boolean attribute, and
the nested calibration flag) all do an optimistic write then call the API, with
a rollback path on failure. We build them with ``object.__new__`` and stub
async_write_ha_state + a mock coordinator API so the optimistic/rollback logic
is exercised without a running HA instance.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.ajax.models import AjaxDevice, DeviceType
from custom_components.ajax.switch import (
    AjaxDimmerBoolSwitch,
    AjaxDimmerCalibrationSwitch,
    AjaxDimmerSettingsSwitch,
)


def _device(attributes: dict | None = None, online: bool = True) -> AjaxDevice:
    return AjaxDevice(
        id="d1",
        name="Dimmer",
        type=DeviceType.WALLSWITCH,
        space_id="s1",
        hub_id="hub1",
        online=online,
        attributes=attributes or {},
    )


def _coordinator(device: AjaxDevice | None, *, update_success: bool = True, hub_id: str | None = "hub1"):
    space = SimpleNamespace(devices={"d1": device} if device else {}, hub_id=hub_id)
    return SimpleNamespace(
        get_space=lambda sid: space,
        last_update_success=update_success,
        api=SimpleNamespace(async_update_device=AsyncMock(), async_update_device_nested=AsyncMock()),
        async_request_refresh=AsyncMock(),
    )


# ---------------------------------------------------------------------------
# AjaxDimmerSettingsSwitch
# ---------------------------------------------------------------------------


def _settings_switch(device: AjaxDevice | None, **coord_kwargs) -> AjaxDimmerSettingsSwitch:
    sw = object.__new__(AjaxDimmerSettingsSwitch)
    sw._space_id = "s1"
    sw._device_id = "d1"
    sw._switch_def = {"key": "smooth_on", "translation_key": "smooth_on", "settings_key": "SMOOTH_ON"}
    sw.coordinator = _coordinator(device, **coord_kwargs)
    sw.async_write_ha_state = lambda: None
    return sw


def test_settings_switch_is_on_reflects_membership() -> None:
    assert _settings_switch(_device({"settingsSwitch": ["SMOOTH_ON"]})).is_on is True
    assert _settings_switch(_device({"settingsSwitch": []})).is_on is False
    assert _settings_switch(None).is_on is False


def test_settings_switch_available() -> None:
    assert _settings_switch(_device(online=True)).available is True
    assert _settings_switch(_device(online=False)).available is False
    assert _settings_switch(None).available is False
    assert _settings_switch(_device(), update_success=False).available is False


@pytest.mark.asyncio
async def test_settings_switch_turn_on_appends_key_and_calls_api() -> None:
    device = _device({"settingsSwitch": []})
    sw = _settings_switch(device)
    await sw.async_turn_on()
    assert device.attributes["settingsSwitch"] == ["SMOOTH_ON"]
    sw.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {"settingsSwitch": ["SMOOTH_ON"]})


@pytest.mark.asyncio
async def test_settings_switch_turn_off_removes_key() -> None:
    device = _device({"settingsSwitch": ["SMOOTH_ON", "OTHER"]})
    sw = _settings_switch(device)
    await sw.async_turn_off()
    assert device.attributes["settingsSwitch"] == ["OTHER"]


@pytest.mark.asyncio
async def test_settings_switch_rollback_on_api_error() -> None:
    device = _device({"settingsSwitch": []})
    sw = _settings_switch(device)
    sw.coordinator.api.async_update_device = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()
    # optimistic append rolled back to the original empty list
    assert device.attributes["settingsSwitch"] == []
    sw.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_settings_switch_raises_when_device_missing() -> None:
    sw = _settings_switch(None)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()


# ---------------------------------------------------------------------------
# AjaxDimmerBoolSwitch
# ---------------------------------------------------------------------------


def _bool_switch(device: AjaxDevice | None, **coord_kwargs) -> AjaxDimmerBoolSwitch:
    sw = object.__new__(AjaxDimmerBoolSwitch)
    sw._space_id = "s1"
    sw._device_id = "d1"
    sw._attr_key = "ledIndication"
    sw._api_key = "ledIndicationMode"
    sw.coordinator = _coordinator(device, **coord_kwargs)
    sw.async_write_ha_state = lambda: None
    return sw


def test_bool_switch_is_on() -> None:
    assert _bool_switch(_device({"ledIndication": True})).is_on is True
    assert _bool_switch(_device({})).is_on is False
    assert _bool_switch(None).is_on is False


@pytest.mark.asyncio
async def test_bool_switch_turn_on_sets_attr_and_calls_api() -> None:
    device = _device({"ledIndication": False})
    sw = _bool_switch(device)
    await sw.async_turn_on()
    assert device.attributes["ledIndication"] is True
    sw.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {"ledIndicationMode": True})


@pytest.mark.asyncio
async def test_bool_switch_rollback_restores_old_value() -> None:
    device = _device({"ledIndication": True})
    sw = _bool_switch(device)
    sw.coordinator.api.async_update_device = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_off()
    assert device.attributes["ledIndication"] is True  # restored
    sw.coordinator.async_request_refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# AjaxDimmerCalibrationSwitch
# ---------------------------------------------------------------------------


def _calib_switch(device: AjaxDevice | None, **coord_kwargs) -> AjaxDimmerCalibrationSwitch:
    sw = object.__new__(AjaxDimmerCalibrationSwitch)
    sw._space_id = "s1"
    sw._device_id = "d1"
    sw.coordinator = _coordinator(device, **coord_kwargs)
    sw.async_write_ha_state = lambda: None
    return sw


def test_calibration_switch_is_on() -> None:
    assert _calib_switch(_device({"dimmerSettings": {"calibration": "ENABLED"}})).is_on is True
    assert _calib_switch(_device({"dimmerSettings": {"calibration": "DISABLED"}})).is_on is False
    assert _calib_switch(_device({})).is_on is False
    assert _calib_switch(None).is_on is False


@pytest.mark.asyncio
async def test_calibration_switch_turn_on_calls_nested_api_and_refreshes() -> None:
    device = _device({"dimmerSettings": {"calibration": "DISABLED"}})
    sw = _calib_switch(device)
    await sw.async_turn_on()
    sw.coordinator.api.async_update_device_nested.assert_awaited_once_with(
        "hub1", "d1", {"dimmerSettings": {"calibration": "ENABLED"}}
    )
    sw.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_calibration_switch_error_still_refreshes_and_raises() -> None:
    device = _device({"dimmerSettings": {"calibration": "DISABLED"}})
    sw = _calib_switch(device)
    sw.coordinator.api.async_update_device_nested = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()
    sw.coordinator.async_request_refresh.assert_awaited_once()
