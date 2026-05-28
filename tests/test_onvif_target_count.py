"""Pin the ONVIF target_count vs connected_count contract.

NVRs are intentionally skipped from ONVIF event subscription (their
channel→camera mapping is unreliable). Reporting the repair issue with
``len(video_edges)`` as the denominator counted the NVR and showed
``connected=2/total=3`` to users with 2 cameras + 1 NVR — making them
think a camera was broken when nothing was wrong. ``target_count``
returns the post-NVR-filter count so the issue says ``2/2`` (all good)
and gets auto-deleted.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

# `onvif-zeep-async` is an optional runtime dependency; the test env
# doesn't ship it. Stub the package surface before importing the manager.
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

from custom_components.ajax.onvif_manager import AjaxOnvifManager  # noqa: E402


def _make_manager(client_states: dict[str, bool]) -> AjaxOnvifManager:
    """Build a manager populated with fake clients (each with a ``connected`` bool)."""
    mgr = AjaxOnvifManager(username="u", password="p", event_callback=lambda _e: None)
    for client_id, connected in client_states.items():
        mgr._clients[client_id] = SimpleNamespace(connected=connected)  # type: ignore[assignment]
    return mgr


def test_target_count_returns_zero_when_no_clients() -> None:
    assert _make_manager({}).target_count == 0


def test_target_count_excludes_nvrs_by_design() -> None:
    """`async_start` never registers NVR clients — so target_count = camera count.

    A user with 2 cameras + 1 NVR has 2 in `_clients` (the NVR is skipped),
    so target_count must be 2 — never 3.
    """
    mgr = _make_manager({"cam1": True, "cam2": True})
    assert mgr.target_count == 2
    assert mgr.connected_count == 2


def test_connected_count_under_target_means_partial() -> None:
    mgr = _make_manager({"cam1": True, "cam2": False, "cam3": True})
    assert mgr.target_count == 3
    assert mgr.connected_count == 2
