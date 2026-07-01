"""Tests for ``AjaxDevicesMixin`` (``_coordinator_devices.py``).

Covers the device-reconciliation pipeline:

* ``_normalize_device_attributes`` per device family (door/motion/smoke/
  flood/glass/socket-relay-switch/MCP).
* ``_async_update_devices`` reconciliation (create/update/delete, dedup of
  MultiTransmitter duplicates, smart-lock skip, ``isinstance`` model guard,
  per-family attribute mapping).
* ``_reset_expired_motion_detections`` (30 s impulse expiry).
* ``_async_cleanup_stale_devices`` (HA registry pruning by known IDs).

Style: ``object.__new__`` the mixin, mock ``api``/``account``/``hass``, build
realistic ``AjaxDevice``/``AjaxSpace`` objects. ``dr.async_get`` is patched in
the module under test so no real HA registry is needed.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.ajax._coordinator_devices import AjaxDevicesMixin
from custom_components.ajax._ids import device_identifier
from custom_components.ajax.const import DOMAIN
from custom_components.ajax.models import (
    AjaxAccount,
    AjaxDevice,
    AjaxRoom,
    AjaxSmartLock,
    AjaxSpace,
    DeviceType,
)

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "DoorProtect": DeviceType.DOOR_CONTACT,
    "MotionProtect": DeviceType.MOTION_DETECTOR,
    "FireProtect": DeviceType.SMOKE_DETECTOR,
    "LeaksProtect": DeviceType.FLOOD_DETECTOR,
    "GlassProtect": DeviceType.GLASS_BREAK,
    "Socket": DeviceType.SOCKET,
    "Relay": DeviceType.RELAY,
    "WallSwitch": DeviceType.WALLSWITCH,
    "WaterStop": DeviceType.WATERSTOP,
    "LightSwitch": DeviceType.WALLSWITCH,
    "LightSwitchDimmer": DeviceType.WALLSWITCH,
    "Siren": DeviceType.SIREN,
    "MultiTransmitter": DeviceType.MULTI_TRANSMITTER,
    "ManualCallPoint": DeviceType.MANUAL_CALL_POINT,
    "LockBridge": DeviceType.SMART_LOCK,
}


def _parse_type(_self: object, type_str: str) -> DeviceType:
    """Stub mirroring the coordinator's ``_parse_device_type``."""
    return _TYPE_MAP.get(type_str, DeviceType.UNKNOWN)


def _make_mixin(
    *,
    account: AjaxAccount | None = None,
    initial_load_done: bool = True,
    devices_list: list[dict] | None = None,
) -> AjaxDevicesMixin:
    mixin = object.__new__(AjaxDevicesMixin)
    mixin.account = account
    mixin.hass = MagicMock()
    mixin.entry_id = "entry_test"
    mixin._initial_load_done = initial_load_done
    api = MagicMock()
    api.async_get_devices = AsyncMock(return_value=devices_list or [])
    mixin.api = api
    # _parse_device_type is provided by the host coordinator; stub it.
    mixin._parse_device_type = lambda type_str: _parse_type(mixin, type_str)
    return mixin


def _make_space(*, hub_id: str = "hub1", rooms: dict[str, AjaxRoom] | None = None) -> AjaxSpace:
    space = AjaxSpace(id="s1", name="Maison", hub_id=hub_id)
    if rooms:
        space.rooms = rooms
        space.rooms_map = {rid: r.name for rid, r in rooms.items()}
    return space


def _account_with_space(space: AjaxSpace) -> AjaxAccount:
    acc = AjaxAccount(user_id="u1", name="N", email="u@example.com")
    acc.spaces[space.id] = space
    return acc


def _device(device_type: DeviceType = DeviceType.RELAY, **kwargs) -> AjaxDevice:
    base = {
        "id": "d1",
        "name": "Device",
        "type": device_type,
        "space_id": "s1",
        "hub_id": "hub1",
    }
    base.update(kwargs)
    return AjaxDevice(**base)


# ===========================================================================
# _normalize_device_attributes
# ===========================================================================


def _norm(attrs: dict, device_type: DeviceType) -> dict:
    mixin = object.__new__(AjaxDevicesMixin)
    return mixin._normalize_device_attributes(attrs, device_type)


def test_normalize_door_contact_reed_closed_inverts_to_door_opened() -> None:
    out = _norm({"reedClosed": False}, DeviceType.DOOR_CONTACT)
    assert out["door_opened"] is True
    out_closed = _norm({"reedClosed": True}, DeviceType.DOOR_CONTACT)
    assert out_closed["door_opened"] is False


def test_normalize_door_contact_keeps_explicit_door_opened() -> None:
    out = _norm({"door_opened": True, "reedClosed": True}, DeviceType.DOOR_CONTACT)
    # Explicit door_opened wins over reedClosed conversion.
    assert out["door_opened"] is True


def test_normalize_door_contact_external_contact_opened() -> None:
    out = _norm({"extraContactClosed": False}, DeviceType.DOOR_CONTACT)
    assert out["external_contact_opened"] is True


