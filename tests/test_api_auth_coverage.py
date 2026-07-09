"""Tests for AjaxRestApi authentication and HTTP transport.

These cover the wire-level paths that ``test_api_helpers``/``test_api_endpoints``
deliberately skip: the login (including the optional single-step TOTP code) /
refresh handshakes, the proactive and recovery auth flows, and the
retry/backoff machinery inside ``_request`` and ``_request_no_response``.

We mock at the lowest practical layer — the ``aiohttp.ClientSession`` —
substituting it with a fake whose ``post``/``request``/``get`` return an async
context manager wrapping a canned response. ``asyncio.sleep`` is patched out so
backoff retries do not actually pause the test suite.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from custom_components.ajax.api import (
    MAX_RETRIES,
    AjaxRestApi,
    AjaxRestApiError,
    AjaxRestAuthError,
    AjaxRestConnectionError,
    AjaxRestRateLimitError,
)
from custom_components.ajax.const import AUTH_MODE_PROXY_SECURE

# pyotp's well-known public test vector (RFC 6238 example secret).
_TEST_TOTP_SECRET = "JBSWY3DPEHPK3PXP"

# ---------------------------------------------------------------------------
# Mock aiohttp plumbing
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for an aiohttp response object."""

    def __init__(
        self,
        status: int = 200,
        json_data: object | None = None,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        text: str = "",
        json_exc: Exception | None = None,
    ) -> None:
        self.status = status
        self._json_data = json_data
        self.headers = headers or {}
        self._body = body
        self._text = text
        self._json_exc = json_exc

    async def json(self) -> object:
        if self._json_exc is not None:
            raise self._json_exc
        return self._json_data

    async def read(self) -> bytes:
        return self._body

    async def text(self) -> str:
        return self._text

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=None,  # type: ignore[arg-type]
                history=(),
                status=self.status,
            )

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _RaisingCtx:
    """Async context manager whose __aenter__ raises (network error path)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> object:
        raise self._exc

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Fake aiohttp.ClientSession returning queued responses.

    ``request``/``post``/``get`` each pop the next item from ``responses``.
    An item may be a ``_FakeResponse`` (returned via an async ctx manager) or
    an ``Exception`` instance (raised on ctx-manager entry to drive the
    ClientError / TimeoutError retry branches).
    """

    closed = False

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def _next(self, method: str, url: str, **kwargs: object) -> object:
        self.calls.append((method, url, kwargs))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            return _RaisingCtx(item)
        return item

    def request(self, method: str, url: str, **kwargs: object) -> object:
        return self._next(method, url, **kwargs)

    def post(self, url: str, **kwargs: object) -> object:
        return self._next("POST", url, **kwargs)

    def get(self, url: str, **kwargs: object) -> object:
        return self._next("GET", url, **kwargs)

    async def close(self) -> None:
        self.closed = True


def _api(
    *,
    user_id: str | None = "USER123",
    session_token: str | None = "tok",
    refresh_token: str | None = "refresh",
    proxy_mode: str | None = None,
    proxy_url: str | None = None,
    totp_secret: str | None = None,
    responses: list[object] | None = None,
) -> AjaxRestApi:
    api = AjaxRestApi(
        api_key="KEY",
        email="u@example.com",
        password="p",
        totp_secret=totp_secret,
        proxy_mode=proxy_mode,
        proxy_url=proxy_url,
    )
    api.user_id = user_id
    api.session_token = session_token
    api.refresh_token = refresh_token
    if responses is not None:
        api.session = _FakeSession(responses)  # type: ignore[assignment]
    return api


@pytest.fixture(autouse=True)
def _no_sleep():
    """Neutralise backoff sleeps so retries run instantly."""
    with patch("custom_components.ajax.api._base.asyncio.sleep", new=AsyncMock()):
        yield


# ---------------------------------------------------------------------------
# async_login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_success_sets_tokens() -> None:
    api = _api(user_id=None, session_token=None, refresh_token=None)
    api.session = _FakeSession([_FakeResponse(200, {"sessionToken": "S", "refreshToken": "R", "userId": "U1"})])  # type: ignore[assignment]
    token = await api.async_login()
    assert token == "S"
    assert api.session_token == "S"
    assert api.refresh_token == "R"
    assert api.user_id == "U1"
    assert api._token_version == 1


