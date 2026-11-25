"""Detections of NVR channels without an Ajax camera (#282)."""

from __future__ import annotations

import logging
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Stub the optional ONVIF package surface before importing any module that
# imports ``onvif`` at module level (onvif_client / onvif_manager).
for _name in ("onvif", "onvif.client", "onvif.exceptions", "zeep", "zeep.exceptions"):
    if _name not in sys.modules:
        _mod = ModuleType(_name)
        if _name == "onvif":
            _mod.__file__ = "/dev/null/onvif/__init__.py"
            _mod.ONVIFCamera = MagicMock()  # type: ignore[attr-defined]
        if _name == "onvif.exceptions":
            _mod.ONVIFError = type("ONVIFError", (Exception,), {})  # type: ignore[attr-defined]
        if _name == "zeep.exceptions":
            _mod.Fault = type("Fault", (Exception,), {})  # type: ignore[attr-defined]
            _mod.TransportError = type("TransportError", (Exception,), {})  # type: ignore[attr-defined]
        sys.modules[_name] = _mod

from custom_components.ajax._coordinator_onvif import AjaxOnvifMixin  # noqa: E402
from custom_components.ajax._nvr_channels import (  # noqa: E402
    find_channel_by_token,
    has_third_party_channels,
    is_third_party_channel,
)
from custom_components.ajax.devices.video_edge import VideoEdgeHandler  # noqa: E402
from custom_components.ajax.models import AjaxSpace, AjaxVideoEdge, VideoEdgeType  # noqa: E402
from custom_components.ajax.onvif_client import AjaxOnvifClient, OnvifDetectionEvent  # noqa: E402
from custom_components.ajax.onvif_manager import AjaxOnvifManager  # noqa: E402

BALCONY, EXT, INT = "Y8nWCzHpYI-0", "pVwD9WKjQ6-00000", "rgn19N6T9T-0"
CAM_CHANNEL = "cF7PnB7Sze-A1B2C3D4E5F6-0"


def _channel(channel_id: str, name: str, ve_id: str, ve_type: str) -> dict[str, Any]:
    source = {"sourceType": "PRIMARY", "type": ve_type, "videoEdgeId": ve_id}
    return {"id": channel_id, "name": name, "sourceAliases": {"sources": [source]}}


def _setup() -> tuple[AjaxVideoEdge, AjaxVideoEdge, AjaxSpace]:
    """The layout of the reporter's NVR: Ajax cameras first, then third-party channels."""
    cam = AjaxVideoEdge("cam1", "Karavidas", "s1", VideoEdgeType.BULLET, ip_address="192.0.2.1")
    nvr = AjaxVideoEdge("nvr1", "NVR AI", "s1", VideoEdgeType.NVR, ip_address="192.0.2.2")
    nvr.channels = [
        _channel(CAM_CHANNEL, "Karavidas", "cam1", "BULLET_HL"),
        _channel(BALCONY, "Balcony", "nvr1", "NVR_H_2D_AI_8P_AC"),
        _channel(EXT, "Doorbell ext.", "nvr1", "NVR_H_2D_AI_8P_AC"),
        _channel(INT, "Doorbell int.", "nvr1", "NVR_H_2D_AI_8P_AC"),
    ]
    space = AjaxSpace(id="s1", name="Home")
    space.video_edges.update({"cam1": cam, "nvr1": nvr})
    return nvr, cam, space


def _coordinator(space: AjaxSpace) -> Any:
    m = object.__new__(AjaxOnvifMixin)
    m.account = SimpleNamespace(spaces={"s1": space})
    m.stats = {"events_onvif_received": 0}
    m.hass = MagicMock()
    m._event_entities = {}
    m.async_set_updated_data = MagicMock()
    return m


def _states(nvr: AjaxVideoEdge, space: AjaxSpace, detection: str = "human") -> dict[str, bool]:
    """Value of each detection sensor, keyed by channel id."""
    sensors = VideoEdgeHandler(nvr, space.video_edges).get_binary_sensors()
    prefix = f"{detection}_"
    return {s["channel_id"]: s["value_fn"]() for s in sensors if s["key"].startswith(prefix)}


# --- channel helpers -------------------------------------------------------


def test_third_party_channels_are_those_whose_source_is_the_nvr() -> None:
    nvr, cam, _ = _setup()
    assert [is_third_party_channel(nvr, c) for c in nvr.channels] == [False, True, True, True]
    assert has_third_party_channels(nvr)
    nvr.channels = nvr.channels[:1]
    assert not has_third_party_channels(nvr)
    assert not has_third_party_channels(cam)