def test_normalize_wire_input_external_contact_state_ok_is_closed() -> None:
    out = _norm({"externalContactState": "OK"}, DeviceType.WIRE_INPUT)
    assert out["door_opened"] is False


def test_normalize_wire_input_external_contact_state_triggered_is_open() -> None:
    out = _norm({"externalContactState": "TRIGGERED"}, DeviceType.WIRE_INPUT)
    assert out["door_opened"] is True


def test_normalize_wire_input_two_eol_parses_tamper_and_door() -> None:
    attrs = {
        "externalContactState": "OK",
        "wiringSchemeSpecificDetails": {
            "wiringSchemeType": "TWO_EOL",
            "contactOneDetails": {"contactState": "TRIGGERED"},
            "contactTwoDetails": {"contactState": "TRIGGERED"},
        },
    }
    out = _norm(attrs, DeviceType.WIRE_INPUT)
    assert out["wiring_type"] == "TWO_EOL"
    assert out["tampered"] is True
    assert out["door_opened"] is True


def test_normalize_wire_input_one_eol_or_logic() -> None:
    attrs = {
        "externalContactState": "OK",
        "wiringSchemeSpecificDetails": {
            "wiringSchemeType": "ONE_EOL",
            "contactDetails": {"contactState": "TRIGGERED"},
        },
    }
    out = _norm(attrs, DeviceType.WIRE_INPUT)
    assert out["door_opened"] is True


def test_normalize_wire_input_no_eol_or_logic() -> None:
    attrs = {
        "externalContactState": "OK",
        "wiringSchemeSpecificDetails": {
            "wiringSchemeType": "NO_EOL",
            "contactState": "TRIGGERED",
        },
    }
    out = _norm(attrs, DeviceType.WIRE_INPUT)
    assert out["door_opened"] is True


def test_normalize_motion_camelcase_to_snake() -> None:
    out = _norm(
        {"motionDetected": True, "motionDetectedAt": "2026-05-31T10:00:00+00:00"},
        DeviceType.MOTION_DETECTOR,
    )
    assert out["motion_detected"] is True
    assert out["motion_detected_at"] == "2026-05-31T10:00:00+00:00"


def test_normalize_smoke_camelcase() -> None:
    out = _norm({"smokeDetected": True}, DeviceType.SMOKE_DETECTOR)
    assert out["smoke_detected"] is True


def test_normalize_flood_camelcase() -> None:
    out = _norm({"leakDetected": True}, DeviceType.FLOOD_DETECTOR)
    assert out["leak_detected"] is True


def test_normalize_glass_break_camelcase() -> None:
    out = _norm({"glassBreakDetected": True}, DeviceType.GLASS_BREAK)
    assert out["glass_break_detected"] is True


def test_normalize_socket_switch_state_off() -> None:
    out = _norm({"switchState": ["SWITCHED_OFF"]}, DeviceType.SOCKET)
    assert out["is_on"] is False


def test_normalize_relay_switch_state_on_empty_list() -> None:
    out = _norm({"switchState": []}, DeviceType.RELAY)
    assert out["is_on"] is True


def test_normalize_wallswitch_switch_state_non_list_defaults_on() -> None:
    out = _norm({"switchState": "anything"}, DeviceType.WALLSWITCH)
    assert out["is_on"] is True


def test_normalize_manual_call_point_fields() -> None:
    attrs = {
        "switchState": "BUTTON_PRESSED",
        "customEvent": "FIRE_ALARM",
        "color": "RED",
        "selfMonitoringConfig": {"x": 1},
    }
    out = _norm(attrs, DeviceType.MANUAL_CALL_POINT)
    assert out["switchState"] == "BUTTON_PRESSED"
    assert out["customEvent"] == "FIRE_ALARM"
    assert out["color"] == "RED"
    assert out["selfMonitoringConfig"] == {"x": 1}


def test_normalize_unknown_type_passes_through() -> None:
    out = _norm({"foo": "bar"}, DeviceType.UNKNOWN)
    assert out == {"foo": "bar"}


# ===========================================================================
# _reset_expired_motion_detections
# ===========================================================================


def test_reset_motion_expired_after_30s() -> None:
    mixin = _make_mixin()
    space = _make_space()
    stale = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    dev = _device(DeviceType.MOTION_DETECTOR)
    dev.attributes["motion_detected"] = True
    dev.attributes["motion_detected_at"] = stale
    space.devices["d1"] = dev
    mixin._reset_expired_motion_detections(space)
    assert dev.attributes["motion_detected"] is False


def test_reset_motion_kept_within_window() -> None:
    mixin = _make_mixin()
    space = _make_space()
    fresh = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    dev = _device(DeviceType.MOTION_DETECTOR)
    dev.attributes["motion_detected"] = True
    dev.attributes["motion_detected_at"] = fresh
    space.devices["d1"] = dev
    mixin._reset_expired_motion_detections(space)
    assert dev.attributes["motion_detected"] is True


