"""Stale REST data must not overwrite a fresh realtime contact event (#285).

Seen in a user's log: after a realtime event, the REST data kept the previous
state for up to 23 s, so a poll landing in between flipped the door back.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ajax import _coordinator_door_poll as door_poll, models
from custom_components.ajax._device_apply import apply_device_payload
from custom_components.ajax.coordinator import AjaxDataCoordinator
from custom_components.ajax.models import AjaxAccount, AjaxDevice, AjaxSpace, DeviceType, SecurityState
from custom_components.ajax.sqs_manager import SQSManager
from custom_components.ajax.sse_manager import SSEManager

T0 = 1_000_000.0
OBSERVED_LAG = 23.4


def _device(kind: DeviceType = DeviceType.DOOR_CONTACT) -> AjaxDevice:
    return AjaxDevice(id="d1", name="Door", type=kind, space_id="s1", hub_id="h1")


def _space(device: AjaxDevice) -> AjaxSpace:
    space = AjaxSpace(id="s1", name="Home", hub_id="h1", security_state=SecurityState.DISARMED)
    space.devices[device.id] = device
    return space


def _event(manager: str, device: AjaxDevice, tag: str, transition: str) -> None:
    space = _space(device)
    if manager == "sse":
        sse = object.__new__(SSEManager)
        sse._find_device = lambda *_a: device  # type: ignore[method-assign]
        if device.type == DeviceType.WIRE_INPUT:
            sse._handle_wire_input_event(space, tag, "Door", "d1", transition)
        else:
            sse._handle_door_event(space, tag, "Door", "d1", transition)
        return
    sqs = object.__new__(SQSManager)
    sqs._find_device = lambda *_a: device  # type: ignore[method-assign]
    sqs.coordinator = MagicMock()
    sqs._language = "en"
    handler = sqs._handle_wire_input_event if device.type == DeviceType.WIRE_INPUT else sqs._handle_door_event
    asyncio.run(handler(space, tag, "Door", "d1", transition))


def _poll(device: AjaxDevice, payload: dict) -> None:
    """Run exactly one door-poll iteration."""
    coord = object.__new__(AjaxDataCoordinator)
    coord.account = AjaxAccount(user_id="u", name="u", email="u@e.com")
    coord.account.spaces["s1"] = _space(device)
    coord.api = SimpleNamespace(async_get_devices=AsyncMock(return_value=[{"id": "d1", **payload}]))
    coord.async_set_updated_data = MagicMock()
    coord._door_sensor_fast_poll_enabled = True
    coord._sse_url = None
    with (
        patch.object(door_poll.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(coord._async_poll_door_sensors_loop())


CASES = [
    (DeviceType.DOOR_CONTACT, "door_opened", "doorclosed", {"reedClosed": False}),
    (DeviceType.WIRE_INPUT, "door_opened", None, {"externalContactState": "TRIGGERED"}),
]


@pytest.mark.parametrize("manager", ["sse", "sqs"])
@pytest.mark.parametrize("reader", ["door_poll", "main_poll"])
@pytest.mark.parametrize(("kind", "attr", "tag", "stale"), CASES)
def test_stale_rest_state_is_ignored_then_accepted(manager, reader, kind, attr, tag, stale) -> None:
    from custom_components.ajax.event_maps import WIRE_INPUT_EVENTS

    device = _device(kind)
    device.attributes[attr] = True
    read = _poll if reader == "door_poll" else apply_device_payload
    with patch.object(models.time, "time", return_value=T0):
        _event(manager, device, tag or next(iter(WIRE_INPUT_EVENTS)), "RECOVERED")
    assert device.attributes[attr] is False

    for delay in (0.5, 10, OBSERVED_LAG):
        with patch.object(models.time, "time", return_value=T0 + delay):
            read(device, stale)
        assert device.attributes[attr] is False, delay

    # A lost realtime event is still recovered once the data can be trusted.
    with patch.object(models.time, "time", return_value=T0 + 60):
        read(device, stale)
    assert device.attributes[attr] is True


@pytest.mark.parametrize("manager", ["sse", "sqs"])
def test_a_newer_realtime_event_always_wins(manager) -> None:
    device = _device()
    with patch.object(models.time, "time", return_value=T0):
        _event(manager, device, "dooropened", "TRIGGERED")
        _event(manager, device, "doorclosed", "RECOVERED")
    assert device.attributes["door_opened"] is False


@pytest.mark.parametrize("manager", ["sse", "sqs"])
def test_external_contact_is_guarded_separately(manager) -> None:
    device = _device()
    with patch.object(models.time, "time", return_value=T0):
        _event(manager, device, "extcontactopened", "TRIGGERED")
        apply_device_payload(device, {"extraContactClosed": True, "reedClosed": False})
    assert device.attributes["external_contact_opened"] is True
    assert device.attributes["door_opened"] is True


def test_a_contact_without_realtime_event_is_still_polled() -> None:
    device = _device()
    device.attributes["door_opened"] = False
    _poll(device, {"reedClosed": False})
    assert device.attributes["door_opened"] is True