@pytest.mark.asyncio
async def test_login_401_invalid_api_key() -> None:
    api = _api(session_token=None)
    api.session = _FakeSession([_FakeResponse(401, {"message": "User is not authorized"})])  # type: ignore[assignment]
    with pytest.raises(AjaxRestAuthError) as exc:
        await api.async_login()
    assert exc.value.error_type == "invalid_api_key"


@pytest.mark.asyncio
async def test_login_401_wrong_password() -> None:
    api = _api(session_token=None)
    api.session = _FakeSession([_FakeResponse(401, {"message": "Wrong login or password"})])  # type: ignore[assignment]
    with pytest.raises(AjaxRestAuthError) as exc:
        await api.async_login()
    assert exc.value.error_type == "invalid_password"


@pytest.mark.asyncio
async def test_login_401_pro_account() -> None:
    api = _api(session_token=None)
    api.session = _FakeSession([_FakeResponse(401, {"message": "PRO accounts unsupported"})])  # type: ignore[assignment]
    with pytest.raises(AjaxRestAuthError) as exc:
        await api.async_login()
    assert exc.value.error_type == "invalid_account_type"


@pytest.mark.asyncio
async def test_login_401_generic_message() -> None:
    api = _api(session_token=None)
    api.session = _FakeSession([_FakeResponse(401, {"message": "Something odd"})])  # type: ignore[assignment]
    with pytest.raises(AjaxRestAuthError) as exc:
        await api.async_login()
    assert exc.value.error_type == "generic"


@pytest.mark.asyncio
async def test_login_401_unparsable_body_falls_back_to_invalid_password() -> None:
    api = _api(session_token=None)
    api.session = _FakeSession(
        [_FakeResponse(401, json_exc=aiohttp.ContentTypeError(None, ()))]  # type: ignore[arg-type]
    )  # type: ignore[assignment]
    with pytest.raises(AjaxRestAuthError) as exc:
        await api.async_login()
    assert exc.value.error_type == "invalid_password"


@pytest.mark.asyncio
async def test_login_500_invalid_api_key() -> None:
    api = _api(session_token=None)
    api.session = _FakeSession([_FakeResponse(500)])  # type: ignore[assignment]
    with pytest.raises(AjaxRestAuthError) as exc:
        await api.async_login()
    assert exc.value.error_type == "invalid_api_key"


@pytest.mark.asyncio
async def test_login_403_raises_auth_error() -> None:
    # API 1.147.0 has no distinct "2FA required" signal on /login; a 403 is
    # surfaced as a plain auth failure so the config flow re-prompts.
    api = _api(session_token=None)
    api.session = _FakeSession([_FakeResponse(403)])  # type: ignore[assignment]
    with pytest.raises(AjaxRestAuthError) as exc:
        await api.async_login()
    assert exc.value.error_type == "invalid_password"


@pytest.mark.asyncio
async def test_login_no_session_token_raises() -> None:
    api = _api(session_token=None)
    api.session = _FakeSession([_FakeResponse(200, {"userId": "U1"})])  # type: ignore[assignment]
    with pytest.raises(AjaxRestApiError, match="No sessionToken"):
        await api.async_login()


@pytest.mark.asyncio
async def test_login_proxy_uses_user_id_as_token_and_extracts_extras() -> None:
    api = _api(
        session_token=None,
        proxy_mode=AUTH_MODE_PROXY_SECURE,
        proxy_url="https://proxy.example.com",
    )
    api.session = _FakeSession(
        [
            _FakeResponse(
                200,
                {"user_id": "PU", "apiKey": "PROXY-KEY", "sseUrl": "https://proxy.example.com/events?userId=PU"},
            )
        ]
    )  # type: ignore[assignment]
    token = await api.async_login()
    # No sessionToken returned → falls back to user_id.
    assert token == "PU"
    assert api.user_id == "PU"
    assert api.api_key == "PROXY-KEY"
    assert api._base_headers["X-Api-Key"] == "PROXY-KEY"
    assert api.sse_url == "https://proxy.example.com/events?userId=PU"


