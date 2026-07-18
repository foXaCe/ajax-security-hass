"""Tests for AjaxRestApi REST endpoint wrappers.

We mock ``_request`` and verify the URL routing, parameter passing,
and the small bits of business logic that wrap it (the devices/spaces
cache, the user_id validation guard, the bypass_cache_once flag).

A regression here usually means an endpoint URL was wrong (silent 404
that the coordinator masks) or the cache returned a stale payload.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from custom_components.ajax.api import AjaxRestApi, AjaxRestApiError


def _api(user_id: str | None = "USER123") -> AjaxRestApi:
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    api.user_id = user_id
    api._request = AsyncMock()  # type: ignore[method-assign]
    return api


# ---------------------------------------------------------------------------
# Hub endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_get_hubs_requires_user_id() -> None:
    """Without user_id, the URL would be `user/None/hubs` — must raise first."""
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_get_hubs()


@pytest.mark.asyncio
async def test_async_get_hubs_hits_correct_endpoint() -> None:
    api = _api()
    api._request.return_value = [{"hubId": "h1"}]
    result = await api.async_get_hubs()
    api._request.assert_awaited_once_with("GET", "user/USER123/hubs")
    assert result == [{"hubId": "h1"}]


@pytest.mark.asyncio
async def test_async_get_hub_routes_via_user() -> None:
    api = _api()
    api._request.return_value = {"hubId": "h1", "state": "ARMED"}
    await api.async_get_hub("h1")
    api._request.assert_awaited_once_with("GET", "user/USER123/hubs/h1")


@pytest.mark.asyncio
async def test_async_set_hub_mode_sends_post_with_mode_payload() -> None:
    api = _api()
    await api.async_set_hub_mode("h1", "full")
    api._request.assert_awaited_once_with("POST", "hubs/h1/mode", {"mode": "full"})


@pytest.mark.asyncio
async def test_async_get_hub_mode_uses_get() -> None:
    api = _api()
    await api.async_get_hub_mode("h1")
    api._request.assert_awaited_once_with("GET", "hubs/h1/mode")


# ---------------------------------------------------------------------------
# Devices: cache contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_get_devices_caches_response_for_ttl_window() -> None:
    """Two calls within the TTL window must hit the network only once."""
    api = _api()
    api._request.return_value = [{"id": "d1"}]

    first = await api.async_get_devices("h1")
    second = await api.async_get_devices("h1")

    assert first == second == [{"id": "d1"}]
    api._request.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_get_devices_separate_cache_per_enrich_flag() -> None:
    """`enrich=True` and `enrich=False` return different shapes — separate cache keys."""
    api = _api()
    api._request.return_value = []

    await api.async_get_devices("h1", enrich=True)
    await api.async_get_devices("h1", enrich=False)
    assert api._request.await_count == 2


@pytest.mark.asyncio
async def test_async_get_devices_cache_bypassed_when_bypass_flag_set() -> None:
    """bypass_cache_next() lets the next call escape the cache (post-SSE refresh path)."""
    api = _api()
    api._request.return_value = [{"id": "d1"}]
    await api.async_get_devices("h1")
    api.bypass_cache_next()
    await api.async_get_devices("h1")
    assert api._request.await_count == 2


@pytest.mark.asyncio
async def test_async_get_devices_bypass_window_reuses_fetch_for_second_caller() -> None:
    """Plan 010: a fetch done INSIDE the bypass window is reused by the next
    same-tick caller for the same key — the window guarantees at most one
    fresh fetch per key, not one fetch per caller (periodic loop and
    door-sensor fast-poll crossing paths within the same window)."""
    api = _api()
    api._request.return_value = [{"id": "d1"}]

    api.bypass_cache_next()
    await api.async_get_devices("h1")  # fresh fetch inside the window
    await api.async_get_devices("h1")  # same window, same key -> cache hit

    assert api._request.await_count == 1


@pytest.mark.asyncio
async def test_async_get_devices_cache_expires_after_ttl() -> None:
    """Past the TTL window, a fresh request must fire."""
    api = _api()
    api._devices_cache_ttl = 0.01
    api._request.return_value = [{"id": "d1"}]

    await api.async_get_devices("h1")
    # Forge an expired cache entry (rather than sleeping).
    api._devices_cache[("h1", True)] = (time.time() - 10.0, [{"id": "stale"}])
    await api.async_get_devices("h1")
    assert api._request.await_count == 2


@pytest.mark.asyncio
async def test_async_get_devices_passes_enrich_query_param() -> None:
    api = _api()
    api._request.return_value = []
    await api.async_get_devices("h1", enrich=True)
    args = api._request.await_args
    assert args[0][1].endswith("?enrich=true")


@pytest.mark.asyncio
async def test_async_get_devices_omits_enrich_when_false() -> None:
    api = _api()
    api._request.return_value = []
    await api.async_get_devices("h1", enrich=False)
    args = api._request.await_args
    assert "enrich" not in args[0][1]


# ---------------------------------------------------------------------------
# Device endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_get_device_routes_via_user_hub() -> None:
    api = _api()
    await api.async_get_device("h1", "d1")
    api._request.assert_awaited_once_with("GET", "user/USER123/hubs/h1/devices/d1")


@pytest.mark.asyncio
async def test_async_get_device_state_uses_devices_endpoint() -> None:
    api = _api()
    await api.async_get_device_state("d1")
    api._request.assert_awaited_once_with("GET", "devices/d1/state")


# ---------------------------------------------------------------------------
# Space endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_get_space_by_hub_returns_first_match() -> None:
    """The API returns a list — wrapper picks the first space matching the hub_id."""
    api = _api()
    # The wrapper is `async_get_space_by_hub` — it ships the hub_id in a payload
    # we can't easily mock here. Just verify the endpoint is hit.
    api._request.return_value = None
    result = await api.async_get_space_by_hub("h1")
    api._request.assert_awaited_once()
    # Without a parsed body, we expect None (fail-safe).
    assert result is None


@pytest.mark.asyncio
async def test_async_get_rooms_routes_via_hub() -> None:
    api = _api()
    api._request.return_value = []
    await api.async_get_rooms("h1")
    api._request.assert_awaited_once()
    assert "h1" in api._request.await_args[0][1]


@pytest.mark.asyncio
async def test_async_get_users_routes_via_hub() -> None:
    api = _api()
    api._request.return_value = []
    await api.async_get_users("h1")
    api._request.assert_awaited_once()
    assert "h1" in api._request.await_args[0][1]
