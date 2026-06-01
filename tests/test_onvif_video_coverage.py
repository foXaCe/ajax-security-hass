"""Coverage for the ONVIF local-AI detection path.

Covers four modules:
* ``onvif_client.py`` — connect, PullPoint subscription, message parsing
  (ObjectDetection / Motion / LineCrossing / Doorbell), duplicate filtering,
  poll loop and clean shutdown.
* ``onvif_manager.py`` — add/remove/update video edges, target/connected
  counts, start lifecycle (NVR skipped).
* ``_coordinator_onvif.py`` — multi-space orchestration, repair-issue
  bookkeeping and the NVR-channel-to-camera event routing.
* ``devices/video_edge.py`` — ``VideoEdgeHandler`` sensors/binary sensors,
  detection-by-id resolution and NVR channel helpers.

The optional ``onvif-zeep-async`` wheel isn't installed in CI, so the
package surface (``onvif``, ``onvif.exceptions``, ``zeep.exceptions``) is
stubbed before importing the client — mirroring ``test_onvif_target_count``.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
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

from custom_components.ajax import onvif_client as oc  # noqa: E402
from custom_components.ajax._coordinator_onvif import AjaxOnvifMixin  # noqa: E402
from custom_components.ajax.devices.video_edge import (  # noqa: E402
    VideoEdgeHandler,
    _parse_iso_duration_to_timestamp,
)
from custom_components.ajax.models import (  # noqa: E402
    AjaxAccount,
    AjaxSpace,
    AjaxVideoEdge,
    VideoEdgeType,
)
from custom_components.ajax.onvif_client import (  # noqa: E402
    AjaxOnvifClient,
    OnvifDetectionEvent,
)
from custom_components.ajax.onvif_manager import AjaxOnvifManager  # noqa: E402

ONVIFError = sys.modules["onvif.exceptions"].ONVIFError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _video_edge(
    ve_id: str = "ve1",
    name: str = "Front Cam",
    ve_type: VideoEdgeType = VideoEdgeType.TURRET,
    ip: str | None = "192.168.1.50",
    **kw: Any,
) -> AjaxVideoEdge:
    return AjaxVideoEdge(
        id=ve_id,
        name=name,
        space_id="s1",
        video_edge_type=ve_type,
        ip_address=ip,
        **kw,
    )


def _simple_item(name: str, value: Any) -> SimpleNamespace:
    return SimpleNamespace(Name=name, Value=value)


def _message(
    *,
    topic: str | None,
    data_items: dict[str, Any] | None = None,
    source_items: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    """Build an object that mimics an ONVIF NotificationMessage."""
    msg_message = None
    if data_items is not None or source_items is not None:
        data = None
        if data_items is not None:
            data = SimpleNamespace(SimpleItem=[_simple_item(k, v) for k, v in data_items.items()])
        source = None
        if source_items is not None:
            source = SimpleNamespace(SimpleItem=source_items)
        msg_message = SimpleNamespace(_value_1=SimpleNamespace(Data=data, Source=source))

    topic_obj = SimpleNamespace(_value_1=topic) if topic is not None else None
    return SimpleNamespace(Topic=topic_obj, Message=msg_message)


def _client(callback: Any = None, ve: AjaxVideoEdge | None = None) -> AjaxOnvifClient:
    return AjaxOnvifClient(
        video_edge=ve or _video_edge(),
        username="u",
        password="p",
        event_callback=callback,
    )


# ===========================================================================
# onvif_client.py — dataclass / connect
# ===========================================================================


def test_detection_event_str() -> None:
    evt = OnvifDetectionEvent(video_edge_id="ve1", channel_id="0", detection_type="VIDEO_HUMAN", active=True, rule="Z1")
    assert "VIDEO_HUMAN" in str(evt)
    assert "active=True" in str(evt)
    assert "Z1" in str(evt)
    assert isinstance(evt.timestamp, datetime)


def test_connected_property_false_initially() -> None:
    client = _client()
    assert client.connected is False


async def test_connect_no_ip_returns_false() -> None:
    client = _client(ve=_video_edge(ip=None))
    assert await client.async_connect() is False


async def test_connect_success(monkeypatch: pytest.MonkeyPatch) -> None:
    camera = MagicMock()
    camera.update_xaddrs = AsyncMock()
    monkeypatch.setattr(oc, "ONVIFCamera", MagicMock(return_value=camera))
    client = _client()
    assert await client.async_connect() is True
    assert client._camera is camera
    camera.update_xaddrs.assert_awaited_once()


async def test_connect_failure_resets_camera(monkeypatch: pytest.MonkeyPatch) -> None:
    camera = MagicMock()
    camera.update_xaddrs = AsyncMock(side_effect=ONVIFError("boom"))
    monkeypatch.setattr(oc, "ONVIFCamera", MagicMock(return_value=camera))
    client = _client()
    assert await client.async_connect() is False
    assert client._camera is None


# ===========================================================================
# onvif_client.py — subscribe / poll / stop
# ===========================================================================


async def test_subscribe_no_camera_returns_false() -> None:
    client = _client()
    assert await client.async_subscribe_events() is False


async def test_subscribe_success_and_replaces_old_manager() -> None:
    client = _client()
    client._camera = MagicMock()
    old_manager = MagicMock()
    old_manager.shutdown = AsyncMock()
    client._pullpoint_manager = old_manager

    new_manager = MagicMock()
    new_manager.set_synchronization_point = AsyncMock()
    client._camera.create_pullpoint_manager = AsyncMock(return_value=new_manager)

    assert await client.async_subscribe_events() is True
    old_manager.shutdown.assert_awaited_once()
    new_manager.set_synchronization_point.assert_awaited_once()
    assert client._pullpoint_manager is new_manager


async def test_subscribe_failure_returns_false() -> None:
    client = _client()
    client._camera = MagicMock()
    client._camera.create_pullpoint_manager = AsyncMock(side_effect=ONVIFError("x"))
    assert await client.async_subscribe_events() is False


def test_on_subscription_lost_sets_flag() -> None:
    client = _client()
    assert client._subscription_lost is False
    client._on_subscription_lost()
    assert client._subscription_lost is True


async def test_start_polling_creates_task_and_is_idempotent() -> None:
    client = _client()
    # Make the loop a no-op that respects _running so the task ends quickly.
    client._pull_messages = AsyncMock()  # type: ignore[method-assign]
    await client.async_start_polling()
    assert client._running is True
    first_task = client._poll_task
    # Second call returns early (already running).
    await client.async_start_polling()
    assert client._poll_task is first_task
    await client.async_stop()
    assert client._poll_task is None


async def test_stop_shuts_down_manager_and_camera() -> None:
    client = _client()
    manager = MagicMock()
    manager.closed = False
    manager.shutdown = AsyncMock()
    client._pullpoint_manager = manager
    camera = MagicMock()
    camera.close = AsyncMock()
    client._camera = camera

    await client.async_stop()
    manager.shutdown.assert_awaited_once()
    camera.close.assert_awaited_once()
    assert client._pullpoint_manager is None
    assert client._camera is None


async def test_stop_skips_shutdown_when_manager_closed() -> None:
    client = _client()
    manager = MagicMock()
    manager.closed = True
    manager.shutdown = AsyncMock()
    client._pullpoint_manager = manager
    await client.async_stop()
    manager.shutdown.assert_not_awaited()


# ===========================================================================
# onvif_client.py — _poll_loop
# ===========================================================================


async def test_poll_loop_recreates_subscription_on_lost_flag() -> None:
    client = _client()
    client._running = True
    client._subscription_lost = True
    client.async_subscribe_events = AsyncMock(return_value=True)  # type: ignore[method-assign]

    async def _pull() -> None:
        client._running = False  # stop after one successful pull

    client._pull_messages = _pull  # type: ignore[method-assign]
    await client._poll_loop()
    client.async_subscribe_events.assert_awaited()
    assert client._subscription_lost is False


async def test_poll_loop_backoff_when_resubscribe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    client._running = True
    client._subscription_lost = True
    client.async_subscribe_events = AsyncMock(return_value=False)  # type: ignore[method-assign]

    sleeps: list[float] = []

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)
        client._running = False  # bail after first backoff sleep

    monkeypatch.setattr(oc.asyncio, "sleep", _sleep)
    await client._poll_loop()
    assert sleeps  # at least one backoff sleep happened
    assert sleeps[0] > oc.PULLPOINT_POLL_INTERVAL


async def test_poll_loop_handles_exception_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    client._running = True

    async def _pull() -> None:
        raise RuntimeError("transient")

    client._pull_messages = _pull  # type: ignore[method-assign]

    sleeps: list[float] = []

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)
        client._running = False

    monkeypatch.setattr(oc.asyncio, "sleep", _sleep)
    await client._poll_loop()
    assert sleeps and sleeps[0] > oc.PULLPOINT_POLL_INTERVAL


async def test_poll_loop_breaks_on_cancelled() -> None:
    client = _client()
    client._running = True

    async def _pull() -> None:
        raise asyncio.CancelledError

    client._pull_messages = _pull  # type: ignore[method-assign]
    # Should return cleanly (break), not raise.
    await client._poll_loop()


# ===========================================================================
# onvif_client.py — _pull_messages
# ===========================================================================


async def test_pull_messages_no_manager_returns_early() -> None:
    client = _client()
    client._pullpoint_manager = None
    await client._pull_messages()  # no exception


async def test_pull_messages_processes_notifications() -> None:
    client = _client()
    manager = MagicMock()
    manager.closed = False
    service = MagicMock()
    msg = _message(topic="x")
    service.PullMessages = AsyncMock(return_value=SimpleNamespace(NotificationMessage=[msg]))
    manager.get_service = MagicMock(return_value=service)
    client._pullpoint_manager = manager

    processed: list[Any] = []
    client._process_message = AsyncMock(side_effect=lambda m: processed.append(m))  # type: ignore[method-assign]
    await client._pull_messages()
    assert processed == [msg]


async def test_pull_messages_swallows_timeout() -> None:
    client = _client()
    manager = MagicMock()
    manager.closed = False
    service = MagicMock()
    service.PullMessages = AsyncMock(side_effect=ONVIFError("Timeout occurred"))
    manager.get_service = MagicMock(return_value=service)
    client._pullpoint_manager = manager
    await client._pull_messages()  # must not raise


async def test_pull_messages_logs_non_timeout_error() -> None:
    client = _client()
    manager = MagicMock()
    manager.closed = False
    service = MagicMock()
    service.PullMessages = AsyncMock(side_effect=ONVIFError("connection reset"))
    manager.get_service = MagicMock(return_value=service)
    client._pullpoint_manager = manager
    await client._pull_messages()  # must not raise


# ===========================================================================
# onvif_client.py — _process_message / _extract_source_info
# ===========================================================================


async def test_process_message_no_topic_noop() -> None:
    cb = MagicMock()
    client = _client(cb)
    await client._process_message(_message(topic=None))
    cb.assert_not_called()


async def test_process_message_no_message_data_noop() -> None:
    cb = MagicMock()
    client = _client(cb)
    await client._process_message(_message(topic="some/topic"))
    cb.assert_not_called()


async def test_process_message_fires_callback_and_dedupes() -> None:
    cb = MagicMock()
    client = _client(cb)
    msg = _message(
        topic="tns1:RuleEngine/tnsajax:MotionDetector/Detection",
        data_items={"State": "true"},
    )
    await client._process_message(msg)
    assert cb.call_count == 1
    # Same state again → deduped (no second callback).
    await client._process_message(msg)
    assert cb.call_count == 1


async def test_process_message_extracts_channel_and_rule() -> None:
    cb = MagicMock()
    client = _client(cb)
    msg = _message(
        topic="tns1:RuleEngine/tnsajax:MotionDetector/Detection",
        data_items={"State": "true"},
        source_items=[
            _simple_item("VideoSourceToken", "VideoSource-3"),
            _simple_item("Rule", "Zone Entree"),
        ],
    )
    await client._process_message(msg)
    evt = cb.call_args[0][0]
    assert evt.channel_id == "3"
    assert evt.rule == "Zone Entree"


def test_extract_source_info_defaults_and_token_without_dash() -> None:
    client = _client()
    msg = SimpleNamespace(Source=SimpleNamespace(SimpleItem=[_simple_item("VideoSourceToken", "nodash")]))
    channel_id, rule = client._extract_source_info(msg)
    assert channel_id == "0"  # no dash → unchanged default
    assert rule == ""


def test_extract_source_info_handles_missing_source() -> None:
    client = _client()
    channel_id, rule = client._extract_source_info(SimpleNamespace())
    assert (channel_id, rule) == ("0", "")


def test_extract_source_info_swallows_exception() -> None:
    client = _client()

    # Source whose SimpleItem iteration raises → caught and defaults returned.
    class _Boom:
        Source = property(lambda self: (_ for _ in ()).throw(RuntimeError("x")))

    channel_id, rule = client._extract_source_info(_Boom())
    assert (channel_id, rule) == ("0", "")


async def test_process_message_swallows_exception() -> None:
    cb = MagicMock()
    client = _client(cb)

    # Topic access raises → caught by the outer try in _process_message.
    class _Boom:
        @property
        def Topic(self) -> Any:
            raise RuntimeError("topic boom")

    await client._process_message(_Boom())  # must not raise
    cb.assert_not_called()


def test_parse_event_swallows_exception() -> None:
    client = _client()

    # message_data.Data access raises → caught by the try in _parse_event.
    class _Boom:
        @property
        def Data(self) -> Any:
            raise RuntimeError("data boom")

    assert client._parse_event("tns1:RuleEngine/ObjectDetection/Object", _Boom(), "0") is None


# ===========================================================================
# onvif_client.py — _parse_event
# ===========================================================================


def test_parse_object_detection_human_active() -> None:
    cb = MagicMock()
    client = _client(cb)
    msg_data = SimpleNamespace(Data=SimpleNamespace(SimpleItem=[_simple_item("ClassTypes", "Human")]))
    evt = client._parse_event("tns1:RuleEngine/ObjectDetection/Object", msg_data, "0", "rule")
    assert evt is not None
    assert evt.detection_type == "VIDEO_HUMAN"
    assert evt.active is True


def test_parse_object_detection_multiclass_emits_cleared_for_others() -> None:
    cb = MagicMock()
    client = _client(cb)
    msg_data = SimpleNamespace(Data=SimpleNamespace(SimpleItem=[_simple_item("ClassTypes", "Animal,Vehicle")]))
    first = client._parse_event("tns1:RuleEngine/tnsajax:ObjectDetector/Detection", msg_data, "1", "")
    # First active event returned; the other types fired through callback.
    assert first is not None and first.active is True
    fired_types = {c.args[0].detection_type for c in cb.call_args_list}
    # VIDEO_HUMAN is not in the detected set → fired as cleared.
    assert "VIDEO_HUMAN" in fired_types


def test_parse_motion_detector_state_false() -> None:
    client = _client()
    msg_data = SimpleNamespace(Data=SimpleNamespace(SimpleItem=[_simple_item("State", "false")]))
    evt = client._parse_event("tns1:RuleEngine/tnsajax:MotionDetector/Detection", msg_data, "0")
    assert evt is not None
    assert evt.detection_type == "VIDEO_MOTION"
    assert evt.active is False


def test_parse_motion_detector_detected_fallback() -> None:
    client = _client()
    msg_data = SimpleNamespace(Data=SimpleNamespace(SimpleItem=[_simple_item("Detected", "true")]))
    evt = client._parse_event("tns1:RuleEngine/tnsajax:MotionDetector/Detection", msg_data, "0")
    assert evt is not None and evt.active is True


def test_parse_videosource_motionalarm() -> None:
    client = _client()
    msg_data = SimpleNamespace(Data=SimpleNamespace(SimpleItem=[_simple_item("State", "true")]))
    evt = client._parse_event("tns1:VideoSource/MotionAlarm", msg_data, "0")
    assert evt is not None
    assert evt.detection_type == "VIDEO_MOTION"
    assert evt.active is True


def test_parse_line_crossing_active() -> None:
    client = _client()
    msg_data = SimpleNamespace(Data=SimpleNamespace(SimpleItem=[_simple_item("ClassTypes", "Human")]))
    evt = client._parse_event("tns1:RuleEngine/tnsajax:LineDetector/Crossing", msg_data, "0")
    assert evt is not None
    assert evt.detection_type == "VIDEO_LINE_CROSSING"
    assert evt.active is True


def test_parse_line_crossing_empty_class_inactive() -> None:
    client = _client()
    msg_data = SimpleNamespace(Data=SimpleNamespace(SimpleItem=[]))
    evt = client._parse_event("tns1:RuleEngine/LineDetector/Crossed", msg_data, "0")
    assert evt is not None and evt.active is False


def test_parse_doorbell_ring() -> None:
    client = _client()
    msg_data = SimpleNamespace(Data=SimpleNamespace(SimpleItem=[_simple_item("Detected", "true")]))
    evt = client._parse_event("tns1:RuleEngine/RingDetector/Detection", msg_data, "0")
    assert evt is not None
    assert evt.detection_type == "DOORBELL_RING"
    assert evt.active is True


def test_parse_unknown_topic_returns_none() -> None:
    client = _client()
    msg_data = SimpleNamespace(Data=SimpleNamespace(SimpleItem=[]))
    assert client._parse_event("tns1:Unknown/Topic", msg_data, "0") is None


# ===========================================================================
# onvif_manager.py
# ===========================================================================


async def test_manager_add_video_edge_no_ip() -> None:
    mgr = AjaxOnvifManager("u", "p")
    assert await mgr.async_add_video_edge(_video_edge(ip=None)) is False


async def test_manager_add_video_edge_success(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.async_connect = AsyncMock(return_value=True)
    client.async_subscribe_events = AsyncMock(return_value=True)
    client.async_start_polling = AsyncMock()
    client.connected = True
    monkeypatch.setattr(
        "custom_components.ajax.onvif_manager.AjaxOnvifClient",
        MagicMock(return_value=client),
    )
    mgr = AjaxOnvifManager("u", "p")
    ve = _video_edge()
    assert await mgr.async_add_video_edge(ve) is True
    assert mgr._clients[ve.id] is client
    assert mgr.connected_count == 1


async def test_manager_add_already_connected_returns_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = AjaxOnvifManager("u", "p")
    ve = _video_edge()
    existing = MagicMock()
    existing.connected = True
    mgr._clients[ve.id] = existing
    # Should short-circuit without creating a new client.
    factory = MagicMock()
    monkeypatch.setattr("custom_components.ajax.onvif_manager.AjaxOnvifClient", factory)
    assert await mgr.async_add_video_edge(ve) is True
    factory.assert_not_called()


async def test_manager_add_replaces_disconnected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = AjaxOnvifManager("u", "p")
    ve = _video_edge()
    stale = MagicMock()
    stale.connected = False
    stale.async_stop = AsyncMock()
    mgr._clients[ve.id] = stale

    fresh = MagicMock()
    fresh.async_connect = AsyncMock(return_value=True)
    fresh.async_subscribe_events = AsyncMock(return_value=True)
    fresh.async_start_polling = AsyncMock()
    fresh.connected = True
    monkeypatch.setattr(
        "custom_components.ajax.onvif_manager.AjaxOnvifClient",
        MagicMock(return_value=fresh),
    )
    assert await mgr.async_add_video_edge(ve) is True
    stale.async_stop.assert_awaited_once()
    assert mgr._clients[ve.id] is fresh


async def test_manager_add_connect_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.async_connect = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "custom_components.ajax.onvif_manager.AjaxOnvifClient",
        MagicMock(return_value=client),
    )
    mgr = AjaxOnvifManager("u", "p")
    ve = _video_edge()
    assert await mgr.async_add_video_edge(ve) is False
    assert ve.id not in mgr._clients


async def test_manager_add_subscribe_failure_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    client.async_connect = AsyncMock(return_value=True)
    client.async_subscribe_events = AsyncMock(return_value=False)
    client.async_stop = AsyncMock()
    monkeypatch.setattr(
        "custom_components.ajax.onvif_manager.AjaxOnvifClient",
        MagicMock(return_value=client),
    )
    mgr = AjaxOnvifManager("u", "p")
    ve = _video_edge()
    assert await mgr.async_add_video_edge(ve) is False
    client.async_stop.assert_awaited_once()
    assert ve.id not in mgr._clients


async def test_manager_remove_video_edge() -> None:
    mgr = AjaxOnvifManager("u", "p")
    client = MagicMock()
    client.async_stop = AsyncMock()
    mgr._clients["ve1"] = client
    await mgr.async_remove_video_edge("ve1")
    client.async_stop.assert_awaited_once()
    assert "ve1" not in mgr._clients


async def test_manager_remove_unknown_noop() -> None:
    mgr = AjaxOnvifManager("u", "p")
    await mgr.async_remove_video_edge("nope")  # no exception


async def test_manager_start_no_credentials() -> None:
    mgr = AjaxOnvifManager("", "", event_callback=None)
    await mgr.async_start([_video_edge()])
    assert mgr.target_count == 0


async def test_manager_start_no_targets_only_nvr() -> None:
    mgr = AjaxOnvifManager("u", "p")
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    await mgr.async_start([nvr])
    assert mgr.target_count == 0  # NVR excluded → no targets


async def test_manager_start_connects_cameras_skips_nvr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = AjaxOnvifManager("u", "p")
    added: list[str] = []

    async def _add(ve: AjaxVideoEdge) -> bool:
        added.append(ve.id)
        return True

    monkeypatch.setattr(mgr, "async_add_video_edge", _add)
    cam = _video_edge("cam1", "Cam", VideoEdgeType.TURRET)
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    await mgr.async_start([cam, nvr])
    assert added == ["cam1"]
    assert mgr.target_count == 1


async def test_manager_start_no_nvr_logs_camera_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = AjaxOnvifManager("u", "p")

    async def _add(ve: AjaxVideoEdge) -> bool:
        return True

    monkeypatch.setattr(mgr, "async_add_video_edge", _add)
    cam = _video_edge("cam1", "Cam", VideoEdgeType.TURRET)
    # No NVR present → exercises the "No NVR" logging branch.
    await mgr.async_start([cam])
    assert mgr.target_count == 1


async def test_manager_stop_stops_all_clients() -> None:
    mgr = AjaxOnvifManager("u", "p")
    c1 = MagicMock()
    c1.async_stop = AsyncMock()
    c2 = MagicMock()
    c2.async_stop = AsyncMock(side_effect=RuntimeError("fail"))
    mgr._clients = {"a": c1, "b": c2}
    await mgr.async_stop()  # gathers with return_exceptions, logs failure
    c1.async_stop.assert_awaited_once()
    assert mgr._clients == {}


async def test_manager_update_adds_and_removes(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = AjaxOnvifManager("u", "p")
    old = MagicMock()
    old.async_stop = AsyncMock()
    mgr._clients = {"gone": old}

    added: list[str] = []

    async def _add(ve: AjaxVideoEdge) -> bool:
        added.append(ve.id)
        return True

    monkeypatch.setattr(mgr, "async_add_video_edge", _add)

    new_cam = _video_edge("cam_new", "New", VideoEdgeType.BULLET)
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    await mgr.async_update_video_edges([new_cam, nvr])

    old.async_stop.assert_awaited_once()  # removed device no longer targeted
    assert "gone" not in mgr._clients
    assert added == ["cam_new"]
    assert mgr.target_count == 1  # NVR excluded


# ===========================================================================
# _coordinator_onvif.py — _find_camera_for_nvr_channel
# ===========================================================================


def _mixin() -> AjaxOnvifMixin:
    """Bare mixin instance with the host attributes a method reads."""
    m = object.__new__(AjaxOnvifMixin)
    return m


def test_find_camera_channels_not_list_returns_nvr() -> None:
    m = _mixin()
    space = SimpleNamespace(video_edges={})
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    nvr.channels = "not a list"  # type: ignore[assignment]
    assert m._find_camera_for_nvr_channel(space, nvr, "0") is nvr


def test_find_camera_by_index() -> None:
    m = _mixin()
    cam = _video_edge("cam1", "Cam", VideoEdgeType.TURRET)
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    nvr.channels = [
        {
            "id": "0",
            "sourceAliases": {"sources": [{"sourceType": "PRIMARY", "type": "TURRET", "videoEdgeId": "cam1"}]},
        }
    ]
    space = SimpleNamespace(video_edges={"cam1": cam, "nvr1": nvr})
    assert m._find_camera_for_nvr_channel(space, nvr, "0") is cam


def test_find_camera_fallback_scan_by_id() -> None:
    m = _mixin()
    cam = _video_edge("cam1", "Cam", VideoEdgeType.BULLET)
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    # channel_id "7" is out of index range → falls to scan-by-id pass.
    nvr.channels = [
        {"id": "7", "sourceAliases": {"sources": [{"sourceType": "PRIMARY", "type": "BULLET", "videoEdgeId": "cam1"}]}},
    ]
    space = SimpleNamespace(video_edges={"cam1": cam, "nvr1": nvr})
    assert m._find_camera_for_nvr_channel(space, nvr, "7") is cam


def test_find_camera_no_link_returns_nvr() -> None:
    m = _mixin()
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    nvr.channels = [{"id": "0", "sourceAliases": {"sources": []}}]
    space = SimpleNamespace(video_edges={"nvr1": nvr})
    assert m._find_camera_for_nvr_channel(space, nvr, "0") is nvr


def test_find_camera_index_hits_non_dict_channel() -> None:
    m = _mixin()
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    # channel_id "0" indexes a non-dict entry → get_linked returns None at the
    # isinstance guard, then the scan pass also finds nothing → NVR.
    nvr.channels = ["not a dict"]
    space = SimpleNamespace(video_edges={"nvr1": nvr})
    assert m._find_camera_for_nvr_channel(space, nvr, "0") is nvr


def test_find_camera_malformed_channel_entries() -> None:
    m = _mixin()
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    # Non-dict channel, dict with bad sourceAliases / sources / source entries.
    nvr.channels = [
        "not a dict",
        {"id": "1", "sourceAliases": "bad"},
        {"id": "2", "sourceAliases": {"sources": "bad"}},
        {"id": "3", "sourceAliases": {"sources": ["not a dict"]}},
    ]
    space = SimpleNamespace(video_edges={"nvr1": nvr})
    # None resolve → returns the NVR itself for every probe.
    assert m._find_camera_for_nvr_channel(space, nvr, "1") is nvr
    assert m._find_camera_for_nvr_channel(space, nvr, "2") is nvr
    assert m._find_camera_for_nvr_channel(space, nvr, "3") is nvr
    # channel_id "9" out of range → scan pass walks the non-dict entry too.
    assert m._find_camera_for_nvr_channel(space, nvr, "9") is nvr


def test_find_camera_invalid_channel_id_string() -> None:
    m = _mixin()
    cam = _video_edge("cam1", "Cam", VideoEdgeType.TURRET)
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    nvr.channels = [
        {
            "id": "abc",
            "sourceAliases": {"sources": [{"sourceType": "PRIMARY", "type": "TURRET", "videoEdgeId": "cam1"}]},
        },
    ]
    space = SimpleNamespace(video_edges={"cam1": cam, "nvr1": nvr})
    # channel_id "abc" → int() raises, fallback scan matches id "abc".
    assert m._find_camera_for_nvr_channel(space, nvr, "abc") is cam


# ===========================================================================
# _coordinator_onvif.py — _handle_onvif_event
# ===========================================================================


def _handler_mixin(account: AjaxAccount | None) -> AjaxOnvifMixin:
    m = object.__new__(AjaxOnvifMixin)
    m.account = account  # type: ignore[attr-defined]
    m.stats = {"events_onvif_received": 0}  # type: ignore[attr-defined]
    m._event_entities = {}  # type: ignore[attr-defined]
    hass = MagicMock()
    hass.bus.async_fire = MagicMock()
    m.hass = hass  # type: ignore[attr-defined]
    m.async_set_updated_data = MagicMock()  # type: ignore[attr-defined]
    return m


def _account_with(space: AjaxSpace) -> AjaxAccount:
    acc = AjaxAccount(user_id="u", name="n", email="e")
    acc.spaces[space.id] = space
    return acc


def test_handle_event_no_account_returns() -> None:
    m = _handler_mixin(None)
    m._handle_onvif_event(OnvifDetectionEvent("ve1", "0", "VIDEO_HUMAN", True))
    assert m.stats["events_onvif_received"] == 1


def test_handle_event_updates_camera_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.ajax._coordinator_onvif.resolve_camera_entity_id",
        lambda *_a: "camera.front",
    )
    cam = _video_edge("cam1", "Cam", VideoEdgeType.TURRET)
    space = AjaxSpace(id="s1", name="Home")
    space.video_edges["cam1"] = cam
    m = _handler_mixin(_account_with(space))

    event_entity = MagicMock()
    event_entity.entity_id = "event.cam1_detection"
    m._event_entities["cam1_detection"] = event_entity

    m._handle_onvif_event(OnvifDetectionEvent("cam1", "0", "VIDEO_HUMAN", True, rule="Zone"))
    assert cam.detections["video_human"] is True
    event_entity.fire.assert_called_once_with("human", {"rule": "Zone"})
    m.hass.bus.async_fire.assert_called()
    m.async_set_updated_data.assert_called_once()


def test_handle_event_motion_clear_resets_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "custom_components.ajax._coordinator_onvif.resolve_camera_entity_id",
        lambda *_a: None,
    )
    cam = _video_edge("cam1", "Cam", VideoEdgeType.TURRET)
    cam.detections.update({"video_human": True, "video_vehicle": True, "video_pet": True})
    space = AjaxSpace(id="s1", name="Home")
    space.video_edges["cam1"] = cam
    m = _handler_mixin(_account_with(space))

    m._handle_onvif_event(OnvifDetectionEvent("cam1", "0", "VIDEO_MOTION", False))
    assert cam.detections["video_motion"] is False
    assert cam.detections["video_human"] is False
    assert cam.detections["video_vehicle"] is False
    assert cam.detections["video_pet"] is False


def test_handle_event_doorbell_ring_routes_to_doorbell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.ajax._coordinator_onvif.resolve_camera_entity_id",
        lambda *_a: "camera.bell",
    )
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    doorbell = _video_edge("bell1", "Doorbell", VideoEdgeType.DOORBELL)
    space = AjaxSpace(id="s1", name="Home")
    space.video_edges["nvr1"] = nvr
    space.video_edges["bell1"] = doorbell
    m = _handler_mixin(_account_with(space))

    bell_entity = MagicMock()
    bell_entity.entity_id = "event.bell1_press"
    m._event_entities["bell1_doorbell_press"] = bell_entity

    m._handle_onvif_event(OnvifDetectionEvent("nvr1", "0", "DOORBELL_RING", True))
    # The NVR doorbell event routes to the doorbell device.
    assert doorbell.detections["doorbell_ring"] is True
    bell_entity.fire.assert_called_once_with("ring")
    fired_topics = [c.args[0] for c in m.hass.bus.async_fire.call_args_list]
    assert "ajax_doorbell_ring" in fired_topics


def test_handle_event_nvr_routes_to_linked_camera(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.ajax._coordinator_onvif.resolve_camera_entity_id",
        lambda *_a: None,
    )
    cam = _video_edge("cam1", "Cam", VideoEdgeType.TURRET)
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    nvr.channels = [
        {"id": "0", "sourceAliases": {"sources": [{"sourceType": "PRIMARY", "type": "TURRET", "videoEdgeId": "cam1"}]}},
    ]
    space = AjaxSpace(id="s1", name="Home")
    space.video_edges["nvr1"] = nvr
    space.video_edges["cam1"] = cam
    m = _handler_mixin(_account_with(space))

    m._handle_onvif_event(OnvifDetectionEvent("nvr1", "0", "VIDEO_VEHICLE", True))
    # Detection is recorded on the linked camera, not the NVR.
    assert cam.detections["video_vehicle"] is True


def test_handle_event_unknown_source_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    space = AjaxSpace(id="s1", name="Home")
    m = _handler_mixin(_account_with(space))
    m._handle_onvif_event(OnvifDetectionEvent("ghost", "0", "VIDEO_HUMAN", True))
    # No video edge matched → no entity refresh.
    m.async_set_updated_data.assert_not_called()


# ===========================================================================
# _coordinator_onvif.py — _async_init_onvif
# ===========================================================================


def _init_mixin(
    *,
    account: AjaxAccount | None,
    options: dict[str, str] | None = None,
) -> AjaxOnvifMixin:
    m = object.__new__(AjaxOnvifMixin)
    m.account = account  # type: ignore[attr-defined]
    m._onvif_initialized = False  # type: ignore[attr-defined]
    m.onvif_manager = None  # type: ignore[attr-defined]
    m.hass = MagicMock()  # type: ignore[attr-defined]
    entry = MagicMock()
    entry.entry_id = "entry1"
    entry.options = options if options is not None else {}
    m.config_entry = entry  # type: ignore[attr-defined]
    return m


async def test_init_onvif_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.ajax._coordinator_onvif.ONVIF_AVAILABLE", False)
    m = _init_mixin(account=None)
    await m._async_init_onvif()
    assert m._onvif_initialized is True


async def test_init_onvif_no_config_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.ajax._coordinator_onvif.ONVIF_AVAILABLE", True)
    m = _init_mixin(account=None)
    m.config_entry = None  # type: ignore[attr-defined]
    await m._async_init_onvif()
    assert m._onvif_initialized is True


async def test_init_onvif_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.ajax._coordinator_onvif.ONVIF_AVAILABLE", True)
    m = _init_mixin(account=None, options={})
    await m._async_init_onvif()
    assert m._onvif_initialized is True


async def test_init_onvif_no_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.ajax._coordinator_onvif.ONVIF_AVAILABLE", True)
    from custom_components.ajax.const import CONF_RTSP_PASSWORD, CONF_RTSP_USERNAME

    m = _init_mixin(
        account=None,
        options={CONF_RTSP_USERNAME: "u", CONF_RTSP_PASSWORD: "p"},
    )
    await m._async_init_onvif()
    assert m._onvif_initialized is True


async def test_init_onvif_no_video_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.ajax._coordinator_onvif.ONVIF_AVAILABLE", True)
    from custom_components.ajax.const import CONF_RTSP_PASSWORD, CONF_RTSP_USERNAME

    space = AjaxSpace(id="s1", name="Home")
    m = _init_mixin(
        account=_account_with(space),
        options={CONF_RTSP_USERNAME: "u", CONF_RTSP_PASSWORD: "p"},
    )
    await m._async_init_onvif()
    assert m._onvif_initialized is True
    assert m.onvif_manager is None


def _setup_init_manager(
    monkeypatch: pytest.MonkeyPatch, connected: int, target: int
) -> tuple[AjaxOnvifMixin, MagicMock]:
    monkeypatch.setattr("custom_components.ajax._coordinator_onvif.ONVIF_AVAILABLE", True)
    from custom_components.ajax.const import CONF_RTSP_PASSWORD, CONF_RTSP_USERNAME

    cam = _video_edge("cam1", "Cam", VideoEdgeType.TURRET)
    space = AjaxSpace(id="s1", name="Home")
    space.video_edges["cam1"] = cam
    m = _init_mixin(
        account=_account_with(space),
        options={CONF_RTSP_USERNAME: "u", CONF_RTSP_PASSWORD: "p"},
    )

    manager = MagicMock()
    manager.async_start = AsyncMock()
    manager.async_stop = AsyncMock()
    manager.connected_count = connected
    manager.target_count = target
    monkeypatch.setattr(
        "custom_components.ajax._coordinator_onvif._AjaxOnvifManager",
        MagicMock(return_value=manager),
    )
    return m, manager


async def test_init_onvif_all_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    ir = MagicMock()
    monkeypatch.setattr("custom_components.ajax._coordinator_onvif.ir", ir)
    m, manager = _setup_init_manager(monkeypatch, connected=2, target=2)
    await m._async_init_onvif()
    manager.async_start.assert_awaited_once()
    # init issue + no_cam + partial all deleted (clean state).
    assert ir.async_delete_issue.call_count >= 3
    ir.async_create_issue.assert_not_called()


async def test_init_onvif_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    ir = MagicMock()
    ir.IssueSeverity = SimpleNamespace(WARNING="warning")
    monkeypatch.setattr("custom_components.ajax._coordinator_onvif.ir", ir)
    m, _manager = _setup_init_manager(monkeypatch, connected=1, target=2)
    await m._async_init_onvif()
    create_keys = [c.args[2] for c in ir.async_create_issue.call_args_list]
    assert any("partial" in k for k in create_keys)


async def test_init_onvif_none_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    ir = MagicMock()
    ir.IssueSeverity = SimpleNamespace(WARNING="warning")
    monkeypatch.setattr("custom_components.ajax._coordinator_onvif.ir", ir)
    m, _manager = _setup_init_manager(monkeypatch, connected=0, target=2)
    await m._async_init_onvif()
    create_keys = [c.args[2] for c in ir.async_create_issue.call_args_list]
    assert any("no_cameras" in k for k in create_keys)


async def test_init_onvif_nvr_only_no_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    ir = MagicMock()
    monkeypatch.setattr("custom_components.ajax._coordinator_onvif.ir", ir)
    m, _manager = _setup_init_manager(monkeypatch, connected=0, target=0)
    await m._async_init_onvif()
    ir.async_create_issue.assert_not_called()


async def test_init_onvif_exception_creates_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ir = MagicMock()
    ir.IssueSeverity = SimpleNamespace(WARNING="warning")
    monkeypatch.setattr("custom_components.ajax._coordinator_onvif.ir", ir)
    m, manager = _setup_init_manager(monkeypatch, connected=1, target=1)
    manager.async_start = AsyncMock(side_effect=RuntimeError("kaput"))
    await m._async_init_onvif()
    assert m.onvif_manager is None
    manager.async_stop.assert_awaited_once()
    create_keys = [c.args[2] for c in ir.async_create_issue.call_args_list]
    assert any("init_failed" in k for k in create_keys)


# ===========================================================================
# devices/video_edge.py — duration parsing
# ===========================================================================


def test_parse_duration_none() -> None:
    assert _parse_iso_duration_to_timestamp(None) is None
    assert _parse_iso_duration_to_timestamp("") is None


def test_parse_duration_invalid() -> None:
    assert _parse_iso_duration_to_timestamp("garbage") is None
    assert _parse_iso_duration_to_timestamp("P") is None
    assert _parse_iso_duration_to_timestamp("PT") is None


def test_parse_duration_valid() -> None:
    ts = _parse_iso_duration_to_timestamp("P1DT2H30M15.5S")
    assert isinstance(ts, datetime)
    assert ts.second == 0 and ts.microsecond == 0
    # Should be in the past relative to now.
    assert ts < datetime.now(UTC)


# ===========================================================================
# devices/video_edge.py — VideoEdgeHandler
# ===========================================================================


def test_handler_binary_sensors_single_channel() -> None:
    ve = _video_edge(ve_type=VideoEdgeType.TURRET)
    ve.channels = [{"id": "0"}]
    handler = VideoEdgeHandler(ve)
    sensors = handler.get_binary_sensors()
    keys = {s["key"] for s in sensors}
    # Single channel → no suffix.
    assert {"motion", "human", "vehicle", "pet", "line_crossing"} <= keys


def test_handler_binary_sensors_multi_channel_suffix() -> None:
    ve = _video_edge(ve_type=VideoEdgeType.NVR)
    ve.channels = [{"id": "0"}, {"id": "1"}]
    handler = VideoEdgeHandler(ve)
    sensors = handler.get_binary_sensors()
    keys = {s["key"] for s in sensors}
    assert "motion_0" in keys and "motion_1" in keys


def test_handler_binary_sensors_channels_not_list() -> None:
    ve = _video_edge()
    ve.channels = "nope"  # type: ignore[assignment]
    handler = VideoEdgeHandler(ve)
    assert handler.get_binary_sensors() == []


def test_handler_binary_tamper_and_onvif_sensors() -> None:
    ve = _video_edge()
    ve.channels = [{"id": "0"}]
    ve.raw_data = {
        "systemInfo": {"lidClosed": True},
        "onvif": {"userAuthEnabled": True},
    }
    handler = VideoEdgeHandler(ve)
    sensors = handler.get_binary_sensors()
    keys = {s["key"] for s in sensors}
    assert "tamper" in keys
    assert "onvif_enabled" in keys
    tamper = next(s for s in sensors if s["key"] == "tamper")
    # lidClosed=True → tamper off (not tampered).
    assert tamper["value_fn"]() is False
    onvif = next(s for s in sensors if s["key"] == "onvif_enabled")
    assert onvif["value_fn"]() is True


def test_handler_binary_camera_recorded_by_nvr_skips_ai() -> None:
    cam = _video_edge("cam1", "Cam", VideoEdgeType.TURRET)
    cam.channels = [{"id": "0"}]
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    nvr.channels = [
        {"id": "0", "sourceAliases": {"sources": [{"sourceType": "PRIMARY", "type": "TURRET", "videoEdgeId": "cam1"}]}},
    ]
    handler = VideoEdgeHandler(cam, {"cam1": cam, "nvr1": nvr})
    sensors = handler.get_binary_sensors()
    keys = {s["key"] for s in sensors}
    # AI sensors skipped because the camera is recorded by the NVR.
    assert "motion" not in keys


def test_handler_binary_nvr_channel_targets_linked_camera() -> None:
    cam = _video_edge("cam1", "Cam", VideoEdgeType.TURRET)
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    nvr.channels = [
        {
            "id": "0",
            "name": "Cam",
            "sourceAliases": {"sources": [{"sourceType": "PRIMARY", "type": "TURRET", "videoEdgeId": "cam1"}]},
        },
    ]
    handler = VideoEdgeHandler(nvr, {"cam1": cam, "nvr1": nvr})
    sensors = handler.get_binary_sensors()
    motion = next(s for s in sensors if s["key"] == "motion")
    assert motion["target_video_edge_id"] == "cam1"


def test_handler_get_sensors_full() -> None:
    ve = _video_edge(ve_type=VideoEdgeType.TURRET)
    ve.mac_address = "AA:BB"
    ve.firmware_version = "1.0"
    ve.connection_state = "ONLINE"
    ve.channels = [{"id": "0", "recordMode": "PERMANENT", "recordPolicy": "ALWAYS"}]
    ve.raw_data = {
        "systemInfo": {
            "uptime": "PT5H",
            "averageCpuConsumption": 12,
            "ramConsumption": 40,
        },
        "storageDevices": [
            {
                "sizeTotal": 2 * 1024**3,
                "temperature": 35,
                "status": {"state": "READY"},
            }
        ],
        "networkInterface": {"wifi": {"signalStrength": 80}},
    }
    handler = VideoEdgeHandler(ve)
    sensors = handler.get_sensors()
    keys = {s["key"] for s in sensors}
    assert {
        "ip_address",
        "mac_address",
        "firmware",
        "uptime",
        "cpu_usage",
        "ram_usage",
        "storage_total",
        "storage_temperature",
        "storage_status",
        "connection_state",
        "wifi_signal",
        "record_mode",
        "record_policy",
    } <= keys
    # Exercise the value functions.
    storage_total = next(s for s in sensors if s["key"] == "storage_total")
    assert storage_total["value_fn"]() == 2.0
    status = next(s for s in sensors if s["key"] == "storage_status")
    assert status["value_fn"]() == "ready"
    conn = next(s for s in sensors if s["key"] == "connection_state")
    assert conn["value_fn"]() == "online"
    rec_mode = next(s for s in sensors if s["key"] == "record_mode")
    assert rec_mode["value_fn"]() == "permanent"
    rec_policy = next(s for s in sensors if s["key"] == "record_policy")
    assert rec_policy["value_fn"]() == "always"


def test_handler_get_sensors_nvr_specific() -> None:
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    cam = _video_edge("cam1", "Cam", VideoEdgeType.TURRET)
    nvr.channels = [
        {
            "id": "0",
            "name": "Cam",
            "sourceAliases": {"sources": [{"sourceType": "PRIMARY", "type": "TURRET", "videoEdgeId": "cam1"}]},
        },
    ]
    nvr.raw_data = {
        "storageDevices": [{"archiveDepth": 7}],
        "ledBrightnessLevel": 50,
        "maxPreviewDuration": 120,
    }
    handler = VideoEdgeHandler(nvr, {"cam1": cam, "nvr1": nvr})
    sensors = handler.get_sensors()
    keys = {s["key"] for s in sensors}
    assert {"cameras", "archive_depth", "led_brightness", "max_preview_duration"} <= keys
    cameras = next(s for s in sensors if s["key"] == "cameras")
    assert cameras["value_fn"]() == 1
    attrs = cameras["extra_state_attributes_fn"]()
    assert attrs["cameras"][0]["type"] == "TURRET"
    mpd = next(s for s in sensors if s["key"] == "max_preview_duration")
    assert mpd["value_fn"]() == 2  # 120s → 2 minutes


def test_handler_get_first_storage_empty() -> None:
    ve = _video_edge()
    ve.raw_data = {}
    handler = VideoEdgeHandler(ve)
    assert handler._get_first_storage() == {}


def test_handler_storage_status_unmapped_and_none() -> None:
    ve = _video_edge()
    ve.raw_data = {"storageDevices": [{"status": {"state": "WEIRD"}}]}
    handler = VideoEdgeHandler(ve)
    assert handler._get_storage_status() == "unknown"
    ve.raw_data = {}
    assert handler._get_storage_status() == "none"


def test_handler_channel_record_helpers_unmapped() -> None:
    ve = _video_edge()
    ve.channels = [{"id": "0", "recordMode": "WEIRD", "recordPolicy": "WEIRD"}]
    handler = VideoEdgeHandler(ve)
    assert handler._get_channel_record_mode("0") == "unknown"
    assert handler._get_channel_record_policy("0") == "unknown"
    # Missing channel → None.
    assert handler._get_channel_record_mode("99") is None
    assert handler._get_channel_record_policy("99") is None


def test_handler_get_channel_by_id_positional_fallback() -> None:
    ve = _video_edge()
    ve.channels = [{"foo": "bar"}]  # no explicit id
    handler = VideoEdgeHandler(ve)
    # Positional fallback: index 0 matches channel_id "0".
    assert handler._get_channel_by_id("0") == {"foo": "bar"}
    assert handler._get_channel_by_id("5") is None


def test_handler_get_channel_by_id_not_a_list() -> None:
    ve = _video_edge()
    ve.channels = "x"  # type: ignore[assignment]
    handler = VideoEdgeHandler(ve)
    assert handler._get_channel_by_id("0") is None


def test_handler_has_detection_by_id_onvif_state() -> None:
    ve = _video_edge()
    ve.channels = [{"id": "0"}]
    ve.detections = {"video_human": True}
    handler = VideoEdgeHandler(ve)
    assert handler._has_detection_by_id("0", "VIDEO_HUMAN") is True


def test_handler_has_detection_by_id_rest_state() -> None:
    ve = _video_edge()
    ve.channels = [{"id": "0", "state": [{"type": "VIDEO_MOTION", "active": True}]}]
    handler = VideoEdgeHandler(ve)
    assert handler._has_detection_by_id("0", "VIDEO_MOTION") is True
    assert handler._has_detection_by_id("0", "VIDEO_HUMAN") is False


def test_handler_has_detection_by_id_missing_channel() -> None:
    ve = _video_edge()
    ve.channels = [{"id": "0"}]
    handler = VideoEdgeHandler(ve)
    assert handler._has_detection_by_id("99", "VIDEO_MOTION") is False


def test_handler_has_detection_by_id_nvr_reads_linked_camera() -> None:
    cam = _video_edge("cam1", "Cam", VideoEdgeType.TURRET)
    cam.detections = {"video_human": True}
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    nvr.channels = [
        {"id": "0", "sourceAliases": {"sources": [{"sourceType": "PRIMARY", "type": "TURRET", "videoEdgeId": "cam1"}]}},
    ]
    handler = VideoEdgeHandler(nvr, {"cam1": cam, "nvr1": nvr})
    assert handler._has_detection_by_id("0", "VIDEO_HUMAN") is True


def test_handler_has_detection_states_not_list() -> None:
    ve = _video_edge()
    handler = VideoEdgeHandler(ve)
    assert handler._has_detection({"state": "notalist"}, "VIDEO_MOTION") is False
    assert handler._has_detection("notadict", "VIDEO_MOTION") is False  # type: ignore[arg-type]


def test_handler_linked_camera_info_non_nvr_returns_none() -> None:
    cam = _video_edge("cam1", "Cam", VideoEdgeType.TURRET)
    handler = VideoEdgeHandler(cam)
    assert handler._get_linked_camera_info({"id": "0"}) is None


def test_handler_linked_camera_info_resolves_name() -> None:
    cam = _video_edge("cam1", "MyCam", VideoEdgeType.TURRET)
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    handler = VideoEdgeHandler(nvr, {"cam1": cam, "nvr1": nvr})
    channel = {"sourceAliases": {"sources": [{"sourceType": "PRIMARY", "type": "TURRET", "videoEdgeId": "cam1"}]}}
    info = handler._get_linked_camera_info(channel)
    assert info == {"id": "cam1", "name": "MyCam"}


def test_handler_linked_camera_info_malformed() -> None:
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    handler = VideoEdgeHandler(nvr, {"nvr1": nvr})
    # Non-dict channel.
    assert handler._get_linked_camera_info("nope") is None  # type: ignore[arg-type]
    # Bad sourceAliases / sources types.
    assert handler._get_linked_camera_info({"sourceAliases": "bad"}) is None
    assert handler._get_linked_camera_info({"sourceAliases": {"sources": "bad"}}) is None
    # Non-dict source entry → skipped, no match.
    assert handler._get_linked_camera_info({"sourceAliases": {"sources": ["x"]}}) is None


def test_handler_nvr_cameras_count_channels_not_list() -> None:
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    nvr.channels = "x"  # type: ignore[assignment]
    handler = VideoEdgeHandler(nvr)
    assert handler._get_nvr_cameras_count() == 0
    assert handler._get_nvr_cameras_attributes() == {"cameras": []}


def test_handler_get_linked_nvrs_malformed_channels() -> None:
    cam = _video_edge("cam1", "Cam", VideoEdgeType.TURRET)
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    nvr.channels = [
        "not a dict",
        {"sourceAliases": "bad"},
        {"sourceAliases": {"sources": "bad"}},
        {"sourceAliases": {"sources": ["not a dict"]}},
    ]
    handler = VideoEdgeHandler(cam, {"cam1": cam, "nvr1": nvr})
    # No PRIMARY source referencing cam1 → no linked NVRs.
    assert handler._get_linked_nvrs() == []


def test_handler_nvr_cameras_count_and_attrs_non_nvr() -> None:
    cam = _video_edge("cam1", "Cam", VideoEdgeType.TURRET)
    handler = VideoEdgeHandler(cam)
    assert handler._get_nvr_cameras_count() == 0
    assert handler._get_nvr_cameras_attributes() == {}


def test_handler_get_linked_nvrs() -> None:
    cam = _video_edge("cam1", "Cam", VideoEdgeType.TURRET)
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    nvr.channels = [
        {"id": "0", "sourceAliases": {"sources": [{"sourceType": "PRIMARY", "videoEdgeId": "cam1"}]}},
    ]
    handler = VideoEdgeHandler(cam, {"cam1": cam, "nvr1": nvr})
    linked = handler._get_linked_nvrs()
    assert linked == [{"id": "nvr1", "name": "NVR"}]


def test_handler_get_linked_nvrs_self_is_nvr() -> None:
    nvr = _video_edge("nvr1", "NVR", VideoEdgeType.NVR)
    handler = VideoEdgeHandler(nvr)
    assert handler._get_linked_nvrs() == []
