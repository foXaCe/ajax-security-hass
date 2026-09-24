"""One-shot 2FA code: a single login, then a session kept alive by refresh.

Users who only have the 6-digit code (no TOTP secret) cannot log in
unattended. These tests pin the contract that makes that work: the code is
sent once, every token change is reported for persistence, a stored session
is resumed through /refresh, and auth recovery never retries a login that
would need a new code — it hands over to reauth instead.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.ajax.api import AjaxRestApi, AjaxRestApiError, AjaxRestAuthError
from tests.test_api_auth_coverage import _FakeResponse, _FakeSession

_LOGIN_OK = {"sessionToken": "S1", "refreshToken": "R1", "userId": "U1"}


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("custom_components.ajax.api._base.asyncio.sleep", new=AsyncMock()):
        yield


def _code_api(responses: list[object], *, totp_code: str | None = "123456") -> AjaxRestApi:
    api = AjaxRestApi(api_key="KEY", email="u@example.com", password="p", totp_code=totp_code)
    api.session = _FakeSession(responses)  # type: ignore[assignment]
    return api


def _resumed_api(responses: list[object]) -> AjaxRestApi:
    """Client in the state setup leaves it in after resuming a stored session."""
    api = _code_api(responses, totp_code=None)
    api.user_id = "U1"
    api.session_token = "S1"
    api.refresh_token = "R1"
    api.totp_required = True
    return api


@pytest.mark.asyncio
async def test_code_is_sent_once_and_marks_totp_required() -> None:
    api = _code_api([_FakeResponse(200, _LOGIN_OK), _FakeResponse(423, {"messageId": "m"})])
    assert api.totp_required is True

    await api.async_login()
    assert api.session.calls[0][2]["json"]["totp"] == "123456"  # type: ignore[union-attr]

    # A second login has no code left to send (it is valid for 30 s only).
    with pytest.raises(AjaxRestAuthError):
        await api.async_login()
    assert "totp" not in api.session.calls[1][2]["json"]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_secret_wins_over_code() -> None:
    api = AjaxRestApi(api_key="KEY", email="u@e.com", password="p", totp_secret="JBSWY3DPEHPK3PXP", totp_code="123456")
    api.session = _FakeSession([_FakeResponse(200, _LOGIN_OK)])  # type: ignore[assignment]
    with patch("pyotp.TOTP.now", return_value="999999"):
        await api.async_login()
    assert api.session.calls[0][2]["json"]["totp"] == "999999"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_login_423_marks_totp_required() -> None:
    api = _code_api([_FakeResponse(423, {"messageId": "m"})], totp_code=None)
    assert api.totp_required is False
    with pytest.raises(AjaxRestAuthError):
        await api.async_login()
    assert api.totp_required is True


@pytest.mark.asyncio
async def test_tokens_reported_after_login_and_refresh() -> None:
    seen: list[tuple[str, str]] = []
    api = _code_api([_FakeResponse(200, _LOGIN_OK), _FakeResponse(200, {"sessionToken": "S2", "refreshToken": "R2"})])
    api.on_tokens_updated = lambda uid, rt: seen.append((uid, rt))

    await api.async_login()
    await api.async_refresh_token()

    assert seen == [("U1", "R1"), ("U1", "R2")]


@pytest.mark.asyncio
async def test_refresh_without_new_refresh_token_keeps_the_old_one() -> None:
    api = _resumed_api([_FakeResponse(200, {"sessionToken": "S2"})])
    await api.async_refresh_token()
    assert api.refresh_token == "R1"
    assert api.session_token == "S2"


@pytest.mark.asyncio
async def test_persistence_failure_does_not_break_auth() -> None:
    api = _code_api([_FakeResponse(200, _LOGIN_OK)])

    def boom(uid: str, rt: str) -> None:
        raise RuntimeError("disk full")

    api.on_tokens_updated = boom
    assert await api.async_login() == "S1"


@pytest.mark.asyncio
async def test_resume_session_uses_refresh_not_login() -> None:
    api = _code_api([_FakeResponse(200, {"sessionToken": "S2", "refreshToken": "R2"})], totp_code=None)

    assert await api.async_resume_session("U1", "R1") == "S2"

    method, url, kwargs = api.session.calls[0]  # type: ignore[union-attr]
    assert url.endswith("/refresh")
    assert kwargs["json"] == {"refreshToken": "R1", "userId": "U1"}
    assert api.totp_required is True
    assert api.refresh_token == "R2"


@pytest.mark.asyncio
async def test_resume_session_rejected_asks_for_a_new_code() -> None:
    api = _code_api([_FakeResponse(401, {})], totp_code=None)
    with pytest.raises(AjaxRestAuthError) as exc:
        await api.async_resume_session("U1", "R1")
    assert exc.value.error_type == "totp_required"


@pytest.mark.asyncio
async def test_resume_session_network_error_stays_transient() -> None:
    api = _code_api([_FakeResponse(503, {"message": "down"})], totp_code=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_resume_session("U1", "R1")


@pytest.mark.asyncio
async def test_recover_auth_refreshes_even_after_repeated_failures() -> None:
    """The 3-failure cut-off that switches to login must not apply here."""
    api = _resumed_api([_FakeResponse(200, {"sessionToken": "S2", "refreshToken": "R2"})])
    api._refresh_failures = 5
    await api._recover_auth()
    assert api.session_token == "S2"
    assert api.session.calls[0][1].endswith("/refresh")  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_recover_auth_rejected_refresh_hands_over_to_reauth_without_login() -> None:
    api = _resumed_api([_FakeResponse(401, {})])
    with pytest.raises(AjaxRestAuthError) as exc:
        await api._recover_auth()
    assert exc.value.error_type == "totp_required"
    # Only the refresh was attempted: no doomed /login.
    assert len(api.session.calls) == 1  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_recover_auth_transient_refresh_error_is_not_an_auth_failure() -> None:
    api = _resumed_api([_FakeResponse(502, {"message": "bad gateway"})])
    with pytest.raises(AjaxRestApiError):
        await api._recover_auth()
    assert len(api.session.calls) == 1  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_recover_auth_without_refresh_token_does_not_login() -> None:
    api = _resumed_api([])
    api.refresh_token = None
    with pytest.raises(AjaxRestAuthError) as exc:
        await api._recover_auth()
    assert exc.value.error_type == "totp_required"
    assert api.session.calls == []  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_proactive_refresh_failure_does_not_login() -> None:
    api = _resumed_api([_FakeResponse(401, {})])
    api._token_obtained_at = 1.0  # ancient: refresh is due
    await api._proactive_token_refresh()
    assert len(api.session.calls) == 1  # type: ignore[union-attr]
    assert api.session.calls[0][1].endswith("/refresh")  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_proactive_refresh_success_reports_tokens() -> None:
    seen: list[tuple[str, str]] = []
    api = _resumed_api([_FakeResponse(200, {"sessionToken": "S2", "refreshToken": "R2"})])
    api.on_tokens_updated = lambda uid, rt: seen.append((uid, rt))
    api._token_obtained_at = 1.0
    await api._proactive_token_refresh()
    assert seen == [("U1", "R2")]


@pytest.mark.asyncio
async def test_without_2fa_recovery_still_falls_back_to_login() -> None:
    """Accounts that need no code keep the historical refresh→login fallback."""
    api = _code_api([_FakeResponse(401, {}), _FakeResponse(200, _LOGIN_OK)], totp_code=None)
    api.user_id, api.session_token, api.refresh_token = "U1", "S0", "R0"
    await api._recover_auth()
    assert api.session.calls[1][1].endswith("/login")  # type: ignore[union-attr]