@pytest.mark.asyncio
async def test_login_proxy_builds_sse_url_when_absent() -> None:
    api = _api(
        session_token=None,
        proxy_mode=AUTH_MODE_PROXY_SECURE,
        proxy_url="https://proxy.example.com",
    )
    api.session = _FakeSession([_FakeResponse(200, {"user_id": "PU"})])  # type: ignore[assignment]
    await api.async_login()
    assert api.sse_url == "https://proxy.example.com/events?userId=PU"


@pytest.mark.asyncio
async def test_login_client_error_wrapped() -> None:
    api = _api(session_token=None)
    api.session = _FakeSession([aiohttp.ClientError("boom")])  # type: ignore[assignment]
    with pytest.raises(AjaxRestApiError, match="Login failed"):
        await api.async_login()


@pytest.mark.asyncio
async def test_login_timeout_wrapped() -> None:
    api = _api(session_token=None)
    api.session = _FakeSession([TimeoutError()])  # type: ignore[assignment]
    with pytest.raises(AjaxRestApiError, match="Login timeout"):
        await api.async_login()


# ---------------------------------------------------------------------------
# async_login — TOTP (single-step 2FA, mandatory on Ajax's side 2025-09-01)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_with_totp_secret_includes_code_in_payload() -> None:
    api = _api(session_token=None, totp_secret=_TEST_TOTP_SECRET)
    session = _FakeSession([_FakeResponse(200, {"sessionToken": "S", "refreshToken": "R", "userId": "U1"})])
    api.session = session  # type: ignore[assignment]
    # Pin the code so the assertion cannot flake across a 30s window roll.
    with patch("pyotp.TOTP.now", return_value="654321"):
        await api.async_login()
    _, _, kwargs = session.calls[0]
    payload = kwargs["json"]
    assert payload["totp"] == "654321"
    assert len(payload["totp"]) == 6
    assert payload["totp"].isdigit()


@pytest.mark.asyncio
async def test_login_without_totp_secret_omits_code() -> None:
    api = _api(session_token=None, totp_secret=None)
    session = _FakeSession([_FakeResponse(200, {"sessionToken": "S", "refreshToken": "R", "userId": "U1"})])
    api.session = session  # type: ignore[assignment]
    await api.async_login()
    _, _, kwargs = session.calls[0]
    assert "totp" not in kwargs["json"]


@pytest.mark.asyncio
async def test_login_invalid_totp_secret_raises_without_logging_secret(caplog) -> None:
    bad_secret = "not-a-valid-base32-secret!"
    api = _api(session_token=None, totp_secret=bad_secret)
    api.session = _FakeSession([])  # type: ignore[assignment]  # never reached: fails before the request
    with caplog.at_level("DEBUG"), pytest.raises(AjaxRestAuthError) as exc:
        await api.async_login()
    assert exc.value.error_type == "invalid_totp_secret"
    assert bad_secret not in caplog.text


# ---------------------------------------------------------------------------
# async_refresh_token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_token_success() -> None:
    api = _api()
    api.session = _FakeSession([_FakeResponse(200, {"sessionToken": "NEW", "refreshToken": "NEWR"})])  # type: ignore[assignment]
    token = await api.async_refresh_token()
    assert token == "NEW"
    assert api.session_token == "NEW"
    assert api.refresh_token == "NEWR"


@pytest.mark.asyncio
async def test_refresh_token_requires_refresh_token_and_user_id() -> None:
    api = _api(refresh_token=None)
    with pytest.raises(AjaxRestApiError, match="No refresh token"):
        await api.async_refresh_token()


@pytest.mark.asyncio
async def test_refresh_token_401_raises_auth_error() -> None:
    api = _api()
    api.session = _FakeSession([_FakeResponse(401)])  # type: ignore[assignment]
    with pytest.raises(AjaxRestAuthError, match="expired or invalid"):
        await api.async_refresh_token()


@pytest.mark.asyncio
async def test_refresh_token_429_raises_auth_error() -> None:
    api = _api()
    api.session = _FakeSession([_FakeResponse(429)])  # type: ignore[assignment]
    with pytest.raises(AjaxRestAuthError, match="rate limited"):
        await api.async_refresh_token()


