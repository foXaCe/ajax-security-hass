"""Extra coverage for the dimmer Bool / Calibration / Settings switch edges.

Covers availability, device_info, is_on and the ``device_not_found`` /
``hub_not_found`` guards in _switch_dimmer.py via the same ``object.__new__``
+ SimpleNamespace coordinator pattern as tests/test_dimmer_switch_entities.py.
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
        entry_id="entry1",
        api=SimpleNamespace(async_update_device=AsyncMock(), async_update_device_nested=AsyncMock()),
        async_request_refresh=AsyncMock(),
    )


def _bool_switch(device: AjaxDevice | None, **coord_kwargs) -> AjaxDimmerBoolSwitch:
    sw = object.__new__(AjaxDimmerBoolSwitch)
    sw._space_id = "s1"
    sw._device_id = "d1"
    sw._attr_key = "always_on"
    sw._api_key = "alwaysOn"
    sw.coordinator = _coordinator(device, **coord_kwargs)
    sw.async_write_ha_state = lambda: None
    return sw


def _calib_switch(device: AjaxDevice | None, **coord_kwargs) -> AjaxDimmerCalibrationSwitch:
    sw = object.__new__(AjaxDimmerCalibrationSwitch)
    sw._space_id = "s1"
    sw._device_id = "d1"
    sw.coordinator = _coordinator(device, **coord_kwargs)
    sw.async_write_ha_state = lambda: None
    return sw


def _settings_switch(device: AjaxDevice | None, **coord_kwargs) -> AjaxDimmerSettingsSwitch:
    sw = object.__new__(AjaxDimmerSettingsSwitch)
    sw._space_id = "s1"
    sw._device_id = "d1"
    sw._switch_def = {"key": "smooth_on", "translation_key": "smooth_on", "settings_key": "SMOOTH_ON"}
    sw.coordinator = _coordinator(device, **coord_kwargs)
    sw.async_write_ha_state = lambda: None
    return sw


# ---------------------------------------------------------------------------
# AjaxDimmerBoolSwitch
# ---------------------------------------------------------------------------


def test_bool_switch_available() -> None:
    assert _bool_switch(_device(online=True)).available is True
    assert _bool_switch(_device(online=False)).available is False
    assert _bool_switch(None).available is False
    assert _bool_switch(_device(), update_success=False).available is False


def test_bool_switch_is_on() -> None:
    assert _bool_switch(_device({"always_on": True})).is_on is True
    assert _bool_switch(_device({})).is_on is False
    assert _bool_switch(None).is_on is False


def test_bool_switch_device_info_uses_identifier() -> None:
    info = _bool_switch(_device()).device_info
    assert info["identifiers"]


@pytest.mark.asyncio
async def test_bool_switch_turn_on_calls_api() -> None:
    device = _device({"always_on": False})
    sw = _bool_switch(device)
    await sw.async_turn_on()
    assert device.attributes["always_on"] is True
    sw.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {"alwaysOn": True})


@pytest.mark.asyncio
async def test_bool_switch_set_value_no_device_raises() -> None:
    sw = _bool_switch(None)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()


@pytest.mark.asyncio
async def test_bool_switch_set_value_no_hub_raises() -> None:
    sw = _bool_switch(_device(), hub_id=None)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_off()


@pytest.mark.asyncio
async def test_bool_switch_rollback_on_api_error() -> None:
    device = _device({"always_on": False})
    sw = _bool_switch(device)
    sw.coordinator.api.async_update_device = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()
    assert device.attributes["always_on"] is False
    assert "always_on" not in device.attributes.get("_optimistic_attrs", {})
    sw.coordinator.async_request_refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# AjaxDimmerCalibrationSwitch
# ---------------------------------------------------------------------------


def test_calibration_available_and_device_info() -> None:
    assert _calib_switch(_device(online=True)).available is True
    assert _calib_switch(None).available is False
    assert _calib_switch(_device()).device_info["identifiers"]


def test_calibration_is_on() -> None:
    assert _calib_switch(_device({"dimmerSettings": {"calibration": "ENABLED"}})).is_on is True
    assert _calib_switch(_device({"dimmerSettings": {"calibration": "DISABLED"}})).is_on is False
    assert _calib_switch(None).is_on is False


@pytest.mark.asyncio
async def test_calibration_turn_on_calls_nested_api() -> None:
    sw = _calib_switch(_device())
    await sw.async_turn_on()
    sw.coordinator.api.async_update_device_nested.assert_awaited_once_with(
        "hub1", "d1", {"dimmerSettings": {"calibration": "ENABLED"}}
    )


@pytest.mark.asyncio
async def test_calibration_turn_off_disables() -> None:
    sw = _calib_switch(_device())
    await sw.async_turn_off()
    sw.coordinator.api.async_update_device_nested.assert_awaited_once_with(
        "hub1", "d1", {"dimmerSettings": {"calibration": "DISABLED"}}
    )


@pytest.mark.asyncio
async def test_calibration_set_value_no_device_raises() -> None:
    with pytest.raises(HomeAssistantError):
        await _calib_switch(None).async_turn_on()


@pytest.mark.asyncio
async def test_calibration_set_value_no_hub_raises() -> None:
    with pytest.raises(HomeAssistantError):
        await _calib_switch(_device(), hub_id=None).async_turn_on()


# ---------------------------------------------------------------------------
# AjaxDimmerSettingsSwitch — device_info + hub_not_found guard
# ---------------------------------------------------------------------------


def test_settings_switch_device_info() -> None:
    assert _settings_switch(_device()).device_info["identifiers"]


@pytest.mark.asyncio
async def test_settings_switch_no_hub_raises() -> None:
    with pytest.raises(HomeAssistantError):
        await _settings_switch(_device(), hub_id=None).async_turn_on()
