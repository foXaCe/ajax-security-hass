"""Tests for AjaxRestApi pure helpers and configuration logic.

The async API methods all funnel through ``_request`` which mocks
poorly without a full aiohttp fixture. Here we pin the pure / sync
plumbing — password hashing on init, URL routing per auth mode,
exponential backoff math, the bypass_cache_next() flag, and the
proactive_token_refresh schedule.
"""

from __future__ import annotations

import hashlib
import time
from unittest.mock import patch

import pytest

from custom_components.ajax.api import (
    RATE_LIMIT_REQUESTS,
    RETRY_BACKOFF_BASE,
    RETRY_BACKOFF_MAX,
    AjaxRestApi,
)
from custom_components.ajax.const import AUTH_MODE_PROXY_SECURE

# ---------------------------------------------------------------------------
# __init__ behaviour
# ---------------------------------------------------------------------------


def test_init_hashes_plain_password() -> None:
    api = AjaxRestApi(api_key="k", email="u@example.com", password="hunter2")
    assert api.password_hash == hashlib.sha256(b"hunter2").hexdigest()


def test_init_keeps_pre_hashed_password_verbatim() -> None:
    """We must NOT re-hash an already-hashed password — that would lock the user out."""
    pre_hashed = hashlib.sha256(b"hunter2").hexdigest()
    api = AjaxRestApi(
        api_key="k",
        email="u@example.com",
        password=pre_hashed,
        password_is_hashed=True,
    )
    assert api.password_hash == pre_hashed


def test_init_strips_trailing_slash_from_proxy_url() -> None:
    """Trailing slash on proxy_url would double-slash every endpoint."""
    api = AjaxRestApi(
        api_key="k",
        email="u@example.com",
        password="p",
        proxy_url="https://proxy.example.com/",
    )
    assert api.proxy_url == "https://proxy.example.com"


def test_init_sets_x_api_key_header_when_key_provided() -> None:
    api = AjaxRestApi(api_key="MY-KEY", email="u@example.com", password="p")
    assert api._base_headers["X-Api-Key"] == "MY-KEY"


def test_init_omits_x_api_key_header_when_key_empty() -> None:
    """Proxy mode users have no API key — the header must NOT be sent."""
    api = AjaxRestApi(
        api_key="", email="u@example.com", password="p", proxy_url="https://p", proxy_mode=AUTH_MODE_PROXY_SECURE
    )
    assert "X-Api-Key" not in api._base_headers


# ---------------------------------------------------------------------------
# is_proxy_mode
# ---------------------------------------------------------------------------


def test_is_proxy_mode_false_for_direct_auth() -> None:
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    assert api.is_proxy_mode is False


def test_is_proxy_mode_true_when_proxy_url_set() -> None:
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p", proxy_url="https://proxy")
    assert api.is_proxy_mode is True


def test_is_proxy_mode_true_for_proxy_secure_mode() -> None:
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p", proxy_mode=AUTH_MODE_PROXY_SECURE)
    assert api.is_proxy_mode is True


# ---------------------------------------------------------------------------
# _get_base_url routing
# ---------------------------------------------------------------------------


def test_get_base_url_direct_mode_uses_ajax_url() -> None:
    from custom_components.ajax.const import AJAX_REST_API_BASE_URL

    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    assert api._get_base_url() == AJAX_REST_API_BASE_URL


def test_get_base_url_proxy_secure_always_routes_through_proxy() -> None:
    """`secure` mode: ALL traffic — login *and* data — goes through the proxy."""
    api = AjaxRestApi(
        api_key="k",
        email="u@example.com",
        password="p",
        proxy_url="https://proxy.example.com",
        proxy_mode=AUTH_MODE_PROXY_SECURE,
    )
    assert api._get_base_url(for_login=False) == "https://proxy.example.com/api"
    assert api._get_base_url(for_login=True) == "https://proxy.example.com/api"


# ---------------------------------------------------------------------------
# _parse_retry_after — must tolerate HTTP-date headers (RFC 7231/9110) without
# crashing the retry path (code-review MEDIUM finding).
# ---------------------------------------------------------------------------


def test_parse_retry_after_integer_seconds() -> None:
    assert AjaxRestApi._parse_retry_after("120") == 120


def test_parse_retry_after_missing_uses_default() -> None:
    assert AjaxRestApi._parse_retry_after(None) == 60
    assert AjaxRestApi._parse_retry_after(None, default=30) == 30


def test_parse_retry_after_http_date_does_not_raise() -> None:
    """A bare int() would raise ValueError here and crash the back-off path."""
    # A far-future HTTP-date → a positive (non-raising) delay.
    delay = AjaxRestApi._parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert isinstance(delay, int)
    assert delay > 0


def test_parse_retry_after_past_date_clamps_to_zero() -> None:
    assert AjaxRestApi._parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0


def test_parse_retry_after_garbage_uses_default() -> None:
    assert AjaxRestApi._parse_retry_after("not-a-date-or-number") == 60


# ---------------------------------------------------------------------------
# _calculate_backoff
# ---------------------------------------------------------------------------


def test_calculate_backoff_doubles_on_each_attempt() -> None:
    assert AjaxRestApi._calculate_backoff(0) == RETRY_BACKOFF_BASE
    assert AjaxRestApi._calculate_backoff(1) == RETRY_BACKOFF_BASE * 2
    assert AjaxRestApi._calculate_backoff(2) == RETRY_BACKOFF_BASE * 4