@pytest.mark.parametrize(
    "channel",
    ["x", {}, {"sourceAliases": "x"}, {"sourceAliases": {"sources": "x"}}, {"sourceAliases": {"sources": [1]}}],
)
def test_malformed_channels_are_not_third_party(channel: Any) -> None:
    nvr, _, _ = _setup()
    assert not is_third_party_channel(nvr, channel)


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (BALCONY, BALCONY),
        (EXT, EXT),
        ("VideoSource_rgn19N6T9T", INT),
        ("Y8nWCzHpYI", BALCONY),
        (CAM_CHANNEL, CAM_CHANNEL),
        # Anything that looks like an index must match nothing.
        ("0", None),
        ("00000", None),
        ("VideoSource_1", None),
        ("nvr-0", None),
        ("", None),
        ("xY8nWCzHpYIx", None),
        (f"{BALCONY}/{INT}", None),
    ],
)
def test_channel_is_matched_by_token_only(token: str, expected: str | None) -> None:
    nvr, _, _ = _setup()
    channel = find_channel_by_token(nvr, token)
    assert (channel["id"] if channel else None) == expected


def test_channel_lookup_survives_missing_channels() -> None:
    nvr, _, _ = _setup()
    nvr.channels = "x"  # type: ignore[assignment]
    assert find_channel_by_token(nvr, BALCONY) is None
    assert not has_third_party_channels(nvr)


# --- coordinator -----------------------------------------------------------


@pytest.mark.parametrize("channel_id", [BALCONY, EXT, INT])
def test_a_detection_lights_its_own_channel_only(channel_id: str) -> None:
    nvr, cam, space = _setup()
    m = _coordinator(space)
    m._handle_onvif_event(OnvifDetectionEvent("nvr1", channel_id, "VIDEO_HUMAN", True))
    assert [c for c, on in _states(nvr, space).items() if on] == [channel_id]
    assert cam.detections == {}
    assert nvr.detections == {}
    m.async_set_updated_data.assert_called_once()

    m._handle_onvif_event(OnvifDetectionEvent("nvr1", channel_id, "VIDEO_HUMAN", False))
    assert not any(_states(nvr, space).values())


def test_motion_clearing_clears_the_ai_detections_of_that_channel_only() -> None:
    nvr, _, space = _setup()
    m = _coordinator(space)
    for channel_id in (BALCONY, INT):
        m._handle_onvif_event(OnvifDetectionEvent("nvr1", channel_id, "VIDEO_HUMAN", True))
    m._handle_onvif_event(OnvifDetectionEvent("nvr1", BALCONY, "VIDEO_MOTION", False))
    assert [c for c, on in _states(nvr, space).items() if on] == [INT]


def test_bus_event_names_the_channel_and_no_camera() -> None:
    _, _, space = _setup()
    m = _coordinator(space)
    entity = MagicMock(entity_id="event.nvr_detection")
    m._event_entities["nvr1_detection"] = entity
    m._handle_onvif_event(OnvifDetectionEvent("nvr1", EXT, "VIDEO_PET", True, rule="Yard"))
    entity.fire.assert_called_once_with("pet", {"channel": "Doorbell ext.", "rule": "Yard"})
    name, data = m.hass.bus.async_fire.call_args.args
    assert name == "ajax_camera_detection"
    assert data == {
        "device_id": "nvr1",
        "device_name": "NVR AI",
        "channel_id": EXT,
        "channel_name": "Doorbell ext.",
        "event_type": "pet",
        "source": "onvif",
        "entity_id": "event.nvr_detection",
    }


@pytest.mark.parametrize(
    ("token", "detection"),
    [(CAM_CHANNEL, "VIDEO_HUMAN"), ("0", "VIDEO_HUMAN"), ("", "VIDEO_MOTION"), (EXT, "DOORBELL_RING")],
)
def test_other_nvr_events_change_nothing(token: str, detection: str) -> None:
    nvr, cam, space = _setup()
    m = _coordinator(space)
    m._handle_onvif_event(OnvifDetectionEvent("nvr1", token, detection, True))
    assert nvr.channel_detections == {}
    assert cam.detections == {}
    m.hass.bus.async_fire.assert_not_called()


def test_camera_backed_sensor_still_reads_its_camera() -> None:
    nvr, cam, space = _setup()
    cam.detections["video_human"] = True
    assert [c for c, on in _states(nvr, space).items() if on] == [CAM_CHANNEL]


# --- manager ---------------------------------------------------------------