def test_reset_motion_naive_timestamp_assumed_utc() -> None:
    mixin = _make_mixin()
    space = _make_space()
    naive = (datetime.now(UTC) - timedelta(seconds=60)).replace(tzinfo=None).isoformat()
    dev = _device(DeviceType.MOTION_DETECTOR)
    dev.attributes["motion_detected"] = True
    dev.attributes["motion_detected_at"] = naive
    space.devices["d1"] = dev
    mixin._reset_expired_motion_detections(space)
    assert dev.attributes["motion_detected"] is False


def test_reset_motion_no_timestamp_clears_immediately() -> None:
    mixin = _make_mixin()
    space = _make_space()
    dev = _device(DeviceType.MOTION_DETECTOR)
    dev.attributes["motion_detected"] = True
    space.devices["d1"] = dev
    mixin._reset_expired_motion_detections(space)
    assert dev.attributes["motion_detected"] is False


def test_reset_motion_bad_timestamp_dropped() -> None:
    mixin = _make_mixin()
    space = _make_space()
    dev = _device(DeviceType.MOTION_DETECTOR)
    dev.attributes["motion_detected"] = True
    dev.attributes["motion_detected_at"] = "not-a-date"
    space.devices["d1"] = dev
    mixin._reset_expired_motion_detections(space)
    assert dev.attributes["motion_detected"] is False
    assert "motion_detected_at" not in dev.attributes


def test_reset_motion_ignores_non_motion_devices() -> None:
    mixin = _make_mixin()
    space = _make_space()
    dev = _device(DeviceType.DOOR_CONTACT)
    dev.attributes["motion_detected"] = True
    space.devices["d1"] = dev
    mixin._reset_expired_motion_detections(space)
    # Untouched: not a motion detector.
    assert dev.attributes["motion_detected"] is True


def test_reset_motion_skips_when_not_detected() -> None:
    mixin = _make_mixin()
    space = _make_space()
    dev = _device(DeviceType.MOTION_DETECTOR)
    dev.attributes["motion_detected"] = False
    space.devices["d1"] = dev
    mixin._reset_expired_motion_detections(space)
    assert dev.attributes["motion_detected"] is False


# ===========================================================================
# _async_cleanup_stale_devices
# ===========================================================================


def _registry_with_devices(*identifiers_sets: set[tuple[str, str]]) -> MagicMock:
    """Build a registry stub.

    Identifiers are now namespaced ``(DOMAIN, f"{entry_id}_{raw}")``; the
    source strips the ``entry_test_`` prefix before comparing against the bare
    known Ajax ids. The HA device entries are exposed via ``_entries`` so the
    test can feed them to a patched ``async_entries_for_config_entry``.
    """
    registry = MagicMock()
    entries = []
    for i, ids in enumerate(identifiers_sets):
        ha_dev = SimpleNamespace(id=f"ha{i}", identifiers=ids)
        entries.append(ha_dev)
    registry._entries = entries
    registry.async_remove_device = MagicMock()
    return registry


def test_cleanup_stale_noop_when_account_none() -> None:
    mixin = _make_mixin(account=None)
    # Should simply return without touching the registry.
    mixin._async_cleanup_stale_devices()


def test_cleanup_stale_removes_unknown_ajax_device() -> None:
    space = _make_space()
    space.devices["known1"] = _device()
    account = _account_with_space(space)
    mixin = _make_mixin(account=account)

    registry = _registry_with_devices(
        {(DOMAIN, "entry_test_known1")},
        {(DOMAIN, "entry_test_ghost99")},
        {("other_domain", "x")},
    )
    with (
        patch(
            "custom_components.ajax._coordinator_devices.dr.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.ajax._coordinator_devices.dr.async_entries_for_config_entry",
            return_value=registry._entries,
        ),
    ):
        mixin._async_cleanup_stale_devices()

    registry.async_remove_device.assert_called_once_with("ha1")


def test_cleanup_stale_keeps_hub_and_space_ids() -> None:
    space = _make_space(hub_id="hub1")
    account = _account_with_space(space)
    mixin = _make_mixin(account=account)

    registry = _registry_with_devices(
        {(DOMAIN, "entry_test_s1")},  # space id
        {(DOMAIN, "entry_test_hub1")},  # hub id
    )
    with (
        patch(
            "custom_components.ajax._coordinator_devices.dr.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.ajax._coordinator_devices.dr.async_entries_for_config_entry",
            return_value=registry._entries,
        ),
    ):
        mixin._async_cleanup_stale_devices()

    registry.async_remove_device.assert_not_called()


def test_cleanup_stale_includes_video_edges_and_smart_locks() -> None:
    space = _make_space()
    space.video_edges["cam1"] = MagicMock()
    space.smart_locks["lock1"] = MagicMock()
    account = _account_with_space(space)
    mixin = _make_mixin(account=account)

    registry = _registry_with_devices(
        {(DOMAIN, "entry_test_cam1")},
        {(DOMAIN, "entry_test_lock1")},
    )
    with (
        patch(
            "custom_components.ajax._coordinator_devices.dr.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.ajax._coordinator_devices.dr.async_entries_for_config_entry",
            return_value=registry._entries,
        ),
    ):
        mixin._async_cleanup_stale_devices()

    registry.async_remove_device.assert_not_called()


