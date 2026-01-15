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
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import AjaxConfigEntry
from .api import (
    AjaxRest2FARequiredError,
    AjaxRestApi,
    AjaxRestApiError,
    AjaxRestAuthError,
)
from .const import (
    AUTH_MODE_DIRECT,
    AUTH_MODE_PROXY_HYBRID,
    AUTH_MODE_PROXY_SECURE,
    CONF_API_KEY,
    CONF_AUTH_MODE,
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_DOOR_SENSOR_FAST_POLL,
    CONF_EMAIL,
    CONF_ENABLED_SPACES,
    CONF_MONITORED_SPACES,
    CONF_NOTIFICATION_FILTER,
    CONF_PASSWORD,
    CONF_PERSISTENT_NOTIFICATION,
    CONF_PROXY_URL,
    CONF_QUEUE_NAME,
    DOMAIN,
    NOTIFICATION_FILTER_ALARMS_ONLY,
    NOTIFICATION_FILTER_ALL,
    NOTIFICATION_FILTER_NONE,
    NOTIFICATION_FILTER_SECURITY_EVENTS,
)

_LOGGER = logging.getLogger(__name__)


class AjaxConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ajax Security Systems."""

    VERSION = 1
    MINOR_VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: AjaxConfigEntry,
    ) -> OptionsFlow:
        """Get the options flow for this handler."""
        return AjaxOptionsFlow()

    def __init__(self):
        """Initialize the config flow."""
        self._api: AjaxRestApi | None = None
        self._user_input: dict[str, Any] = {}
        self._request_id: str | None = None
        self._auth_mode: str = AUTH_MODE_DIRECT
        self._spaces: list[
            dict[str, str]
        ] = []  # List of {id, name} for discovered spaces
        self._entry_data: dict[str, Any] = {}  # Prepared entry data

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
                vol.Required(
                    CONF_AUTH_MODE, default=AUTH_MODE_PROXY_SECURE
                ): SelectSelector(
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

    async def async_step_direct(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
                self._api = AjaxRestApi(
                    api_key=user_input[CONF_API_KEY],
                    email=user_input[CONF_EMAIL],
                    password=user_input[CONF_PASSWORD],
                )

                # Test API connection by logging in
                await self._api.async_login()

                # If login successful, try to get hubs to verify access and discover spaces
                hubs = await self._api.async_get_hubs()

                # Build list of spaces from hubs
                self._spaces = []
                for hub in hubs:
                    hub_id = hub.get("hubId")
                    if hub_id:
                        # Get proper space name via space binding API
                        hub_name = hub.get("hubName", f"Hub {hub_id[:6]}")
                        try:
                            space_binding = await self._api.async_get_space_by_hub(
                                hub_id
                            )
                            if space_binding and space_binding.get("name"):
                                hub_name = space_binding.get("name")
                        except Exception:
                            pass  # Keep fallback name
                        self._spaces.append({"id": hub_id, "name": hub_name})

                await self._api.close()

                # Hash password for secure storage (never store plain password!)
                password_hash = hashlib.sha256(
                    user_input[CONF_PASSWORD].encode()
                ).hexdigest()

                # Prepare entry data
                self._entry_data = {
                    CONF_AUTH_MODE: AUTH_MODE_DIRECT,
                    CONF_API_KEY: user_input[CONF_API_KEY],
                    CONF_EMAIL: user_input[CONF_EMAIL],
                    CONF_PASSWORD: password_hash,  # Store ONLY the hash
                }

                # Add optional AWS SQS credentials if provided
                if user_input.get(CONF_AWS_ACCESS_KEY_ID):
                    self._entry_data[CONF_AWS_ACCESS_KEY_ID] = user_input[
                        CONF_AWS_ACCESS_KEY_ID
                    ]
                if user_input.get(CONF_AWS_SECRET_ACCESS_KEY):
                    self._entry_data[CONF_AWS_SECRET_ACCESS_KEY] = user_input[
                        CONF_AWS_SECRET_ACCESS_KEY
                    ]
                if user_input.get(CONF_QUEUE_NAME):
                    self._entry_data[CONF_QUEUE_NAME] = user_input[CONF_QUEUE_NAME]

                # If multiple spaces, let user select which to enable
                if len(self._spaces) > 1:
                    return await self.async_step_select_spaces()

                # Single space or no spaces - enable all by default
                self._entry_data[CONF_ENABLED_SPACES] = [s["id"] for s in self._spaces]

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
                _LOGGER.error(
                    "Authentication failed: %s (type: %s)", err, err.error_type
                )
                # Map error type to translation key
                error_map = {
                    "invalid_api_key": "invalid_api_key",
                    "invalid_password": "invalid_password",
                    "invalid_account_type": "invalid_account_type",
                    "generic": "invalid_auth",
                }
                errors["base"] = error_map.get(err.error_type, "invalid_auth")
            except AjaxRestApiError as err:
                _LOGGER.error("Cannot connect to Ajax API: %s", err)
                errors["base"] = "cannot_connect"
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception: %s", err)
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

    async def async_step_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle proxy mode - proxy URL + credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Store user input for potential 2FA step
            self._user_input = user_input
            self._user_input[CONF_AUTH_MODE] = self._auth_mode

            proxy_url = user_input[CONF_PROXY_URL].rstrip("/")

            await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
            self._abort_if_unique_id_configured()
            try:
                # For proxy mode, we authenticate via the proxy
                # The proxy will provide API key (hybrid) or handle all requests (secure)
                self._api = AjaxRestApi(
                    api_key="",  # Will be provided by proxy
                    email=user_input[CONF_EMAIL],
                    password=user_input[CONF_PASSWORD],
                    proxy_url=proxy_url,
                    proxy_mode=self._auth_mode,
                )

                # Test connection by logging in via proxy
                await self._api.async_login()

                # Get hubs to discover spaces
                try:
                    hubs = await self._api.async_get_hubs()
                    self._spaces = []
                    for hub in hubs:
                        hub_id = hub.get("hubId")
                        if hub_id:
                            # Get proper space name via space binding API
                            hub_name = hub.get("hubName", f"Hub {hub_id[:6]}")
                            try:
                                space_binding = await self._api.async_get_space_by_hub(
                                    hub_id
                                )
                                if space_binding and space_binding.get("name"):
                                    hub_name = space_binding.get("name")
                            except Exception:
                                pass  # Keep fallback name
                            self._spaces.append({"id": hub_id, "name": hub_name})
                except Exception:
                    # Proxy may not have all endpoints - continue without space selection
                    self._spaces = []

                await self._api.close()

                # Hash password for secure storage
                password_hash = hashlib.sha256(
                    user_input[CONF_PASSWORD].encode()
                ).hexdigest()

                # Prepare entry data
                self._entry_data = {
                    CONF_AUTH_MODE: self._auth_mode,
                    CONF_PROXY_URL: proxy_url,
                    CONF_EMAIL: user_input[CONF_EMAIL],
                    CONF_PASSWORD: password_hash,
                }

                # If multiple spaces, let user select which to enable
                if len(self._spaces) > 1:
                    return await self.async_step_select_spaces()

                # Single space or no spaces - enable all by default
                if self._spaces:
                    self._entry_data[CONF_ENABLED_SPACES] = [
                        s["id"] for s in self._spaces
                    ]

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
                _LOGGER.error(
                    "Authentication failed: %s (type: %s)", err, err.error_type
                )
                # Map error type to translation key
                error_map = {
                    "invalid_api_key": "invalid_api_key",
                    "invalid_password": "invalid_password",
                    "invalid_account_type": "invalid_account_type",
                    "generic": "invalid_auth",
                }
                errors["base"] = error_map.get(err.error_type, "invalid_auth")
            except AjaxRestApiError as err:
                _LOGGER.error("Cannot connect to proxy: %s", err)
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected exception: %s", err)
                errors["base"] = "unknown"

        # Show configuration form for proxy mode
        data_schema = vol.Schema(
            {
                vol.Required(CONF_PROXY_URL): str,
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )

        return self.async_show_form(
            step_id="proxy",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_2fa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle 2FA verification step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            code = user_input.get("code", "").strip()

            try:
                # Verify 2FA code
                await self._api.async_verify_2fa(self._request_id, code)

                # 2FA successful, discover spaces
                hubs = await self._api.async_get_hubs()
                self._spaces = []
                for hub in hubs:
                    hub_id = hub.get("hubId")
                    if hub_id:
                        # Get proper space name via space binding API
                        hub_name = hub.get("hubName", f"Hub {hub_id[:6]}")
                        try:
                            space_binding = await self._api.async_get_space_by_hub(
                                hub_id
                            )
                            if space_binding and space_binding.get("name"):
                                hub_name = space_binding.get("name")
                        except Exception:
                            pass  # Keep fallback name
                        self._spaces.append({"id": hub_id, "name": hub_name})

                await self._api.close()

                # Hash password for secure storage (never store plain password!)
                password_hash = hashlib.sha256(
                    self._user_input[CONF_PASSWORD].encode()
                ).hexdigest()

                # Get auth mode from stored input
                auth_mode = self._user_input.get(CONF_AUTH_MODE, AUTH_MODE_DIRECT)

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
                        self._entry_data[CONF_AWS_ACCESS_KEY_ID] = self._user_input[
                            CONF_AWS_ACCESS_KEY_ID
                        ]
                    if self._user_input.get(CONF_AWS_SECRET_ACCESS_KEY):
                        self._entry_data[CONF_AWS_SECRET_ACCESS_KEY] = self._user_input[
                            CONF_AWS_SECRET_ACCESS_KEY
                        ]
                    if self._user_input.get(CONF_QUEUE_NAME):
                        self._entry_data[CONF_QUEUE_NAME] = self._user_input[
                            CONF_QUEUE_NAME
                        ]
                else:
                    # Proxy mode: include proxy URL
                    self._entry_data[CONF_PROXY_URL] = self._user_input[CONF_PROXY_URL]

                # If multiple spaces, let user select which to enable
                if len(self._spaces) > 1:
                    return await self.async_step_select_spaces()

                # Single space or no spaces - enable all by default
                self._entry_data[CONF_ENABLED_SPACES] = [s["id"] for s in self._spaces]

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
                errors["base"] = "cannot_connect"
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception during 2FA: %s", err)
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

    async def async_step_select_spaces(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle space selection when multiple spaces are found."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_spaces = user_input.get(CONF_ENABLED_SPACES, [])

            if not selected_spaces:
                errors["base"] = "no_spaces_selected"
            else:
                # Store selected spaces and create entry
                self._entry_data[CONF_ENABLED_SPACES] = selected_spaces

                return self.async_create_entry(
                    title=f"Ajax - {self._entry_data.get(CONF_EMAIL, 'Unknown')}",
                    data=self._entry_data,
                )

        # Build options from discovered spaces
        space_options = [
            {"value": space["id"], "label": space["name"]} for space in self._spaces
        ]

        # Select all by default
        default_spaces = [space["id"] for space in self._spaces]

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_ENABLED_SPACES,
                    default=default_spaces,
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=space_options,
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

    async def async_step_reauth(
        self, entry_data: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle re-authentication when token expires."""
        self._user_input = dict(entry_data) if entry_data else {}
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle re-authentication confirmation."""
        errors: dict[str, str] = {}

        # Get config entry being re-authenticated
        reauth_entry = self.hass.config_entries.async_get_entry(
            self.context.get("entry_id", "")
        )
        if not reauth_entry:
            return self.async_abort(reason="reauth_failed")

        if user_input is not None:
            auth_mode = reauth_entry.data.get(CONF_AUTH_MODE, AUTH_MODE_DIRECT)
            proxy_url = reauth_entry.data.get(CONF_PROXY_URL)

            try:
                # Create API client based on auth mode
                if auth_mode == AUTH_MODE_DIRECT:
                    self._api = AjaxRestApi(
                        api_key=reauth_entry.data.get(CONF_API_KEY, ""),
                        email=reauth_entry.data.get(CONF_EMAIL, ""),
                        password=user_input[CONF_PASSWORD],
                    )
                else:
                    self._api = AjaxRestApi(
                        api_key="",
                        email=reauth_entry.data.get(CONF_EMAIL, ""),
                        password=user_input[CONF_PASSWORD],
                        proxy_url=proxy_url,
                        proxy_mode=auth_mode,
                    )

                # Test login
                await self._api.async_login()
                await self._api.close()

                # Hash new password
                password_hash = hashlib.sha256(
                    user_input[CONF_PASSWORD].encode()
                ).hexdigest()

                # Update config entry with new password
                new_data = {**reauth_entry.data, CONF_PASSWORD: password_hash}
                self.hass.config_entries.async_update_entry(reauth_entry, data=new_data)

                await self.hass.config_entries.async_reload(reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

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
                error_map = {
                    "invalid_api_key": "invalid_api_key",
                    "invalid_password": "invalid_password",
                    "invalid_account_type": "invalid_account_type",
                    "generic": "invalid_auth",
                }
                errors["base"] = error_map.get(err.error_type, "invalid_auth")
            except AjaxRestApiError as err:
                _LOGGER.error("Reauth failed: %s", err)
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected error during reauth: %s", err)
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


class AjaxOptionsFlow(OptionsFlow):
    """Handle Ajax options."""

    def _mask_credential(self, value: str | None) -> str:
        """Mask a credential for display (show first 4 and last 4 chars)."""
        if not value or len(value) < 10:
            return "Not configured"
        return f"{value[:4]}****{value[-4:]}"

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options - main menu."""
        # Build menu options based on auth mode
        menu_options = ["enabled_spaces", "notifications", "polling_settings"]

        auth_mode = self.config_entry.data.get(CONF_AUTH_MODE, AUTH_MODE_DIRECT)

        # Show proxy settings only for proxy modes
        if auth_mode in (AUTH_MODE_PROXY_SECURE, AUTH_MODE_PROXY_HYBRID):
            menu_options.append("proxy_settings")

        # Show AWS credentials only for direct mode
        if auth_mode == AUTH_MODE_DIRECT:
            menu_options.append("aws_credentials")

        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
        )

    async def async_step_enabled_spaces(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage enabled spaces."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_spaces = user_input.get(CONF_ENABLED_SPACES, [])

            if not selected_spaces:
                errors["base"] = "no_spaces_selected"
            else:
                # Update config entry data with new enabled spaces
                new_data = {**self.config_entry.data}
                new_data[CONF_ENABLED_SPACES] = selected_spaces

                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=new_data,
                )

                # Reload integration to apply changes
                await self.hass.config_entries.async_reload(self.config_entry.entry_id)

                return self.async_create_entry(title="", data=self.config_entry.options)

        # Get all available spaces from coordinator
        space_options = []
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)

        if coordinator and hasattr(coordinator, "all_discovered_spaces"):
            # Use all discovered spaces (not just enabled ones)
            for space_id, space_name in coordinator.all_discovered_spaces.items():
                space_options.append({"value": space_id, "label": space_name})
        elif coordinator and hasattr(coordinator, "account") and coordinator.account:
            # Fallback to currently loaded spaces
            for space_id, space in coordinator.account.spaces.items():
                space_options.append({"value": space_id, "label": space.name})

        # Get currently enabled spaces
        current_enabled = self.config_entry.data.get(CONF_ENABLED_SPACES, [])
        if not current_enabled and space_options:
            # If no spaces configured, default to all available
            current_enabled = [opt["value"] for opt in space_options]

        # Build schema
        if space_options:
            data_schema = vol.Schema(
                {
                    vol.Required(
                        CONF_ENABLED_SPACES,
                        default=current_enabled,
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=space_options,
                            mode=SelectSelectorMode.LIST,
                            multiple=True,
                        )
                    ),
                }
            )
        else:
            # No spaces available - show message
            return self.async_abort(reason="no_spaces_available")

        return self.async_show_form(
            step_id="enabled_spaces",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_notifications(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage notification options."""
        if user_input is not None:
            # Merge with existing options
            new_options = {**self.config_entry.options, **user_input}
            return self.async_create_entry(title="", data=new_options)

        # Get current options
        current_filter = self.config_entry.options.get(
            CONF_NOTIFICATION_FILTER, NOTIFICATION_FILTER_NONE
        )
        current_persistent = self.config_entry.options.get(
            CONF_PERSISTENT_NOTIFICATION, False
        )
        current_spaces = self.config_entry.options.get(CONF_MONITORED_SPACES, [])

        # Get available spaces from coordinator
        space_options = []
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator and hasattr(coordinator, "account") and coordinator.account:
            for space_id, space in coordinator.account.spaces.items():
                space_options.append(
                    {
                        "value": space_id,
                        "label": space.name,
                    }
                )
            # If no spaces selected, select all by default
            if not current_spaces:
                current_spaces = list(coordinator.account.spaces.keys())

        # Build options schema
        schema_dict = {
            vol.Optional(
                CONF_PERSISTENT_NOTIFICATION,
                default=current_persistent,
            ): bool,
            vol.Optional(
                CONF_NOTIFICATION_FILTER,
                default=current_filter,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        NOTIFICATION_FILTER_NONE,
                        NOTIFICATION_FILTER_ALARMS_ONLY,
                        NOTIFICATION_FILTER_SECURITY_EVENTS,
                        NOTIFICATION_FILTER_ALL,
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="notification_filter",
                )
            ),
        }

        # Add spaces selector only if spaces are available
        if space_options:
            schema_dict[
                vol.Optional(
                    CONF_MONITORED_SPACES,
                    default=current_spaces,
                )
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=space_options,
                    mode=SelectSelectorMode.DROPDOWN,
                    multiple=True,
                )
            )

        return self.async_show_form(
            step_id="notifications",
            data_schema=vol.Schema(schema_dict),
        )

    async def async_step_polling_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage polling settings."""
        if user_input is not None:
            # Merge with existing options
            new_options = {**self.config_entry.options, **user_input}
            return self.async_create_entry(title="", data=new_options)

        # Get current options (default: disabled to reduce API calls)
        current_fast_poll = self.config_entry.options.get(
            CONF_DOOR_SENSOR_FAST_POLL, False
        )

        data_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_DOOR_SENSOR_FAST_POLL,
                    default=current_fast_poll,
                ): bool,
            }
        )

        return self.async_show_form(
            step_id="polling_settings",
            data_schema=data_schema,
        )

    async def async_step_proxy_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage proxy settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            new_proxy_url = user_input.get(CONF_PROXY_URL, "").strip()

            if new_proxy_url:
                # Validate URL format
                if not new_proxy_url.startswith(("http://", "https://")):
                    errors["base"] = "invalid_proxy_url"
                else:
                    # Update config entry data with new proxy URL
                    new_data = {**self.config_entry.data}
                    new_data[CONF_PROXY_URL] = new_proxy_url.rstrip("/")

                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        data=new_data,
                    )

                    return self.async_create_entry(
                        title="", data=self.config_entry.options
                    )

        # Get current proxy URL
        current_proxy_url = self.config_entry.data.get(CONF_PROXY_URL, "")

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_PROXY_URL,
                    default=current_proxy_url,
                ): str,
            }
        )

        return self.async_show_form(
            step_id="proxy_settings",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_aws_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage AWS SQS credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Update config entry data with new AWS credentials
            new_data = {**self.config_entry.data}

            # Only update if user provided new values (not empty)
            if user_input.get(CONF_AWS_ACCESS_KEY_ID):
                new_data[CONF_AWS_ACCESS_KEY_ID] = user_input[CONF_AWS_ACCESS_KEY_ID]
            if user_input.get(CONF_AWS_SECRET_ACCESS_KEY):
                new_data[CONF_AWS_SECRET_ACCESS_KEY] = user_input[
                    CONF_AWS_SECRET_ACCESS_KEY
                ]
            if user_input.get(CONF_QUEUE_NAME):
                new_data[CONF_QUEUE_NAME] = user_input[CONF_QUEUE_NAME]

            # Update the config entry data
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data=new_data,
            )

            return self.async_create_entry(title="", data=self.config_entry.options)

        # Get current AWS credentials from config entry data
        current_access_key = self.config_entry.data.get(CONF_AWS_ACCESS_KEY_ID, "")
        current_secret_key = self.config_entry.data.get(CONF_AWS_SECRET_ACCESS_KEY, "")
        current_queue = self.config_entry.data.get(CONF_QUEUE_NAME, "")

        # Build schema - show current values masked
        data_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_AWS_ACCESS_KEY_ID,
                    description={"suggested_value": current_access_key},
                ): str,
                vol.Optional(
                    CONF_AWS_SECRET_ACCESS_KEY,
                    description={
                        "suggested_value": ""
                    },  # Don't show secret, let user re-enter
                ): str,
                vol.Optional(
                    CONF_QUEUE_NAME,
                    description={"suggested_value": current_queue},
                ): str,
            }
        )

        # Show current configuration in description
        return self.async_show_form(
            step_id="aws_credentials",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "current_access_key": self._mask_credential(current_access_key),
                "current_secret_key": self._mask_credential(current_secret_key),
                "current_queue": current_queue or "Not configured",
            },
        )
