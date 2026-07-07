"""Coverage tests for the AjaxSwitch command paths and discovery.

These exercise the optimistic-write / rollback machinery of ``AjaxSwitch``
(``_set_value``, ``_set_trigger_value``, ``_set_settings_value``,
``_set_channel_value``) plus ``async_setup_entry`` / ``_build_device``
discovery, all without a running HA instance: the entities are built with
``object.__new__`` and the coordinator/API are mocks.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.ajax.const import DOMAIN as DOMAIN_ID
from custom_components.ajax.models import (
    AjaxAccount,
    AjaxDevice,
    AjaxSpace,
    DeviceType,
    SecurityState,
)
from custom_components.ajax.switch import (
    AjaxSwitch,
    async_setup_entry,
    is_lightswitch_device,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _device(
    *,
    device_type: DeviceType = DeviceType.RELAY,
    raw_type: str | None = "RELAY",
    attributes: dict | None = None,
    online: bool = True,
) -> AjaxDevice:
    return AjaxDevice(
        id="d1",
        name="Relay Office",
        type=device_type,
        space_id="s1",
        hub_id="hub1",
        raw_type=raw_type,
        online=online,
        attributes=attributes or {},
    )


def _coordinator(
    device: AjaxDevice | None,
    *,
    hub_id: str | None = "hub1",
    security_state: SecurityState = SecurityState.DISARMED,
):
    space = SimpleNamespace(
        devices={"d1": device} if device else {},
        hub_id=hub_id,
        security_state=security_state,
    )
    api = SimpleNamespace(
        async_update_device=AsyncMock(),
        async_update_device_nested=AsyncMock(),
        async_set_switch_state=AsyncMock(),
        async_set_channel_state=AsyncMock(),
    )
    return SimpleNamespace(
        entry_id="entry_test",
        get_space=lambda sid: space,
        last_update_success=True,
        api=api,
        async_request_refresh=AsyncMock(),
        async_update_listeners=MagicMock(),
    )


def _switch(switch_desc: dict, device: AjaxDevice | None, **coord_kwargs) -> AjaxSwitch:
    sw = object.__new__(AjaxSwitch)
    sw._space_id = "s1"
    sw._device_id = "d1"
    sw._switch_key = switch_desc["key"]
    sw._switch_desc = switch_desc
    sw.coordinator = _coordinator(device, **coord_kwargs)
    sw.async_write_ha_state = lambda: None
    return sw


# ---------------------------------------------------------------------------
# is_lightswitch_device helper
# ---------------------------------------------------------------------------


def test_is_lightswitch_device_true_for_lightswitch() -> None:
    assert is_lightswitch_device(_device(raw_type="LIGHT_SWITCH_ONE_GANG")) is True


def test_is_lightswitch_device_false_for_dimmer() -> None:
    assert is_lightswitch_device(_device(raw_type="LightSwitchDimmer")) is False


def test_is_lightswitch_device_false_for_none_raw_type() -> None:
    assert is_lightswitch_device(_device(raw_type=None)) is False


# ---------------------------------------------------------------------------
# _set_value guard rails (device / hub missing, no api_key)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_value_raises_when_device_missing() -> None:
    sw = _switch({"key": "x", "api_key": "foo"}, None)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()


@pytest.mark.asyncio
async def test_set_value_raises_when_hub_missing() -> None:
    sw = _switch({"key": "x", "api_key": "foo"}, _device(), hub_id=None)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()


@pytest.mark.asyncio
async def test_set_value_raises_when_no_api_key() -> None:
    # Not a socket/relay/wallswitch main switch and no api_key -> no_api_key error.
    sw = _switch({"key": "x"}, _device(device_type=DeviceType.SIREN, raw_type="SIREN"))
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()


# ---------------------------------------------------------------------------
# Main on/off switch for Socket/Relay/WallSwitch (no api_key, no channel)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_main_switch_turn_on_optimistic_and_command() -> None:
    device = _device(attributes={"is_on": False})
    sw = _switch({"key": "socket"}, device)
    await sw.async_turn_on()
    assert device.attributes["is_on"] is True
    assert device.is_optimistic("is_on") is True
    sw.coordinator.api.async_set_switch_state.assert_awaited_once_with("hub1", "d1", True, "RELAY")


@pytest.mark.asyncio
async def test_main_switch_uses_type_value_when_raw_type_none() -> None:
    device = _device(raw_type=None, attributes={"is_on": False})
    sw = _switch({"key": "socket"}, device)
    await sw.async_turn_off()
    sw.coordinator.api.async_set_switch_state.assert_awaited_once_with("hub1", "d1", False, "relay")


@pytest.mark.asyncio
async def test_main_switch_rollback_and_purge_guard_on_error() -> None:
    device = _device(attributes={"is_on": False})
    sw = _switch({"key": "socket"}, device)
    sw.coordinator.api.async_set_switch_state = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()
    # Optimistic value reverted and the guard entry purged so a poll can correct it.
    assert device.attributes["is_on"] is False
    assert "is_on" not in device.attributes.get("_optimistic_attrs", {})
    sw.coordinator.async_request_refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# Multi-gang channel switch (channel set in descriptor)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_switch_turn_on_updates_attrs_and_statuses() -> None:
    device = _device(
        device_type=DeviceType.WALLSWITCH,
        raw_type="LIGHT_SWITCH_TWO_GANG",
        attributes={"channel_1_on": False, "channelStatuses": []},
    )
    sw = _switch({"key": "channel_1", "channel": 0}, device)
    await sw.async_turn_on()
    assert device.attributes["channel_1_on"] is True
    assert "CHANNEL_1_ON" in device.attributes["channelStatuses"]
    assert device.attributes["_channel_optimistic_until"][0] > 0
    sw.coordinator.api.async_set_channel_state.assert_awaited_once_with("hub1", "d1", 0, True, "LIGHT_SWITCH_TWO_GANG")
    sw.coordinator.async_update_listeners.assert_called()


@pytest.mark.asyncio
async def test_channel_switch_rollback_clears_only_this_channel() -> None:
    device = _device(
        device_type=DeviceType.WALLSWITCH,
        raw_type="LIGHT_SWITCH_TWO_GANG",
        attributes={
            "channel_1_on": False,
            "channelStatuses": [],
            # A second channel already has an in-flight optimistic update.
            "_channel_optimistic_until": {1: 9999999999.0},
            "_optimistic_until": 9999999999.0,
        },
    )
    sw = _switch({"key": "channel_1", "channel": 0}, device)
    sw.coordinator.api.async_set_channel_state = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()
    # channel 0 rolled back, channel 1's guard preserved
    assert device.attributes["channel_1_on"] is False
    assert "CHANNEL_1_ON" not in device.attributes["channelStatuses"]
    assert 0 not in device.attributes["_channel_optimistic_until"]
    assert 1 in device.attributes["_channel_optimistic_until"]
    sw.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_channel_switch_turn_off_removes_status() -> None:
    device = _device(
        device_type=DeviceType.WALLSWITCH,
        raw_type="LIGHT_SWITCH_TWO_GANG",
        attributes={"channel_1_on": True, "channelStatuses": ["CHANNEL_1_ON"]},
    )
    sw = _switch({"key": "channel_1", "channel": 0}, device)
    await sw.async_turn_off()
    assert device.attributes["channel_1_on"] is False
    assert "CHANNEL_1_ON" not in device.attributes["channelStatuses"]


@pytest.mark.asyncio
async def test_channel_switch_turn_off_rollback_restores_status() -> None:
    device = _device(
        device_type=DeviceType.WALLSWITCH,
        raw_type="LIGHT_SWITCH_TWO_GANG",
        attributes={"channel_1_on": True, "channelStatuses": ["CHANNEL_1_ON"]},
    )
    sw = _switch({"key": "channel_1", "channel": 0}, device)
    sw.coordinator.api.async_set_channel_state = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_off()
    # rollback re-appends CHANNEL_1_ON to channelStatuses
    assert device.attributes["channel_1_on"] is True
    assert "CHANNEL_1_ON" in device.attributes["channelStatuses"]


@pytest.mark.asyncio
async def test_channel_switch_rollback_drops_guard_when_last_channel() -> None:
    device = _device(
        device_type=DeviceType.WALLSWITCH,
        raw_type="LIGHT_SWITCH_TWO_GANG",
        attributes={"channel_1_on": False, "channelStatuses": []},
    )
    sw = _switch({"key": "channel_1", "channel": 0}, device)
    sw.coordinator.api.async_set_channel_state = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()
    assert "_channel_optimistic_until" not in device.attributes
    assert "_optimistic_until" not in device.attributes


# ---------------------------------------------------------------------------
# Trigger-type switches (sirenTriggers list)
# ---------------------------------------------------------------------------


def _trigger_desc() -> dict:
    return {"key": "siren_on_intrusion", "api_key": "sirenTriggers", "trigger_key": "INTRUSION"}


@pytest.mark.asyncio
async def test_trigger_switch_turn_on_appends_and_calls_api() -> None:
    device = _device(
        device_type=DeviceType.SIREN,
        raw_type="SIREN",
        attributes={"siren_triggers": []},
    )
    sw = _switch(_trigger_desc(), device)
    await sw.async_turn_on()
    assert device.attributes["siren_triggers"] == ["INTRUSION"]
    assert device.is_optimistic("siren_triggers") is True
    sw.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {"sirenTriggers": ["INTRUSION"]})


@pytest.mark.asyncio
async def test_trigger_switch_turn_off_removes_key() -> None:
    device = _device(
        device_type=DeviceType.SIREN,
        raw_type="SIREN",
        attributes={"siren_triggers": ["INTRUSION", "FIRE"]},
    )
    sw = _switch(_trigger_desc(), device)
    await sw.async_turn_off()
    assert device.attributes["siren_triggers"] == ["FIRE"]


@pytest.mark.asyncio
async def test_trigger_switch_nested_payload() -> None:
    device = _device(
        device_type=DeviceType.SIREN,
        raw_type="SIREN",
        attributes={"siren_triggers": []},
    )
    desc = _trigger_desc()
    desc["api_nested_key"] = "wiredDeviceSettings"
    sw = _switch(desc, device)
    await sw.async_turn_on()
    sw.coordinator.api.async_update_device_nested.assert_awaited_once_with(
        "hub1", "d1", {"wiredDeviceSettings": {"sirenTriggers": ["INTRUSION"]}}
    )


@pytest.mark.asyncio
async def test_trigger_switch_rollback_on_error() -> None:
    device = _device(
        device_type=DeviceType.SIREN,
        raw_type="SIREN",
        attributes={"siren_triggers": []},
    )
    sw = _switch(_trigger_desc(), device)
    sw.coordinator.api.async_update_device = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()
    assert device.attributes["siren_triggers"] == []
    sw.coordinator.async_request_refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# Settings-type switches (settingsSwitch list for LightSwitch)
# ---------------------------------------------------------------------------


def _settings_desc() -> dict:
    return {
        "key": "led_indicator",
        "api_key": "settingsSwitch",
        "settings_key": "LED_INDICATOR_ENABLED",
        "bypass_security_check": True,
    }


@pytest.mark.asyncio
async def test_settings_value_turn_on_appends_and_calls_api() -> None:
    device = _device(
        device_type=DeviceType.WALLSWITCH,
        raw_type="LIGHT_SWITCH_ONE_GANG",
        attributes={"settingsSwitch": []},
    )
    sw = _switch(_settings_desc(), device)
    await sw.async_turn_on()
    assert device.attributes["settingsSwitch"] == ["LED_INDICATOR_ENABLED"]
    sw.coordinator.api.async_update_device.assert_awaited_once_with(
        "hub1", "d1", {"settingsSwitch": ["LED_INDICATOR_ENABLED"]}
    )


@pytest.mark.asyncio
async def test_settings_value_turn_off_removes_key() -> None:
    device = _device(
        device_type=DeviceType.WALLSWITCH,
        raw_type="LIGHT_SWITCH_ONE_GANG",
        attributes={"settingsSwitch": ["LED_INDICATOR_ENABLED", "OTHER"]},
    )
    sw = _switch(_settings_desc(), device)
    await sw.async_turn_off()
    assert device.attributes["settingsSwitch"] == ["OTHER"]


@pytest.mark.asyncio
async def test_settings_value_rollback_on_error() -> None:
    device = _device(
        device_type=DeviceType.WALLSWITCH,
        raw_type="LIGHT_SWITCH_ONE_GANG",
        attributes={"settingsSwitch": []},
    )
    sw = _switch(_settings_desc(), device)
    sw.coordinator.api.async_update_device = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()
    assert device.attributes["settingsSwitch"] == []
    sw.coordinator.async_request_refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# Standard boolean settings switch (api_key, security check, attr map, nested)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_standard_bool_switch_security_check_blocks_when_armed() -> None:
    device = _device(
        device_type=DeviceType.MOTION_DETECTOR,
        raw_type="MOTION",
        attributes={"always_active": False},
    )
    sw = _switch(
        {"key": "always_active", "api_key": "alwaysActive"},
        device,
        security_state=SecurityState.ARMED,
    )
    with pytest.raises(ServiceValidationError):
        await sw.async_turn_on()
    sw.coordinator.api.async_update_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_standard_bool_switch_maps_attr_key_and_calls_api() -> None:
    device = _device(
        device_type=DeviceType.MOTION_DETECTOR,
        raw_type="MOTION",
        attributes={"always_active": False},
    )
    sw = _switch({"key": "always_active", "api_key": "alwaysActive"}, device)
    await sw.async_turn_on()
    # camelCase api_key mapped to snake_case attribute for optimistic write
    assert device.attributes["always_active"] is True
    assert device.is_optimistic("always_active") is True
    sw.coordinator.api.async_update_device.assert_awaited_once_with("hub1", "d1", {"alwaysActive": True})
    sw.coordinator.async_update_listeners.assert_called_once()


@pytest.mark.asyncio
async def test_standard_bool_switch_bypass_security_with_extra() -> None:
    device = _device(
        device_type=DeviceType.SOCKET,
        raw_type="SOCKET",
        attributes={"indicationEnabled": False},
    )
    sw = _switch(
        {
            "key": "indication_enabled",
            "api_key": "indicationEnabled",
            "api_value_on": True,
            "api_extra": {"mode": "FAST"},
            "bypass_security_check": True,
        },
        device,
        security_state=SecurityState.ARMED,
    )
    await sw.async_turn_on()
    sw.coordinator.api.async_update_device.assert_awaited_once_with(
        "hub1", "d1", {"indicationEnabled": True, "mode": "FAST"}
    )


@pytest.mark.asyncio
async def test_standard_bool_switch_turn_off_uses_off_value_and_extra() -> None:
    device = _device(
        device_type=DeviceType.SOCKET,
        raw_type="SOCKET",
        attributes={"indicationEnabled": True},
    )
    sw = _switch(
        {
            "key": "indication_enabled",
            "api_key": "indicationEnabled",
            "api_value_off": False,
            "api_extra_off": {"reason": "user"},
            "bypass_security_check": True,
        },
        device,
    )
    await sw.async_turn_off()
    sw.coordinator.api.async_update_device.assert_awaited_once_with(
        "hub1", "d1", {"indicationEnabled": False, "reason": "user"}
    )


@pytest.mark.asyncio
async def test_standard_bool_switch_nested_payload() -> None:
    device = _device(
        device_type=DeviceType.MOTION_DETECTOR,
        raw_type="MOTION",
        attributes={"extra_contact_aware": False},
    )
    sw = _switch(
        {
            "key": "extra_contact_aware",
            "api_key": "extraContactAware",
            "api_nested_key": "wiredDeviceSettings",
            "bypass_security_check": True,
        },
        device,
    )
    await sw.async_turn_on()
    sw.coordinator.api.async_update_device_nested.assert_awaited_once_with(
        "hub1", "d1", {"wiredDeviceSettings": {"extraContactAware": True}}
    )


@pytest.mark.asyncio
async def test_standard_bool_switch_rollback_restores_old_value() -> None:
    device = _device(
        device_type=DeviceType.MOTION_DETECTOR,
        raw_type="MOTION",
        attributes={"always_active": True},
    )
    sw = _switch({"key": "always_active", "api_key": "alwaysActive", "bypass_security_check": True}, device)
    sw.coordinator.api.async_update_device = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_off()
    assert device.attributes["always_active"] is True
    sw.coordinator.async_request_refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# extra_state_attributes / device_info
# ---------------------------------------------------------------------------


def test_extra_state_attributes_returns_device_data() -> None:
    sw = _switch({"key": "x", "api_key": "foo"}, _device(raw_type="RELAY_X"))
    attrs = sw.extra_state_attributes
    assert attrs == {"device_type": "RELAY_X", "device_id": "d1"}


def test_extra_state_attributes_empty_when_device_missing() -> None:
    sw = _switch({"key": "x", "api_key": "foo"}, None)
    assert sw.extra_state_attributes == {}


def test_device_info_none_when_device_missing() -> None:
    sw = _switch({"key": "x", "api_key": "foo"}, None)
    assert sw.device_info is None


def test_device_info_built_for_device() -> None:
    device = _device(raw_type="RELAY_X")
    device.firmware_version = "1.2.3"
    sw = _switch({"key": "x", "api_key": "foo"}, device)
    info = sw.device_info
    assert info is not None
    assert (DOMAIN_ID, "entry_test_d1") in info["identifiers"]


# ---------------------------------------------------------------------------
# async_setup_entry / _build_device discovery
# ---------------------------------------------------------------------------


def _account_with(device: AjaxDevice) -> AjaxAccount:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    space.devices[device.id] = device
    account = AjaxAccount(user_id="u1", name="U", email="u@e.com")
    account.spaces["s1"] = space
    return account


@pytest.mark.asyncio
async def test_async_setup_entry_no_account_returns_early() -> None:
    coordinator = SimpleNamespace(account=None)
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    await async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_creates_relay_switch() -> None:
    device = _device(attributes={"is_on": True})
    account = _account_with(device)
    coordinator = SimpleNamespace(
        entry_id="entry_test",
        account=account,
        get_space=account.spaces.get,
    )
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    with patch("custom_components.ajax.switch.connect_new_entity_signal"):
        await async_setup_entry(MagicMock(), entry, add)
    add.assert_called_once()
    created = add.call_args[0][0]
    assert any(isinstance(e, AjaxSwitch) for e in created)


@pytest.mark.asyncio
async def test_async_setup_entry_creates_dimmer_switches() -> None:
    device = _device(
        device_type=DeviceType.WALLSWITCH,
        raw_type="LightSwitchDimmer",
        attributes={
            "settingsSwitch": [],
            "nightModeArm": False,
            "dimmerSettings": {"calibration": "DISABLED"},
        },
    )
    account = _account_with(device)
    coordinator = SimpleNamespace(
        entry_id="entry_test",
        account=account,
        get_space=account.spaces.get,
    )
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    with patch("custom_components.ajax.switch.connect_new_entity_signal"):
        await async_setup_entry(MagicMock(), entry, add)
    add.assert_called_once()
    created = add.call_args[0][0]
    # 4 settings switches + night mode + calibration
    assert len(created) == 6


@pytest.mark.asyncio
async def test_async_setup_entry_creates_lightswitch_settings_switches() -> None:
    # Non-dimmer LightSwitch: SocketHandler main switch + LightSwitchHandler settings.
    device = _device(
        device_type=DeviceType.WALLSWITCH,
        raw_type="LightSwitchOneGang",
        attributes={"is_on": False, "settingsSwitch": []},
    )
    account = _account_with(device)
    coordinator = SimpleNamespace(
        entry_id="entry_test",
        account=account,
        get_space=account.spaces.get,
    )
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    with patch("custom_components.ajax.switch.connect_new_entity_signal"):
        await async_setup_entry(MagicMock(), entry, add)
    add.assert_called_once()
    created = add.call_args[0][0]
    keys = {e._switch_key for e in created}
    assert "led_indicator" in keys  # from LightSwitchHandler settings switches


@pytest.mark.asyncio
async def test_async_setup_entry_no_entities_skips_add() -> None:
    # A device with no handler and no switches -> nothing added.
    device = _device(device_type=DeviceType.UNKNOWN, raw_type="MYSTERY", attributes={})
    account = _account_with(device)
    coordinator = SimpleNamespace(
        entry_id="entry_test",
        account=account,
        get_space=account.spaces.get,
    )
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    with patch("custom_components.ajax.switch.connect_new_entity_signal"):
        await async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


async def _capture_builder(device: AjaxDevice | None):
    """Run async_setup_entry and return the _build_device closure."""
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    if device:
        space.devices[device.id] = device
    coordinator = SimpleNamespace(
        entry_id="entry_test",
        account=AjaxAccount(user_id="u1", name="U", email="u@e.com", spaces={"s1": space}),
        get_space=lambda sid: space if sid == "s1" else None,
    )
    entry = SimpleNamespace(runtime_data=coordinator)
    captured: dict = {}

    def _fake_connect(hass, entry_, signal, domain, add, builder, label):
        captured["builder"] = builder

    with patch("custom_components.ajax.switch.connect_new_entity_signal", _fake_connect):
        await async_setup_entry(MagicMock(), entry, MagicMock())
    return captured["builder"]


@pytest.mark.asyncio
async def test_build_device_returns_empty_when_device_missing() -> None:
    builder = await _capture_builder(None)
    assert builder("s1", "ghost") == []


@pytest.mark.asyncio
async def test_build_device_dimmer_pairs() -> None:
    device = _device(
        device_type=DeviceType.WALLSWITCH,
        raw_type="LightSwitchDimmer",
        attributes={
            "settingsSwitch": [],
            "nightModeArm": False,
            "dimmerSettings": {"calibration": "DISABLED"},
        },
    )
    builder = await _capture_builder(device)
    pairs = builder("s1", "d1")
    uids = {uid for uid, _ in pairs}
    assert "d1_led_indicator" in uids
    assert "d1_night_mode" in uids
    assert "d1_dimmer_calibration" in uids


@pytest.mark.asyncio
async def test_build_device_handler_pairs() -> None:
    device = _device(device_type=DeviceType.RELAY, raw_type="RELAY", attributes={"is_on": False})
    builder = await _capture_builder(device)
    pairs = builder("s1", "d1")
    assert pairs  # SocketHandler yields at least the main switch
    assert all(isinstance(ent, AjaxSwitch) for _, ent in pairs)


@pytest.mark.asyncio
async def test_build_device_lightswitch_settings_pairs() -> None:
    device = _device(
        device_type=DeviceType.WALLSWITCH,
        raw_type="LightSwitchOneGang",
        attributes={"is_on": False, "settingsSwitch": []},
    )
    builder = await _capture_builder(device)
    pairs = builder("s1", "d1")
    uids = {uid for uid, _ in pairs}
    assert "d1_led_indicator" in uids