# ===========================================================================
# _async_update_devices
# ===========================================================================


async def test_update_devices_noop_when_account_none() -> None:
    mixin = _make_mixin(account=None)
    await mixin._async_update_devices("s1")
    mixin.api.async_get_devices.assert_not_called()


async def test_update_devices_noop_when_space_missing() -> None:
    account = AjaxAccount(user_id="u1", name="N", email="u@example.com")
    mixin = _make_mixin(account=account)
    await mixin._async_update_devices("nope")
    mixin.api.async_get_devices.assert_not_called()


async def test_update_devices_noop_when_no_hub_id() -> None:
    space = AjaxSpace(id="s1", name="Maison", hub_id=None)
    account = _account_with_space(space)
    mixin = _make_mixin(account=account)
    await mixin._async_update_devices("s1")
    mixin.api.async_get_devices.assert_not_called()


async def test_update_devices_creates_new_device_and_dispatches() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Relais Salon",
            "deviceType": "Relay",
            "model": {"switchState": [], "batteryChargeLevelPercentage": 87.4},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)

    with patch("custom_components.ajax._coordinator_devices.async_dispatcher_send") as disp:
        await mixin._async_update_devices("s1")

    dev = space.devices["d1"]
    assert dev.name == "Relais Salon"
    assert dev.type == DeviceType.RELAY
    assert dev.battery_level == 87  # rounded
    assert dev.attributes["is_on"] is True
    disp.assert_called_once()


async def test_update_devices_skips_dispatch_before_initial_load() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [{"id": "d1", "deviceName": "X", "deviceType": "Relay", "model": {}}]
    mixin = _make_mixin(account=account, devices_list=payload, initial_load_done=False)

    with patch("custom_components.ajax._coordinator_devices.async_dispatcher_send") as disp:
        await mixin._async_update_devices("s1")

    disp.assert_not_called()
    assert "d1" in space.devices


async def test_update_devices_skips_entry_without_id() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [{"deviceName": "no id"}]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    assert space.devices == {}


async def test_update_devices_dedups_multitransmitter_duplicate() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {"id": "mt1", "deviceName": "MT input 1", "deviceType": "MultiTransmitter", "model": {}},
        {"id": "mt1", "deviceName": "MT input 2", "deviceType": "MultiTransmitter", "model": {}},
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    # Only created once despite two entries.
    assert list(space.devices.keys()) == ["mt1"]


async def test_update_devices_skips_smart_locks() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [{"id": "lock1", "deviceName": "Lock", "deviceType": "LockBridge", "model": {}}]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    assert "lock1" not in space.devices


async def test_update_devices_model_not_dict_is_guarded() -> None:
    space = _make_space()
    account = _account_with_space(space)
    # model is a list (non-conformant) — must not raise.
    payload = [{"id": "d1", "deviceName": "X", "deviceType": "Relay", "model": ["bad"]}]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    assert "d1" in space.devices


async def test_update_devices_updates_existing_device() -> None:
    space = _make_space()
    existing = _device(DeviceType.RELAY, id="d1", name="Old")
    space.devices["d1"] = existing
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Old",
            "deviceType": "Relay",
            "model": {"online": False, "bypassed": True, "malfunctions": ["m1", "m2"]},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    assert existing.online is False
    assert existing.bypassed is True
    assert existing.malfunctions == 2  # list normalized to count


async def test_update_devices_signal_level_string_mapping() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "X",
            "deviceType": "Relay",
            "model": {"signalLevel": "GOOD"},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    assert space.devices["d1"].signal_strength == 70


async def test_update_devices_signal_level_numeric_rounded() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "X",
            "deviceType": "Relay",
            "model": {"signalLevel": 88.6},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    assert space.devices["d1"].signal_strength == 89


async def test_update_devices_unknown_type_logs_and_stores() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [{"id": "d1", "deviceName": "X", "deviceType": "Wat", "model": {}}]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    assert space.devices["d1"].type == DeviceType.UNKNOWN


async def test_update_devices_room_membership_rebuilt() -> None:
    room = AjaxRoom(id="r1", name="Salon", space_id="s1")
    # Pre-seed a stale id to verify the clear() at poll start.
    room.device_ids.append("stale")
    space = _make_space(rooms={"r1": room})
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "X",
            "deviceType": "Relay",
            "roomId": "r1",
            "model": {},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    assert room.device_ids == ["d1"]
    assert space.devices["d1"].room_name == "Salon"


async def test_update_devices_door_contact_reed_and_temperature() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Porte",
            "deviceType": "DoorProtect",
            "model": {
                "reedClosed": False,
                "temperature": 21.55,
                "tampered": True,
            },
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    dev = space.devices["d1"]
    assert dev.attributes["door_opened"] is True
    assert dev.attributes["temperature"] == 21.6  # rounded to 1 decimal
    assert dev.attributes["tampered"] is True


