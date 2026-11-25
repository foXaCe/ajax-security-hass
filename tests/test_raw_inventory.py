"""Unit tests for the shared raw-devices collector.

``async_collect_raw_inventory`` is exercised indirectly by
``tests/test_diagnostics.py`` and ``tests/test_init_coverage.py`` through its
two callers; these tests pin the module's own contract directly, including
the defensive "no account" early return that neither caller currently
triggers (both already guard on ``coordinator.account`` before calling in).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ajax._raw_inventory import async_collect_raw_inventory


def _coordinator(devices=None, cameras=None, video_edges=None, account=True):
    api = MagicMock()
    api.async_get_devices = AsyncMock(return_value=[{"id": d["id"]} for d in (devices or [])])
    api.async_get_device = AsyncMock(side_effect=lambda hub, did: next(d for d in devices if d["id"] == did))
    api.async_get_cameras = AsyncMock(return_value=[{"id": c["id"]} for c in (cameras or [])])
    api.async_get_camera = AsyncMock(side_effect=lambda hub, cid: next(c for c in cameras if c["id"] == cid))
    api.async_get_video_edges = AsyncMock(return_value=video_edges or [])
    space = SimpleNamespace(hub_id="hub1", real_space_id="real-space-1")
    coord_account = SimpleNamespace(spaces={"sp1": space}) if account else None
    return SimpleNamespace(api=api, account=coord_account)


@pytest.mark.asyncio
async def test_returns_empty_inventory_without_account() -> None:
    coord = _coordinator(account=False)
    result = await async_collect_raw_inventory(coord)
    assert result == {"devices": [], "cameras": [], "video_edges": [], "hub_count": 0, "details_skipped": 0}


@pytest.mark.asyncio
async def test_collects_devices_cameras_video_edges() -> None:
    coord = _coordinator(
        devices=[{"id": "d1", "deviceType": "DoorProtect"}],
        cameras=[{"id": "c1", "deviceType": "MotionCam"}],
        video_edges=[{"id": "ve1"}, {"id": "ve2"}],
    )
    result = await async_collect_raw_inventory(coord)
    assert [d["id"] for d in result["devices"]] == ["d1"]
    assert [c["id"] for c in result["cameras"]] == ["c1"]
    assert [ve["id"] for ve in result["video_edges"]] == ["ve1", "ve2"]
    assert result["hub_count"] == 1


@pytest.mark.asyncio
async def test_target_device_id_filters_devices_and_cameras_but_not_video_edges() -> None:
    coord = _coordinator(
        devices=[{"id": "d1"}, {"id": "d2"}],
        cameras=[{"id": "d2"}, {"id": "c1"}],
        video_edges=[{"id": "ve1"}, {"id": "ve2"}],
    )
    result = await async_collect_raw_inventory(coord, target_device_id="d2")
    assert [d["id"] for d in result["devices"]] == ["d2"]
    assert [c["id"] for c in result["cameras"]] == ["d2"]
    assert [ve["id"] for ve in result["video_edges"]] == ["ve1", "ve2"]


@pytest.mark.asyncio
async def test_detail_budget_falls_back_to_summaries_but_keeps_everything_listed() -> None:
    """Diagnostics of a large install must not outlast the 60 s download limit (#282)."""
    coord = _coordinator(
        devices=[{"id": f"d{i}", "full": True} for i in range(5)],
        cameras=[{"id": "c1", "full": True}],
        video_edges=[{"id": "ve1"}],
    )

    async def slow(_hub: str, device_id: str) -> dict:
        if device_id == "d1":
            await asyncio.sleep(30)
        return {"id": device_id, "full": True}

    coord.api.async_get_device = AsyncMock(side_effect=slow)
    result = await asyncio.wait_for(async_collect_raw_inventory(coord, detail_budget=0.05), timeout=5)
    assert [d["id"] for d in result["devices"]] == ["d0", "d1", "d2", "d3", "d4"]
    assert [d.get("full", False) for d in result["devices"]] == [True, False, False, False, False]
    assert result["cameras"] == [{"id": "c1"}]
    assert result["video_edges"] == [{"id": "ve1"}]
    assert result["details_skipped"] == 5
    # Once the time is over, no further request is sent.
    assert coord.api.async_get_device.await_count == 2
    coord.api.async_get_camera.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_timeout_of_the_call_itself_is_not_counted_as_skipped() -> None:
    coord = _coordinator(devices=[{"id": "d1"}, {"id": "d2", "full": True}])
    coord.api.async_get_device = AsyncMock(side_effect=[TimeoutError, {"id": "d2", "full": True}])
    result = await async_collect_raw_inventory(coord, detail_budget=30)
    assert result["devices"] == [{"id": "d1"}, {"id": "d2", "full": True}]
    assert result["details_skipped"] == 0