def test_calculate_backoff_capped_at_max() -> None:
    """Without the cap, attempt=20 would be a 1M-second sleep."""
    assert AjaxRestApi._calculate_backoff(20) == RETRY_BACKOFF_MAX


# ---------------------------------------------------------------------------
# bypass_cache_next — opens a short time window so EVERY cache-backed getter
# in the upcoming refresh cycle bypasses its cache (not just the first one).
# ---------------------------------------------------------------------------


def test_bypass_cache_next_opens_window() -> None:
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    # No window open initially.
    assert api._cache_bypass_active() is False
    api.bypass_cache_next()
    # Window is now open — and stays open across multiple getter checks
    # within the same refresh cycle (the old one-shot flag failed here).
    assert api._cache_bypass_active() is True
    assert api._cache_bypass_active() is True


def test_bypass_cache_window_expires() -> None:
    """The window auto-expires so caches resume after the refresh cycle."""
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    api.bypass_cache_next()
    # Simulate the window having elapsed.
    api._bypass_cache_until = time.time() - 1.0
    assert api._cache_bypass_active() is False


# ---------------------------------------------------------------------------
# _cache_entry_usable — while a bypass window is open, an entry fetched fresh
# INSIDE it is still served to the next same-tick caller; only entries older
# than the window's opening are treated as stale (plan 010). This is what
# lets async_get_video_edges and async_get_smart_locks share a single
# GET /spaces/{id} fetch per bypass window instead of one each.
# ---------------------------------------------------------------------------


def test_cache_entry_written_before_window_is_not_usable() -> None:
    """An entry cached before the bypass window opened must be refetched."""
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    now = time.time()
    written_at = now - 1.0  # cached well before the window opened
    api._bypass_cache_opened_at = now
    api._bypass_cache_until = now + 2.0
    assert api._cache_entry_usable(written_at, ttl=5.0) is False


def test_cache_entry_written_during_window_is_usable() -> None:
    """A fetch done inside the window is fresh — a second same-tick caller reuses it."""
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    now = time.time()
    api._bypass_cache_opened_at = now
    api._bypass_cache_until = now + 2.0
    written_at = now + 0.1  # fetched just after the window opened
    assert api._cache_entry_usable(written_at, ttl=5.0) is True


def test_cache_entry_respects_ttl_regardless_of_bypass_window() -> None:
    """An entry past its TTL is never usable, bypass window or not."""
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    stale_written_at = time.time() - 10.0
    assert api._cache_entry_usable(stale_written_at, ttl=5.0) is False


def test_cache_entry_usable_falls_back_to_ttl_once_window_expired() -> None:
    """Once the bypass window has closed, only the TTL decides again.

    An entry that would have been rejected while the window was open (it
    predates ``_bypass_cache_opened_at``) is usable once the window itself
    has expired — the special "must postdate the window" rule only applies
    while a bypass is actually in progress.
    """
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    now = time.time()
    written_at = now - 1.0
    api._bypass_cache_opened_at = now
    api._bypass_cache_until = now - 0.5  # window already closed
    assert api._cache_entry_usable(written_at, ttl=5.0) is True


# ---------------------------------------------------------------------------
# _check_rate_limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_records_request_timestamp() -> None:
    """Each call to _check_rate_limit must add to the rolling window."""
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    assert api._request_timestamps == []
    await api._check_rate_limit()
    assert len(api._request_timestamps) == 1


@pytest.mark.asyncio
async def test_rate_limit_does_not_sleep_under_threshold() -> None:
    """Below the threshold, _check_rate_limit returns instantly."""
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    start = time.monotonic()
    for _ in range(min(5, RATE_LIMIT_REQUESTS - 1)):
        await api._check_rate_limit()
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"unexpected sleep ({elapsed:.2f}s) under rate limit threshold"


@pytest.mark.asyncio
async def test_rate_limit_sleeps_when_over_threshold() -> None:
    """At the threshold, we must wait for the oldest timestamp to age out."""
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    # Fill the window with timestamps just shy of falling out.
    api._request_timestamps = [time.time() - 5.0] * RATE_LIMIT_REQUESTS

    sleep_calls: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleep_calls.append(d)

    with patch("asyncio.sleep", side_effect=fake_sleep):
        await api._check_rate_limit()

    assert sleep_calls, "expected asyncio.sleep to be invoked at the threshold"
    assert sleep_calls[0] > 0


# ---------------------------------------------------------------------------
# close() — owns_session contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_skips_session_we_do_not_own() -> None:
    """When HA owns the aiohttp session, close() must NOT close it (would break HA)."""

    class _Sentinel:
        closed = False

        async def close(self) -> None:
            self.closed = True

    fake_session = _Sentinel()
    api = AjaxRestApi(
        api_key="k",
        email="u@example.com",
        password="p",
        session=fake_session,  # type: ignore[arg-type]
    )
    assert api._owns_session is False
    await api.close()
    assert fake_session.closed is False


@pytest.mark.asyncio
async def test_close_closes_session_we_own() -> None:
    """When we created the session ourselves (verify_ssl=False path), close it."""

    class _Sentinel:
        closed = False

        async def close(self) -> None:
            self.closed = True

    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    assert api._owns_session is True
    api.session = _Sentinel()  # type: ignore[assignment]
    await api.close()
    assert api.session.closed is True  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------


def test_init_starts_with_empty_caches() -> None:
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    assert api._space_cache == {}
    assert api._devices_cache == {}


def test_caches_use_configured_ttl() -> None:
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    assert api._space_cache_ttl == 5.0
    assert api._devices_cache_ttl == 5.0