async def test_update_devices_socket_power_monitoring() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Prise",
            "deviceType": "Socket",
            "model": {
                "switchState": [],
                "powerConsumedWattsPerHour": 2500,
                "currentMilliAmpers": 1500,
                "voltageVolts": 230,
            },
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    dev = space.devices["d1"]
    assert dev.attributes["energy"] == 2.5  # Wh -> kWh
    assert dev.attributes["current"] == 1.5  # mA -> A
    assert dev.attributes["voltage"] == 230
    assert dev.attributes["is_on"] is True


async def test_update_devices_socket_outlet_socket_state() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Outlet",
            "deviceType": "Socket",
            "model": {
                "socketState": ["FIRST_CHANNEL_ON"],
                "powerConsumptionWatts": 42,
                "currentMilliAmpere": 200,
            },
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    dev = space.devices["d1"]
    assert dev.attributes["is_on"] is True
    assert dev.attributes["power"] == 42
    assert dev.attributes["current"] == 0.2


async def test_update_devices_valve_state_respects_optimistic_guard() -> None:
    space = _make_space()
    dev = _device(DeviceType.WATERSTOP, id="d1", name="Vanne")
    dev.mark_optimistic("valveState")
    space.devices["d1"] = dev
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Vanne",
            "deviceType": "WaterStop",
            "model": {"valveState": "CLOSED", "motorState": "IDLE"},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    # valveState NOT written because optimistic guard is active.
    assert "valveState" not in dev.attributes
    # but other waterstop attrs are still written.
    assert dev.attributes["motorState"] == "IDLE"


async def test_update_devices_valve_state_written_without_guard() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Vanne",
            "deviceType": "WaterStop",
            "model": {"valveState": "OPEN"},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    assert space.devices["d1"].attributes["valveState"] == "OPEN"


async def test_update_devices_dimmer_brightness_respects_guard() -> None:
    space = _make_space()
    dev = _device(DeviceType.WALLSWITCH, id="d1", name="Dimmer")
    dev.mark_optimistic("actualBrightnessCh1")
    space.devices["d1"] = dev
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Dimmer",
            "deviceType": "LightSwitchDimmer",
            "model": {"actualBrightnessCh1": 50, "maxBrightnessLimitCh1": 100},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    assert "actualBrightnessCh1" not in dev.attributes
    assert dev.attributes["maxBrightnessLimitCh1"] == 100


async def test_update_devices_lightswitch_channels_and_buttons() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Inter",
            "deviceType": "LightSwitch",
            "model": {
                "channelStatuses": ["CHANNEL_1_ON"],
                "buttonOne": "Cuisine",
                "buttonTwo": {"buttonName": "Salon"},
            },
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    dev = space.devices["d1"]
    assert dev.attributes["channel_1_on"] is True
    assert dev.attributes["channel_2_on"] is False
    assert dev.attributes["channel_1_name"] == "Cuisine"
    assert dev.attributes["channel_2_name"] == "Salon"
    assert dev.attributes["is_multi_gang"] is True


async def test_update_devices_lightswitch_channels_skipped_when_optimistic() -> None:
    space = _make_space()
    dev = _device(DeviceType.WALLSWITCH, id="d1", name="Inter")
    dev.attributes["_optimistic_until"] = time.time() + 30
    dev.attributes["channel_1_on"] = True
    space.devices["d1"] = dev
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Inter",
            "deviceType": "LightSwitch",
            "model": {"channelStatuses": []},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    # Optimistic window active: channelStatuses not overwritten.
    assert dev.attributes["channel_1_on"] is True


async def test_update_devices_siren_volume_and_led() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Sirene",
            "deviceType": "Siren",
            "model": {
                "v2sirenVolumeLevel": "HIGH",
                "beepVolumeLevel": "LOW",
                "v2sirenIndicatorLightMode": "ON",
                "alertIfMoved": True,
            },
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    dev = space.devices["d1"]
    assert dev.attributes["siren_volume_level"] == "HIGH"
    assert dev.attributes["beep_volume_level"] == "LOW"
    assert dev.attributes["led_indication"] == "ON"
    assert dev.attributes["alert_if_moved"] is True


async def test_update_devices_switch_state_skipped_when_optimistic() -> None:
    space = _make_space()
    dev = _device(DeviceType.RELAY, id="d1", name="Relais")
    dev.mark_optimistic("is_on")
    dev.attributes["is_on"] = True
    space.devices["d1"] = dev
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Relais",
            "deviceType": "Relay",
            "model": {"switchState": ["SWITCHED_OFF"]},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    # is_on left at optimistic True, not bounced back to False.
    assert dev.attributes["is_on"] is True


async def test_update_devices_normalizes_nested_attributes() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Porte",
            "deviceType": "DoorProtect",
            "model": {},
            "attributes": {"reedClosed": False},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    assert space.devices["d1"].attributes["door_opened"] is True


