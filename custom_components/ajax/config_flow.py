"""Config flow for Ajax integration."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from . import AjaxConfigEntry
from .api import (
    AjaxRest2FARequiredError,
    AjaxRestApi,
    AjaxRestApiError,
    AjaxRestAuthError,
)
from .config_flow_options import AjaxOptionsFlow
from .const import (
    AUTH_MODE_DIRECT,
    AUTH_MODE_PROXY_SECURE,
    CONF_API_KEY,
    CONF_AUTH_MODE,
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_DISCOVERED_MACS,
    CONF_EMAIL,
    CONF_ENABLED_SPACES,
    CONF_PASSWORD,
    CONF_PROXY_URL,
    CONF_QUEUE_NAME,
    CONF_VERIFY_SSL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# AjaxRestAuthError.error_type -> config-flow error translation key.
_AUTH_ERROR_MAP = {
    "invalid_api_key": "invalid_api_key",
    "invalid_password": "invalid_password",
    "invalid_account_type": "invalid_account_type",
    "generic": "invalid_auth",
}


def _build_api(
    *,
    email: str,
    password: str,
    auth_mode: str,
    api_key: str = "",
    proxy_url: str | None = None,
    verify_ssl: bool = True,
) -> AjaxRestApi:
    """Build the REST client for either auth mode (single construction point).

    Direct mode authenticates with the enterprise API key; proxy modes get
    their key from the proxy and carry the proxy URL / TLS preference.
    """
    if auth_mode == AUTH_MODE_DIRECT:
        return AjaxRestApi(api_key=api_key, email=email, password=password)
    return AjaxRestApi(
        api_key="",
        email=email,
        password=password,
        proxy_url=proxy_url,
        proxy_mode=auth_mode,
        verify_ssl=verify_ssl,
    )


class AjaxConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ajax Security Systems."""

    VERSION = 1
    MINOR_VERSION = 3

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: AjaxConfigEntry,
    ) -> OptionsFlow:
        """Get the options flow for this handler."""
        return AjaxOptionsFlow()

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._api: AjaxRestApi | None = None
        self._user_input: dict[str, Any] = {}
        self._request_id: str | None = None
        self._auth_mode: str = AUTH_MODE_DIRECT
        self._spaces: list[dict[str, str]] = []  # List of {id, name} for discovered spaces
        self._entry_data: dict[str, Any] = {}  # Prepared entry data

    def _add_discovered_mac_to_entry_data(self) -> None:
        """Add discovered MAC address to entry data if available."""
        discovered_mac = self.context.get("discovered_mac")
        if discovered_mac:
            existing_macs = self._entry_data.get(CONF_DISCOVERED_MACS, [])
            if discovered_mac not in existing_macs:
                self._entry_data[CONF_DISCOVERED_MACS] = existing_macs + [discovered_mac]

    async def _async_discover_spaces(self, hubs: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Resolve the {id, name} space list from the hubs payload.

        The proper space name comes from the space-binding endpoint; on
        failure the hub name (or a short id) is used instead.
        """
        assert self._api is not None
        spaces: list[dict[str, str]] = []
        for hub in hubs:
            hub_id = hub.get("hubId")
            if not hub_id:
                continue
            hub_name = hub.get("hubName", f"Hub {hub_id[:6]}")
            try:
                space_binding = await self._api.async_get_space_by_hub(hub_id)
                if space_binding and space_binding.get("name"):
                    hub_name = space_binding.get("name")
            except AjaxRestApiError as err:
                _LOGGER.debug("Could not resolve space name for %s: %s", hub_id, err)
            spaces.append({"id": hub_id, "name": hub_name})
        return spaces

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step - choose authentication mode."""
        if user_input is not None:
            self._auth_mode = user_input[CONF_AUTH_MODE]

            if self._auth_mode == AUTH_MODE_DIRECT:
                return await self.async_step_direct()
            else:
                # Proxy mode - the proxy decides between secure/hybrid
                return await self.async_step_proxy()

        # Show auth mode selection (2 options only, Proxy is default)
        data_schema = vol.Schema(
            {
                vol.Required(CONF_AUTH_MODE, default=AUTH_MODE_PROXY_SECURE): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            AUTH_MODE_PROXY_SECURE,
                            AUTH_MODE_DIRECT,
                        ],
                        mode=SelectSelectorMode.LIST,
                        translation_key="auth_mode",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
        )

    async def async_step_direct(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle direct mode - API key + credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Store user input for potential 2FA step
            self._user_input = user_input
            self._user_input[CONF_AUTH_MODE] = AUTH_MODE_DIRECT

            await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
            self._abort_if_unique_id_configured()
            # Validate API credentials
            try:
                self._api = _build_api(
                    email=user_input[CONF_EMAIL],
                    password=user_input[CONF_PASSWORD],
                    auth_mode=AUTH_MODE_DIRECT,
                    api_key=user_input[CONF_API_KEY],
                )

                # Test API connection by logging in
                await self._api.async_login()

                # If login successful, try to get hubs to verify access and discover spaces
                hubs = await self._api.async_get_hubs()

                # Build list of spaces from hubs
                self._spaces = await self._async_discover_spaces(hubs)

                await self._api.close()

                # Hash password for secure storage (never store plain password!)
                password_hash = hashlib.sha256(user_input[CONF_PASSWORD].encode()).hexdigest()

                # Prepare entry data
                self._entry_data = {
                    CONF_AUTH_MODE: AUTH_MODE_DIRECT,
                    CONF_API_KEY: user_input[CONF_API_KEY],
                    CONF_EMAIL: user_input[CONF_EMAIL],
                    CONF_PASSWORD: password_hash,  # Store ONLY the hash
                }

                # Add optional AWS SQS credentials if provided
                if user_input.get(CONF_AWS_ACCESS_KEY_ID):
                    self._entry_data[CONF_AWS_ACCESS_KEY_ID] = user_input[CONF_AWS_ACCESS_KEY_ID]
                if user_input.get(CONF_AWS_SECRET_ACCESS_KEY):
                    self._entry_data[CONF_AWS_SECRET_ACCESS_KEY] = user_input[CONF_AWS_SECRET_ACCESS_KEY]
                if user_input.get(CONF_QUEUE_NAME):
                    self._entry_data[CONF_QUEUE_NAME] = user_input[CONF_QUEUE_NAME]

                # If multiple spaces, let user select which to enable
                if len(self._spaces) > 1:
                    return await self.async_step_select_spaces()

                # Single space or no spaces - enable all by default.
                # Leave the key unset (=> None => all enabled) when discovery
                # returned no spaces; an empty list would disable *every* hub.
                if self._spaces:
                    self._entry_data[CONF_ENABLED_SPACES] = [s["id"] for s in self._spaces]

                # Add discovered MAC if from DHCP discovery
                self._add_discovered_mac_to_entry_data()

                # Create entry
                return self.async_create_entry(
                    title=f"Ajax - {user_input[CONF_EMAIL]}",
                    data=self._entry_data,
                )

            except AjaxRest2FARequiredError as err:
                # 2FA is required, store request_id and show 2FA form
                _LOGGER.info("2FA required for login")
                self._request_id = err.request_id
                return await self.async_step_2fa()

            except AjaxRestAuthError as err:
                _LOGGER.error("Authentication failed: %s (type: %s)", err, err.error_type)
                if self._api:
                    await self._api.close()
                # Map error type to translation key
                errors["base"] = _AUTH_ERROR_MAP.get(err.error_type, "invalid_auth")
            except AjaxRestApiError as err:
                _LOGGER.error("Cannot connect to Ajax API: %s", err)
                if self._api:
                    await self._api.close()
                errors["base"] = "cannot_connect"
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception: %s", err)
                if self._api:
                    await self._api.close()
                errors["base"] = "unknown"

        # Show configuration form for direct mode
        data_schema = vol.Schema(
            {
                vol.Required(CONF_API_KEY): str,
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
                # AWS SQS credentials (optional - for real-time events)
                vol.Optional(CONF_AWS_ACCESS_KEY_ID): str,
                vol.Optional(CONF_AWS_SECRET_ACCESS_KEY): str,
                vol.Optional(CONF_QUEUE_NAME): str,
            }
        )

        return self.async_show_form(
            step_id="direct",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_proxy(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle proxy mode - proxy URL + credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Store user input for potential 2FA step
            self._user_input = user_input
            self._user_input[CONF_AUTH_MODE] = self._auth_mode

            proxy_url = user_input[CONF_PROXY_URL].rstrip("/")
            if not proxy_url.startswith(("http://", "https://")):
                # Immediate feedback on a malformed URL — otherwise it only
                # surfaces later as a generic cannot_connect (mirrors the
                # options-flow proxy_settings validation).
                errors["base"] = "invalid_proxy_url"
            else:
                verify_ssl = user_input.get(CONF_VERIFY_SSL, True)
                self._user_input[CONF_PROXY_URL] = proxy_url
                self._user_input[CONF_VERIFY_SSL] = verify_ssl

                await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
                self._abort_if_unique_id_configured()
                try:
                    # For proxy mode, we authenticate via the proxy
                    # The proxy will provide API key (hybrid) or handle all requests (secure)
                    self._api = _build_api(
                        email=user_input[CONF_EMAIL],
                        password=user_input[CONF_PASSWORD],
                        auth_mode=self._auth_mode,
                        proxy_url=proxy_url,
                        verify_ssl=verify_ssl,
                    )

                    # Test connection by logging in via proxy
                    await self._api.async_login()

                    # Get hubs to discover spaces
                    try:
                        hubs = await self._api.async_get_hubs()
                        self._spaces = await self._async_discover_spaces(hubs)
                    except AjaxRestApiError as err:
                        # Proxy may not have all endpoints - continue without space selection
                        _LOGGER.debug("Proxy hubs discovery failed: %s", err)
                        self._spaces = []

                    await self._api.close()

                    # Hash password for secure storage
                    password_hash = hashlib.sha256(user_input[CONF_PASSWORD].encode()).hexdigest()

                    # Prepare entry data
                    self._entry_data = {
                        CONF_AUTH_MODE: self._auth_mode,
                        CONF_PROXY_URL: proxy_url,
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: password_hash,
                        CONF_VERIFY_SSL: user_input.get(CONF_VERIFY_SSL, True),
                    }

                    # If multiple spaces, let user select which to enable
                    if len(self._spaces) > 1:
                        return await self.async_step_select_spaces()

                    # Single space or no spaces - enable all by default
                    if self._spaces:
                        self._entry_data[CONF_ENABLED_SPACES] = [s["id"] for s in self._spaces]

                    # Add discovered MAC if from DHCP discovery
                    self._add_discovered_mac_to_entry_data()

                    # Create entry
                    return self.async_create_entry(
                        title=f"Ajax - {user_input[CONF_EMAIL]}",
                        data=self._entry_data,
                    )

                except AjaxRest2FARequiredError as err:
                    _LOGGER.info("2FA required for proxy login")
                    self._request_id = err.request_id
                    return await self.async_step_2fa()

                except AjaxRestAuthError as err:
                    _LOGGER.error("Authentication failed: %s (type: %s)", err, err.error_type)
                    if self._api:
                        await self._api.close()
                    # Map error type to translation key
                    errors["base"] = _AUTH_ERROR_MAP.get(err.error_type, "invalid_auth")
                except AjaxRestApiError as err:
                    _LOGGER.error("Cannot connect to proxy: %s", err)
                    if self._api:
                        await self._api.close()
                    errors["base"] = "cannot_connect"
                except Exception as err:
                    _LOGGER.exception("Unexpected exception: %s", err)
                    if self._api:
                        await self._api.close()
                    errors["base"] = "unknown"

        # Show configuration form for proxy mode
        data_schema = vol.Schema(
            {
                vol.Required(CONF_PROXY_URL): str,
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_VERIFY_SSL, default=True): bool,
            }
        )

        return self.async_show_form(
            step_id="proxy",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_2fa(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle 2FA verification step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            code = user_input.get("code", "").strip()

            if self._api is None or self._request_id is None:
                return self.async_abort(reason="api_not_initialized")

            try:
                # Verify 2FA code
                await self._api.async_verify_2fa(self._request_id, code)

                # Get auth mode from stored input
                auth_mode = self._user_input.get(CONF_AUTH_MODE, AUTH_MODE_DIRECT)

                # 2FA successful, discover spaces
                try:
                    hubs = await self._api.async_get_hubs()
                except AjaxRestApiError as err:
                    if auth_mode == AUTH_MODE_DIRECT:
                        raise
                    # Proxy may not expose all discovery endpoints.
                    _LOGGER.debug("Proxy hubs discovery failed after 2FA: %s", err)
                    hubs = []
                self._spaces = await self._async_discover_spaces(hubs)

                await self._api.close()

                # Hash password for secure storage (never store plain password!)
                password_hash = hashlib.sha256(self._user_input[CONF_PASSWORD].encode()).hexdigest()

                # Prepare entry data based on auth mode
                self._entry_data = {
                    CONF_AUTH_MODE: auth_mode,
                    CONF_EMAIL: self._user_input[CONF_EMAIL],
                    CONF_PASSWORD: password_hash,
                }

                if auth_mode == AUTH_MODE_DIRECT:
                    # Direct mode: include API key and optional AWS credentials
                    self._entry_data[CONF_API_KEY] = self._user_input[CONF_API_KEY]

                    if self._user_input.get(CONF_AWS_ACCESS_KEY_ID):
                        self._entry_data[CONF_AWS_ACCESS_KEY_ID] = self._user_input[CONF_AWS_ACCESS_KEY_ID]
                    if self._user_input.get(CONF_AWS_SECRET_ACCESS_KEY):
                        self._entry_data[CONF_AWS_SECRET_ACCESS_KEY] = self._user_input[CONF_AWS_SECRET_ACCESS_KEY]
                    if self._user_input.get(CONF_QUEUE_NAME):
                        self._entry_data[CONF_QUEUE_NAME] = self._user_input[CONF_QUEUE_NAME]
                else:
                    # Proxy mode: include proxy URL and TLS preference
                    self._entry_data[CONF_PROXY_URL] = self._user_input[CONF_PROXY_URL]
                    self._entry_data[CONF_VERIFY_SSL] = self._user_input.get(CONF_VERIFY_SSL, True)

                # Check if this is a reauth flow
                if self.context.get("source") == "reauth":
                    reauth_entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id", ""))
                    if reauth_entry:
                        # Update + abort("reauth_successful"); the update
                        # listener schedules the reload (HA deprecates the
                        # flow-side reload helper on entries with a listener).
                        # Same password (expired-token case) → the listener
                        # will not fire, so retry the setup explicitly.
                        if reauth_entry.data.get(CONF_PASSWORD) == password_hash:
                            self.hass.config_entries.async_schedule_reload(reauth_entry.entry_id)
                        return self.async_update_and_abort(
                            reauth_entry,
                            data_updates={CONF_PASSWORD: password_hash},
                        )

                # If multiple spaces, let user select which to enable
                if len(self._spaces) > 1:
                    return await self.async_step_select_spaces()

                # Single space or no spaces - enable all by default.
                # Leave the key unset (=> None => all enabled) when discovery
                # returned no spaces (e.g. proxy without the hubs endpoint);
                # an empty list would disable *every* hub and create no entities.
                if self._spaces:
                    self._entry_data[CONF_ENABLED_SPACES] = [s["id"] for s in self._spaces]

                # Add discovered MAC if from DHCP discovery
                self._add_discovered_mac_to_entry_data()

                # Create entry
                return self.async_create_entry(
                    title=f"Ajax - {self._user_input[CONF_EMAIL]}",
                    data=self._entry_data,
                )

            except AjaxRestAuthError:
                _LOGGER.error("Invalid 2FA code")
                errors["base"] = "invalid_2fa"
            except AjaxRestApiError as err:
                _LOGGER.error("2FA verification failed: %s", err)
                if self._api:
                    await self._api.close()
                errors["base"] = "cannot_connect"
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception during 2FA: %s", err)
                if self._api:
                    await self._api.close()
                errors["base"] = "unknown"

        # Show 2FA form
        data_schema = vol.Schema(
            {
                vol.Required("code"): str,
            }
        )

        return self.async_show_form(
            step_id="2fa",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "email": self._user_input.get(CONF_EMAIL, ""),
            },
        )

    async def async_step_select_spaces(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle space selection when multiple spaces are found."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_spaces = user_input.get(CONF_ENABLED_SPACES, [])

            if not selected_spaces:
                errors["base"] = "no_spaces_selected"
            else:
                # Store selected spaces and create entry
                self._entry_data[CONF_ENABLED_SPACES] = selected_spaces

                # Add discovered MAC if from DHCP discovery
                self._add_discovered_mac_to_entry_data()

                return self.async_create_entry(
                    title=f"Ajax - {email}" if (email := self._entry_data.get(CONF_EMAIL)) else "Ajax",
                    data=self._entry_data,
                )

        # Build options from discovered spaces
        space_options = [{"value": space["id"], "label": space["name"]} for space in self._spaces]

        # Select all by default
        default_spaces = [space["id"] for space in self._spaces]

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_ENABLED_SPACES,
                    default=default_spaces,
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=space_options,  # type: ignore[typeddict-item]
                        mode=SelectSelectorMode.LIST,
                        multiple=True,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="select_spaces",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "space_count": str(len(self._spaces)),
            },
        )

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        """Handle DHCP discovery of Ajax hubs."""
        # Check if this MAC is already associated with an existing config entry
        discovered_mac = discovery_info.macaddress.upper()
        for entry in self._async_current_entries():
            entry_macs = entry.data.get(CONF_DISCOVERED_MACS, [])
            if discovered_mac in entry_macs:
                # This hub is already configured, abort discovery
                return self.async_abort(reason="already_configured")

        # Store discovered MAC and check for existing entries.
        # `discovered_mac` is a custom extension key not in HA's ConfigFlowContext TypedDict.
        self.context["discovered_mac"] = discovered_mac  # type: ignore[typeddict-unknown-key]
        self.context["title_placeholders"] = {"name": discovery_info.hostname or "Ajax Hub"}

        # If there are existing entries, ask user if they want to associate
        existing_entries = self._async_current_entries()
        if existing_entries:
            return await self.async_step_dhcp_confirm()

        return await self.async_step_user()

    async def async_step_dhcp_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask user whether to associate discovered hub with existing entry."""
        errors: dict[str, str] = {}
        discovered_mac = self.context.get("discovered_mac", "")

        if user_input is not None:
            action = user_input.get("action")
            if action == "new":
                # User wants to create a new configuration
                return await self.async_step_user()
            elif isinstance(action, str):
                # Associate with existing entry — action holds the target entry_id.
                entry = self.hass.config_entries.async_get_entry(action)
                if entry:
                    # Add MAC to existing entry
                    existing_macs = list(entry.data.get(CONF_DISCOVERED_MACS, []))
                    if discovered_mac not in existing_macs:
                        existing_macs.append(discovered_mac)
                        new_data = {**entry.data, CONF_DISCOVERED_MACS: existing_macs}
                        self.hass.config_entries.async_update_entry(entry, data=new_data)
                    return self.async_abort(reason="hub_associated")
                errors["base"] = "entry_not_found"

        # Build options from existing entries
        options: list[SelectOptionDict] = []
        for entry in self._async_current_entries():
            email = entry.data.get(CONF_EMAIL, "Unknown")
            options.append(SelectOptionDict(value=entry.entry_id, label=f"{email}"))
        options.append(SelectOptionDict(value="new", label="Create new configuration"))

        data_schema = vol.Schema(
            {
                vol.Required("action"): SelectSelector(
                    # translation_key so the static "new" option is localised;
                    # email options have no translation and fall back to their label.
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST, translation_key="dhcp_action")
                ),
            }
        )

        return self.async_show_form(
            step_id="dhcp_confirm",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "mac": str(discovered_mac),
                "hostname": str(self.context.get("title_placeholders", {}).get("name", "Ajax Hub")),
            },
        )

    async def async_step_reauth(self, entry_data: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle re-authentication when token expires."""
        self._user_input = dict(entry_data) if entry_data else {}
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle re-authentication confirmation."""
        errors: dict[str, str] = {}

        # Get config entry being re-authenticated
        reauth_entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id", ""))
        if not reauth_entry:
            return self.async_abort(reason="reauth_failed")

        if user_input is not None:
            auth_mode = reauth_entry.data.get(CONF_AUTH_MODE, AUTH_MODE_DIRECT)
            proxy_url = reauth_entry.data.get(CONF_PROXY_URL)

            try:
                # Create API client based on auth mode
                self._api = _build_api(
                    email=reauth_entry.data.get(CONF_EMAIL, ""),
                    password=user_input[CONF_PASSWORD],
                    auth_mode=auth_mode,
                    api_key=reauth_entry.data.get(CONF_API_KEY, ""),
                    proxy_url=proxy_url,
                    verify_ssl=reauth_entry.data.get(CONF_VERIFY_SSL, True),
                )

                # Test login
                await self._api.async_login()
                await self._api.close()

                # Hash new password
                password_hash = hashlib.sha256(user_input[CONF_PASSWORD].encode()).hexdigest()

                # Update + abort("reauth_successful"); the update listener
                # schedules the reload (HA deprecates the flow-side reload
                # helper on entries with a listener). Same password
                # (expired-token case) → the listener will not fire, so
                # retry the setup explicitly.
                if reauth_entry.data.get(CONF_PASSWORD) == password_hash:
                    self.hass.config_entries.async_schedule_reload(reauth_entry.entry_id)
                return self.async_update_and_abort(
                    reauth_entry,
                    data_updates={CONF_PASSWORD: password_hash},
                )

            except AjaxRest2FARequiredError as err:
                # Store info for 2FA
                self._user_input = {
                    **reauth_entry.data,
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                }
                self._request_id = err.request_id
                return await self.async_step_2fa()

            except AjaxRestAuthError as err:
                _LOGGER.error("Reauth failed: %s (type: %s)", err, err.error_type)
                if self._api:
                    await self._api.close()
                errors["base"] = _AUTH_ERROR_MAP.get(err.error_type, "invalid_auth")
            except AjaxRestApiError as err:
                _LOGGER.error("Reauth failed: %s", err)
                if self._api:
                    await self._api.close()
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected error during reauth: %s", err)
                if self._api:
                    await self._api.close()
                errors["base"] = "unknown"

        # Show password re-entry form
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={
                "email": reauth_entry.data.get(CONF_EMAIL, ""),
            },
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle reconfiguration of the integration."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            auth_mode = reconfigure_entry.data.get(CONF_AUTH_MODE, AUTH_MODE_DIRECT)

            try:
                # Validate new credentials
                proxy_url = user_input.get(CONF_PROXY_URL, reconfigure_entry.data.get(CONF_PROXY_URL))
                proxy_url = proxy_url.rstrip("/") if proxy_url else proxy_url
                self._api = _build_api(
                    email=user_input[CONF_EMAIL],
                    password=user_input[CONF_PASSWORD],
                    auth_mode=auth_mode,
                    api_key=user_input.get(CONF_API_KEY, reconfigure_entry.data.get(CONF_API_KEY, "")),
                    proxy_url=proxy_url,
                    verify_ssl=user_input.get(CONF_VERIFY_SSL, reconfigure_entry.data.get(CONF_VERIFY_SSL, True)),
                )

                # Test login
                await self._api.async_login()
                await self._api.close()

                # Hash new password
                password_hash = hashlib.sha256(user_input[CONF_PASSWORD].encode()).hexdigest()

                # Prepare new data
                new_data = {
                    **reconfigure_entry.data,
                    CONF_EMAIL: user_input[CONF_EMAIL],
                    CONF_PASSWORD: password_hash,
                }

                # Update API key if in direct mode
                if auth_mode == AUTH_MODE_DIRECT and CONF_API_KEY in user_input:
                    new_data[CONF_API_KEY] = user_input[CONF_API_KEY]

                # Update proxy URL if in proxy mode
                if auth_mode != AUTH_MODE_DIRECT and CONF_PROXY_URL in user_input:
                    new_data[CONF_PROXY_URL] = user_input[CONF_PROXY_URL].rstrip("/")
                if auth_mode != AUTH_MODE_DIRECT and CONF_VERIFY_SSL in user_input:
                    new_data[CONF_VERIFY_SSL] = user_input[CONF_VERIFY_SSL]

                # The update listener schedules the reload on data change;
                # identical resubmission still retries the setup explicitly.
                if new_data == dict(reconfigure_entry.data):
                    self.hass.config_entries.async_schedule_reload(reconfigure_entry.entry_id)
                return self.async_update_and_abort(
                    reconfigure_entry,
                    data_updates=new_data,
                )

            except AjaxRestAuthError as err:
                _LOGGER.error("Reconfigure failed: %s", err)
                if self._api:
                    await self._api.close()
                errors["base"] = "invalid_auth"
            except AjaxRestApiError as err:
                _LOGGER.error("Reconfigure failed: %s", err)
                if self._api:
                    await self._api.close()
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected error during reconfigure: %s", err)
                if self._api:
                    await self._api.close()
                errors["base"] = "unknown"

        # Build schema based on auth mode
        auth_mode = reconfigure_entry.data.get(CONF_AUTH_MODE, AUTH_MODE_DIRECT)

        if auth_mode == AUTH_MODE_DIRECT:
            data_schema = vol.Schema(
                {
                    vol.Required(CONF_API_KEY, default=reconfigure_entry.data.get(CONF_API_KEY, "")): str,
                    vol.Required(CONF_EMAIL, default=reconfigure_entry.data.get(CONF_EMAIL, "")): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            )
        else:
            data_schema = vol.Schema(
                {
                    vol.Required(CONF_PROXY_URL, default=reconfigure_entry.data.get(CONF_PROXY_URL, "")): str,
                    vol.Required(CONF_EMAIL, default=reconfigure_entry.data.get(CONF_EMAIL, "")): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_VERIFY_SSL, default=reconfigure_entry.data.get(CONF_VERIFY_SSL, True)): bool,
                }
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=data_schema,
            errors=errors,
        )
