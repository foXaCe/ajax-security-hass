"""One-shot 2FA code: config flow capture and setup-time session resume."""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ajax import (
    _async_store_session,
    _reload_relevant_config,
    async_setup_entry,
)
from custom_components.ajax.api import AjaxRestAuthError
from custom_components.ajax.config_flow import AjaxConfigFlow
from custom_components.ajax.const import (
    AUTH_MODE_DIRECT,
    AUTH_MODE_PROXY_SECURE,
    CONF_AJAX_USER_ID,
    CONF_API_KEY,
    CONF_AUTH_MODE,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_PROXY_URL,
    CONF_REFRESH_TOKEN,
    CONF_TOTP_SECRET,
    CONF_VERIFY_SSL,
    DOMAIN,
)
from custom_components.ajax.coordinator import AjaxDataCoordinator
from tests.test_config_flow_coverage import API_PATH, _make_flow, _mock_api

# --------------------------------------------------------------------------- #
# Parsing the 2FA field
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ["123456", " 123 456 ", "123456\n"])
def test_six_digits_is_a_code(raw: str) -> None:
    assert AjaxConfigFlow._parse_totp_input(raw) == (None, "123456")


def test_secret_is_a_secret() -> None:
    assert AjaxConfigFlow._parse_totp_input("jbsw y3dp ehpk 3pxp") == ("JBSWY3DPEHPK3PXP", None)


def test_blank_is_nothing() -> None:
    assert AjaxConfigFlow._parse_totp_input("") == (None, None)
    assert AjaxConfigFlow._parse_totp_input(None) == (None, None)


@pytest.mark.parametrize("raw", ["12345", "1234567", "12a456"])
def test_other_digit_strings_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match="invalid_totp_secret"):
        AjaxConfigFlow._parse_totp_input(raw)


# --------------------------------------------------------------------------- #
# Config flow
# --------------------------------------------------------------------------- #


def _code_api() -> MagicMock:
    api = _mock_api()
    api.user_id = "U1"
    api.refresh_token = "R1"
    return api


async def test_direct_step_with_code_stores_the_session() -> None:
    flow = _make_flow()
    api = _code_api()
    with patch(API_PATH, return_value=api) as api_cls:
        result = await flow.async_step_direct(
            {CONF_API_KEY: "key", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "pw", CONF_TOTP_SECRET: "123456"}
        )
    assert result["type"] == "create_entry"
    assert api_cls.call_args.kwargs["totp_code"] == "123456"
    assert api_cls.call_args.kwargs["totp_secret"] is None
    data = result["data"]
    assert data[CONF_AJAX_USER_ID] == "U1"
    assert data[CONF_REFRESH_TOKEN] == "R1"
    assert CONF_TOTP_SECRET not in data


async def test_direct_step_with_secret_stores_no_session() -> None:
    flow = _make_flow()
    with patch(API_PATH, return_value=_code_api()):
        result = await flow.async_step_direct(
            {
                CONF_API_KEY: "key",
                CONF_EMAIL: "u@e.com",
                CONF_PASSWORD: "pw",
                CONF_TOTP_SECRET: "JBSWY3DPEHPK3PXP",
            }
        )
    assert result["data"][CONF_TOTP_SECRET] == "JBSWY3DPEHPK3PXP"
    assert CONF_REFRESH_TOKEN not in result["data"]


async def test_proxy_step_rejects_a_code() -> None:
    flow = _make_flow()
    flow._auth_mode = AUTH_MODE_PROXY_SECURE
    with patch(API_PATH) as api_cls:
        result = await flow.async_step_proxy(
            {
                CONF_PROXY_URL: "https://proxy",
                CONF_EMAIL: "u@e.com",
                CONF_PASSWORD: "pw",
                CONF_TOTP_SECRET: "123456",
            }
        )
    assert result["errors"]["base"] == "totp_code_proxy"
    api_cls.assert_not_called()


def _reauth_flow(state: ConfigEntryState, **extra: Any) -> tuple[AjaxConfigFlow, MagicMock]:
    flow = _make_flow()
    flow.context["entry_id"] = "e1"
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.state = state
    entry.data = {
        CONF_EMAIL: "u@e.com",
        CONF_AUTH_MODE: AUTH_MODE_DIRECT,
        CONF_API_KEY: "key",
        CONF_PASSWORD: hashlib.sha256(b"pw").hexdigest(),
        **extra,
    }
    flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    flow.hass.config_entries.async_schedule_reload = MagicMock()
    flow.async_update_and_abort = MagicMock(
        side_effect=lambda entry, data_updates: {"type": "abort", "data_updates": data_updates}
    )
    return flow, entry


async def test_reauth_with_code_stores_session_and_reloads_loaded_entry() -> None:
    """Same password + new session only: the listener would ignore it, so reload here."""
    flow, _ = _reauth_flow(ConfigEntryState.LOADED, **{CONF_AJAX_USER_ID: "U0", CONF_REFRESH_TOKEN: "R0"})
    with patch(API_PATH, return_value=_code_api()) as api_cls:
        result = await flow.async_step_reauth_confirm({CONF_PASSWORD: "pw", CONF_TOTP_SECRET: "654321"})
    assert api_cls.call_args.kwargs["totp_code"] == "654321"
    assert result["data_updates"][CONF_REFRESH_TOKEN] == "R1"
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("e1")


async def test_reauth_proxy_entry_rejects_a_code() -> None:
    flow, entry = _reauth_flow(ConfigEntryState.LOADED)
    entry.data[CONF_AUTH_MODE] = AUTH_MODE_PROXY_SECURE
    with patch(API_PATH) as api_cls:
        result = await flow.async_step_reauth_confirm({CONF_PASSWORD: "pw", CONF_TOTP_SECRET: "654321"})
    assert result["errors"]["base"] == "totp_code_proxy"
    api_cls.assert_not_called()