@pytest.mark.asyncio
async def test_refresh_token_no_session_token_in_response() -> None:
    api = _api()
    api.session = _FakeSession([_FakeResponse(200, {})])  # type: ignore[assignment]
    with pytest.raises(AjaxRestApiError, match="No sessionToken"):
        await api.async_refresh_token()


@pytest.mark.asyncio
async def test_refresh_token_client_error_wrapped() -> None:
    api = _api()
    api.session = _FakeSession([aiohttp.ClientError("x")])  # type: ignore[assignment]
    with pytest.raises(AjaxRestApiError, match="Token refresh failed"):
        await api.async_refresh_token()


@pytest.mark.asyncio
async def test_refresh_token_timeout_wrapped() -> None:
    api = _api()
    api.session = _FakeSession([TimeoutError()])  # type: ignore[assignment]
    with pytest.raises(AjaxRestApiError, match="Token refresh timeout"):
        await api.async_refresh_token()


# ---------------------------------------------------------------------------
# _proactive_token_refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proactive_refresh_noop_when_token_never_obtained() -> None:
    api = _api()
    api._token_obtained_at = 0.0
    api.async_refresh_token = AsyncMock()  # type: ignore[method-assign]
    await api._proactive_token_refresh()
    api.async_refresh_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_proactive_refresh_noop_when_token_fresh() -> None:
    api = _api()
    import time as _t

    api._token_obtained_at = _t.monotonic()  # just obtained
    api.async_refresh_token = AsyncMock()  # type: ignore[method-assign]
    await api._proactive_token_refresh()
    api.async_refresh_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_proactive_refresh_refreshes_when_stale() -> None:
    api = _api()
    import time as _t

    api._token_obtained_at = _t.monotonic() - 10_000  # far past expiry
    api.async_refresh_token = AsyncMock(return_value="NEW")  # type: ignore[method-assign]
    await api._proactive_token_refresh()
    api.async_refresh_token.assert_awaited_once()
    assert api._refresh_failures == 0


@pytest.mark.asyncio
async def test_proactive_refresh_falls_back_to_login_on_refresh_failure() -> None:
    api = _api()
    import time as _t

    api._token_obtained_at = _t.monotonic() - 10_000
    api._last_login_time = _t.monotonic() - 10_000  # cooldown elapsed
    api.async_refresh_token = AsyncMock(side_effect=AjaxRestAuthError("dead"))  # type: ignore[method-assign]
    api.async_login = AsyncMock(return_value="S")  # type: ignore[method-assign]
    await api._proactive_token_refresh()
    api.async_login.assert_awaited_once()
    assert api._refresh_failures == 1


@pytest.mark.asyncio
async def test_proactive_refresh_login_failure_is_swallowed() -> None:
    api = _api(refresh_token=None)
    import time as _t

    api._token_obtained_at = _t.monotonic() - 10_000
    api._last_login_time = _t.monotonic() - 10_000
    api.async_login = AsyncMock(side_effect=AjaxRestApiError("nope"))  # type: ignore[method-assign]
    # Should not raise.
    await api._proactive_token_refresh()
    api.async_login.assert_awaited_once()


@pytest.mark.asyncio
async def test_proactive_refresh_skips_login_during_cooldown() -> None:
    api = _api(refresh_token=None)
    import time as _t

    api._token_obtained_at = _t.monotonic() - 10_000
    api._last_login_time = _t.monotonic()  # just logged in → cooldown active
    api.async_login = AsyncMock()  # type: ignore[method-assign]
    await api._proactive_token_refresh()
    api.async_login.assert_not_awaited()


# ---------------------------------------------------------------------------
# _recover_auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_auth_uses_refresh_when_available() -> None:
    api = _api()
    api.async_refresh_token = AsyncMock(return_value="NEW")  # type: ignore[method-assign]
    api.async_login = AsyncMock()  # type: ignore[method-assign]
    await api._recover_auth()
    api.async_refresh_token.assert_awaited_once()
    api.async_login.assert_not_awaited()
    assert api._refresh_failures == 0