async def test_update_devices_external_contact_state_two_eol_root() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "MT",
            "deviceType": "MultiTransmitter",
            "model": {
                "externalContactState": "OK",
                "wiringSchemeSpecificDetails": {
                    "wiringSchemeType": "TWO_EOL",
                    "contactOneDetails": {"contactState": "TRIGGERED"},
                    "contactTwoDetails": {"contactState": "TRIGGERED"},
                },
            },
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    dev = space.devices["d1"]
    assert dev.attributes["wiring_type"] == "TWO_EOL"
    assert dev.attributes["tampered"] is True
    assert dev.attributes["door_opened"] is True


# ---------------------------------------------------------------------------
# absent_poll_count removal threshold
# ---------------------------------------------------------------------------


async def test_update_devices_absent_device_removed_after_threshold() -> None:
    space = _make_space()
    # Device present in internal state but absent from the API payload.
    ghost = _device(DeviceType.RELAY, id="ghost", name="Ghost")
    space.devices["ghost"] = ghost
    account = _account_with_space(space)
    payload = [{"id": "d1", "deviceName": "X", "deviceType": "Relay", "model": {}}]
    mixin = _make_mixin(account=account, devices_list=payload)

    registry = MagicMock()
    registry.async_get_device = MagicMock(return_value=None)
    with patch(
        "custom_components.ajax._coordinator_devices.dr.async_get",
        return_value=registry,
    ):
        # First two polls: counter increments, not removed yet.
        await mixin._async_update_devices("s1")
        assert space.devices["ghost"].attributes["_absent_poll_count"] == 1
        await mixin._async_update_devices("s1")
        assert space.devices["ghost"].attributes["_absent_poll_count"] == 2
        # Third poll reaches threshold -> removed.
        await mixin._async_update_devices("s1")

    assert "ghost" not in space.devices


async def test_update_devices_absent_counter_reset_when_reappears() -> None:
    space = _make_space()
    dev = _device(DeviceType.RELAY, id="d1", name="X")
    dev.attributes["_absent_poll_count"] = 2
    space.devices["d1"] = dev
    account = _account_with_space(space)
    payload = [{"id": "d1", "deviceName": "X", "deviceType": "Relay", "model": {}}]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    assert "_absent_poll_count" not in space.devices["d1"].attributes


async def test_update_devices_kitchen_sink_attribute_mapping() -> None:
    """One payload exercising the many ``if key in device_data`` branches."""
    space = _make_space()
    account = _account_with_space(space)
    model = {
        # arm / always-active
        "alwaysActive": True,
        "nightModeArm": True,
        # DoorProtect Plus extras
        "extraContactAware": True,
        "shockSensorAware": True,
        "accelerometerAware": True,
        "shockSensorSensitivity": 3,
        "accelerometerTiltDegrees": 10,
        "ignoreSimpleImpact": True,
        "sirenTriggers": ["INTRUSION"],
        # external contacts
        "extraContactClosed": False,
        "externalContactTriggered": True,
        "sensitivity": 5,
        "color": "WHITE",
        # siren extras
        "alarmDuration": 90,
        "beepOnArmDisarm": True,
        "beepOnDelay": False,
        "chimesEnabled": True,
        "buzzerState": "OFF",
        "externallyPowered": True,
        "postAlarmIndicationMode": "BLINK",
        "alarmRestrictionMode": "NONE",
        "blinkWhileArmed": True,
        "indicatorLightMode": "ON",
        # FireProtect 2
        "coAlarmEnable": True,
        "tempAlarmEnable": True,
        "tempDiffAlarmEnable": False,
        "smokeAlarm": "OK",
        "coAlarm": "OK",
        "steamAlarm": "OK",
        "tempAlarm": "TEMP_ALARM_DETECTED",
        "tempHighDiffAlarm": "TEMP_HIGH_DIFF_ALARM_DETECTED",
        "alertsBySirens": True,
        # MotionCam
        "imageResolution": "HIGH",
        "photosPerAlarm": 3,
        # LifeQuality
        "actualCO2": 600,
        "actualTemperature": 22,
        "actualHumidity": 45,
        "minComfortCO2": 400,
        "maxComfortCO2": 1000,
        "minComfortTemperature": 18,
        "maxComfortTemperature": 26,
        "minComfortHumidity": 30,
        "maxComfortHumidity": 60,
        "calibrationState": "DONE",
        "indication": "ON",
        # socket protection
        "indicationEnabled": True,
        "indicationBrightness": 80,
        "currentProtectionEnabled": True,
        "voltageProtectionEnabled": True,
        "contactNormalState": "OPEN",
        "lockupRelayMode": "ON",
        "lockupRelayTimeSeconds": 5,
        "currentThresholdAmpere": 16,
        "indicationBrightnessV2": 90,
        # button
        "buttonMode": "PANIC",
        "brightness": 70,
        "falsePressFilter": True,
        "customAlarmType": "MEDICAL",
        "associatedUserId": "u9",
        # lightswitch settings
        "protectStatuses": ["OK"],
        "touchSensitivity": 4,
        "touchMode": "TAP",
        "dimmerSettings": {"x": 1},
        "dataChannelSignalQuality": 90,
        "dataChannelOk": True,
        "panelColor": "BLACK",
        # dimmer extras
        "minBrightnessLimitCh1": 5,
        "armActionBrightnessCh1": 100,
        "disarmActionBrightnessCh1": 0,
        "brightnessChangeSpeed": 2,
    }
    payload = [{"id": "d1", "deviceName": "X", "deviceType": "FireProtect", "model": model}]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    attrs = space.devices["d1"].attributes
    assert attrs["always_active"] is True
    assert attrs["armed_in_night_mode"] is True
    assert attrs["night_mode_arm"] is True
    assert attrs["siren_triggers"] == ["INTRUSION"]
    assert attrs["external_contact_opened"] is True
    assert attrs["externalContactTriggered"] is True
    assert attrs["temperatureAlarmDetected"] is True
    assert attrs["highTemperatureDiffDetected"] is True
    assert attrs["actualCO2"] == 600
    assert attrs["custom_alarm_type"] == "MEDICAL"
    assert attrs["customAlarmType"] == "MEDICAL"
    assert attrs["current_threshold"] == 16
    # indicationBrightnessV2 overrides indicationBrightness
    assert attrs["indicationBrightness"] == 90
    assert attrs["blink_while_armed"] is True
    assert attrs["panelColor"] == "BLACK"


