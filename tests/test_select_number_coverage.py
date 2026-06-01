"""Coverage tests for the select and number platforms.

These platforms expose Ajax device-config attributes as HA select/number
entities. The entities read state via ``current_option`` / ``native_value``
property getters backed by coordinator data, and mutate it via
``async_select_option`` / ``async_set_native_value`` which call the REST API
and then request a coordinator refresh. Failures must surface as translated
``HomeAssistantError`` (or ``ServiceValidationError`` when the system is armed)
rather than silently succeeding.

Discovery (``async_setup_entry``) walks every space/device and instantiates the
right entity subclass per attribute. The dynamic ``_build_device`` closure
mirrors that logic for newly-discovered devices.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.ajax import number as number_mod, select as select_mod
from custom_components.ajax.models import AjaxDevice, DeviceType, SecurityState
from custom_components.ajax.number import (
    AjaxCurrentThresholdNumber,
    AjaxDimmerNumber,
    AjaxLedBrightnessV2Number,
    AjaxTiltDegreesNumber,
)
from custom_components.ajax.select import (
    DIMMER_SELECT_DEFINITIONS,
    INDICATION_MODE_OPTIONS,
    LIGHTSWITCH_TOUCH_MODE_SELECT,
    SHOCK_SENSITIVITY_OPTIONS,
    AjaxDimmerSelect,
    AjaxHandlerSelect,
    AjaxIndicationModeSelect,
    AjaxLedBrightnessSelect,
    AjaxShockSensitivitySelect,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _device(
    *,
    device_id: str = "d1",
    dtype: DeviceType = DeviceType.SOCKET,
    raw_type: str | None = None,
    online: bool = True,
    attributes: dict | None = None,
) -> AjaxDevice:
    return AjaxDevice(
        id=device_id,
        name="Test Device",
        type=dtype,
        space_id="s1",
        hub_id="hub1",
        raw_type=raw_type,
        online=online,
        attributes=attributes or {},
    )


def _coordinator_for(
    device: AjaxDevice | None,
    *,
    hub_id: str | None = "hub1",
    security_state: SecurityState = SecurityState.DISARMED,
    last_update_success: bool = True,
) -> SimpleNamespace:
    space = SimpleNamespace(
        devices={device.id: device} if device else {},
        security_state=security_state,
        hub_id=hub_id,
    )
    coordinator = SimpleNamespace(
        get_space=lambda sid: space,
        last_update_success=last_update_success,
        entry_id="entry_test",
    )
    coordinator.api = MagicMock()
    coordinator.api.async_update_device = AsyncMock()
    coordinator.api.async_update_device_nested = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


def _coordinator_no_space() -> SimpleNamespace:
    coordinator = SimpleNamespace(get_space=lambda sid: None, last_update_success=True, entry_id="entry_test")
    coordinator.api = MagicMock()
    coordinator.api.async_update_device = AsyncMock()
    coordinator.api.async_update_device_nested = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


def _build_select(cls, coordinator, **extra):
    ent = object.__new__(cls)
    ent._space_id = "s1"
    ent._device_id = "d1"
    ent.coordinator = coordinator
    for key, value in extra.items():
        setattr(ent, key, value)
    return ent


# ===========================================================================
# select.py — pure helpers
# ===========================================================================


def test_is_dimmer_device_matches_lightswitchdimmer() -> None:
    assert select_mod.is_dimmer_device(_device(raw_type="Light_Switch_Dimmer")) is True
    assert select_mod.is_dimmer_device(_device(raw_type="dimmer")) is True
    assert select_mod.is_dimmer_device(_device(raw_type="LightSwitch")) is False
    assert select_mod.is_dimmer_device(_device(raw_type=None)) is False


def test_is_lightswitch_device_excludes_dimmer() -> None:
    assert select_mod.is_lightswitch_device(_device(raw_type="LightSwitch")) is True
    assert select_mod.is_lightswitch_device(_device(raw_type="LightSwitchDimmer")) is False
    assert select_mod.is_lightswitch_device(_device(raw_type=None)) is False


def test_get_dimmer_attr_flat_and_nested() -> None:
    device = _device(attributes={"touchMode": "TOUCH_MODE_TOGGLE", "dimmerSettings": {"curveType": "CURVE_TYPE_AUTO"}})
    assert select_mod._get_dimmer_attr(device, "touchMode") == "TOUCH_MODE_TOGGLE"
    assert select_mod._get_dimmer_attr(device, "dimmerSettings.curveType") == "CURVE_TYPE_AUTO"
    # Missing nested key
    assert select_mod._get_dimmer_attr(device, "dimmerSettings.missing") is None
    # Non-dict intermediate value short-circuits to None
    assert select_mod._get_dimmer_attr(device, "touchMode.deeper") is None
    # Missing flat key
    assert select_mod._get_dimmer_attr(device, "absent") is None


# ===========================================================================
# AjaxShockSensitivitySelect
# ===========================================================================


def _shock(
    *,
    value: int | None = None,
    online: bool = True,
    security_state: SecurityState = SecurityState.DISARMED,
    hub_id: str | None = "hub1",
) -> AjaxShockSensitivitySelect:
    attrs = {"shock_sensor_sensitivity": value} if value is not None else {}
    device = _device(dtype=DeviceType.DOOR_CONTACT, online=online, attributes=attrs)
    coordinator = _coordinator_for(device, hub_id=hub_id, security_state=security_state)
    return _build_select(AjaxShockSensitivitySelect, coordinator)


def test_shock_current_option_maps_values() -> None:
    for raw, label in SHOCK_SENSITIVITY_OPTIONS.items():
        assert _shock(value=raw).current_option == label
    assert _shock(value=99).current_option is None
    assert _shock(value=None).current_option is None


def test_shock_current_option_none_when_device_missing() -> None:
    ent = _build_select(AjaxShockSensitivitySelect, _coordinator_no_space())
    assert ent.current_option is None


def test_shock_available_and_device_info() -> None:
    assert _shock(online=True).available is True
    assert _shock(online=False).available is False
    ent = _build_select(AjaxShockSensitivitySelect, _coordinator_no_space())
    assert ent.available is False
    info = _shock().device_info
    assert (select_mod.DOMAIN, "entry_test_d1") in info["identifiers"]


def test_shock_handle_coordinator_update_writes_state() -> None:
    ent = _shock()
    ent.async_write_ha_state = MagicMock()
    ent._handle_coordinator_update()
    ent.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_shock_select_success_calls_api_and_refresh() -> None:
    ent = _shock(value=0)
    await ent.async_select_option("high")
    ent.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {"shockSensorSensitivity": 7})
    ent.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_shock_select_unknown_option_defaults_to_zero() -> None:
    ent = _shock(value=0)
    await ent.async_select_option("bogus")
    ent.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {"shockSensorSensitivity": 0})


@pytest.mark.asyncio
async def test_shock_select_raises_when_space_missing() -> None:
    ent = _build_select(AjaxShockSensitivitySelect, _coordinator_no_space())
    with pytest.raises(HomeAssistantError):
        await ent.async_select_option("low")


@pytest.mark.asyncio
async def test_shock_select_raises_when_armed() -> None:
    with pytest.raises(ServiceValidationError):
        await _shock(value=0, security_state=SecurityState.ARMED).async_select_option("low")


@pytest.mark.asyncio
async def test_shock_select_raises_when_hub_missing() -> None:
    with pytest.raises(HomeAssistantError):
        await _shock(value=0, hub_id=None).async_select_option("low")


@pytest.mark.asyncio
async def test_shock_select_wraps_api_error() -> None:
    ent = _shock(value=0)
    ent.coordinator.api.async_update_device.side_effect = RuntimeError("boom")
    with pytest.raises(HomeAssistantError):
        await ent.async_select_option("normal")


# ===========================================================================
# AjaxLedBrightnessSelect (MIN/MAX socket)
# ===========================================================================


def _led(
    *, value=None, online: bool = True, indication_enabled: bool = True, hub_id: str | None = "hub1"
) -> AjaxLedBrightnessSelect:
    attrs: dict = {"indicationEnabled": indication_enabled}
    if value is not None:
        attrs["indicationBrightness"] = value
    device = _device(online=online, attributes=attrs)
    coordinator = _coordinator_for(device, hub_id=hub_id)
    return _build_select(AjaxLedBrightnessSelect, coordinator)


def test_led_current_option() -> None:
    assert _led(value="MIN").current_option == "min"
    assert _led(value="MAX").current_option == "max"
    # Unknown string maps to None
    assert _led(value="WEIRD").current_option is None
    # Non-string value
    assert _led(value=5).current_option is None
    # Missing attribute
    assert _led(value=None).current_option is None
    # Device missing
    assert _build_select(AjaxLedBrightnessSelect, _coordinator_no_space()).current_option is None


def test_led_available_requires_online_and_indication() -> None:
    assert _led(online=True, indication_enabled=True).available is True
    assert _led(online=False, indication_enabled=True).available is False
    assert _led(online=True, indication_enabled=False).available is False
    assert _build_select(AjaxLedBrightnessSelect, _coordinator_no_space()).available is False


def test_led_device_info_and_handle_update() -> None:
    ent = _led(value="MIN")
    assert (select_mod.DOMAIN, "entry_test_d1") in ent.device_info["identifiers"]
    ent.async_write_ha_state = MagicMock()
    ent._handle_coordinator_update()
    ent.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_led_select_uppercases_for_api() -> None:
    ent = _led(value="MIN")
    await ent.async_select_option("max")
    ent.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {"indicationBrightness": "MAX"})
    ent.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_led_select_raises_when_space_missing() -> None:
    ent = _build_select(AjaxLedBrightnessSelect, _coordinator_no_space())
    with pytest.raises(HomeAssistantError):
        await ent.async_select_option("min")


@pytest.mark.asyncio
async def test_led_select_raises_when_hub_missing() -> None:
    with pytest.raises(HomeAssistantError):
        await _led(value="MIN", hub_id=None).async_select_option("min")


@pytest.mark.asyncio
async def test_led_select_wraps_api_error() -> None:
    ent = _led(value="MIN")
    ent.coordinator.api.async_update_device.side_effect = RuntimeError("boom")
    with pytest.raises(HomeAssistantError):
        await ent.async_select_option("max")


# ===========================================================================
# AjaxIndicationModeSelect
# ===========================================================================


def _indication(*, value=None, online: bool = True, hub_id: str | None = "hub1") -> AjaxIndicationModeSelect:
    attrs = {"indicationMode": value} if value is not None else {}
    device = _device(online=online, attributes=attrs)
    coordinator = _coordinator_for(device, hub_id=hub_id)
    return _build_select(AjaxIndicationModeSelect, coordinator)


def test_indication_current_option() -> None:
    for api_value, label in INDICATION_MODE_OPTIONS.items():
        assert _indication(value=api_value).current_option == label
    assert _indication(value="UNKNOWN").current_option is None
    assert _build_select(AjaxIndicationModeSelect, _coordinator_no_space()).current_option is None


def test_indication_available_and_device_info() -> None:
    assert _indication(online=True).available is True
    assert _indication(online=False).available is False
    assert _build_select(AjaxIndicationModeSelect, _coordinator_no_space()).available is False
    ent = _indication(value="ENABLED")
    assert (select_mod.DOMAIN, "entry_test_d1") in ent.device_info["identifiers"]
    ent.async_write_ha_state = MagicMock()
    ent._handle_coordinator_update()
    ent.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_indication_select_maps_option_to_api() -> None:
    ent = _indication(value="ENABLED")
    await ent.async_select_option("if_on")
    ent.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {"indicationMode": "IF_ON"})


@pytest.mark.asyncio
async def test_indication_select_unknown_option_falls_back_enabled() -> None:
    ent = _indication(value="ENABLED")
    await ent.async_select_option("bogus")
    ent.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {"indicationMode": "ENABLED"})


@pytest.mark.asyncio
async def test_indication_select_raises_when_space_missing() -> None:
    ent = _build_select(AjaxIndicationModeSelect, _coordinator_no_space())
    with pytest.raises(HomeAssistantError):
        await ent.async_select_option("off")


@pytest.mark.asyncio
async def test_indication_select_raises_when_hub_missing() -> None:
    with pytest.raises(HomeAssistantError):
        await _indication(value="ENABLED", hub_id=None).async_select_option("off")


@pytest.mark.asyncio
async def test_indication_select_wraps_api_error() -> None:
    ent = _indication(value="ENABLED")
    ent.coordinator.api.async_update_device.side_effect = RuntimeError("boom")
    with pytest.raises(HomeAssistantError):
        await ent.async_select_option("off")


# ===========================================================================
# AjaxDimmerSelect
# ===========================================================================

_TOUCH_DEF = DIMMER_SELECT_DEFINITIONS[0]  # flat key (touchMode)
_CURVE_DEF = DIMMER_SELECT_DEFINITIONS[1]  # nested key (dimmerSettings.curveType)


def _dimmer(
    select_def, *, attributes=None, online: bool = True, last_update_success: bool = True, hub_id: str | None = "hub1"
) -> AjaxDimmerSelect:
    device = _device(raw_type="LightSwitchDimmer", online=online, attributes=attributes or {})
    coordinator = _coordinator_for(device, hub_id=hub_id, last_update_success=last_update_success)
    return _build_select(AjaxDimmerSelect, coordinator, _select_def=select_def)


def test_dimmer_current_option_flat() -> None:
    ent = _dimmer(_TOUCH_DEF, attributes={"touchMode": "touch_mode_toggle"})
    assert ent.current_option == "touch_mode_toggle"


def test_dimmer_current_option_nested() -> None:
    ent = _dimmer(_CURVE_DEF, attributes={"dimmerSettings": {"curveType": "curve_type_linear"}})
    assert ent.current_option == "curve_type_linear"


def test_dimmer_current_option_none_paths() -> None:
    # Value not in option list
    assert _dimmer(_TOUCH_DEF, attributes={"touchMode": "WHATEVER"}).current_option is None
    # Attribute missing
    assert _dimmer(_TOUCH_DEF, attributes={}).current_option is None
    # Device missing
    ent = _build_select(AjaxDimmerSelect, _coordinator_no_space(), _select_def=_TOUCH_DEF)
    assert ent.current_option is None


def test_dimmer_available_and_device_info() -> None:
    assert _dimmer(_TOUCH_DEF, online=True).available is True
    assert _dimmer(_TOUCH_DEF, online=False).available is False
    assert _dimmer(_TOUCH_DEF, last_update_success=False).available is False
    ent = _build_select(AjaxDimmerSelect, _coordinator_no_space(), _select_def=_TOUCH_DEF)
    assert ent.available is False
    assert (select_mod.DOMAIN, "entry_test_d1") in _dimmer(_TOUCH_DEF).device_info["identifiers"]


@pytest.mark.asyncio
async def test_dimmer_select_flat_uses_plain_update() -> None:
    ent = _dimmer(_TOUCH_DEF, attributes={"touchMode": "touch_mode_toggle"})
    await ent.async_select_option("touch_mode_blocked")
    ent.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {"touchMode": "TOUCH_MODE_BLOCKED"})
    ent.coordinator.api.async_update_device_nested.assert_not_called()


@pytest.mark.asyncio
async def test_dimmer_select_nested_uses_nested_update() -> None:
    ent = _dimmer(_CURVE_DEF, attributes={"dimmerSettings": {"curveType": "curve_type_auto"}})
    await ent.async_select_option("curve_type_logarithmic")
    ent.coordinator.api.async_update_device_nested.assert_awaited_once_with(
        "hub1", "d1", {"dimmerSettings": {"curveType": "CURVE_TYPE_LOGARITHMIC"}}
    )
    ent.coordinator.api.async_update_device.assert_not_called()


@pytest.mark.asyncio
async def test_dimmer_select_raises_when_space_missing() -> None:
    ent = _build_select(AjaxDimmerSelect, _coordinator_no_space(), _select_def=_TOUCH_DEF)
    with pytest.raises(HomeAssistantError):
        await ent.async_select_option("touch_mode_toggle")


@pytest.mark.asyncio
async def test_dimmer_select_raises_when_hub_missing() -> None:
    ent = _dimmer(_TOUCH_DEF, hub_id=None)
    with pytest.raises(HomeAssistantError):
        await ent.async_select_option("touch_mode_toggle")


@pytest.mark.asyncio
async def test_dimmer_select_wraps_api_error() -> None:
    ent = _dimmer(_TOUCH_DEF, attributes={"touchMode": "touch_mode_toggle"})
    ent.coordinator.api.async_update_device.side_effect = RuntimeError("boom")
    with pytest.raises(HomeAssistantError):
        await ent.async_select_option("touch_mode_blocked")


# ===========================================================================
# AjaxHandlerSelect (siren-style)
# ===========================================================================


def _handler_select(select_desc, *, online: bool = True, hub_id: str | None = "hub1") -> AjaxHandlerSelect:
    device = _device(dtype=DeviceType.SIREN, online=online)
    coordinator = _coordinator_for(device, hub_id=hub_id)
    return _build_select(AjaxHandlerSelect, coordinator, _select_desc=select_desc)


def test_handler_current_option_uses_value_fn() -> None:
    desc = {"key": "siren_volume", "value_fn": lambda: "loud"}
    assert _handler_select(desc).current_option == "loud"
    # No value_fn -> None
    assert _handler_select({"key": "x"}).current_option is None


def test_handler_available_and_device_info() -> None:
    desc = {"key": "x", "value_fn": lambda: "loud"}
    assert _handler_select(desc, online=True).available is True
    assert _handler_select(desc, online=False).available is False
    ent = _build_select(AjaxHandlerSelect, _coordinator_no_space(), _select_desc=desc)
    assert ent.available is False
    assert (select_mod.DOMAIN, "entry_test_d1") in _handler_select(desc).device_info["identifiers"]
    ent2 = _handler_select(desc)
    ent2.async_write_ha_state = MagicMock()
    ent2._handle_coordinator_update()
    ent2.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_handler_select_with_api_options() -> None:
    desc = {"key": "mode", "api_key": "modeKey", "api_options": {"loud": "LOUD"}}
    ent = _handler_select(desc)
    await ent.async_select_option("loud")
    ent.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {"modeKey": "LOUD"})


@pytest.mark.asyncio
async def test_handler_select_with_api_transform() -> None:
    desc = {"key": "vol", "api_key": "volKey", "api_transform": lambda x: x.upper()}
    ent = _handler_select(desc)
    await ent.async_select_option("loud")
    ent.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {"volKey": "LOUD"})


@pytest.mark.asyncio
async def test_handler_select_plain_passthrough() -> None:
    desc = {"key": "vol", "api_key": "volKey"}
    ent = _handler_select(desc)
    await ent.async_select_option("loud")
    ent.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {"volKey": "loud"})


@pytest.mark.asyncio
async def test_handler_select_nested() -> None:
    desc = {"key": "curve", "api_key": "curveType", "api_nested_key": "dimmerSettings"}
    ent = _handler_select(desc)
    await ent.async_select_option("AUTO")
    ent.coordinator.api.async_update_device_nested.assert_awaited_once_with(
        "hub1", "d1", {"dimmerSettings": {"curveType": "AUTO"}}
    )


@pytest.mark.asyncio
async def test_handler_select_raises_when_space_missing() -> None:
    ent = _build_select(AjaxHandlerSelect, _coordinator_no_space(), _select_desc={"key": "x", "api_key": "k"})
    with pytest.raises(HomeAssistantError):
        await ent.async_select_option("v")


@pytest.mark.asyncio
async def test_handler_select_raises_when_hub_missing() -> None:
    ent = _handler_select({"key": "x", "api_key": "k"}, hub_id=None)
    with pytest.raises(HomeAssistantError):
        await ent.async_select_option("v")


@pytest.mark.asyncio
async def test_handler_select_raises_when_no_api_key() -> None:
    ent = _handler_select({"key": "x"})
    with pytest.raises(HomeAssistantError):
        await ent.async_select_option("v")


@pytest.mark.asyncio
async def test_handler_select_wraps_api_error() -> None:
    ent = _handler_select({"key": "x", "api_key": "k"})
    ent.coordinator.api.async_update_device.side_effect = RuntimeError("boom")
    with pytest.raises(HomeAssistantError):
        await ent.async_select_option("v")


# ===========================================================================
# select.py — async_setup_entry / _build_device discovery
# ===========================================================================


def _setup_coordinator(devices: dict[str, AjaxDevice], *, account: bool = True) -> SimpleNamespace:
    space = SimpleNamespace(devices=devices, security_state=SecurityState.DISARMED, hub_id="hub1")
    coordinator = SimpleNamespace()
    coordinator.account = SimpleNamespace(spaces={"s1": space}) if account else None
    coordinator.get_space = lambda sid: space if sid == "s1" else None
    coordinator.last_update_success = True
    coordinator.entry_id = "entry_test"
    coordinator.api = MagicMock()
    return coordinator


@pytest.mark.asyncio
async def test_select_setup_entry_no_account_returns_early() -> None:
    coordinator = _setup_coordinator({}, account=False)
    entry = SimpleNamespace(runtime_data=coordinator, entry_id="entry_test")
    add = MagicMock()
    await select_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


@pytest.mark.asyncio
async def test_select_setup_entry_creates_all_entity_kinds() -> None:
    devices = {
        "door1": _device(device_id="door1", dtype=DeviceType.DOOR_CONTACT, raw_type="DoorProtectPlus"),
        "socket1": _device(
            device_id="socket1",
            dtype=DeviceType.SOCKET,
            attributes={"indicationBrightness": "MIN", "indicationMode": "ENABLED"},
        ),
        "dimmer1": _device(
            device_id="dimmer1",
            dtype=DeviceType.RELAY,
            raw_type="LightSwitchDimmer",
            attributes={"touchMode": "touch_mode_toggle", "dimmerSettings": {"curveType": "curve_type_auto"}},
        ),
        "lsw1": _device(
            device_id="lsw1",
            dtype=DeviceType.RELAY,
            raw_type="LightSwitch",
            attributes={"touchMode": "touch_mode_toggle"},
        ),
        "siren1": _device(
            device_id="siren1",
            dtype=DeviceType.SIREN,
            attributes={"siren_volume_level": "LOUD"},
        ),
    }
    coordinator = _setup_coordinator(devices)
    entry = SimpleNamespace(runtime_data=coordinator, entry_id="entry_test")
    add = MagicMock()
    with patch.object(select_mod, "connect_new_entity_signal") as mock_connect:
        await select_mod.async_setup_entry(MagicMock(), entry, add)

    add.assert_called_once()
    created = add.call_args[0][0]
    classnames = {type(e).__name__ for e in created}
    assert "AjaxShockSensitivitySelect" in classnames
    assert "AjaxLedBrightnessSelect" in classnames
    assert "AjaxIndicationModeSelect" in classnames
    assert "AjaxDimmerSelect" in classnames
    assert "AjaxHandlerSelect" in classnames
    mock_connect.assert_called_once()


@pytest.mark.asyncio
async def test_select_setup_entry_no_entities_skips_add() -> None:
    # A device that produces no select entities.
    devices = {"plain": _device(device_id="plain", dtype=DeviceType.MOTION_DETECTOR, attributes={})}
    coordinator = _setup_coordinator(devices)
    entry = SimpleNamespace(runtime_data=coordinator, entry_id="entry_test")
    add = MagicMock()
    with patch.object(select_mod, "connect_new_entity_signal"):
        await select_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


def _capture_select_builder(devices: dict[str, AjaxDevice]):
    """Run async_setup_entry and return the captured _build_device closure."""
    coordinator = _setup_coordinator(devices)
    entry = SimpleNamespace(runtime_data=coordinator, entry_id="entry_test")
    captured = {}

    def _capture(hass, ent, signal, domain, add, builder, *, label):
        captured["builder"] = builder

    return coordinator, entry, captured, _capture


@pytest.mark.asyncio
async def test_select_build_device_discovers_all_kinds() -> None:
    devices = {
        "door1": _device(device_id="door1", dtype=DeviceType.DOOR_CONTACT, raw_type="DoorProtectSPlus"),
        "socket1": _device(
            device_id="socket1",
            dtype=DeviceType.SOCKET,
            attributes={"indicationBrightness": "MAX", "indicationMode": "DISABLED"},
        ),
        "dimmer1": _device(
            device_id="dimmer1",
            dtype=DeviceType.RELAY,
            raw_type="LightSwitchDimmer",
            attributes={"touchMode": "touch_mode_toggle"},
        ),
        "lsw1": _device(
            device_id="lsw1",
            dtype=DeviceType.RELAY,
            raw_type="LightSwitch",
            attributes={"touchMode": "touch_mode_toggle"},
        ),
        "siren1": _device(device_id="siren1", dtype=DeviceType.SIREN, attributes={"beep_volume_level": "LOUD"}),
    }
    coordinator, entry, captured, capture = _capture_select_builder(devices)
    with patch.object(select_mod, "connect_new_entity_signal", side_effect=capture):
        await select_mod.async_setup_entry(MagicMock(), entry, MagicMock())
    builder = captured["builder"]

    door_pairs = builder("s1", "door1")
    assert any(uid == "entry_test_door1_shock_sensitivity" for uid, _ in door_pairs)

    socket_pairs = builder("s1", "socket1")
    socket_uids = {uid for uid, _ in socket_pairs}
    assert "entry_test_socket1_led_brightness" in socket_uids
    assert "entry_test_socket1_indication_mode" in socket_uids

    dimmer_pairs = builder("s1", "dimmer1")
    assert any(uid == "entry_test_dimmer1_touch_mode" for uid, _ in dimmer_pairs)

    lsw_pairs = builder("s1", "lsw1")
    assert any(uid == f"entry_test_lsw1_{LIGHTSWITCH_TOUCH_MODE_SELECT['key']}" for uid, _ in lsw_pairs)

    siren_pairs = builder("s1", "siren1")
    assert any(uid == "entry_test_siren1_beep_volume" for uid, _ in siren_pairs)


@pytest.mark.asyncio
async def test_select_build_device_returns_empty_when_device_missing() -> None:
    coordinator, entry, captured, capture = _capture_select_builder({})
    with patch.object(select_mod, "connect_new_entity_signal", side_effect=capture):
        await select_mod.async_setup_entry(MagicMock(), entry, MagicMock())
    builder = captured["builder"]
    assert builder("s1", "ghost") == []
    # Unknown space -> get_space returns None -> empty
    assert builder("unknown", "x") == []


# ===========================================================================
# number.py — helpers
# ===========================================================================


def test_number_is_dimmer_and_lightswitch() -> None:
    assert number_mod.is_dimmer_device(_device(raw_type="LightSwitchDimmer")) is True
    assert number_mod.is_dimmer_device(_device(raw_type="dimmer")) is True
    assert number_mod.is_lightswitch_device(_device(raw_type="LightSwitch")) is True
    assert number_mod.is_lightswitch_device(_device(raw_type="LightSwitchDimmer")) is False


# ===========================================================================
# AjaxTiltDegreesNumber
# ===========================================================================


def _tilt(
    *,
    value=None,
    online: bool = True,
    hub_id: str | None = "hub1",
    security_state: SecurityState = SecurityState.DISARMED,
) -> AjaxTiltDegreesNumber:
    attrs = {"accelerometer_tilt_degrees": value} if value is not None else {}
    device = _device(dtype=DeviceType.DOOR_CONTACT, online=online, attributes=attrs)
    coordinator = _coordinator_for(device, hub_id=hub_id, security_state=security_state)
    return _build_select(AjaxTiltDegreesNumber, coordinator)


def test_tilt_native_value() -> None:
    assert _tilt(value=15).native_value == 15
    assert _tilt(value=None).native_value == 5
    assert _build_select(AjaxTiltDegreesNumber, _coordinator_no_space()).native_value is None


def test_tilt_available_and_device_info() -> None:
    assert _tilt(online=True).available is True
    assert _tilt(online=False).available is False
    assert (number_mod.DOMAIN, "entry_test_d1") in _tilt().device_info["identifiers"]
    ent = _tilt()
    ent.async_write_ha_state = MagicMock()
    ent._handle_coordinator_update()
    ent.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_tilt_set_value_success() -> None:
    ent = _tilt(value=5)
    await ent.async_set_native_value(20.0)
    ent.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {"accelerometerTiltDegrees": 20})
    ent.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_tilt_set_value_raises_when_space_missing() -> None:
    ent = _build_select(AjaxTiltDegreesNumber, _coordinator_no_space())
    with pytest.raises(HomeAssistantError):
        await ent.async_set_native_value(10)


@pytest.mark.asyncio
async def test_tilt_set_value_raises_when_armed() -> None:
    with pytest.raises(ServiceValidationError):
        await _tilt(value=5, security_state=SecurityState.ARMED).async_set_native_value(10)


@pytest.mark.asyncio
async def test_tilt_set_value_raises_when_hub_missing() -> None:
    with pytest.raises(HomeAssistantError):
        await _tilt(value=5, hub_id=None).async_set_native_value(10)


@pytest.mark.asyncio
async def test_tilt_set_value_wraps_api_error() -> None:
    ent = _tilt(value=5)
    ent.coordinator.api.async_update_device.side_effect = RuntimeError("boom")
    with pytest.raises(HomeAssistantError):
        await ent.async_set_native_value(10)


# ===========================================================================
# AjaxCurrentThresholdNumber
# ===========================================================================


def _threshold(*, value=None, online: bool = True, hub_id: str | None = "hub1") -> AjaxCurrentThresholdNumber:
    attrs = {"current_threshold": value} if value is not None else {}
    device = _device(online=online, attributes=attrs)
    coordinator = _coordinator_for(device, hub_id=hub_id)
    return _build_select(AjaxCurrentThresholdNumber, coordinator)


def test_threshold_native_value() -> None:
    assert _threshold(value=10).native_value == 10
    assert _threshold(value=None).native_value is None
    assert _build_select(AjaxCurrentThresholdNumber, _coordinator_no_space()).native_value is None


def test_threshold_available_and_device_info() -> None:
    assert _threshold(online=True).available is True
    assert _threshold(online=False).available is False
    assert (number_mod.DOMAIN, "entry_test_d1") in _threshold().device_info["identifiers"]
    ent = _threshold()
    ent.async_write_ha_state = MagicMock()
    ent._handle_coordinator_update()
    ent.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_threshold_set_value_success() -> None:
    ent = _threshold(value=5)
    await ent.async_set_native_value(12.0)
    ent.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {"currentThresholdAmpere": 12})
    ent.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_threshold_set_value_raises_when_space_missing() -> None:
    ent = _build_select(AjaxCurrentThresholdNumber, _coordinator_no_space())
    with pytest.raises(HomeAssistantError):
        await ent.async_set_native_value(5)


@pytest.mark.asyncio
async def test_threshold_set_value_raises_when_hub_missing() -> None:
    with pytest.raises(HomeAssistantError):
        await _threshold(value=5, hub_id=None).async_set_native_value(5)


@pytest.mark.asyncio
async def test_threshold_set_value_wraps_api_error() -> None:
    ent = _threshold(value=5)
    ent.coordinator.api.async_update_device.side_effect = RuntimeError("boom")
    with pytest.raises(HomeAssistantError):
        await ent.async_set_native_value(5)


# ===========================================================================
# AjaxLedBrightnessV2Number
# ===========================================================================


def _ledv2(
    *, value=None, online: bool = True, indication_mode="ENABLED", hub_id: str | None = "hub1"
) -> AjaxLedBrightnessV2Number:
    attrs: dict = {"indicationMode": indication_mode}
    if value is not None:
        attrs["indicationBrightness"] = value
    device = _device(online=online, attributes=attrs)
    coordinator = _coordinator_for(device, hub_id=hub_id)
    return _build_select(AjaxLedBrightnessV2Number, coordinator)


def test_ledv2_native_value() -> None:
    assert _ledv2(value=4).native_value == 4
    assert _ledv2(value=None).native_value == 8
    assert _build_select(AjaxLedBrightnessV2Number, _coordinator_no_space()).native_value is None


def test_ledv2_available_respects_indication_mode() -> None:
    assert _ledv2(online=True, indication_mode="ENABLED").available is True
    assert _ledv2(online=True, indication_mode="DISABLED").available is False
    assert _ledv2(online=False).available is False
    assert _build_select(AjaxLedBrightnessV2Number, _coordinator_no_space()).available is False


def test_ledv2_device_info_and_handle_update() -> None:
    ent = _ledv2(value=4)
    assert (number_mod.DOMAIN, "entry_test_d1") in ent.device_info["identifiers"]
    ent.async_write_ha_state = MagicMock()
    ent._handle_coordinator_update()
    ent.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_ledv2_set_value_success() -> None:
    ent = _ledv2(value=4)
    await ent.async_set_native_value(6.0)
    ent.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {"indicationBrightnessV2": 6})
    ent.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_ledv2_set_value_raises_when_space_missing() -> None:
    ent = _build_select(AjaxLedBrightnessV2Number, _coordinator_no_space())
    with pytest.raises(HomeAssistantError):
        await ent.async_set_native_value(4)


@pytest.mark.asyncio
async def test_ledv2_set_value_raises_when_hub_missing() -> None:
    with pytest.raises(HomeAssistantError):
        await _ledv2(value=4, hub_id=None).async_set_native_value(4)


@pytest.mark.asyncio
async def test_ledv2_set_value_wraps_api_error() -> None:
    ent = _ledv2(value=4)
    ent.coordinator.api.async_update_device.side_effect = RuntimeError("boom")
    with pytest.raises(HomeAssistantError):
        await ent.async_set_native_value(4)


# ===========================================================================
# AjaxDimmerNumber
# ===========================================================================

_TOUCH_NUM_DEF = number_mod.DIMMER_NUMBER_DEFINITIONS[0]  # touch_sensitivity, config, no unit
_BRIGHT_NUM_DEF = number_mod.DIMMER_NUMBER_DEFINITIONS[2]  # min_brightness, config, PERCENTAGE unit


def _dimmer_num(
    number_def, *, value=None, online: bool = True, last_update_success: bool = True, hub_id: str | None = "hub1"
) -> AjaxDimmerNumber:
    attrs = {number_def["attr_key"]: value} if value is not None else {}
    device = _device(raw_type="LightSwitchDimmer", online=online, attributes=attrs)
    coordinator = _coordinator_for(device, hub_id=hub_id, last_update_success=last_update_success)
    return _build_select(AjaxDimmerNumber, coordinator, _number_def=number_def)


def test_dimmer_number_init_sets_attrs_config_and_diagnostic() -> None:
    coordinator = _coordinator_for(_device(raw_type="LightSwitchDimmer"))
    # config category
    ent = AjaxDimmerNumber(coordinator, "s1", "d1", _TOUCH_NUM_DEF)
    assert ent._attr_unique_id == "entry_test_d1_touch_sensitivity"
    assert ent._attr_native_min_value == 1
    assert ent._attr_native_max_value == 7
    # diagnostic category branch
    diag_def = {
        "key": "x",
        "translation_key": "x",
        "attr_key": "x",
        "min_value": 0,
        "max_value": 1,
        "step": 1,
        "api_key": "x",
        "entity_category": "diagnostic",
    }
    ent2 = AjaxDimmerNumber(coordinator, "s1", "d1", diag_def)
    assert ent2._attr_entity_category is not None
    # no category branch
    plain_def = {
        "key": "y",
        "translation_key": "y",
        "attr_key": "y",
        "min_value": 0,
        "max_value": 1,
        "step": 1,
        "api_key": "y",
    }
    ent3 = AjaxDimmerNumber(coordinator, "s1", "d1", plain_def)
    assert ent3._attr_native_unit_of_measurement is None


def test_dimmer_number_native_value() -> None:
    assert _dimmer_num(_TOUCH_NUM_DEF, value=4).native_value == 4
    assert _dimmer_num(_TOUCH_NUM_DEF, value=None).native_value is None
    ent = _build_select(AjaxDimmerNumber, _coordinator_no_space(), _number_def=_TOUCH_NUM_DEF)
    assert ent.native_value is None


def test_dimmer_number_available_and_device_info() -> None:
    assert _dimmer_num(_TOUCH_NUM_DEF, online=True).available is True
    assert _dimmer_num(_TOUCH_NUM_DEF, online=False).available is False
    assert _dimmer_num(_TOUCH_NUM_DEF, last_update_success=False).available is False
    ent = _build_select(AjaxDimmerNumber, _coordinator_no_space(), _number_def=_TOUCH_NUM_DEF)
    assert ent.available is False
    assert (number_mod.DOMAIN, "entry_test_d1") in _dimmer_num(_TOUCH_NUM_DEF).device_info["identifiers"]


@pytest.mark.asyncio
async def test_dimmer_number_set_value_success() -> None:
    ent = _dimmer_num(_BRIGHT_NUM_DEF, value=10)
    await ent.async_set_native_value(50.0)
    ent.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {_BRIGHT_NUM_DEF["api_key"]: 50})
    ent.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_dimmer_number_set_value_raises_when_device_missing() -> None:
    ent = _build_select(AjaxDimmerNumber, _coordinator_no_space(), _number_def=_TOUCH_NUM_DEF)
    with pytest.raises(HomeAssistantError):
        await ent.async_set_native_value(4)


@pytest.mark.asyncio
async def test_dimmer_number_set_value_raises_when_hub_missing() -> None:
    ent = _dimmer_num(_TOUCH_NUM_DEF, value=4, hub_id=None)
    with pytest.raises(HomeAssistantError):
        await ent.async_set_native_value(4)


@pytest.mark.asyncio
async def test_dimmer_number_set_value_wraps_api_error() -> None:
    ent = _dimmer_num(_TOUCH_NUM_DEF, value=4)
    ent.coordinator.api.async_update_device.side_effect = RuntimeError("boom")
    with pytest.raises(HomeAssistantError):
        await ent.async_set_native_value(4)


# ===========================================================================
# number.py — async_setup_entry / _build_device discovery
# ===========================================================================


@pytest.mark.asyncio
async def test_number_setup_entry_no_account_returns_early() -> None:
    coordinator = _setup_coordinator({}, account=False)
    entry = SimpleNamespace(runtime_data=coordinator, entry_id="entry_test")
    add = MagicMock()
    await number_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


@pytest.mark.asyncio
async def test_number_setup_entry_creates_all_entity_kinds() -> None:
    devices = {
        "door1": _device(device_id="door1", dtype=DeviceType.DOOR_CONTACT, raw_type="DoorProtectPlus"),
        "socket1": _device(
            device_id="socket1",
            dtype=DeviceType.SOCKET,
            attributes={"current_threshold": 5, "indicationBrightness": 4},
        ),
        "dimmer1": _device(
            device_id="dimmer1",
            dtype=DeviceType.RELAY,
            raw_type="LightSwitchDimmer",
            attributes={"touchSensitivity": 3, "minBrightnessLimitCh1": 10},
        ),
        "lsw1": _device(
            device_id="lsw1",
            dtype=DeviceType.RELAY,
            raw_type="LightSwitch",
            attributes={"touchSensitivity": 3},
        ),
    }
    coordinator = _setup_coordinator(devices)
    entry = SimpleNamespace(runtime_data=coordinator, entry_id="entry_test")
    add = MagicMock()
    with patch.object(number_mod, "connect_new_entity_signal") as mock_connect:
        await number_mod.async_setup_entry(MagicMock(), entry, add)

    add.assert_called_once()
    created = add.call_args[0][0]
    classnames = {type(e).__name__ for e in created}
    assert "AjaxTiltDegreesNumber" in classnames
    assert "AjaxCurrentThresholdNumber" in classnames
    assert "AjaxLedBrightnessV2Number" in classnames
    assert "AjaxDimmerNumber" in classnames
    mock_connect.assert_called_once()


@pytest.mark.asyncio
async def test_number_setup_entry_no_entities_skips_add() -> None:
    devices = {"plain": _device(device_id="plain", dtype=DeviceType.MOTION_DETECTOR, attributes={})}
    coordinator = _setup_coordinator(devices)
    entry = SimpleNamespace(runtime_data=coordinator, entry_id="entry_test")
    add = MagicMock()
    with patch.object(number_mod, "connect_new_entity_signal"):
        await number_mod.async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


@pytest.mark.asyncio
async def test_number_build_device_discovers_all_kinds() -> None:
    devices = {
        "door1": _device(device_id="door1", dtype=DeviceType.DOOR_CONTACT, raw_type="DoorProtectSPlus"),
        "socket1": _device(
            device_id="socket1",
            dtype=DeviceType.SOCKET,
            attributes={"current_threshold": 5, "indicationBrightness": 4},
        ),
        "dimmer1": _device(
            device_id="dimmer1",
            dtype=DeviceType.RELAY,
            raw_type="LightSwitchDimmer",
            attributes={"touchSensitivity": 3},
        ),
        "lsw1": _device(
            device_id="lsw1",
            dtype=DeviceType.RELAY,
            raw_type="LightSwitch",
            attributes={"touchSensitivity": 3},
        ),
    }
    coordinator = _setup_coordinator(devices)
    entry = SimpleNamespace(runtime_data=coordinator, entry_id="entry_test")
    captured = {}

    def _capture(hass, ent, signal, domain, add, builder, *, label):
        captured["builder"] = builder

    with patch.object(number_mod, "connect_new_entity_signal", side_effect=_capture):
        await number_mod.async_setup_entry(MagicMock(), entry, MagicMock())
    builder = captured["builder"]

    assert any(uid == "entry_test_door1_tilt_degrees" for uid, _ in builder("s1", "door1"))

    socket_uids = {uid for uid, _ in builder("s1", "socket1")}
    assert "entry_test_socket1_current_threshold" in socket_uids
    assert "entry_test_socket1_led_brightness" in socket_uids

    assert any(uid == "entry_test_dimmer1_touch_sensitivity" for uid, _ in builder("s1", "dimmer1"))
    assert any(uid == "entry_test_lsw1_touch_sensitivity" for uid, _ in builder("s1", "lsw1"))

    # Missing device / unknown space
    assert builder("s1", "ghost") == []
    assert builder("unknown", "x") == []