async def test_reauth_totp_required_error_is_shown() -> None:
    flow, _ = _reauth_flow(ConfigEntryState.SETUP_ERROR)
    api = _mock_api(login_exc=AjaxRestAuthError("nope", error_type="totp_required"))
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_reauth_confirm({CONF_PASSWORD: "pw"})
    assert result["errors"]["base"] == "totp_required"


async def test_reconfigure_with_code_stores_session() -> None:
    flow = _make_flow()
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.state = ConfigEntryState.LOADED
    entry.data = {CONF_EMAIL: "u@e.com", CONF_AUTH_MODE: AUTH_MODE_DIRECT, CONF_API_KEY: "key"}
    flow._get_reconfigure_entry = MagicMock(return_value=entry)
    flow.hass.config_entries.async_schedule_reload = MagicMock()
    flow.async_update_and_abort = MagicMock(
        side_effect=lambda entry, data_updates: {"type": "abort", "data_updates": data_updates}
    )
    with patch(API_PATH, return_value=_code_api()):
        result = await flow.async_step_reconfigure(
            {CONF_API_KEY: "key", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "pw", CONF_TOTP_SECRET: "111222"}
        )
    assert result["data_updates"][CONF_REFRESH_TOKEN] == "R1"


# --------------------------------------------------------------------------- #
# Setup: resume, persist, no reload on rotation
# --------------------------------------------------------------------------- #


def _entry(hass: HomeAssistant, **data: object) -> MockConfigEntry:
    base = {
        CONF_EMAIL: "user@example.com",
        CONF_PASSWORD: "deadbeef",
        CONF_AUTH_MODE: AUTH_MODE_DIRECT,
        CONF_API_KEY: "api-key",
        CONF_VERIFY_SSL: True,
    }
    base.update(data)
    entry = MockConfigEntry(domain=DOMAIN, data=base, unique_id="user@example.com")
    entry.add_to_hass(hass)
    return entry


def _patches(hass: HomeAssistant):
    return (
        patch("custom_components.ajax.AjaxRestApi"),
        patch("custom_components.ajax.async_get_clientsession"),
        patch.object(AjaxDataCoordinator, "async_config_entry_first_refresh", new=AsyncMock()),
        patch("custom_components.ajax._async_setup_areas", new=AsyncMock()),
        patch.object(hass.config_entries, "async_forward_entry_setups", new=AsyncMock()),
    )


async def test_setup_resumes_stored_session_instead_of_login(hass: HomeAssistant) -> None:
    entry = _entry(hass, **{CONF_AJAX_USER_ID: "U1", CONF_REFRESH_TOKEN: "R1"})
    p_api, p_sess, p_ref, p_areas, p_fwd = _patches(hass)
    with p_api as api_cls, p_sess, p_ref, p_areas, p_fwd:
        api = api_cls.return_value
        api.async_login = AsyncMock()
        api.async_resume_session = AsyncMock()
        api.async_get_hubs = AsyncMock(return_value=[])
        api.close = AsyncMock()
        assert await async_setup_entry(hass, entry) is True
        api.async_resume_session.assert_awaited_once_with("U1", "R1")
        api.async_login.assert_not_awaited()
        assert api.on_tokens_updated is not None
    await hass.async_block_till_done()


async def test_setup_with_secret_ignores_stored_session(hass: HomeAssistant) -> None:
    entry = _entry(hass, **{CONF_TOTP_SECRET: "JBSWY3DPEHPK3PXP", CONF_AJAX_USER_ID: "U1", CONF_REFRESH_TOKEN: "R1"})
    p_api, p_sess, p_ref, p_areas, p_fwd = _patches(hass)
    with p_api as api_cls, p_sess, p_ref, p_areas, p_fwd:
        api = api_cls.return_value
        api.async_login = AsyncMock()
        api.async_resume_session = AsyncMock()
        api.async_get_hubs = AsyncMock(return_value=[])
        api.close = AsyncMock()
        assert await async_setup_entry(hass, entry) is True
        api.async_login.assert_awaited_once()
        api.async_resume_session.assert_not_awaited()
    await hass.async_block_till_done()


async def test_setup_expired_session_triggers_reauth(hass: HomeAssistant) -> None:
    entry = _entry(hass, **{CONF_AJAX_USER_ID: "U1", CONF_REFRESH_TOKEN: "R1"})
    p_api, p_sess, *_ = _patches(hass)
    with p_api as api_cls, p_sess:
        api = api_cls.return_value
        api.async_resume_session = AsyncMock(side_effect=AjaxRestAuthError("expired", error_type="totp_required"))
        api.close = AsyncMock()
        with pytest.raises(ConfigEntryAuthFailed):
            await async_setup_entry(hass, entry)


async def test_store_session_updates_entry_only_on_change(hass: HomeAssistant) -> None:
    entry = _entry(hass, **{CONF_AJAX_USER_ID: "U1", CONF_REFRESH_TOKEN: "R1"})
    with patch.object(hass.config_entries, "async_update_entry", wraps=hass.config_entries.async_update_entry) as upd:
        _async_store_session(hass, entry, "U1", "R1")
        upd.assert_not_called()
        _async_store_session(hass, entry, "U1", "R2")
        upd.assert_called_once()
    assert entry.data[CONF_REFRESH_TOKEN] == "R2"


async def test_session_rotation_is_not_a_reload_relevant_change(hass: HomeAssistant) -> None:
    entry = _entry(hass, **{CONF_AJAX_USER_ID: "U1", CONF_REFRESH_TOKEN: "R1"})
    before = _reload_relevant_config(entry)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_REFRESH_TOKEN: "R2"})
    assert _reload_relevant_config(entry) == before