async def test_update_devices_always_active_from_wired_settings() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "MT",
            "deviceType": "MultiTransmitter",
            "model": {"wiredDeviceSettings": {"alwaysActive": True, "nightModeArm": True}},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    attrs = space.devices["d1"].attributes
    assert attrs["always_active"] is True
    assert attrs["armed_in_night_mode"] is True
    assert attrs["night_mode_arm"] is True


async def test_update_devices_siren_volume_fallback_and_led_fallback() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Sirene",
            "deviceType": "Siren",
            "model": {
                "sirenVolumeLevel": "MEDIUM",  # deprecated fallback
                "blinkWhileArmed": False,  # led_indication fallback
            },
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    attrs = space.devices["d1"].attributes
    assert attrs["siren_volume_level"] == "MEDIUM"
    assert attrs["led_indication"] is False


async def test_update_devices_external_contact_one_eol_root() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "MT",
            "deviceType": "MultiTransmitter",
            "model": {
                "externalContactState": "OK",
                "wiringSchemeSpecificDetails": {
                    "wiringSchemeType": "ONE_EOL",
                    "contactDetails": {"contactState": "TRIGGERED"},
                },
            },
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    assert space.devices["d1"].attributes["door_opened"] is True


async def test_update_devices_external_contact_no_eol_root() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "MT",
            "deviceType": "MultiTransmitter",
            "model": {
                "externalContactState": "OK",
                "wiringSchemeSpecificDetails": {
                    "wiringSchemeType": "NO_EOL",
                    "contactState": "TRIGGERED",
                },
            },
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    assert space.devices["d1"].attributes["door_opened"] is True


async def test_update_devices_indication_mode_derives_enabled() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Outlet",
            "deviceType": "Socket",
            "model": {"indicationMode": "ENABLED"},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    attrs = space.devices["d1"].attributes
    assert attrs["indicationMode"] == "ENABLED"
    assert attrs["indicationEnabled"] is True


async def test_update_devices_button_names_non_string_non_dict_fallback() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Inter",
            "deviceType": "LightSwitch",
            "model": {"buttonOne": 12345, "buttonTwo": 67890},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    attrs = space.devices["d1"].attributes
    assert attrs["channel_1_name"] == "Channel 1"
    assert attrs["channel_2_name"] == "Channel 2"
    assert attrs["has_channel_1"] is True
    assert attrs["has_channel_2"] is True


async def test_update_devices_settings_switch_respects_guard() -> None:
    space = _make_space()
    dev = _device(DeviceType.WALLSWITCH, id="d1", name="Inter")
    dev.mark_optimistic("settingsSwitch")
    space.devices["d1"] = dev
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Inter",
            "deviceType": "LightSwitch",
            "model": {"settingsSwitch": ["A"]},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    assert "settingsSwitch" not in dev.attributes


async def test_update_devices_switch_state_non_list_defaults_on() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Relais",
            "deviceType": "Relay",
            "model": {"switchState": "weird"},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    assert space.devices["d1"].attributes["is_on"] is True


async def test_update_devices_socket_state_non_list_defaults_off() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Outlet",
            "deviceType": "Socket",
            "model": {"socketState": "weird"},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    assert space.devices["d1"].attributes["is_on"] is False


async def test_update_devices_button_one_dict_two_string() -> None:
    space = _make_space()
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Inter",
            "deviceType": "LightSwitch",
            "model": {
                "actualBrightnessCh1": 50,  # no guard -> line 597
                "settingsSwitch": ["A"],  # no guard -> line 611
                "buttonOne": {"buttonName": "Couloir"},  # dict -> line 644
                "buttonTwo": "Garage",  # string -> line 649
            },
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    attrs = space.devices["d1"].attributes
    assert attrs["actualBrightnessCh1"] == 50
    assert attrs["settingsSwitch"] == ["A"]
    assert attrs["channel_1_name"] == "Couloir"
    assert attrs["channel_2_name"] == "Garage"


