"""Coverage-focused unit tests for ``config_flow.py``.

These tests drive the flow steps in isolation (no HA flow-manager harness).
The HA helper methods that need a running flow manager
(``async_set_unique_id``, ``_abort_if_unique_id_configured``,
``async_create_entry``, ``async_show_form``, ``async_abort`` ...) are patched
on each instance so the step logic can be exercised directly. ``AjaxRestApi``
is patched at the module level to simulate login OK / invalid_auth /
cannot_connect / 2FA-required without any network IO.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ajax.api import (
    AjaxRest2FARequiredError,
    AjaxRestApiError,
    AjaxRestAuthError,
)
from custom_components.ajax.config_flow import AjaxConfigFlow, AjaxOptionsFlow
from custom_components.ajax.const import (
    AUTH_MODE_DIRECT,
    AUTH_MODE_PROXY_SECURE,
    CONF_API_KEY,
    CONF_AUTH_MODE,
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_DISCOVERED_MACS,
    CONF_DOOR_SENSOR_FAST_POLL,
    CONF_EMAIL,
    CONF_ENABLED_SPACES,
    CONF_MONITORED_SPACES,
    CONF_NOTIFICATION_FILTER,
    CONF_PASSWORD,
    CONF_PERSISTENT_NOTIFICATION,
    CONF_PROXY_URL,
    CONF_QUEUE_NAME,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_USERNAME,
    CONF_VERIFY_SSL,
    NOTIFICATION_FILTER_ALL,
)

API_PATH = "custom_components.ajax.config_flow.AjaxRestApi"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_flow(source: str | None = None) -> AjaxConfigFlow:
    """Build a config-flow instance with the HA helper methods stubbed out.

    The stubbed helpers simply record the call and return a sentinel dict so we
    can assert on the flow's branching decisions without a flow manager.
    """
    flow = AjaxConfigFlow()
    flow.hass = MagicMock()
    flow.context = {}
    if source is not None:
        flow.context["source"] = source

    # HA flow-manager helpers — replaced with lightweight stand-ins.
    flow.async_set_unique_id = AsyncMock(return_value=None)
    flow._abort_if_unique_id_configured = MagicMock(return_value=None)
    flow._async_current_entries = MagicMock(return_value=[])

    def _create_entry(*, title: str, data: dict[str, Any]) -> dict[str, Any]:
        return {"type": "create_entry", "title": title, "data": data}

    def _show_form(**kwargs: Any) -> dict[str, Any]:
        return {"type": "form", **kwargs}

    def _show_menu(**kwargs: Any) -> dict[str, Any]:
        return {"type": "menu", **kwargs}

    def _abort(*, reason: str, **kwargs: Any) -> dict[str, Any]:
        return {"type": "abort", "reason": reason}

    flow.async_create_entry = MagicMock(side_effect=_create_entry)
    flow.async_show_form = MagicMock(side_effect=_show_form)
    flow.async_show_menu = MagicMock(side_effect=_show_menu)
    flow.async_abort = MagicMock(side_effect=_abort)
    return flow


def _mock_api(*, login_exc: Exception | None = None, hubs: list[dict[str, Any]] | None = None) -> MagicMock:
    """Build a mocked AjaxRestApi instance."""
    api = MagicMock()
    if login_exc is not None:
        api.async_login = AsyncMock(side_effect=login_exc)
    else:
        api.async_login = AsyncMock(return_value=None)
    api.async_get_hubs = AsyncMock(return_value=hubs if hubs is not None else [])
    api.async_get_space_by_hub = AsyncMock(return_value=None)
    api.async_verify_2fa = AsyncMock(return_value=None)
    api.close = AsyncMock(return_value=None)
    return api


# --------------------------------------------------------------------------- #
# Static / helper methods
# --------------------------------------------------------------------------- #
def test_async_get_options_flow_returns_options_flow() -> None:
    result = AjaxConfigFlow.async_get_options_flow(MagicMock())
    assert isinstance(result, AjaxOptionsFlow)


def test_add_discovered_mac_appends_when_present() -> None:
    flow = _make_flow()
    flow.context["discovered_mac"] = "AA:BB:CC:DD:EE:FF"
    flow._entry_data = {}
    flow._add_discovered_mac_to_entry_data()
    assert flow._entry_data[CONF_DISCOVERED_MACS] == ["AA:BB:CC:DD:EE:FF"]


def test_add_discovered_mac_no_duplicate() -> None:
    flow = _make_flow()
    flow.context["discovered_mac"] = "AA:BB:CC:DD:EE:FF"
    flow._entry_data = {CONF_DISCOVERED_MACS: ["AA:BB:CC:DD:EE:FF"]}
    flow._add_discovered_mac_to_entry_data()
    assert flow._entry_data[CONF_DISCOVERED_MACS] == ["AA:BB:CC:DD:EE:FF"]


def test_add_discovered_mac_noop_without_context() -> None:
    flow = _make_flow()
    flow._entry_data = {}
    flow._add_discovered_mac_to_entry_data()
    assert CONF_DISCOVERED_MACS not in flow._entry_data


# --------------------------------------------------------------------------- #
# async_step_user
# --------------------------------------------------------------------------- #
async def test_step_user_form_default_proxy() -> None:
    flow = _make_flow()
    result = await flow.async_step_user()
    assert result["type"] == "form"
    assert result["step_id"] == "user"


async def test_step_user_routes_direct() -> None:
    flow = _make_flow()
    with patch.object(flow, "async_step_direct", new=AsyncMock(return_value={"type": "form"})) as routed:
        await flow.async_step_user({CONF_AUTH_MODE: AUTH_MODE_DIRECT})
    routed.assert_awaited_once()
    assert flow._auth_mode == AUTH_MODE_DIRECT


async def test_step_user_routes_proxy() -> None:
    flow = _make_flow()
    with patch.object(flow, "async_step_proxy", new=AsyncMock(return_value={"type": "form"})) as routed:
        await flow.async_step_user({CONF_AUTH_MODE: AUTH_MODE_PROXY_SECURE})
    routed.assert_awaited_once()
    assert flow._auth_mode == AUTH_MODE_PROXY_SECURE


# --------------------------------------------------------------------------- #
# async_step_direct
# --------------------------------------------------------------------------- #
async def test_step_direct_form_no_input() -> None:
    flow = _make_flow()
    result = await flow.async_step_direct()
    assert result["type"] == "form"
    assert result["step_id"] == "direct"
    assert result["errors"] == {}


async def test_step_direct_success_single_space() -> None:
    flow = _make_flow()
    api = _mock_api(hubs=[{"hubId": "HUB1", "hubName": "Home"}])
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_direct(
            {
                CONF_API_KEY: "key",
                CONF_EMAIL: "User@Example.com",
                CONF_PASSWORD: "secret",
            }
        )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_AUTH_MODE] == AUTH_MODE_DIRECT
    # Password must be stored hashed, never plain.
    assert result["data"][CONF_PASSWORD] != "secret"
    assert result["data"][CONF_ENABLED_SPACES] == ["HUB1"]
    # unique id is lower-cased email.
    flow.async_set_unique_id.assert_awaited_once_with("user@example.com")


async def test_step_direct_success_with_aws_and_space_name() -> None:
    flow = _make_flow()
    api = _mock_api(hubs=[{"hubId": "HUB1"}])
    api.async_get_space_by_hub = AsyncMock(return_value={"name": "Resolved Name"})
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_direct(
            {
                CONF_API_KEY: "key",
                CONF_EMAIL: "u@e.com",
                CONF_PASSWORD: "secret",
                CONF_AWS_ACCESS_KEY_ID: "AKIA",
                CONF_AWS_SECRET_ACCESS_KEY: "SEC",
                CONF_QUEUE_NAME: "queue",
            }
        )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_AWS_ACCESS_KEY_ID] == "AKIA"
    assert result["data"][CONF_AWS_SECRET_ACCESS_KEY] == "SEC"
    assert result["data"][CONF_QUEUE_NAME] == "queue"
    assert flow._spaces == [{"id": "HUB1", "name": "Resolved Name"}]


async def test_step_direct_space_name_resolution_fails_gracefully() -> None:
    flow = _make_flow()
    api = _mock_api(hubs=[{"hubId": "HUB1", "hubName": "Fallback"}])
    api.async_get_space_by_hub = AsyncMock(side_effect=AjaxRestApiError("boom"))
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_direct({CONF_API_KEY: "key", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "secret"})
    assert result["type"] == "create_entry"
    assert flow._spaces == [{"id": "HUB1", "name": "Fallback"}]


async def test_step_direct_no_spaces_leaves_key_unset() -> None:
    flow = _make_flow()
    api = _mock_api(hubs=[])
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_direct({CONF_API_KEY: "key", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "secret"})
    assert result["type"] == "create_entry"
    assert CONF_ENABLED_SPACES not in result["data"]


async def test_step_direct_multiple_spaces_goes_to_select() -> None:
    flow = _make_flow()
    api = _mock_api(hubs=[{"hubId": "H1", "hubName": "A"}, {"hubId": "H2", "hubName": "B"}])
    with (
        patch(API_PATH, return_value=api),
        patch.object(
            flow, "async_step_select_spaces", new=AsyncMock(return_value={"type": "form", "step_id": "select_spaces"})
        ) as sel,
    ):
        result = await flow.async_step_direct({CONF_API_KEY: "key", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "secret"})
    sel.assert_awaited_once()
    assert result["step_id"] == "select_spaces"


async def test_step_direct_2fa_required() -> None:
    flow = _make_flow()
    api = _mock_api(login_exc=AjaxRest2FARequiredError("req-123"))
    with (
        patch(API_PATH, return_value=api),
        patch.object(flow, "async_step_2fa", new=AsyncMock(return_value={"type": "form", "step_id": "2fa"})) as twofa,
    ):
        result = await flow.async_step_direct({CONF_API_KEY: "key", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "secret"})
    twofa.assert_awaited_once()
    assert flow._request_id == "req-123"
    assert result["step_id"] == "2fa"


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        ("invalid_api_key", "invalid_api_key"),
        ("invalid_password", "invalid_password"),
        ("invalid_account_type", "invalid_account_type"),
        ("generic", "invalid_auth"),
        ("totally_unknown_type", "invalid_auth"),
    ],
)
async def test_step_direct_auth_error_mapping(error_type: str, expected: str) -> None:
    flow = _make_flow()
    api = _mock_api(login_exc=AjaxRestAuthError("nope", error_type=error_type))
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_direct({CONF_API_KEY: "key", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "secret"})
    assert result["type"] == "form"
    assert result["errors"]["base"] == expected
    api.close.assert_awaited()


async def test_step_direct_cannot_connect() -> None:
    flow = _make_flow()
    api = _mock_api(login_exc=AjaxRestApiError("network down"))
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_direct({CONF_API_KEY: "key", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "secret"})
    assert result["errors"]["base"] == "cannot_connect"


async def test_step_direct_unknown_exception() -> None:
    flow = _make_flow()
    api = _mock_api(login_exc=ValueError("weird"))
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_direct({CONF_API_KEY: "key", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "secret"})
    assert result["errors"]["base"] == "unknown"


# --------------------------------------------------------------------------- #
# async_step_proxy
# --------------------------------------------------------------------------- #
async def test_step_proxy_form_no_input() -> None:
    flow = _make_flow()
    result = await flow.async_step_proxy()
    assert result["type"] == "form"
    assert result["step_id"] == "proxy"


async def test_step_proxy_success_single_space_strips_url() -> None:
    flow = _make_flow()
    flow._auth_mode = AUTH_MODE_PROXY_SECURE
    api = _mock_api(hubs=[{"hubId": "HUB1", "hubName": "Home"}])
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_proxy(
            {
                CONF_PROXY_URL: "https://proxy.example.com/",
                CONF_EMAIL: "u@e.com",
                CONF_PASSWORD: "secret",
                CONF_VERIFY_SSL: False,
            }
        )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PROXY_URL] == "https://proxy.example.com"
    assert result["data"][CONF_VERIFY_SSL] is False
    assert result["data"][CONF_ENABLED_SPACES] == ["HUB1"]


async def test_step_proxy_hubs_discovery_fails_continues() -> None:
    flow = _make_flow()
    flow._auth_mode = AUTH_MODE_PROXY_SECURE
    api = _mock_api()
    api.async_get_hubs = AsyncMock(side_effect=AjaxRestApiError("no endpoint"))
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_proxy(
            {CONF_PROXY_URL: "https://proxy/", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "secret"}
        )
    assert result["type"] == "create_entry"
    assert flow._spaces == []
    assert CONF_ENABLED_SPACES not in result["data"]


async def test_step_proxy_space_name_resolved() -> None:
    flow = _make_flow()
    flow._auth_mode = AUTH_MODE_PROXY_SECURE
    api = _mock_api(hubs=[{"hubId": "HUB1", "hubName": "Fallback"}])
    api.async_get_space_by_hub = AsyncMock(return_value={"name": "Resolved Proxy"})
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_proxy(
            {CONF_PROXY_URL: "https://proxy", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "secret"}
        )
    assert result["type"] == "create_entry"
    assert flow._spaces == [{"id": "HUB1", "name": "Resolved Proxy"}]


async def test_step_proxy_space_name_resolution_fails() -> None:
    flow = _make_flow()
    flow._auth_mode = AUTH_MODE_PROXY_SECURE
    api = _mock_api(hubs=[{"hubId": "HUB1"}])
    api.async_get_space_by_hub = AsyncMock(side_effect=AjaxRestApiError("boom"))
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_proxy(
            {CONF_PROXY_URL: "https://proxy", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "secret"}
        )
    assert result["type"] == "create_entry"
    # Fallback hub name format "Hub <first6>".
    assert flow._spaces[0]["name"].startswith("Hub ")


async def test_step_proxy_multiple_spaces() -> None:
    flow = _make_flow()
    flow._auth_mode = AUTH_MODE_PROXY_SECURE
    api = _mock_api(hubs=[{"hubId": "H1", "hubName": "A"}, {"hubId": "H2", "hubName": "B"}])
    with (
        patch(API_PATH, return_value=api),
        patch.object(
            flow, "async_step_select_spaces", new=AsyncMock(return_value={"type": "form", "step_id": "select_spaces"})
        ) as sel,
    ):
        await flow.async_step_proxy({CONF_PROXY_URL: "https://proxy", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "secret"})
    sel.assert_awaited_once()


async def test_step_proxy_2fa_required() -> None:
    flow = _make_flow()
    flow._auth_mode = AUTH_MODE_PROXY_SECURE
    api = _mock_api(login_exc=AjaxRest2FARequiredError("rid"))
    with (
        patch(API_PATH, return_value=api),
        patch.object(flow, "async_step_2fa", new=AsyncMock(return_value={"type": "form"})) as twofa,
    ):
        await flow.async_step_proxy({CONF_PROXY_URL: "https://proxy", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "secret"})
    twofa.assert_awaited_once()
    assert flow._request_id == "rid"


async def test_step_proxy_auth_error() -> None:
    flow = _make_flow()
    flow._auth_mode = AUTH_MODE_PROXY_SECURE
    api = _mock_api(login_exc=AjaxRestAuthError("nope", error_type="invalid_password"))
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_proxy(
            {CONF_PROXY_URL: "https://proxy", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "secret"}
        )
    assert result["errors"]["base"] == "invalid_password"


async def test_step_proxy_cannot_connect() -> None:
    flow = _make_flow()
    flow._auth_mode = AUTH_MODE_PROXY_SECURE
    api = _mock_api(login_exc=AjaxRestApiError("down"))
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_proxy(
            {CONF_PROXY_URL: "https://proxy", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "secret"}
        )
    assert result["errors"]["base"] == "cannot_connect"


async def test_step_proxy_unknown_exception() -> None:
    flow = _make_flow()
    flow._auth_mode = AUTH_MODE_PROXY_SECURE
    api = _mock_api(login_exc=RuntimeError("weird"))
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_proxy(
            {CONF_PROXY_URL: "https://proxy", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "secret"}
        )
    assert result["errors"]["base"] == "unknown"


# --------------------------------------------------------------------------- #
# async_step_2fa
# --------------------------------------------------------------------------- #
async def test_step_2fa_form_no_input() -> None:
    flow = _make_flow()
    flow._user_input = {CONF_EMAIL: "u@e.com"}
    result = await flow.async_step_2fa()
    assert result["type"] == "form"
    assert result["step_id"] == "2fa"
    assert result["description_placeholders"]["email"] == "u@e.com"


async def test_step_2fa_not_initialized_aborts() -> None:
    flow = _make_flow()
    flow._api = None
    flow._request_id = None
    result = await flow.async_step_2fa({"code": "123456"})
    assert result["type"] == "abort"
    assert result["reason"] == "api_not_initialized"


async def test_step_2fa_success_direct_single_space() -> None:
    flow = _make_flow()
    flow._api = _mock_api(hubs=[{"hubId": "HUB1", "hubName": "Home"}])
    flow._request_id = "rid"
    flow._user_input = {
        CONF_AUTH_MODE: AUTH_MODE_DIRECT,
        CONF_API_KEY: "key",
        CONF_EMAIL: "u@e.com",
        CONF_PASSWORD: "secret",
        CONF_AWS_ACCESS_KEY_ID: "AKIA",
        CONF_AWS_SECRET_ACCESS_KEY: "SEC",
        CONF_QUEUE_NAME: "q",
    }
    result = await flow.async_step_2fa({"code": " 123456 "})
    assert result["type"] == "create_entry"
    assert result["data"][CONF_API_KEY] == "key"
    assert result["data"][CONF_AWS_ACCESS_KEY_ID] == "AKIA"
    assert result["data"][CONF_ENABLED_SPACES] == ["HUB1"]
    flow._api.async_verify_2fa.assert_awaited_once_with("rid", "123456")


async def test_step_2fa_success_proxy_mode() -> None:
    flow = _make_flow()
    flow._api = _mock_api(hubs=[])
    flow._request_id = "rid"
    flow._user_input = {
        CONF_AUTH_MODE: AUTH_MODE_PROXY_SECURE,
        CONF_EMAIL: "u@e.com",
        CONF_PASSWORD: "secret",
        CONF_PROXY_URL: "https://proxy",
        CONF_VERIFY_SSL: False,
    }
    result = await flow.async_step_2fa({"code": "123456"})
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PROXY_URL] == "https://proxy"
    assert result["data"][CONF_VERIFY_SSL] is False
    assert CONF_ENABLED_SPACES not in result["data"]


async def test_step_2fa_proxy_hubs_discovery_fails_continues() -> None:
    flow = _make_flow()
    api = _mock_api()
    api.async_get_hubs = AsyncMock(side_effect=AjaxRestApiError("no endpoint"))
    flow._api = api
    flow._request_id = "rid"
    flow._user_input = {
        CONF_AUTH_MODE: AUTH_MODE_PROXY_SECURE,
        CONF_EMAIL: "u@e.com",
        CONF_PASSWORD: "secret",
        CONF_PROXY_URL: "https://proxy",
    }
    result = await flow.async_step_2fa({"code": "123456"})
    assert result["type"] == "create_entry"
    assert flow._spaces == []


async def test_step_2fa_direct_hubs_discovery_fails_is_error() -> None:
    flow = _make_flow()
    api = _mock_api()
    api.async_get_hubs = AsyncMock(side_effect=AjaxRestApiError("boom"))
    flow._api = api
    flow._request_id = "rid"
    flow._user_input = {
        CONF_AUTH_MODE: AUTH_MODE_DIRECT,
        CONF_API_KEY: "key",
        CONF_EMAIL: "u@e.com",
        CONF_PASSWORD: "secret",
    }
    result = await flow.async_step_2fa({"code": "123456"})
    # Re-raised AjaxRestApiError -> cannot_connect form.
    assert result["type"] == "form"
    assert result["errors"]["base"] == "cannot_connect"


async def test_step_2fa_multiple_spaces() -> None:
    flow = _make_flow()
    flow._api = _mock_api(hubs=[{"hubId": "H1", "hubName": "A"}, {"hubId": "H2", "hubName": "B"}])
    flow._request_id = "rid"
    flow._user_input = {
        CONF_AUTH_MODE: AUTH_MODE_DIRECT,
        CONF_API_KEY: "key",
        CONF_EMAIL: "u@e.com",
        CONF_PASSWORD: "secret",
    }
    with patch.object(
        flow, "async_step_select_spaces", new=AsyncMock(return_value={"type": "form", "step_id": "select_spaces"})
    ) as sel:
        await flow.async_step_2fa({"code": "123456"})
    sel.assert_awaited_once()


async def test_step_2fa_space_name_resolved() -> None:
    flow = _make_flow()
    api = _mock_api(hubs=[{"hubId": "HUB1", "hubName": "Fallback"}])
    api.async_get_space_by_hub = AsyncMock(return_value={"name": "Resolved 2FA"})
    flow._api = api
    flow._request_id = "rid"
    flow._user_input = {
        CONF_AUTH_MODE: AUTH_MODE_DIRECT,
        CONF_API_KEY: "key",
        CONF_EMAIL: "u@e.com",
        CONF_PASSWORD: "secret",
    }
    result = await flow.async_step_2fa({"code": "123456"})
    assert result["type"] == "create_entry"
    assert flow._spaces == [{"id": "HUB1", "name": "Resolved 2FA"}]


async def test_step_2fa_space_name_resolution_fails() -> None:
    flow = _make_flow()
    api = _mock_api(hubs=[{"hubId": "HUB1"}])
    api.async_get_space_by_hub = AsyncMock(side_effect=AjaxRestApiError("boom"))
    flow._api = api
    flow._request_id = "rid"
    flow._user_input = {
        CONF_AUTH_MODE: AUTH_MODE_DIRECT,
        CONF_API_KEY: "key",
        CONF_EMAIL: "u@e.com",
        CONF_PASSWORD: "secret",
    }
    result = await flow.async_step_2fa({"code": "123456"})
    assert result["type"] == "create_entry"
    assert flow._spaces[0]["name"].startswith("Hub ")


async def test_step_2fa_reauth_success() -> None:
    flow = _make_flow(source="reauth")
    flow.context["entry_id"] = "entry-1"
    flow._api = _mock_api(hubs=[])
    flow._request_id = "rid"
    flow._user_input = {
        CONF_AUTH_MODE: AUTH_MODE_DIRECT,
        CONF_API_KEY: "key",
        CONF_EMAIL: "u@e.com",
        CONF_PASSWORD: "secret",
    }
    reauth_entry = MagicMock()
    reauth_entry.data = {CONF_EMAIL: "u@e.com"}
    reauth_entry.entry_id = "entry-1"
    flow.hass.config_entries.async_get_entry = MagicMock(return_value=reauth_entry)
    flow.hass.config_entries.async_update_entry = MagicMock()
    flow.hass.config_entries.async_schedule_reload = MagicMock()

    result = await flow.async_step_2fa({"code": "123456"})
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    # async_update_and_abort → update only; the update listener owns the reload.
    flow.hass.config_entries.async_update_entry.assert_called_once()
    flow.hass.config_entries.async_schedule_reload.assert_not_called()


async def test_step_2fa_invalid_code() -> None:
    flow = _make_flow()
    api = _mock_api()
    api.async_verify_2fa = AsyncMock(side_effect=AjaxRestAuthError("bad"))
    flow._api = api
    flow._request_id = "rid"
    flow._user_input = {CONF_EMAIL: "u@e.com"}
    result = await flow.async_step_2fa({"code": "000000"})
    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_2fa"


async def test_step_2fa_api_error_cannot_connect() -> None:
    flow = _make_flow()
    api = _mock_api()
    api.async_verify_2fa = AsyncMock(side_effect=AjaxRestApiError("down"))
    flow._api = api
    flow._request_id = "rid"
    flow._user_input = {CONF_EMAIL: "u@e.com"}
    result = await flow.async_step_2fa({"code": "000000"})
    assert result["errors"]["base"] == "cannot_connect"


async def test_step_2fa_unknown_exception() -> None:
    flow = _make_flow()
    api = _mock_api()
    api.async_verify_2fa = AsyncMock(side_effect=RuntimeError("weird"))
    flow._api = api
    flow._request_id = "rid"
    flow._user_input = {CONF_EMAIL: "u@e.com"}
    result = await flow.async_step_2fa({"code": "000000"})
    assert result["errors"]["base"] == "unknown"


# --------------------------------------------------------------------------- #
# async_step_select_spaces
# --------------------------------------------------------------------------- #
async def test_step_select_spaces_form() -> None:
    flow = _make_flow()
    flow._spaces = [{"id": "H1", "name": "A"}, {"id": "H2", "name": "B"}]
    result = await flow.async_step_select_spaces()
    assert result["type"] == "form"
    assert result["step_id"] == "select_spaces"
    assert result["description_placeholders"]["space_count"] == "2"


async def test_step_select_spaces_no_selection_error() -> None:
    flow = _make_flow()
    flow._spaces = [{"id": "H1", "name": "A"}]
    result = await flow.async_step_select_spaces({CONF_ENABLED_SPACES: []})
    assert result["type"] == "form"
    assert result["errors"]["base"] == "no_spaces_selected"


async def test_step_select_spaces_creates_entry() -> None:
    flow = _make_flow()
    flow._spaces = [{"id": "H1", "name": "A"}, {"id": "H2", "name": "B"}]
    flow._entry_data = {CONF_EMAIL: "u@e.com"}
    result = await flow.async_step_select_spaces({CONF_ENABLED_SPACES: ["H1"]})
    assert result["type"] == "create_entry"
    assert result["data"][CONF_ENABLED_SPACES] == ["H1"]
    assert result["title"] == "Ajax - u@e.com"


# --------------------------------------------------------------------------- #
# async_step_dhcp / dhcp_confirm
# --------------------------------------------------------------------------- #
def _dhcp_info(mac: str = "AA:BB:CC:DD:EE:FF", hostname: str = "ajax-hub") -> Any:
    info = MagicMock()
    info.macaddress = mac
    info.hostname = hostname
    return info


async def test_step_dhcp_already_configured_aborts() -> None:
    flow = _make_flow()
    existing = MagicMock()
    existing.data = {CONF_DISCOVERED_MACS: ["AA:BB:CC:DD:EE:FF"]}
    flow._async_current_entries = MagicMock(return_value=[existing])
    result = await flow.async_step_dhcp(_dhcp_info())
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_step_dhcp_no_existing_goes_to_user() -> None:
    flow = _make_flow()
    flow._async_current_entries = MagicMock(return_value=[])
    with patch.object(flow, "async_step_user", new=AsyncMock(return_value={"type": "form", "step_id": "user"})) as user:
        result = await flow.async_step_dhcp(_dhcp_info())
    user.assert_awaited_once()
    assert result["step_id"] == "user"
    assert flow.context["discovered_mac"] == "AA:BB:CC:DD:EE:FF"


async def test_step_dhcp_existing_goes_to_confirm() -> None:
    flow = _make_flow()
    existing = MagicMock()
    existing.data = {}
    flow._async_current_entries = MagicMock(return_value=[existing])
    with patch.object(
        flow, "async_step_dhcp_confirm", new=AsyncMock(return_value={"type": "form", "step_id": "dhcp_confirm"})
    ) as conf:
        result = await flow.async_step_dhcp(_dhcp_info(hostname=""))
    conf.assert_awaited_once()
    assert result["step_id"] == "dhcp_confirm"


async def test_step_dhcp_confirm_form() -> None:
    flow = _make_flow()
    flow.context["discovered_mac"] = "AA:BB:CC:DD:EE:FF"
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {CONF_EMAIL: "u@e.com"}
    flow._async_current_entries = MagicMock(return_value=[entry])
    result = await flow.async_step_dhcp_confirm()
    assert result["type"] == "form"
    assert result["step_id"] == "dhcp_confirm"
    assert result["description_placeholders"]["mac"] == "AA:BB:CC:DD:EE:FF"


async def test_step_dhcp_confirm_new_goes_to_user() -> None:
    flow = _make_flow()
    flow.context["discovered_mac"] = "AA:BB:CC:DD:EE:FF"
    with patch.object(flow, "async_step_user", new=AsyncMock(return_value={"type": "form", "step_id": "user"})) as user:
        result = await flow.async_step_dhcp_confirm({"action": "new"})
    user.assert_awaited_once()
    assert result["step_id"] == "user"


async def test_step_dhcp_confirm_associate_existing() -> None:
    flow = _make_flow()
    flow.context["discovered_mac"] = "AA:BB:CC:DD:EE:FF"
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {CONF_EMAIL: "u@e.com"}
    flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    flow.hass.config_entries.async_update_entry = MagicMock()
    result = await flow.async_step_dhcp_confirm({"action": "e1"})
    assert result["type"] == "abort"
    assert result["reason"] == "hub_associated"
    flow.hass.config_entries.async_update_entry.assert_called_once()


async def test_step_dhcp_confirm_associate_already_present_mac() -> None:
    flow = _make_flow()
    flow.context["discovered_mac"] = "AA:BB:CC:DD:EE:FF"
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {CONF_DISCOVERED_MACS: ["AA:BB:CC:DD:EE:FF"], CONF_EMAIL: "u@e.com"}
    flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    flow.hass.config_entries.async_update_entry = MagicMock()
    result = await flow.async_step_dhcp_confirm({"action": "e1"})
    assert result["reason"] == "hub_associated"
    # MAC already present -> no entry update.
    flow.hass.config_entries.async_update_entry.assert_not_called()


async def test_step_dhcp_confirm_entry_not_found() -> None:
    flow = _make_flow()
    flow.context["discovered_mac"] = "AA:BB:CC:DD:EE:FF"
    flow.hass.config_entries.async_get_entry = MagicMock(return_value=None)
    flow._async_current_entries = MagicMock(return_value=[])
    result = await flow.async_step_dhcp_confirm({"action": "missing"})
    assert result["type"] == "form"
    assert result["errors"]["base"] == "entry_not_found"


# --------------------------------------------------------------------------- #
# async_step_reauth / reauth_confirm
# --------------------------------------------------------------------------- #
async def test_step_reauth_routes_to_confirm() -> None:
    flow = _make_flow()
    with patch.object(
        flow, "async_step_reauth_confirm", new=AsyncMock(return_value={"type": "form", "step_id": "reauth_confirm"})
    ) as conf:
        await flow.async_step_reauth({CONF_EMAIL: "u@e.com"})
    conf.assert_awaited_once()
    assert flow._user_input == {CONF_EMAIL: "u@e.com"}


async def test_step_reauth_confirm_no_entry_aborts() -> None:
    flow = _make_flow()
    flow.context["entry_id"] = "missing"
    flow.hass.config_entries.async_get_entry = MagicMock(return_value=None)
    result = await flow.async_step_reauth_confirm()
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_failed"


async def test_step_reauth_confirm_form() -> None:
    flow = _make_flow()
    flow.context["entry_id"] = "e1"
    entry = MagicMock()
    entry.data = {CONF_EMAIL: "u@e.com", CONF_AUTH_MODE: AUTH_MODE_DIRECT}
    flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    result = await flow.async_step_reauth_confirm()
    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["email"] == "u@e.com"


async def test_step_reauth_confirm_direct_success() -> None:
    flow = _make_flow()
    flow.context["entry_id"] = "e1"
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {CONF_EMAIL: "u@e.com", CONF_AUTH_MODE: AUTH_MODE_DIRECT, CONF_API_KEY: "key"}
    flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    flow.hass.config_entries.async_update_entry = MagicMock()
    flow.hass.config_entries.async_reload = AsyncMock()
    api = _mock_api()
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_reauth_confirm({CONF_PASSWORD: "newpass"})
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    flow.hass.config_entries.async_update_entry.assert_called_once()


async def test_step_reauth_confirm_same_password_schedules_reload() -> None:
    """Re-entering the SAME password (expired-token case) must retry the setup.

    Unchanged data does not fire the update listener, so the flow schedules
    the reload explicitly — otherwise the entry stays in its failed state.
    """
    import hashlib

    same_hash = hashlib.sha256(b"samepass").hexdigest()
    flow = _make_flow()
    flow.context["entry_id"] = "e1"
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {
        CONF_EMAIL: "u@e.com",
        CONF_AUTH_MODE: AUTH_MODE_DIRECT,
        CONF_API_KEY: "key",
        CONF_PASSWORD: same_hash,
    }
    flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    flow.hass.config_entries.async_update_entry = MagicMock()
    flow.hass.config_entries.async_schedule_reload = MagicMock()
    api = _mock_api()
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_reauth_confirm({CONF_PASSWORD: "samepass"})
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("e1")


async def test_step_reauth_confirm_proxy_success() -> None:
    flow = _make_flow()
    flow.context["entry_id"] = "e1"
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {
        CONF_EMAIL: "u@e.com",
        CONF_AUTH_MODE: AUTH_MODE_PROXY_SECURE,
        CONF_PROXY_URL: "https://proxy",
        CONF_VERIFY_SSL: True,
    }
    flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    flow.hass.config_entries.async_update_entry = MagicMock()
    flow.hass.config_entries.async_reload = AsyncMock()
    api = _mock_api()
    with patch(API_PATH, return_value=api) as api_cls:
        result = await flow.async_step_reauth_confirm({CONF_PASSWORD: "newpass"})
    assert result["reason"] == "reauth_successful"
    # Proxy client created with proxy_mode set.
    assert api_cls.call_args.kwargs["proxy_mode"] == AUTH_MODE_PROXY_SECURE


async def test_step_reauth_confirm_2fa_required() -> None:
    flow = _make_flow()
    flow.context["entry_id"] = "e1"
    entry = MagicMock()
    entry.data = {CONF_EMAIL: "u@e.com", CONF_AUTH_MODE: AUTH_MODE_DIRECT}
    flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    api = _mock_api(login_exc=AjaxRest2FARequiredError("rid"))
    with (
        patch(API_PATH, return_value=api),
        patch.object(flow, "async_step_2fa", new=AsyncMock(return_value={"type": "form"})) as twofa,
    ):
        await flow.async_step_reauth_confirm({CONF_PASSWORD: "newpass"})
    twofa.assert_awaited_once()
    assert flow._request_id == "rid"
    assert flow._user_input[CONF_PASSWORD] == "newpass"


async def test_step_reauth_confirm_auth_error() -> None:
    flow = _make_flow()
    flow.context["entry_id"] = "e1"
    entry = MagicMock()
    entry.data = {CONF_EMAIL: "u@e.com", CONF_AUTH_MODE: AUTH_MODE_DIRECT}
    flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    api = _mock_api(login_exc=AjaxRestAuthError("bad", error_type="invalid_password"))
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_reauth_confirm({CONF_PASSWORD: "newpass"})
    assert result["errors"]["base"] == "invalid_password"


async def test_step_reauth_confirm_cannot_connect() -> None:
    flow = _make_flow()
    flow.context["entry_id"] = "e1"
    entry = MagicMock()
    entry.data = {CONF_EMAIL: "u@e.com", CONF_AUTH_MODE: AUTH_MODE_DIRECT}
    flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    api = _mock_api(login_exc=AjaxRestApiError("down"))
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_reauth_confirm({CONF_PASSWORD: "newpass"})
    assert result["errors"]["base"] == "cannot_connect"


async def test_step_reauth_confirm_unknown_exception() -> None:
    flow = _make_flow()
    flow.context["entry_id"] = "e1"
    entry = MagicMock()
    entry.data = {CONF_EMAIL: "u@e.com", CONF_AUTH_MODE: AUTH_MODE_DIRECT}
    flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    api = _mock_api(login_exc=RuntimeError("weird"))
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_reauth_confirm({CONF_PASSWORD: "newpass"})
    assert result["errors"]["base"] == "unknown"


# --------------------------------------------------------------------------- #
# async_step_reconfigure
# --------------------------------------------------------------------------- #
def _reconfigure_flow(entry_data: dict[str, Any]) -> AjaxConfigFlow:
    flow = _make_flow()
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = entry_data
    flow._get_reconfigure_entry = MagicMock(return_value=entry)
    flow.async_update_and_abort = MagicMock(
        side_effect=lambda *a, **kw: {
            "type": "abort",
            "reason": "reconfigure_successful",
            "data": kw.get("data_updates"),
        }
    )
    return flow


async def test_step_reconfigure_direct_form() -> None:
    flow = _reconfigure_flow({CONF_AUTH_MODE: AUTH_MODE_DIRECT, CONF_API_KEY: "k", CONF_EMAIL: "u@e.com"})
    result = await flow.async_step_reconfigure()
    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"


async def test_step_reconfigure_proxy_form() -> None:
    flow = _reconfigure_flow(
        {CONF_AUTH_MODE: AUTH_MODE_PROXY_SECURE, CONF_PROXY_URL: "https://proxy", CONF_EMAIL: "u@e.com"}
    )
    result = await flow.async_step_reconfigure()
    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"


async def test_step_reconfigure_direct_success() -> None:
    flow = _reconfigure_flow({CONF_AUTH_MODE: AUTH_MODE_DIRECT, CONF_API_KEY: "oldkey", CONF_EMAIL: "old@e.com"})
    api = _mock_api()
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_reconfigure(
            {CONF_API_KEY: "newkey", CONF_EMAIL: "new@e.com", CONF_PASSWORD: "newpass"}
        )
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert result["data"][CONF_API_KEY] == "newkey"
    assert result["data"][CONF_EMAIL] == "new@e.com"
    assert result["data"][CONF_PASSWORD] != "newpass"


async def test_step_reconfigure_proxy_success_strips_url() -> None:
    flow = _reconfigure_flow(
        {
            CONF_AUTH_MODE: AUTH_MODE_PROXY_SECURE,
            CONF_PROXY_URL: "https://old",
            CONF_EMAIL: "u@e.com",
            CONF_VERIFY_SSL: True,
        }
    )
    api = _mock_api()
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_reconfigure(
            {
                CONF_PROXY_URL: "https://new/",
                CONF_EMAIL: "u@e.com",
                CONF_PASSWORD: "newpass",
                CONF_VERIFY_SSL: False,
            }
        )
    assert result["reason"] == "reconfigure_successful"
    assert result["data"][CONF_PROXY_URL] == "https://new"
    assert result["data"][CONF_VERIFY_SSL] is False


async def test_step_reconfigure_auth_error() -> None:
    flow = _reconfigure_flow({CONF_AUTH_MODE: AUTH_MODE_DIRECT, CONF_API_KEY: "k", CONF_EMAIL: "u@e.com"})
    api = _mock_api(login_exc=AjaxRestAuthError("bad"))
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_reconfigure({CONF_API_KEY: "k", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "newpass"})
    assert result["errors"]["base"] == "invalid_auth"


async def test_step_reconfigure_cannot_connect() -> None:
    flow = _reconfigure_flow({CONF_AUTH_MODE: AUTH_MODE_DIRECT, CONF_API_KEY: "k", CONF_EMAIL: "u@e.com"})
    api = _mock_api(login_exc=AjaxRestApiError("down"))
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_reconfigure({CONF_API_KEY: "k", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "newpass"})
    assert result["errors"]["base"] == "cannot_connect"


async def test_step_reconfigure_unknown_exception() -> None:
    flow = _reconfigure_flow({CONF_AUTH_MODE: AUTH_MODE_DIRECT, CONF_API_KEY: "k", CONF_EMAIL: "u@e.com"})
    api = _mock_api(login_exc=RuntimeError("weird"))
    with patch(API_PATH, return_value=api):
        result = await flow.async_step_reconfigure({CONF_API_KEY: "k", CONF_EMAIL: "u@e.com", CONF_PASSWORD: "newpass"})
    assert result["errors"]["base"] == "unknown"


# --------------------------------------------------------------------------- #
# AjaxOptionsFlow
# --------------------------------------------------------------------------- #
class _FakeEntry:
    """Minimal config-entry stand-in for the options flow tests.

    Unlike a bare ``MagicMock`` this can faithfully model the
    ``entry.runtime_data`` AttributeError branch without mutating the shared
    ``MagicMock`` class.
    """

    def __init__(self, *, entry_id: str, data: dict[str, Any], options: dict[str, Any], coordinator: Any) -> None:
        self.entry_id = entry_id
        self.data = data
        self.options = options
        self._coordinator = coordinator
        self._has_coordinator = coordinator is not None

    @property
    def runtime_data(self) -> Any:
        if not self._has_coordinator:
            raise AttributeError("no runtime_data")
        return self._coordinator


def _make_options_flow(
    *,
    data: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    coordinator: Any = None,
) -> AjaxOptionsFlow:
    flow = AjaxOptionsFlow()
    flow.hass = MagicMock()
    entry = _FakeEntry(
        entry_id="e1",
        data=data if data is not None else {},
        options=options if options is not None else {},
        coordinator=coordinator,
    )
    # ``config_entry`` is a read-only property resolving via ``self.handler``
    # and the config_entries registry, so wire both through.
    flow.handler = "e1"
    flow.hass.config_entries.async_get_known_entry = MagicMock(return_value=entry)

    def _create_entry(*, title: str, data: dict[str, Any]) -> dict[str, Any]:
        return {"type": "create_entry", "title": title, "data": data}

    def _show_form(**kwargs: Any) -> dict[str, Any]:
        return {"type": "form", **kwargs}

    def _show_menu(**kwargs: Any) -> dict[str, Any]:
        return {"type": "menu", **kwargs}

    def _abort(*, reason: str, **kwargs: Any) -> dict[str, Any]:
        return {"type": "abort", "reason": reason}

    flow.async_create_entry = MagicMock(side_effect=_create_entry)
    flow.async_show_form = MagicMock(side_effect=_show_form)
    flow.async_show_menu = MagicMock(side_effect=_show_menu)
    flow.async_abort = MagicMock(side_effect=_abort)
    return flow


def _coordinator_with_spaces() -> MagicMock:
    coord = MagicMock(spec=["all_discovered_spaces", "account"])
    coord.all_discovered_spaces = {"H1": "Home", "H2": "Office"}
    account = MagicMock()
    space1 = MagicMock()
    space1.name = "Home"
    account.spaces = {"H1": space1}
    coord.account = account
    return coord


def test_mask_credential() -> None:
    flow = _make_options_flow()
    assert flow._mask_credential(None) == "—"
    assert flow._mask_credential("short") == "—"
    assert flow._mask_credential("ABCDEFGHIJKL") == "ABCD****IJKL"


async def test_options_init_direct_menu() -> None:
    flow = _make_options_flow(data={CONF_AUTH_MODE: AUTH_MODE_DIRECT})
    result = await flow.async_step_init()
    assert result["type"] == "menu"
    assert "aws_credentials" in result["menu_options"]
    assert "proxy_settings" not in result["menu_options"]
    assert "rtsp_credentials" in result["menu_options"]


async def test_options_init_proxy_menu() -> None:
    flow = _make_options_flow(data={CONF_AUTH_MODE: AUTH_MODE_PROXY_SECURE})
    result = await flow.async_step_init()
    assert "proxy_settings" in result["menu_options"]
    assert "aws_credentials" not in result["menu_options"]


async def test_options_enabled_spaces_form_from_discovered() -> None:
    coord = _coordinator_with_spaces()
    flow = _make_options_flow(data={CONF_ENABLED_SPACES: []}, coordinator=coord)
    result = await flow.async_step_enabled_spaces()
    assert result["type"] == "form"
    assert result["step_id"] == "enabled_spaces"


async def test_options_enabled_spaces_form_fallback_account() -> None:
    coord = MagicMock(spec=["account"])
    account = MagicMock()
    space1 = MagicMock()
    space1.name = "Home"
    account.spaces = {"H1": space1}
    coord.account = account
    flow = _make_options_flow(data={}, coordinator=coord)
    result = await flow.async_step_enabled_spaces()
    assert result["type"] == "form"


async def test_options_enabled_spaces_no_spaces_aborts() -> None:
    flow = _make_options_flow(data={})
    result = await flow.async_step_enabled_spaces()
    assert result["type"] == "abort"
    assert result["reason"] == "no_spaces_available"


async def test_options_enabled_spaces_no_selection_error() -> None:
    coord = _coordinator_with_spaces()
    flow = _make_options_flow(data={}, coordinator=coord)
    result = await flow.async_step_enabled_spaces({CONF_ENABLED_SPACES: []})
    assert result["type"] == "form"
    assert result["errors"]["base"] == "no_spaces_selected"


async def test_options_enabled_spaces_saves() -> None:
    coord = _coordinator_with_spaces()
    flow = _make_options_flow(data={}, options={}, coordinator=coord)
    flow.hass.config_entries.async_update_entry = MagicMock()
    flow.hass.config_entries.async_reload = AsyncMock()
    result = await flow.async_step_enabled_spaces({CONF_ENABLED_SPACES: ["H1"]})
    assert result["type"] == "create_entry"
    flow.hass.config_entries.async_update_entry.assert_called_once()
    # The update listener owns the reload — the step must not reload itself.
    flow.hass.config_entries.async_reload.assert_not_awaited()


async def test_options_notifications_form_with_spaces() -> None:
    coord = MagicMock(spec=["account"])
    account = MagicMock()
    space1 = MagicMock()
    space1.name = "Home"
    account.spaces = {"H1": space1}
    coord.account = account
    flow = _make_options_flow(options={}, coordinator=coord)
    result = await flow.async_step_notifications()
    assert result["type"] == "form"
    assert result["step_id"] == "notifications"


async def test_options_notifications_form_no_coordinator() -> None:
    flow = _make_options_flow(options={})
    result = await flow.async_step_notifications()
    assert result["type"] == "form"


async def test_options_notifications_saves() -> None:
    flow = _make_options_flow(options={"existing": "kept"})
    result = await flow.async_step_notifications(
        {
            CONF_NOTIFICATION_FILTER: NOTIFICATION_FILTER_ALL,
            CONF_PERSISTENT_NOTIFICATION: True,
            CONF_MONITORED_SPACES: ["H1"],
        }
    )
    assert result["type"] == "create_entry"
    assert result["data"]["existing"] == "kept"
    assert result["data"][CONF_NOTIFICATION_FILTER] == NOTIFICATION_FILTER_ALL


async def test_options_polling_settings_form() -> None:
    flow = _make_options_flow(options={})
    result = await flow.async_step_polling_settings()
    assert result["type"] == "form"
    assert result["step_id"] == "polling_settings"


async def test_options_polling_settings_saves() -> None:
    flow = _make_options_flow(options={})
    result = await flow.async_step_polling_settings({CONF_DOOR_SENSOR_FAST_POLL: True})
    assert result["type"] == "create_entry"
    assert result["data"][CONF_DOOR_SENSOR_FAST_POLL] is True


async def test_options_proxy_settings_form() -> None:
    flow = _make_options_flow(data={CONF_PROXY_URL: "https://proxy", CONF_VERIFY_SSL: True})
    result = await flow.async_step_proxy_settings()
    assert result["type"] == "form"
    assert result["step_id"] == "proxy_settings"


async def test_options_proxy_settings_invalid_url() -> None:
    flow = _make_options_flow(data={CONF_PROXY_URL: "https://proxy"})
    result = await flow.async_step_proxy_settings({CONF_PROXY_URL: "ftp://bad"})
    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_proxy_url"


async def test_options_proxy_settings_empty_url_shows_form() -> None:
    flow = _make_options_flow(data={CONF_PROXY_URL: "https://proxy"})
    result = await flow.async_step_proxy_settings({CONF_PROXY_URL: "   "})
    # Empty after strip -> falls through to form (no error, no save).
    assert result["type"] == "form"
    assert result["errors"] == {}


async def test_options_proxy_settings_saves() -> None:
    flow = _make_options_flow(data={CONF_PROXY_URL: "https://old"}, options={})
    flow.hass.config_entries.async_update_entry = MagicMock()
    flow.hass.config_entries.async_reload = AsyncMock()
    result = await flow.async_step_proxy_settings({CONF_PROXY_URL: "https://new/", CONF_VERIFY_SSL: False})
    assert result["type"] == "create_entry"
    flow.hass.config_entries.async_update_entry.assert_called_once()
    new_data = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_PROXY_URL] == "https://new"
    assert new_data[CONF_VERIFY_SSL] is False


async def test_options_aws_credentials_form() -> None:
    flow = _make_options_flow(data={CONF_AWS_ACCESS_KEY_ID: "AKIA12345678", CONF_QUEUE_NAME: "q"})
    result = await flow.async_step_aws_credentials()
    assert result["type"] == "form"
    assert result["step_id"] == "aws_credentials"
    assert result["description_placeholders"]["current_queue"] == "q"


async def test_options_aws_credentials_saves() -> None:
    flow = _make_options_flow(data={})
    flow.hass.config_entries.async_update_entry = MagicMock()
    flow.hass.config_entries.async_reload = AsyncMock()
    result = await flow.async_step_aws_credentials(
        {CONF_AWS_ACCESS_KEY_ID: "AKIA", CONF_AWS_SECRET_ACCESS_KEY: "SEC", CONF_QUEUE_NAME: "queue"}
    )
    assert result["type"] == "create_entry"
    new_data = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_AWS_ACCESS_KEY_ID] == "AKIA"
    assert new_data[CONF_AWS_SECRET_ACCESS_KEY] == "SEC"
    assert new_data[CONF_QUEUE_NAME] == "queue"
    # New credentials must restart the SQS manager — via the update
    # listener's scheduled reload, not a manual one in the step.
    flow.hass.config_entries.async_reload.assert_not_awaited()


async def test_options_aws_credentials_unchanged_no_reload() -> None:
    flow = _make_options_flow(data={CONF_AWS_ACCESS_KEY_ID: "AKIA"})
    flow.hass.config_entries.async_update_entry = MagicMock()
    flow.hass.config_entries.async_reload = AsyncMock()
    # Empty form (user kept everything) → no update, no reload.
    result = await flow.async_step_aws_credentials({})
    assert result["type"] == "create_entry"
    flow.hass.config_entries.async_update_entry.assert_not_called()
    flow.hass.config_entries.async_reload.assert_not_awaited()


async def test_options_rtsp_credentials_form() -> None:
    flow = _make_options_flow(options={CONF_RTSP_USERNAME: "admin", CONF_RTSP_PASSWORD: "secretpassword"})
    result = await flow.async_step_rtsp_credentials()
    assert result["type"] == "form"
    assert result["step_id"] == "rtsp_credentials"
    assert result["description_placeholders"]["current_username"] == "admin"


async def test_options_rtsp_credentials_form_empty() -> None:
    flow = _make_options_flow(options={})
    result = await flow.async_step_rtsp_credentials()
    assert result["description_placeholders"]["current_username"] == "—"
    assert result["description_placeholders"]["current_password"] == "—"


async def test_options_rtsp_credentials_saves() -> None:
    flow = _make_options_flow(options={"keep": "me"})
    flow.hass.config_entries.async_update_entry = MagicMock()
    flow.hass.config_entries.async_reload = AsyncMock()
    result = await flow.async_step_rtsp_credentials({CONF_RTSP_USERNAME: "admin", CONF_RTSP_PASSWORD: "pass"})
    assert result["type"] == "create_entry"
    assert result["data"][CONF_RTSP_USERNAME] == "admin"
    assert result["data"][CONF_RTSP_PASSWORD] == "pass"
    assert result["data"]["keep"] == "me"
    # The options change fires the update listener, which owns the reload.
    flow.hass.config_entries.async_reload.assert_not_awaited()


async def test_options_rtsp_credentials_unchanged_no_reload() -> None:
    flow = _make_options_flow(options={CONF_RTSP_USERNAME: "admin", CONF_RTSP_PASSWORD: "pass"})
    flow.hass.config_entries.async_update_entry = MagicMock()
    flow.hass.config_entries.async_reload = AsyncMock()
    result = await flow.async_step_rtsp_credentials({CONF_RTSP_USERNAME: "admin", CONF_RTSP_PASSWORD: "pass"})
    assert result["type"] == "create_entry"
    flow.hass.config_entries.async_update_entry.assert_not_called()
    flow.hass.config_entries.async_reload.assert_not_awaited()