async def test_manager_connects_the_nvr_only_for_third_party_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    nvr, cam, _ = _setup()
    mgr = AjaxOnvifManager("u", "p")
    added: list[str] = []

    async def _add(ve: AjaxVideoEdge) -> bool:
        added.append(ve.id)
        mgr._clients[ve.id] = SimpleNamespace(connected=True, async_stop=AsyncMock())  # type: ignore[assignment]
        return True

    monkeypatch.setattr(mgr, "async_add_video_edge", _add)
    await mgr.async_start([cam, nvr])
    assert added == ["cam1", "nvr1"]
    # The camera counters, shown in Repairs and in a sensor, leave the NVR out.
    assert (mgr.connected_count, mgr.target_count) == (1, 1)

    nvr.channels = nvr.channels[:1]
    await mgr.async_update_video_edges([cam, nvr])
    assert set(mgr._clients) == {"cam1"}


async def test_manager_connects_the_nvr_once_a_third_party_channel_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    nvr, cam, _ = _setup()
    mgr = AjaxOnvifManager("u", "p")
    added: list[str] = []

    async def _add(ve: AjaxVideoEdge) -> bool:
        added.append(ve.id)
        return True

    monkeypatch.setattr(mgr, "async_add_video_edge", _add)
    await mgr.async_update_video_edges([nvr])
    assert added == ["nvr1"]
    assert mgr.target_count == 0


# --- client ----------------------------------------------------------------


def _message(topic: str, source: dict[str, str], data: dict[str, str]) -> Any:
    def items(values: dict[str, str]) -> Any:
        return SimpleNamespace(SimpleItem=[SimpleNamespace(Name=k, Value=v) for k, v in values.items()])

    return SimpleNamespace(
        Topic=SimpleNamespace(_value_1=topic),
        Message=SimpleNamespace(_value_1=SimpleNamespace(Source=items(source), Data=items(data))),
    )


async def test_nvr_client_keeps_the_full_token_and_separates_channels(caplog: pytest.LogCaptureFixture) -> None:
    nvr, _, _ = _setup()
    events: list[OnvifDetectionEvent] = []
    client = AjaxOnvifClient(nvr, "u", "p", event_callback=events.append)
    topic = "tns1:RuleEngine/tnsajax:MotionDetector/Detection"
    with caplog.at_level(logging.DEBUG, logger="custom_components.ajax.onvif_client"):
        # Both tokens end with "-0": they must not be taken for the same channel.
        for token in (BALCONY, INT, BALCONY):
            await client._process_message(_message(topic, {"VideoSourceToken": token, "Rule": "r"}, {"State": "true"}))
    assert [(e.channel_id, e.active, e.rule) for e in events] == [(BALCONY, True, "r"), (INT, True, "r")]
    assert f"source={{'VideoSourceToken': '{BALCONY}'" in caplog.text
    assert topic in caplog.text


async def test_nvr_message_without_a_token_has_no_channel() -> None:
    nvr, _, space = _setup()
    events: list[OnvifDetectionEvent] = []
    client = AjaxOnvifClient(nvr, "u", "p", event_callback=events.append)
    await client._process_message(_message("tns1:VideoSource/MotionAlarm", {}, {"State": "true"}))
    assert [e.channel_id for e in events] == [""]
    _coordinator(space)._handle_onvif_event(events[0])
    assert nvr.channel_detections == {}


async def test_camera_client_still_uses_the_channel_index() -> None:
    _, cam, _ = _setup()
    events: list[OnvifDetectionEvent] = []
    client = AjaxOnvifClient(cam, "u", "p", event_callback=events.append)
    await client._process_message(
        _message("tns1:VideoSource/MotionAlarm", {"VideoSourceToken": "abc-0"}, {"State": "true"})
    )
    assert [e.channel_id for e in events] == ["0"]


async def test_profiles_are_logged_and_a_failure_is_harmless(caplog: pytest.LogCaptureFixture) -> None:
    nvr, _, _ = _setup()
    client = AjaxOnvifClient(nvr, "u", "p")
    await client._log_profiles()  # not connected: nothing to do
    profile = SimpleNamespace(
        token="p1", Name="Balcony", VideoSourceConfiguration=SimpleNamespace(SourceToken=BALCONY, token="c1")
    )
    media = SimpleNamespace(GetProfiles=AsyncMock(return_value=[profile]))
    client._camera = SimpleNamespace(create_media_service=AsyncMock(return_value=media))  # type: ignore[assignment]
    with caplog.at_level(logging.DEBUG, logger="custom_components.ajax.onvif_client"):
        await client._log_profiles()
        assert f"token=p1 name=Balcony source={BALCONY} config=c1" in caplog.text
        media.GetProfiles.side_effect = RuntimeError("nope")
        await client._log_profiles()
    assert "could not list ONVIF profiles: nope" in caplog.text