async def test_update_devices_removed_device_unregistered_from_ha() -> None:
    space = _make_space()
    ghost = _device(DeviceType.RELAY, id="ghost", name="Ghost")
    ghost.attributes["_absent_poll_count"] = 2  # one short of threshold
    space.devices["ghost"] = ghost
    account = _account_with_space(space)
    payload = [{"id": "d1", "deviceName": "X", "deviceType": "Relay", "model": {}}]
    mixin = _make_mixin(account=account, devices_list=payload)

    ha_device = SimpleNamespace(id="ha-ghost")
    registry = MagicMock()
    registry.async_get_device = MagicMock(return_value=ha_device)
    registry.async_remove_device = MagicMock()
    with patch(
        "custom_components.ajax._coordinator_devices.dr.async_get",
        return_value=registry,
    ):
        await mixin._async_update_devices("s1")

    # Lookup uses the namespaced identifier (DOMAIN, "entry_test_ghost").
    registry.async_get_device.assert_called_once_with(identifiers={device_identifier("entry_test", "ghost")})
    registry.async_remove_device.assert_called_once_with("ha-ghost")
    assert "ghost" not in space.devices


# ---------------------------------------------------------------------------
# _apply_smart_lock_rest_state (#88 — REST polling state for event-less locks)
# ---------------------------------------------------------------------------


def _lock_mixin() -> AjaxDevicesMixin:
    return object.__new__(AjaxDevicesMixin)


def test_apply_smart_lock_rest_state_from_record() -> None:
    mixin = _lock_mixin()
    space = _make_space()
    lock = AjaxSmartLock(id="lock1", name="Doorman", space_id="s1")
    space.smart_locks["lock1"] = lock
    mixin._apply_smart_lock_rest_state(space, "lock1", {"lockStatus": "LOCKED", "doorStatus": "OPEN"})
    assert lock.is_locked is True
    assert lock.is_door_open is True
    mixin._apply_smart_lock_rest_state(space, "lock1", {"lockStatus": "UNLOCKED", "doorStatus": "CLOSED"})
    assert lock.is_locked is False
    assert lock.is_door_open is False


def test_apply_smart_lock_rest_state_missing_lock_is_noop() -> None:
    # Lock not discovered yet → no entry, must not raise.
    _lock_mixin()._apply_smart_lock_rest_state(_make_space(), "ghost", {"lockStatus": "LOCKED"})


def test_apply_smart_lock_rest_state_ignores_unknown_status() -> None:
    mixin = _lock_mixin()
    space = _make_space()
    lock = AjaxSmartLock(id="lock1", name="Doorman", space_id="s1")
    space.smart_locks["lock1"] = lock
    mixin._apply_smart_lock_rest_state(space, "lock1", {})
    assert lock.is_locked is None  # nothing in the record → stays unknown


def test_apply_smart_lock_rest_state_skips_recent_event() -> None:
    """A real-time event within 30 s wins over the slower poll."""
    mixin = _lock_mixin()
    space = _make_space()
    lock = AjaxSmartLock(id="lock1", name="Doorman", space_id="s1", is_locked=False)
    lock.last_event_time = datetime.now(UTC)
    space.smart_locks["lock1"] = lock
    mixin._apply_smart_lock_rest_state(space, "lock1", {"lockStatus": "LOCKED"})
    assert lock.is_locked is False  # event value preserved


def test_apply_smart_lock_rest_state_applies_after_stale_event() -> None:
    mixin = _lock_mixin()
    space = _make_space()
    lock = AjaxSmartLock(id="lock1", name="Doorman", space_id="s1", is_locked=False)
    lock.last_event_time = datetime.now(UTC) - timedelta(minutes=5)
    space.smart_locks["lock1"] = lock
    mixin._apply_smart_lock_rest_state(space, "lock1", {"lockStatus": "LOCKED"})
    assert lock.is_locked is True  # stale event → poll updates


async def test_update_devices_nested_attributes_respect_optimistic_guard() -> None:
    """The late ``attributes`` merge must honour per-key optimistic guards.

    A nested ``attributes.switchState`` is re-normalised to ``is_on`` by
    ``_normalize_device_attributes``; without the guard the merge undid the
    root-level ``is_on`` protection during the 15 s optimistic window.
    """
    space = _make_space()
    dev = _device(DeviceType.RELAY, id="d1", name="Relais")
    dev.mark_optimistic("is_on")
    dev.attributes["is_on"] = True
    space.devices["d1"] = dev
    account = _account_with_space(space)
    payload = [
        {
            "id": "d1",
            "deviceName": "Relais",
            "deviceType": "Relay",
            "attributes": {"switchState": ["SWITCHED_OFF"], "signalLevel": "GOOD"},
        }
    ]
    mixin = _make_mixin(account=account, devices_list=payload)
    await mixin._async_update_devices("s1")
    # is_on stays optimistic-True; the non-guarded key still merges normally.
    assert dev.attributes["is_on"] is True
    assert dev.attributes.get("signal_level") == "GOOD" or "signalLevel" in dev.attributes
