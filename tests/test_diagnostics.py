"""Unit tests for the zero-IO diagnostics snapshots.

The heavy `ajax_data` dump (REST round-trips) belongs in an HA
integration test; here we pin the cheap snapshots that drive triage:
runtime state, connectivity status, stats counters, cache sizes and
spaces summary.
"""

from __future__ import annotations

import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ajax import diagnostics


def _fake_coordinator(**overrides) -> SimpleNamespace:
    """Build a coordinator stub with just the attributes diagnostics reads."""
    api = SimpleNamespace(
        _devices_cache={("hub1", True): (time.time(), [])},
        _devices_cache_ttl=5.0,
        _space_cache={"sp1": (time.time(), {})},
        _space_cache_ttl=5.0,
    )
    account = SimpleNamespace(
        spaces={
            "sp1": SimpleNamespace(
                security_state=SimpleNamespace(value="disarmed"),
                group_mode_enabled=True,
                devices={"d1": {}, "d2": {}},
                video_edges={"ve1": {}},
                smart_locks={},
                groups={"g1": {}},
                recent_events=[{}, {}, {}],
            ),
        }
    )
    config_entry = SimpleNamespace(data={"auth_mode": "proxy_secure"})
    coord = SimpleNamespace(
        api=api,
        account=account,
        config_entry=config_entry,
        last_update_success=True,
        last_exception=None,
        update_interval=timedelta(seconds=30),
        _cycle_counter=12,
        _consecutive_auth_errors=0,
        _last_metadata_refresh=time.time() - 100,
        stats={
            "events_sse_received": 17,
            "events_sqs_received": 0,
            "events_onvif_received": 4,
            "auth_errors": 0,
            "discovery_refreshes": 1,
        },
        sse_manager=None,
        sqs_manager=None,
        onvif_manager=None,
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def test_integration_version_matches_manifest() -> None:
    # The function caches the value, so this also verifies the cache returns
    # the same string on a second call.
    first = diagnostics._integration_version()
    second = diagnostics._integration_version()
    assert first == second
    assert first != "unknown", "manifest.json must be readable from tests"


def test_seconds_since_returns_none_for_falsy() -> None:
    assert diagnostics._seconds_since(None) is None
    assert diagnostics._seconds_since(0) is None


def test_seconds_since_rounds_to_tenth() -> None:
    now = time.time()
    elapsed = diagnostics._seconds_since(now - 12.34)
    # Rounded to 1 decimal place per the function's contract.
    assert elapsed is not None
    assert round(elapsed - 12.3, 1) <= 0.1


def test_runtime_snapshot_shape_and_values() -> None:
    coord = _fake_coordinator()
    snap = diagnostics._runtime_snapshot(coord)
    assert snap["auth_mode"] == "proxy_secure"
    assert snap["last_update_success"] is True
    assert snap["last_exception"] is None
    assert snap["update_interval_seconds"] == 30
    assert snap["cycle_counter"] == 12
    assert snap["consecutive_auth_errors"] == 0
    assert snap["spaces"] == 1
    assert snap["integration_version"] != "unknown"
    # 100 s ± rounding noise.
    assert snap["seconds_since_last_metadata_refresh"] >= 99


def test_runtime_snapshot_formats_exception_when_set() -> None:
    coord = _fake_coordinator(last_exception=RuntimeError("boom"))
    snap = diagnostics._runtime_snapshot(coord)
    assert "boom" in snap["last_exception"]


def test_runtime_snapshot_handles_missing_account() -> None:
    coord = _fake_coordinator(account=None)
    snap = diagnostics._runtime_snapshot(coord)
    assert snap["spaces"] == 0


def test_connectivity_snapshot_all_disabled() -> None:
    coord = _fake_coordinator()
    snap = diagnostics._connectivity_snapshot(coord)
    assert snap == {
        "sse": {"enabled": False, "connected": False},
        "sqs": {"enabled": False, "connected": False, "seconds_since_last_event": None},
        "onvif": {"configured_count": 0, "connected_count": 0},
    }


def test_connectivity_snapshot_with_sse_connected() -> None:
    sse_client = MagicMock()
    # ``AjaxSSEClient.is_connected`` is a @property returning a bool, not a
    # callable — model it as a plain bool so this test would catch the regression
    # where diagnostics tried to call it.
    sse_client.is_connected = True
    sse_manager = SimpleNamespace(sse_client=sse_client)
    coord = _fake_coordinator(sse_manager=sse_manager)
    snap = diagnostics._connectivity_snapshot(coord)
    assert snap["sse"] == {"enabled": True, "connected": True}


def test_connectivity_snapshot_with_sqs_last_event() -> None:
    sqs_client = MagicMock()
    sqs_client.is_connected = MagicMock(return_value=True)
    last = time.time() - 4
    sqs_manager = SimpleNamespace(sqs_client=sqs_client, _last_event_time=last)
    coord = _fake_coordinator(sqs_manager=sqs_manager)
    snap = diagnostics._connectivity_snapshot(coord)
    assert snap["sqs"]["enabled"] is True
    assert snap["sqs"]["connected"] is True
    assert snap["sqs"]["seconds_since_last_event"] >= 3.9


def test_connectivity_snapshot_with_onvif() -> None:
    onvif_manager = SimpleNamespace(_clients={"a": object(), "b": object()}, connected_count=1)
    coord = _fake_coordinator(onvif_manager=onvif_manager)
    snap = diagnostics._connectivity_snapshot(coord)
    assert snap["onvif"] == {"configured_count": 2, "connected_count": 1}


def test_cache_snapshot_reports_sizes_and_ttls() -> None:
    coord = _fake_coordinator()
    snap = diagnostics._cache_snapshot(coord)
    assert snap == {
        "devices_cache_entries": 1,
        "devices_cache_ttl_seconds": 5.0,
        "space_cache_entries": 1,
        "space_cache_ttl_seconds": 5.0,
    }


def test_cache_snapshot_handles_missing_attributes() -> None:
    """An older API instance without cache attributes must not crash."""
    coord = _fake_coordinator(api=SimpleNamespace())
    snap = diagnostics._cache_snapshot(coord)
    assert snap["devices_cache_entries"] == 0
    assert snap["space_cache_entries"] == 0


def test_spaces_summary_counts_per_space() -> None:
    coord = _fake_coordinator()
    summary = diagnostics._spaces_summary(coord)
    assert summary == [
        {
            "security_state": "disarmed",
            "group_mode_enabled": True,
            "devices": 2,
            "video_edges": 1,
            "smart_locks": 0,
            "groups": 1,
            "recent_events": 3,
        }
    ]


def test_spaces_summary_empty_when_no_account() -> None:
    coord = _fake_coordinator(account=None)
    assert diagnostics._spaces_summary(coord) == []


def test_runtime_diagnostics_bundles_all_sections() -> None:
    """The orchestrator must return exactly the documented sections."""
    coord = _fake_coordinator()
    bundle = diagnostics._runtime_diagnostics(coord)
    assert set(bundle.keys()) == {"runtime", "connectivity", "stats", "cache", "spaces"}
    # stats must be a *copy* (mutating downstream must not pollute the
    # coordinator's live counters).
    bundle["stats"]["events_sse_received"] = 9999
    assert coord.stats["events_sse_received"] == 17


# ---------------------------------------------------------------------------
# get_ajax_raw_data — exercises the per-hub fetch + summary aggregation
# ---------------------------------------------------------------------------


def _coord_with_api_responses(devices=None, cameras=None, video_edges=None):
    """Build a coordinator with mocked async API methods."""
    api = MagicMock()
    api.async_get_devices = AsyncMock(return_value=[{"id": d["id"]} for d in (devices or [])])
    api.async_get_device = AsyncMock(side_effect=lambda hub, did: next(d for d in devices if d["id"] == did))
    api.async_get_cameras = AsyncMock(return_value=[{"id": c["id"]} for c in (cameras or [])])
    api.async_get_camera = AsyncMock(side_effect=lambda hub, cid: next(c for c in cameras if c["id"] == cid))
    api.async_get_video_edges = AsyncMock(return_value=video_edges or [])
    space = SimpleNamespace(
        hub_id="hub1",
        real_space_id="real-space-1",
        security_state=SimpleNamespace(value="disarmed"),
        group_mode_enabled=False,
        devices={},
        video_edges={},
        smart_locks={},
        groups={},
        recent_events=[],
    )
    account = SimpleNamespace(spaces={"sp1": space})
    coord = SimpleNamespace(api=api, account=account, entry_id="entry_test")
    entry = SimpleNamespace(runtime_data=coord)
    return entry, coord


@pytest.mark.asyncio
async def test_get_ajax_raw_data_aggregates_devices_cameras_video_edges() -> None:
    """Summary block counts hubs / devices / cameras / video_edges + groups types."""
    entry, _coord = _coord_with_api_responses(
        devices=[
            {"id": "d1", "deviceType": "DoorProtect"},
            {"id": "d2", "deviceType": "DoorProtect"},
            {"id": "d3", "deviceType": "MotionProtect"},
        ],
        cameras=[{"id": "c1", "model": "MotionCam"}],
        video_edges=[{"id": "ve1"}, {"id": "ve2"}],
    )

    result = await diagnostics.get_ajax_raw_data(hass=MagicMock(), entry=entry)

    summary = result["summary"]
    assert summary == {
        "hubs": 1,
        "devices": 3,
        "cameras": 1,
        "video_edges": 2,
        "device_types": {"DoorProtect": 2, "MotionProtect": 1},
    }
    assert len(result["devices"]) == 3
    assert len(result["cameras"]) == 1
    assert len(result["video_edges"]) == 2


@pytest.mark.asyncio
async def test_get_ajax_raw_data_filters_to_target_device_when_set() -> None:
    """When called from `async_get_device_diagnostics` only the matching id is fetched."""
    entry, _coord = _coord_with_api_responses(
        devices=[{"id": "d1", "deviceType": "DoorProtect"}, {"id": "d2", "deviceType": "MotionProtect"}],
        cameras=[],
        video_edges=[],
    )
    # Device identifiers are namespaced f"{entry_id}_{ajax_id}" (schema v1.3);
    # diagnostics strips the prefix to recover the bare Ajax id "d2".
    device = SimpleNamespace(identifiers={(diagnostics.DOMAIN, "entry_test_d2")})

    result = await diagnostics.get_ajax_raw_data(hass=MagicMock(), entry=entry, device=device)
    ids = [d["id"] for d in result["devices"]]
    assert ids == ["d2"]


@pytest.mark.asyncio
async def test_get_ajax_raw_data_returns_zero_summary_without_account() -> None:
    api = MagicMock()
    coord = SimpleNamespace(api=api, account=None)
    entry = SimpleNamespace(runtime_data=coord)
    result = await diagnostics.get_ajax_raw_data(hass=MagicMock(), entry=entry)
    assert result["summary"] == {
        "hubs": 0,
        "devices": 0,
        "cameras": 0,
        "video_edges": 0,
        "device_types": {},
    }


@pytest.mark.asyncio
async def test_get_ajax_raw_data_falls_back_to_summary_when_full_device_fetch_fails() -> None:
    """A per-device exception must keep the summary entry from disappearing."""
    api = MagicMock()
    api.async_get_devices = AsyncMock(return_value=[{"id": "d1", "type": "MotionProtect"}])
    api.async_get_device = AsyncMock(side_effect=RuntimeError("boom"))
    api.async_get_cameras = AsyncMock(return_value=[])
    api.async_get_video_edges = AsyncMock(return_value=[])
    space = SimpleNamespace(
        hub_id="hub1",
        real_space_id=None,
        security_state=SimpleNamespace(value="disarmed"),
        group_mode_enabled=False,
        devices={},
        video_edges={},
        smart_locks={},
        groups={},
        recent_events=[],
    )
    account = SimpleNamespace(spaces={"sp1": space})
    coord = SimpleNamespace(api=api, account=account)
    entry = SimpleNamespace(runtime_data=coord)

    result = await diagnostics.get_ajax_raw_data(hass=MagicMock(), entry=entry)
    assert len(result["devices"]) == 1, "summary fallback must keep the device row"


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_get_config_entry_diagnostics_redacts_and_bundles_runtime() -> None:
    """Public entry must ship the `runtime` / `connectivity` / `stats` bundle and redact secrets."""
    entry, _coord = _coord_with_api_responses(devices=[], cameras=[], video_edges=[])
    # Add sensitive fields that must be scrubbed.
    entry.data = {
        "email": "user@example.com",
        "api_key": "SECRET-API-KEY",
        "auth_mode": "proxy_secure",
    }
    _coord.config_entry = entry
    _coord.last_update_success = True
    _coord.last_exception = None
    _coord.update_interval = timedelta(seconds=30)
    _coord._cycle_counter = 0
    _coord._consecutive_auth_errors = 0
    _coord._last_metadata_refresh = time.time()
    _coord.stats = {
        "events_sse_received": 0,
        "events_sqs_received": 0,
        "events_onvif_received": 0,
        "auth_errors": 0,
        "discovery_refreshes": 0,
    }
    _coord.sse_manager = None
    _coord.sqs_manager = None
    _coord.onvif_manager = None
    _coord.api._devices_cache = {}
    _coord.api._space_cache = {}
    _coord.api._devices_cache_ttl = 5.0
    _coord.api._space_cache_ttl = 5.0

    result = await diagnostics.async_get_config_entry_diagnostics(hass=MagicMock(), entry=entry)

    assert {"config_entry_data", "diagnostics", "ajax_data"} <= set(result.keys())
    # Redaction: email and api_key must be scrubbed.
    assert result["config_entry_data"]["email"] == "**REDACTED**"
    assert result["config_entry_data"]["api_key"] == "**REDACTED**"
    # Runtime bundle present.
    assert set(result["diagnostics"].keys()) == {"runtime", "connectivity", "stats", "cache", "spaces"}


def _raw_coordinator() -> SimpleNamespace:
    """Coordinator with a populated space + API mocks for get_ajax_raw_data."""
    coord = _fake_coordinator()
    coord.entry_id = "e1"
    space = next(iter(coord.account.spaces.values()))
    space.hub_id = "hub1"
    space.real_space_id = "rs1"
    coord.api.async_get_devices = AsyncMock(return_value=[{"id": "d1"}, {"id": "d2"}])
    # d1 resolves to a full payload; d2 fetch fails -> falls back to the summary.
    coord.api.async_get_device = AsyncMock(
        side_effect=[{"id": "d1", "deviceType": "DoorProtect"}, RuntimeError("boom")]
    )
    coord.api.async_get_cameras = AsyncMock(return_value=[{"id": "c1"}])
    coord.api.async_get_camera = AsyncMock(return_value={"id": "c1", "full": True})
    coord.api.async_get_video_edges = AsyncMock(return_value=[{"id": "ve1"}])
    return coord


async def test_get_ajax_raw_data_collects_and_falls_back() -> None:
    coord = _raw_coordinator()
    entry = SimpleNamespace(runtime_data=coord, data={})
    raw = await diagnostics.get_ajax_raw_data(MagicMock(), entry)
    # d1 full + d2 fallback summary == 2 devices; camera + video-edge counted.
    assert raw["summary"] == {
        "hubs": 1,
        "devices": 2,
        "cameras": 1,
        "video_edges": 1,
        "device_types": {"DoorProtect": 1, "unknown": 1},
    }
    assert {"id": "d2"} in raw["devices"]  # fallback used the light summary


async def test_async_get_device_diagnostics_builds_device_info() -> None:
    from custom_components.ajax.const import DOMAIN

    coord = _raw_coordinator()
    entry = SimpleNamespace(runtime_data=coord, data={"email": "user@example.com"})
    device = SimpleNamespace(
        identifiers={(DOMAIN, "e1_d1")},  # namespaced; stripped to bare "d1"
        manufacturer="Ajax Systems",
        model="DoorProtect",
        model_id="dp1",
        serial_number="SN1",
        sw_version="2.0",
        hw_version="HW1",
    )
    result = await diagnostics.async_get_device_diagnostics(MagicMock(), entry, device)
    assert result["device_info"]["model"] == "DoorProtect"
    assert result["config_entry_data"]["email"] == "**REDACTED**"
    # target_device_id filtered the fetch to the requested device only.
    assert [d["id"] for d in result["ajax_data"]["devices"]] == ["d1"]