@pytest.mark.asyncio
async def test_recover_auth_falls_back_to_login_after_refresh_fails() -> None:
    api = _api()
    import time as _t

    api._last_login_time = _t.monotonic() - 10_000  # no cooldown
    api.async_refresh_token = AsyncMock(side_effect=AjaxRestAuthError("x"))  # type: ignore[method-assign]
    api.async_login = AsyncMock(return_value="S")  # type: ignore[method-assign]
    await api._recover_auth()
    api.async_login.assert_awaited_once()
    assert api._refresh_failures == 1


@pytest.mark.asyncio
async def test_recover_auth_skips_refresh_after_three_failures() -> None:
    api = _api()
    import time as _t

    api._refresh_failures = 3
    api._last_login_time = _t.monotonic() - 10_000
    api.async_refresh_token = AsyncMock()  # type: ignore[method-assign]
    api.async_login = AsyncMock(return_value="S")  # type: ignore[method-assign]
    await api._recover_auth()
    api.async_refresh_token.assert_not_awaited()
    api.async_login.assert_awaited_once()


@pytest.mark.asyncio
async def test_recover_auth_adaptive_ttl_reduced_on_early_expiry() -> None:
    api = _api(refresh_token=None)
    import time as _t

    # Token died at 100s (well under half of 900s TTL) → effective TTL shrinks.
    api._token_obtained_at = _t.monotonic() - 100
    api._last_login_time = _t.monotonic() - 10_000
    api.async_login = AsyncMock(return_value="S")  # type: ignore[method-assign]
    before = api._effective_ttl
    await api._recover_auth()
    assert api._effective_ttl < before


@pytest.mark.asyncio
async def test_recover_auth_waits_for_login_cooldown() -> None:
    api = _api(refresh_token=None)
    import time as _t

    api._last_login_time = _t.monotonic()  # just logged in → must wait
    api.async_login = AsyncMock(return_value="S")  # type: ignore[method-assign]
    sleep_mock = AsyncMock()
    with patch("custom_components.ajax.api._base.asyncio.sleep", new=sleep_mock):
        await api._recover_auth()
    sleep_mock.assert_awaited()  # cooldown sleep happened
    api.async_login.assert_awaited_once()


@pytest.mark.asyncio
async def test_recover_auth_login_failure_raises_auth_error() -> None:
    api = _api(refresh_token=None)
    import time as _t

    api._last_login_time = _t.monotonic() - 10_000
    api.async_login = AsyncMock(side_effect=AjaxRestApiError("boom"))  # type: ignore[method-assign]
    with pytest.raises(AjaxRestAuthError, match="Token renewal failed"):
        await api._recover_auth()


# ---------------------------------------------------------------------------
# _request — success, headers, cache headers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_not_logged_in_raises() -> None:
    api = _api(session_token=None)
    with pytest.raises(AjaxRestApiError, match="Not logged in"):
        await api._request("GET", "hubs")


@pytest.mark.asyncio
async def test_request_success_returns_json() -> None:
    api = _api(responses=[_FakeResponse(200, {"ok": True})])
    result = await api._request("GET", "hubs")
    assert result == {"ok": True}
    method, url, kwargs = api.session.calls[0]  # type: ignore[union-attr]
    assert method == "GET"
    assert url.endswith("/hubs")
    assert kwargs["headers"]["X-Session-Token"] == "tok"


@pytest.mark.asyncio
async def test_request_proxy_adds_user_and_cache_headers() -> None:
    api = _api(
        proxy_mode=AUTH_MODE_PROXY_SECURE,
        proxy_url="https://proxy.example.com",
        responses=[
            _FakeResponse(
                200,
                {"ok": True},
                headers={"X-Suggested-Interval": "45", "X-Cache-TTL": "12", "X-Cache": "HIT"},
            )
        ],
    )
    api.bypass_cache_next()  # opens the no-cache window
    await api._request("GET", "hubs")
    _m, _u, kwargs = api.session.calls[0]  # type: ignore[union-attr]
    headers = kwargs["headers"]
    assert headers["X-User-Id"] == "USER123"
    assert headers["X-Cache-Control"] == "no-cache"
    # Cache/interval headers parsed off the response.
    assert api.suggested_interval == 45
    assert api.last_cache_ttl == 12
    assert api.last_cache_hit is True


