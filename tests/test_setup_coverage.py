"""Coverage for ``__init__.async_setup_entry`` and the real coordinator __init__.

The light unit tests elsewhere bypass ``AjaxDataCoordinator.__init__`` with
``object.__new__``; these exercise the real setup flow with the HA test harness:
auth/connection error mapping, the direct (SQS) and proxy (SSE) happy paths, and
the verify_ssl=False repair-issue branch. ``async_config_entry_first_refresh``
and platform forwarding are stubbed so no real I/O happens, while the coordinator
constructor (and therefore its branches) runs for real.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ajax import async_setup_entry
from custom_components.ajax.api import AjaxRestApiError, AjaxRestAuthError
from custom_components.ajax.const import (
    AUTH_MODE_DIRECT,
    AUTH_MODE_PROXY_SECURE,
    CONF_API_KEY,
    CONF_AUTH_MODE,
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_PROXY_URL,
    CONF_QUEUE_NAME,
    CONF_TOTP_SECRET,
    CONF_VERIFY_SSL,
    DOMAIN,
)
from custom_components.ajax.coordinator import AjaxDataCoordinator


def _entry(hass: HomeAssistant, **data: object) -> MockConfigEntry:
    base = {
        CONF_EMAIL: "user@example.com",
        CONF_PASSWORD: "deadbeef",  # already a SHA256 hash in real life
        CONF_AUTH_MODE: AUTH_MODE_DIRECT,
        CONF_API_KEY: "api-key",
        CONF_VERIFY_SSL: True,
    }
    base.update(data)
    entry = MockConfigEntry(domain=DOMAIN, data=base, unique_id="user@example.com")
    entry.add_to_hass(hass)
    return entry


def _mock_api(api_cls: AsyncMock, *, sse_url: str | None = None) -> AsyncMock:
    api = api_cls.return_value
    api.async_login = AsyncMock()
    api.async_get_hubs = AsyncMock(return_value=[])
    api.close = AsyncMock()
    api.sse_url = sse_url
    return api


async def test_setup_entry_auth_failure_raises_auth_failed(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    with (
        patch("custom_components.ajax.AjaxRestApi") as api_cls,
        patch("custom_components.ajax.async_get_clientsession"),
    ):
        api = _mock_api(api_cls)
        api.async_login = AsyncMock(side_effect=AjaxRestAuthError("bad creds"))
        with pytest.raises(ConfigEntryAuthFailed):
            await async_setup_entry(hass, entry)
        api.close.assert_awaited()


async def test_setup_entry_api_error_raises_not_ready(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    with (
        patch("custom_components.ajax.AjaxRestApi") as api_cls,
        patch("custom_components.ajax.async_get_clientsession"),
    ):
        api = _mock_api(api_cls)
        api.async_get_hubs = AsyncMock(side_effect=AjaxRestApiError("unreachable"))
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)
        api.close.assert_awaited()


async def test_setup_entry_direct_mode_happy_path(hass: HomeAssistant) -> None:
    entry = _entry(
        hass,
        **{
            CONF_AWS_ACCESS_KEY_ID: "ak",
            CONF_AWS_SECRET_ACCESS_KEY: "sk",
            CONF_QUEUE_NAME: "queue",
        },
    )
    with (
        patch("custom_components.ajax.AjaxRestApi") as api_cls,
        patch("custom_components.ajax.async_get_clientsession"),
        patch.object(AjaxDataCoordinator, "async_config_entry_first_refresh", new=AsyncMock()),
        patch("custom_components.ajax._async_setup_areas", new=AsyncMock()),
        patch.object(hass.config_entries, "async_forward_entry_setups", new=AsyncMock()) as fwd,
    ):
        _mock_api(api_cls)
        assert await async_setup_entry(hass, entry) is True
        # Real coordinator was constructed and stored.
        assert isinstance(entry.runtime_data, AjaxDataCoordinator)
        assert entry.runtime_data._aws_access_key_id == "ak"
        fwd.assert_awaited_once()
    await hass.async_block_till_done()


async def test_setup_entry_passes_totp_secret_to_api(hass: HomeAssistant) -> None:
    entry = _entry(hass, **{CONF_TOTP_SECRET: "JBSWY3DPEHPK3PXP"})
    with (
        patch("custom_components.ajax.AjaxRestApi") as api_cls,
        patch("custom_components.ajax.async_get_clientsession"),
        patch.object(AjaxDataCoordinator, "async_config_entry_first_refresh", new=AsyncMock()),
        patch("custom_components.ajax._async_setup_areas", new=AsyncMock()),
        patch.object(hass.config_entries, "async_forward_entry_setups", new=AsyncMock()),
    ):
        _mock_api(api_cls)
        assert await async_setup_entry(hass, entry) is True
        assert api_cls.call_args.kwargs["totp_secret"] == "JBSWY3DPEHPK3PXP"
    await hass.async_block_till_done()


async def test_setup_entry_without_totp_secret_passes_none(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    with (
        patch("custom_components.ajax.AjaxRestApi") as api_cls,
        patch("custom_components.ajax.async_get_clientsession"),
        patch.object(AjaxDataCoordinator, "async_config_entry_first_refresh", new=AsyncMock()),
        patch("custom_components.ajax._async_setup_areas", new=AsyncMock()),
        patch.object(hass.config_entries, "async_forward_entry_setups", new=AsyncMock()),
    ):
        _mock_api(api_cls)
        assert await async_setup_entry(hass, entry) is True
        assert api_cls.call_args.kwargs["totp_secret"] is None
    await hass.async_block_till_done()


async def test_setup_entry_proxy_mode_uses_sse_url(hass: HomeAssistant) -> None:
    entry = _entry(
        hass,
        **{
            CONF_AUTH_MODE: AUTH_MODE_PROXY_SECURE,
            CONF_PROXY_URL: "https://proxy.example",
        },
    )
    with (
        patch("custom_components.ajax.AjaxRestApi") as api_cls,
        patch("custom_components.ajax.async_get_clientsession"),
        patch.object(AjaxDataCoordinator, "async_config_entry_first_refresh", new=AsyncMock()),
        patch("custom_components.ajax._async_setup_areas", new=AsyncMock()),
        patch.object(hass.config_entries, "async_forward_entry_setups", new=AsyncMock()),
    ):
        _mock_api(api_cls, sse_url="https://proxy.example/sse?userId=abc")
        assert await async_setup_entry(hass, entry) is True
        assert entry.runtime_data._sse_url == "https://proxy.example/sse?userId=abc"
    await hass.async_block_till_done()


async def test_setup_entry_verify_ssl_disabled_creates_repair_issue(hass: HomeAssistant) -> None:
    from homeassistant.helpers import issue_registry as ir

    entry = _entry(hass, **{CONF_VERIFY_SSL: False})
    with (
        patch("custom_components.ajax.AjaxRestApi") as api_cls,
        patch("custom_components.ajax.async_get_clientsession"),
        patch.object(AjaxDataCoordinator, "async_config_entry_first_refresh", new=AsyncMock()),
        patch("custom_components.ajax._async_setup_areas", new=AsyncMock()),
        patch.object(hass.config_entries, "async_forward_entry_setups", new=AsyncMock()),
    ):
        _mock_api(api_cls)
        assert await async_setup_entry(hass, entry) is True
        registry = ir.async_get(hass)
        assert registry.async_get_issue(DOMAIN, f"verify_ssl_disabled_{entry.entry_id}") is not None
    await hass.async_block_till_done()
