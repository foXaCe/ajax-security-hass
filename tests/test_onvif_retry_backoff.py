"""A camera that keeps refusing ONVIF must not be retried (and logged) every 30 s.

Seen on a live install (2026-10-23): a camera answering "Sender not Authorized"
to every PullPoint subscription was re-attempted on each 30 s video-edge
refresh, logging one ERROR per attempt (81 in 40 minutes). The manager now
backs off per camera, logs the failure once, and logs the recovery.
"""

from __future__ import annotations

import logging
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

for name in ("onvif", "onvif.client", "onvif.exceptions", "zeep", "zeep.exceptions"):
    if name not in sys.modules:
        mod = ModuleType(name)
        if name == "onvif":
            mod.__file__ = "/dev/null/onvif/__init__.py"
            mod.ONVIFCamera = MagicMock()  # type: ignore[attr-defined]
        if name == "onvif.exceptions":
            mod.ONVIFError = type("ONVIFError", (Exception,), {})  # type: ignore[attr-defined]
        if name == "zeep.exceptions":
            mod.Fault = type("Fault", (Exception,), {})  # type: ignore[attr-defined]
            mod.TransportError = type("TransportError", (Exception,), {})  # type: ignore[attr-defined]
        sys.modules[name] = mod

from custom_components.ajax import onvif_manager  # noqa: E402
from custom_components.ajax.models import VideoEdgeType  # noqa: E402
from custom_components.ajax.onvif_manager import (  # noqa: E402
    ONVIF_RETRY_MAX_SECONDS,
    ONVIF_RETRY_MIN_SECONDS,
    AjaxOnvifManager,
)

CAM = SimpleNamespace(id="cam1", name="Caméra Arrière", ip_address="192.168.1.70", video_edge_type=VideoEdgeType.TURRET)


class FakeClient:
    """Connects fine, subscription refused until ``accept`` is set."""

    accept = False
    subscribe_calls: list[bool] = []

    def __init__(self, video_edge, username, password, event_callback) -> None:
        self.video_edge = video_edge
        self.connected = False

    async def async_connect(self, log_errors: bool = True) -> bool:
        return True

    async def async_subscribe_events(self, log_errors: bool = True) -> bool:
        FakeClient.subscribe_calls.append(log_errors)
        self.connected = FakeClient.accept
        return FakeClient.accept

    async def async_start_polling(self) -> None:
        return None

    async def async_stop(self) -> None:
        return None


@pytest.fixture
def clock():
    now = [1000.0]
    FakeClient.accept = False
    FakeClient.subscribe_calls = []
    with (
        patch.object(onvif_manager, "AjaxOnvifClient", FakeClient),
        patch.object(onvif_manager.time, "monotonic", lambda: now[0]),
    ):
        yield now


def _manager() -> AjaxOnvifManager:
    return AjaxOnvifManager(username="u", password="p", event_callback=lambda _e: None)


async def test_refused_camera_is_not_retried_on_every_refresh(clock) -> None:
    mgr = _manager()
    await mgr.async_update_video_edges([CAM])
    assert len(FakeClient.subscribe_calls) == 1

    # The 30 s refresh that follows, inside the backoff window: no new attempt.
    clock[0] += 30
    await mgr.async_update_video_edges([CAM])
    assert len(FakeClient.subscribe_calls) == 1

    # Once the delay has elapsed, it is tried again.
    clock[0] = 1000.0 + ONVIF_RETRY_MIN_SECONDS + 1
    await mgr.async_update_video_edges([CAM])
    assert len(FakeClient.subscribe_calls) == 2


async def test_backoff_doubles_up_to_the_cap(clock) -> None:
    mgr = _manager()
    delays = []
    for _ in range(12):
        await mgr.async_update_video_edges([CAM])
        delays.append(mgr._retry_at["cam1"] - clock[0])
        clock[0] = mgr._retry_at["cam1"]
    assert delays[0] == ONVIF_RETRY_MIN_SECONDS
    assert delays[1] == 2 * ONVIF_RETRY_MIN_SECONDS
    assert max(delays) == ONVIF_RETRY_MAX_SECONDS


async def test_failure_logged_once_then_quiet(clock) -> None:
    mgr = _manager()
    for _ in range(3):
        await mgr.async_update_video_edges([CAM])
        clock[0] = mgr._retry_at["cam1"]
    # Only the first attempt is allowed to log at error level.
    assert FakeClient.subscribe_calls == [True, False, False]


async def test_recovery_resets_the_backoff_and_is_logged(clock, caplog) -> None:
    mgr = _manager()
    await mgr.async_update_video_edges([CAM])
    clock[0] = mgr._retry_at["cam1"]
    FakeClient.accept = True
    with caplog.at_level(logging.INFO, logger="custom_components.ajax.onvif_manager"):
        await mgr.async_update_video_edges([CAM])
    assert mgr.connected_count == 1
    assert "cam1" not in mgr._retry_at
    assert any("back" in r.getMessage().lower() for r in caplog.records)


async def test_start_records_the_failure_for_the_next_refresh(clock) -> None:
    """A camera refused at startup also waits for its backoff before the next try."""
    mgr = _manager()
    await mgr.async_start([CAM])
    clock[0] += 30
    await mgr.async_update_video_edges([CAM])
    assert len(FakeClient.subscribe_calls) == 1
