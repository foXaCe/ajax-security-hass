"""Tests for AjaxTiltDegreesNumber and the DoorPlus number base.

The number entities expose device attributes as user-tunable sliders.
The most important contract: changing the value while the system is
armed must raise ServiceValidationError (Ajax refuses config writes
while armed) — silently failing would leave the user thinking the value
was set when it wasn't.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.ajax.models import AjaxDevice, DeviceType, SecurityState
from custom_components.ajax.number import AjaxTiltDegreesNumber


def _make_number(
    *,
    tilt_value: float | None = None,
    security_state: SecurityState = SecurityState.DISARMED,
    hub_id: str | None = "hub1",
    device_online: bool = True,
    has_device: bool = True,
    has_space: bool = True,
) -> AjaxTiltDegreesNumber:
    attrs = {"accelerometer_tilt_degrees": tilt_value} if tilt_value is not None else {}
    device = (
        AjaxDevice(
            id="d1",
            name="Door Plus",
            type=DeviceType.DOOR_CONTACT,
            space_id="s1",
            hub_id=hub_id or "hub1",
            online=device_online,
            attributes=attrs,
        )
        if has_device
        else None
    )
    space = (
        SimpleNamespace(devices={"d1": device} if device else {}, security_state=security_state, hub_id=hub_id)
        if has_space
        else None
    )

    entity = object.__new__(AjaxTiltDegreesNumber)
    entity._space_id = "s1"
    entity._device_id = "d1"
    entity.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: space)
    return entity


def test_native_value_returns_attribute() -> None:
    assert _make_number(tilt_value=15).native_value == 15


def test_native_value_defaults_to_min_when_attribute_missing() -> None:
    """Convention: default to the slider's native_min_value (5°) so HA never shows None."""
    assert _make_number(tilt_value=None).native_value == 5


def test_native_value_returns_none_when_device_missing() -> None:
    """Device removed from Ajax → surface unknown."""
    assert _make_number(has_device=False).native_value is None


def test_available_tracks_device_online_flag() -> None:
    assert _make_number(device_online=True).available is True
    assert _make_number(device_online=False).available is False


def test_available_false_when_device_missing() -> None:
    assert _make_number(has_device=False).available is False


@pytest.mark.asyncio
async def test_async_set_native_value_raises_when_space_missing() -> None:
    """A removed config entry must raise HomeAssistantError, not crash."""
    with pytest.raises(HomeAssistantError):
        await _make_number(has_space=False).async_set_native_value(15)


@pytest.mark.asyncio
async def test_async_set_native_value_refuses_change_when_armed() -> None:
    """Ajax rejects config writes while the system is armed — surface to the user."""
    with pytest.raises(ServiceValidationError):
        await _make_number(security_state=SecurityState.ARMED).async_set_native_value(15)