@pytest.mark.asyncio
async def test_request_proxy_ignores_bad_interval_header() -> None:
    api = _api(
        proxy_mode=AUTH_MODE_PROXY_SECURE,
        proxy_url="https://proxy.example.com",
        responses=[_FakeResponse(200, {"ok": True}, headers={"X-Suggested-Interval": "garbage"})],
    )
    await api._request("GET", "hubs")
    assert api.suggested_interval is None


# ---------------------------------------------------------------------------
# _request — 401 recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_401_recovers_and_retries() -> None:
    api = _api(responses=[_FakeResponse(401), _FakeResponse(200, {"ok": True})])

    async def _recover() -> None:
        api._token_version += 1  # simulate refreshed token

    api._recover_auth = AsyncMock(side_effect=_recover)  # type: ignore[method-assign]
    result = await api._request("GET", "hubs")
    assert result == {"ok": True}
    api._recover_auth.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_401_skips_recovery_when_token_already_refreshed() -> None:
    api = _api(responses=[_FakeResponse(401), _FakeResponse(200, {"ok": True})])

    # Another coroutine bumps the version while our request is in flight.
    original_request = api.session.request  # type: ignore[union-attr]

    def _bump(method: str, url: str, **kwargs: object) -> object:
        api._token_version += 1
        return original_request(method, url, **kwargs)

    api.session.request = _bump  # type: ignore[union-attr,assignment]
    api._recover_auth = AsyncMock()  # type: ignore[method-assign]
    result = await api._request("GET", "hubs")
    assert result == {"ok": True}
    api._recover_auth.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_401_second_time_raises() -> None:
    api = _api(responses=[_FakeResponse(401)])
    with pytest.raises(AjaxRestAuthError, match="Invalid or expired token"):
        await api._request("GET", "hubs", _retry_on_auth_error=False)


@pytest.mark.asyncio
async def test_request_403_raises_access_denied() -> None:
    api = _api(responses=[_FakeResponse(403)])
    with pytest.raises(AjaxRestAuthError, match="Access denied"):
        await api._request("GET", "hubs")


# ---------------------------------------------------------------------------
# _request — 429 / 5xx / network retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_429_retries_then_succeeds() -> None:
    api = _api(
        responses=[
            _FakeResponse(429, headers={"Retry-After": "1"}),
            _FakeResponse(200, {"ok": True}),
        ]
    )
    result = await api._request("GET", "hubs")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_request_429_exhausts_retries_raises() -> None:
    api = _api(responses=[_FakeResponse(429) for _ in range(MAX_RETRIES + 1)])
    with pytest.raises(AjaxRestRateLimitError):
        await api._request("GET", "hubs")


@pytest.mark.asyncio
async def test_request_500_retries_then_succeeds() -> None:
    api = _api(responses=[_FakeResponse(503), _FakeResponse(200, {"ok": True})])
    result = await api._request("GET", "hubs")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_request_500_exhausts_retries_raises() -> None:
    api = _api(responses=[_FakeResponse(500) for _ in range(MAX_RETRIES + 1)])
    with pytest.raises(AjaxRestApiError, match="Server error"):
        await api._request("GET", "hubs")


@pytest.mark.asyncio
async def test_request_client_error_retries_then_succeeds() -> None:
    api = _api(responses=[aiohttp.ClientError("net"), _FakeResponse(200, {"ok": True})])
    result = await api._request("GET", "hubs")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_request_client_error_exhausts_retries_raises_connection_error() -> None:
    api = _api(responses=[aiohttp.ClientError("net") for _ in range(MAX_RETRIES + 1)])
    with pytest.raises(AjaxRestConnectionError, match="Connection failed"):
        await api._request("GET", "hubs")


@pytest.mark.asyncio
async def test_request_timeout_exhausts_retries_raises_connection_error() -> None:
    api = _api(responses=[TimeoutError() for _ in range(MAX_RETRIES + 1)])
    with pytest.raises(AjaxRestConnectionError, match="Request timeout"):
        await api._request("GET", "hubs")


