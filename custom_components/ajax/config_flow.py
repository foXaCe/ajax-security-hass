"""Config flow for Ajax integration."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import (
    AjaxRest2FARequiredError,
    AjaxRestApi,
    AjaxRestApiError,
    AjaxRestAuthError,
)
from .const import (
    AUTH_MODE_DIRECT,
    AUTH_MODE_PROXY_SECURE,
    CONF_API_KEY,
    CONF_AUTH_MODE,
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_EMAIL,
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


class AjaxConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ajax Security Systems."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return AjaxOptionsFlow(config_entry)

    def __init__(self):
        """Initialize the config flow."""
        self._api: AjaxRestApi | None = None
        self._user_input: dict[str, Any] = {}
        self._request_id: str | None = None
        self._auth_mode: str = AUTH_MODE_DIRECT

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
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
                            {
                                "value": AUTH_MODE_PROXY_SECURE,
                                "label": "Proxy Ajax",
                            },
                            {
                                "value": AUTH_MODE_DIRECT,
                                "label": "Direct (Clé API entreprise uniquement)",
                            },
                        ],
                        mode=SelectSelectorMode.LIST,
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
    ) -> FlowResult:
        """Handle direct mode - API key + credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Store user input for potential 2FA step
            self._user_input = user_input
            self._user_input[CONF_AUTH_MODE] = AUTH_MODE_DIRECT

            # Validate API credentials
            try:
                self._api = AjaxRestApi(
                    api_key=user_input[CONF_API_KEY],
                    email=user_input[CONF_EMAIL],
                    password=user_input[CONF_PASSWORD],
                )

                # Test API connection by logging in
                await self._api.async_login()

                # If login successful, try to get hubs to verify access
                await self._api.async_get_hubs()
                await self._api.close()

                # Hash password for secure storage (never store plain password!)
                password_hash = hashlib.sha256(
                    user_input[CONF_PASSWORD].encode()
                ).hexdigest()

                # Prepare entry data
                entry_data = {
                    CONF_AUTH_MODE: AUTH_MODE_DIRECT,
                    CONF_API_KEY: user_input[CONF_API_KEY],
                    CONF_EMAIL: user_input[CONF_EMAIL],
                    CONF_PASSWORD: password_hash,  # Store ONLY the hash
                }

                # Add optional AWS SQS credentials if provided
                if user_input.get(CONF_AWS_ACCESS_KEY_ID):
                    entry_data[CONF_AWS_ACCESS_KEY_ID] = user_input[
                        CONF_AWS_ACCESS_KEY_ID
                    ]
                if user_input.get(CONF_AWS_SECRET_ACCESS_KEY):
                    entry_data[CONF_AWS_SECRET_ACCESS_KEY] = user_input[
                        CONF_AWS_SECRET_ACCESS_KEY
                    ]
                if user_input.get(CONF_QUEUE_NAME):
                    entry_data[CONF_QUEUE_NAME] = user_input[CONF_QUEUE_NAME]

                # Create entry
                return self.async_create_entry(
                    title=f"Ajax - {user_input[CONF_EMAIL]}",
                    data=entry_data,
                )

            except AjaxRest2FARequiredError as err:
                # 2FA is required, store request_id and show 2FA form
                _LOGGER.info("2FA required for login")
                self._request_id = err.request_id
                return await self.async_step_2fa()

            except AjaxRestAuthError:
                _LOGGER.error("Invalid API credentials")
                errors["base"] = "invalid_auth"
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
    ) -> FlowResult:
        """Handle proxy mode - proxy URL + credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Store user input for potential 2FA step
            self._user_input = user_input
            self._user_input[CONF_AUTH_MODE] = self._auth_mode

            proxy_url = user_input[CONF_PROXY_URL].rstrip("/")

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

                # Skip hub verification for proxy mode (proxy may not have all endpoints)
                # await self._api.async_get_hubs()
                await self._api.close()

                # Hash password for secure storage
                password_hash = hashlib.sha256(
                    user_input[CONF_PASSWORD].encode()
                ).hexdigest()

                # Prepare entry data
                entry_data = {
                    CONF_AUTH_MODE: self._auth_mode,
                    CONF_PROXY_URL: proxy_url,
                    CONF_EMAIL: user_input[CONF_EMAIL],
                    CONF_PASSWORD: password_hash,
                }

                # Create entry
                return self.async_create_entry(
                    title=f"Ajax - {user_input[CONF_EMAIL]}",
                    data=entry_data,
                )

            except AjaxRest2FARequiredError as err:
                _LOGGER.info("2FA required for proxy login")
                self._request_id = err.request_id
                return await self.async_step_2fa()

            except AjaxRestAuthError:
                _LOGGER.error("Invalid credentials for proxy")
                errors["base"] = "invalid_auth"
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
    ) -> FlowResult:
        """Handle 2FA verification step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            code = user_input.get("code", "").strip()

            try:
                # Verify 2FA code
                await self._api.async_verify_2fa(self._request_id, code)

                # 2FA successful, verify access
                await self._api.async_get_hubs()
                await self._api.close()

                # Hash password for secure storage (never store plain password!)
                password_hash = hashlib.sha256(
                    self._user_input[CONF_PASSWORD].encode()
                ).hexdigest()

                # Get auth mode from stored input
                auth_mode = self._user_input.get(CONF_AUTH_MODE, AUTH_MODE_DIRECT)

                # Prepare entry data based on auth mode
                entry_data = {
                    CONF_AUTH_MODE: auth_mode,
                    CONF_EMAIL: self._user_input[CONF_EMAIL],
                    CONF_PASSWORD: password_hash,
                }

                if auth_mode == AUTH_MODE_DIRECT:
                    # Direct mode: include API key and optional AWS credentials
                    entry_data[CONF_API_KEY] = self._user_input[CONF_API_KEY]

                    if self._user_input.get(CONF_AWS_ACCESS_KEY_ID):
                        entry_data[CONF_AWS_ACCESS_KEY_ID] = self._user_input[
                            CONF_AWS_ACCESS_KEY_ID
                        ]
                    if self._user_input.get(CONF_AWS_SECRET_ACCESS_KEY):
                        entry_data[CONF_AWS_SECRET_ACCESS_KEY] = self._user_input[
                            CONF_AWS_SECRET_ACCESS_KEY
                        ]
                    if self._user_input.get(CONF_QUEUE_NAME):
                        entry_data[CONF_QUEUE_NAME] = self._user_input[CONF_QUEUE_NAME]
                else:
                    # Proxy mode: include proxy URL
                    entry_data[CONF_PROXY_URL] = self._user_input[CONF_PROXY_URL]

                # Create entry
                return self.async_create_entry(
                    title=f"Ajax - {self._user_input[CONF_EMAIL]}",
                    data=entry_data,
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


class AjaxOptionsFlow(config_entries.OptionsFlow):
    """Handle Ajax options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    def _mask_credential(self, value: str | None) -> str:
        """Mask a credential for display (show first 4 and last 4 chars)."""
        if not value or len(value) < 10:
            return "Non configuré"
        return f"{value[:4]}****{value[-4:]}"

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options - main menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["notifications", "aws_credentials"],
        )

    async def async_step_notifications(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
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
                        {"value": NOTIFICATION_FILTER_NONE, "label": "Aucune"},
                        {
                            "value": NOTIFICATION_FILTER_ALARMS_ONLY,
                            "label": "Alarmes uniquement",
                        },
                        {
                            "value": NOTIFICATION_FILTER_SECURITY_EVENTS,
                            "label": "Événements de sécurité (alarmes + armement)",
                        },
                        {
                            "value": NOTIFICATION_FILTER_ALL,
                            "label": "Toutes les notifications",
                        },
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
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

    async def async_step_aws_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
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
                "current_queue": current_queue or "Non configuré",
            },
        )