# ---------------------------------------------------------------------------
# _request_no_response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_no_response_not_logged_in_raises() -> None:
    api = _api(session_token=None)
    with pytest.raises(AjaxRestApiError, match="Not logged in"):
        await api._request_no_response("POST", "cmd")


@pytest.mark.asyncio
async def test_request_no_response_204_success() -> None:
    api = _api(responses=[_FakeResponse(204)])
    result = await api._request_no_response("POST", "cmd", {"k": "v"})
    assert result is None
    _m, _u, kwargs = api.session.calls[0]  # type: ignore[union-attr]
    assert kwargs["json"] == {"k": "v"}


@pytest.mark.asyncio
async def test_request_no_response_unexpected_status_raises() -> None:
    api = _api(responses=[_FakeResponse(418, text="teapot")])
    with pytest.raises(AjaxRestApiError, match="API error 418"):
        await api._request_no_response("POST", "cmd")


@pytest.mark.asyncio
async def test_request_no_response_401_recovers_and_retries() -> None:
    api = _api(responses=[_FakeResponse(401), _FakeResponse(204)])

    async def _recover() -> None:
        api._token_version += 1

    api._recover_auth = AsyncMock(side_effect=_recover)  # type: ignore[method-assign]
    await api._request_no_response("POST", "cmd")
    api._recover_auth.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_no_response_401_already_retried_raises() -> None:
    api = _api(responses=[_FakeResponse(401)])
    with pytest.raises(AjaxRestAuthError, match="Invalid or expired token"):
        await api._request_no_response("POST", "cmd", _retry_on_auth_error=False)


@pytest.mark.asyncio
async def test_request_no_response_429_exhausts_retries_raises() -> None:
    api = _api(responses=[_FakeResponse(429) for _ in range(MAX_RETRIES + 1)])
    with pytest.raises(AjaxRestRateLimitError):
        await api._request_no_response("POST", "cmd")


@pytest.mark.asyncio
async def test_request_no_response_500_exhausts_retries_raises() -> None:
    api = _api(responses=[_FakeResponse(500, text="boom") for _ in range(MAX_RETRIES + 1)])
    with pytest.raises(AjaxRestApiError, match="Server error 500"):
        await api._request_no_response("POST", "cmd")


@pytest.mark.asyncio
async def test_request_no_response_500_retries_then_succeeds() -> None:
    api = _api(responses=[_FakeResponse(502), _FakeResponse(204)])
    await api._request_no_response("POST", "cmd")


@pytest.mark.asyncio
async def test_request_no_response_client_error_exhausts_retries() -> None:
    api = _api(responses=[aiohttp.ClientError("x") for _ in range(MAX_RETRIES + 1)])
    with pytest.raises(AjaxRestConnectionError, match="Connection failed"):
        await api._request_no_response("POST", "cmd")


@pytest.mark.asyncio
async def test_request_no_response_timeout_exhausts_retries() -> None:
    api = _api(responses=[TimeoutError() for _ in range(MAX_RETRIES + 1)])
    with pytest.raises(AjaxRestConnectionError, match="Request timeout"):
        await api._request_no_response("POST", "cmd")


@pytest.mark.asyncio
async def test_request_no_response_401_skips_recovery_when_already_refreshed() -> None:
    api = _api(responses=[_FakeResponse(401), _FakeResponse(204)])
    original_request = api.session.request  # type: ignore[union-attr]

    def _bump(method: str, url: str, **kwargs: object) -> object:
        api._token_version += 1
        return original_request(method, url, **kwargs)

    api.session.request = _bump  # type: ignore[union-attr,assignment]
    api._recover_auth = AsyncMock()  # type: ignore[method-assign]
    await api._request_no_response("POST", "cmd")
    api._recover_auth.assert_not_awaited()


# ---------------------------------------------------------------------------
# _get_base_url — hybrid login routing (proxy set but not "secure" mode)
# ---------------------------------------------------------------------------


def test_get_base_url_hybrid_login_routes_through_proxy() -> None:
    api = AjaxRestApi(
        api_key="k",
        email="u@example.com",
        password="p",
        proxy_url="https://proxy.example.com",
        proxy_mode="hybrid",
    )
    # Login goes through the proxy directly (no /api suffix).
    assert api._get_base_url(for_login=True) == "https://proxy.example.com"
    # Data requests after login go direct to Ajax.
    from custom_components.ajax.const import AJAX_REST_API_BASE_URL

    assert api._get_base_url(for_login=False) == AJAX_REST_API_BASE_URL


# ---------------------------------------------------------------------------
# _proactive_token_refresh — re-check after acquiring the auth lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proactive_refresh_rechecks_freshness_after_lock() -> None:
    """A concurrent refresh between the cheap check and the lock must short-circuit."""
    api = _api()
    import time as _t

    api._token_obtained_at = _t.monotonic() - 10_000  # stale at first check
    api.async_refresh_token = AsyncMock()  # type: ignore[method-assign]
    api.async_login = AsyncMock()  # type: ignore[method-assign]

    real_lock = api._auth_lock

    class _BumpingLock:
        async def __aenter__(self) -> object:
            await real_lock.acquire()
            # Simulate another coroutine having just refreshed the token.
            api._token_obtained_at = _t.monotonic()
            return self

        async def __aexit__(self, *exc: object) -> bool:
            real_lock.release()
            return False

    api._auth_lock = _BumpingLock()  # type: ignore[assignment]
    await api._proactive_token_refresh()
    # The post-lock re-check saw a fresh token → no refresh, no login.
    api.async_refresh_token.assert_not_awaited()
    api.async_login.assert_not_awaited()


# ---------------------------------------------------------------------------
# _get_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_creates_session_when_none() -> None:
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    created = object()
    with patch("custom_components.ajax.api._base.aiohttp.ClientSession", return_value=created) as mk:
        session = await api._get_session()
    assert session is created
    assert api._owns_session is True
    mk.assert_called_once()


@pytest.mark.asyncio
async def test_get_session_creates_insecure_connector_when_ssl_disabled() -> None:
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p", verify_ssl=False)
    with (
        patch("custom_components.ajax.api._base.aiohttp.ClientSession", return_value=object()),
        patch("custom_components.ajax.api._base.aiohttp.TCPConnector") as connector,
    ):
        await api._get_session()
    # ssl=False is passed for the insecure path.
    assert connector.call_args.kwargs.get("ssl") is False


@pytest.mark.asyncio
async def test_get_session_reuses_open_session() -> None:
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    existing = _FakeSession([])
    api.session = existing  # type: ignore[assignment]
    session = await api._get_session()
    assert session is existing


# ---------------------------------------------------------------------------
# _request — bypass_cache must survive backoff retries (overhaul 2026-07)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_5xx_retry_preserves_bypass_cache() -> None:
    """The no-cache header must survive a 5xx backoff retry.

    ``bypass_cache_next()`` only opens a 2 s window; a retry lands after the
    backoff, so only the explicit ``bypass_cache`` argument can keep the
    post-arm/disarm freshness guarantee. It used to be dropped on the 5xx /
    ClientError / TimeoutError retry paths.
    """
    api = _api(
        proxy_mode=AUTH_MODE_PROXY_SECURE,
        proxy_url="https://proxy.local",
        responses=[
            _FakeResponse(500, {}),
            _FakeResponse(200, {"ok": True}),
        ],
    )
    result = await api._request("GET", "endpoint", bypass_cache=True)
    assert result == {"ok": True}
    session = api.session
    assert len(session.calls) == 2
    for _method, _url, kwargs in session.calls:
        assert kwargs["headers"].get("X-Cache-Control") == "no-cache"


@pytest.mark.asyncio
async def test_request_timeout_retry_preserves_bypass_cache() -> None:
    """Same guarantee on the TimeoutError retry path."""
    api = _api(
        proxy_mode=AUTH_MODE_PROXY_SECURE,
        proxy_url="https://proxy.local",
        responses=[
            TimeoutError(),
            _FakeResponse(200, {"ok": True}),
        ],
    )
    result = await api._request("GET", "endpoint", bypass_cache=True)
    assert result == {"ok": True}
    session = api.session
    assert len(session.calls) == 2
    assert session.calls[1][2]["headers"].get("X-Cache-Control") == "no-cache"
