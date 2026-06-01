"""Options flow for the Ajax integration.

Split out of ``config_flow`` (which keeps the credential-driven
``AjaxConfigFlow``). ``AjaxOptionsFlow`` only reads the existing config
entry / runtime data — it never talks to the Ajax API.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    AUTH_MODE_DIRECT,
    AUTH_MODE_PROXY_SECURE,
    CONF_AUTH_MODE,
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_DOOR_SENSOR_FAST_POLL,
    CONF_ENABLED_SPACES,
    CONF_MONITORED_SPACES,
    CONF_NOTIFICATION_FILTER,
    CONF_PERSISTENT_NOTIFICATION,
    CONF_PROXY_URL,
    CONF_QUEUE_NAME,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_USERNAME,
    CONF_VERIFY_SSL,
    NOTIFICATION_FILTER_ALARMS_ONLY,
    NOTIFICATION_FILTER_ALL,
    NOTIFICATION_FILTER_NONE,
    NOTIFICATION_FILTER_SECURITY_EVENTS,
)

_LOGGER = logging.getLogger(__name__)


class AjaxOptionsFlow(OptionsFlow):
    """Handle Ajax options."""

    def _mask_credential(self, value: str | None) -> str:
        """Mask a credential for display (show first 4 and last 4 chars)."""
        if not value or len(value) < 10:
            return "Not configured"
        return f"{value[:4]}****{value[-4:]}"

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options - main menu."""
        # Build menu options based on auth mode
        menu_options = ["enabled_spaces", "notifications", "polling_settings"]

        auth_mode = self.config_entry.data.get(CONF_AUTH_MODE, AUTH_MODE_DIRECT)

        # Show proxy settings only for proxy mode
        if auth_mode == AUTH_MODE_PROXY_SECURE:
            menu_options.append("proxy_settings")

        # Show AWS credentials only for direct mode
        if auth_mode == AUTH_MODE_DIRECT:
            menu_options.append("aws_credentials")

        # Always show RTSP credentials option (for Video Edge cameras)
        menu_options.append("rtsp_credentials")

        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
        )

    async def async_step_enabled_spaces(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
        try:
            coordinator = self.config_entry.runtime_data
        except AttributeError:
            coordinator = None

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
                            options=space_options,  # type: ignore[typeddict-item]
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

    async def async_step_notifications(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage notification options."""
        if user_input is not None:
            # Merge with existing options
            new_options = {**self.config_entry.options, **user_input}
            return self.async_create_entry(title="", data=new_options)

        # Get current options
        current_filter = self.config_entry.options.get(CONF_NOTIFICATION_FILTER, NOTIFICATION_FILTER_NONE)
        current_persistent = self.config_entry.options.get(CONF_PERSISTENT_NOTIFICATION, False)
        current_spaces = self.config_entry.options.get(CONF_MONITORED_SPACES, [])

        # Get available spaces from coordinator
        space_options = []
        try:
            coordinator = self.config_entry.runtime_data
        except AttributeError:
            coordinator = None

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
                    options=space_options,  # type: ignore[typeddict-item]
                    mode=SelectSelectorMode.DROPDOWN,
                    multiple=True,
                )
            )

        return self.async_show_form(
            step_id="notifications",
            data_schema=vol.Schema(schema_dict),
        )

    async def async_step_polling_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage polling settings."""
        if user_input is not None:
            # Merge with existing options
            new_options = {**self.config_entry.options, **user_input}
            return self.async_create_entry(title="", data=new_options)

        # Get current options (default: disabled to reduce API calls)
        current_fast_poll = self.config_entry.options.get(CONF_DOOR_SENSOR_FAST_POLL, False)

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

    async def async_step_proxy_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage proxy settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            new_proxy_url = user_input.get(CONF_PROXY_URL, "").strip()

            if new_proxy_url:
                # Validate URL format
                if not new_proxy_url.startswith(("http://", "https://")):
                    errors["base"] = "invalid_proxy_url"
                else:
                    # Update config entry data with new proxy URL and verify_ssl
                    new_data = {**self.config_entry.data}
                    new_data[CONF_PROXY_URL] = new_proxy_url.rstrip("/")
                    new_data[CONF_VERIFY_SSL] = user_input.get(CONF_VERIFY_SSL, True)

                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        data=new_data,
                    )

                    # Reload to apply SSL changes
                    await self.hass.config_entries.async_reload(self.config_entry.entry_id)

                    return self.async_create_entry(title="", data=self.config_entry.options)

        # Get current proxy URL and verify_ssl
        current_proxy_url = self.config_entry.data.get(CONF_PROXY_URL, "")
        current_verify_ssl = self.config_entry.data.get(CONF_VERIFY_SSL, True)

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_PROXY_URL,
                    default=current_proxy_url,
                ): str,
                vol.Optional(
                    CONF_VERIFY_SSL,
                    default=current_verify_ssl,
                ): bool,
            }
        )

        return self.async_show_form(
            step_id="proxy_settings",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_aws_credentials(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage AWS SQS credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Update config entry data with new AWS credentials
            new_data = {**self.config_entry.data}

            # Only update if user provided new values (not empty)
            if user_input.get(CONF_AWS_ACCESS_KEY_ID):
                new_data[CONF_AWS_ACCESS_KEY_ID] = user_input[CONF_AWS_ACCESS_KEY_ID]
            if user_input.get(CONF_AWS_SECRET_ACCESS_KEY):
                new_data[CONF_AWS_SECRET_ACCESS_KEY] = user_input[CONF_AWS_SECRET_ACCESS_KEY]
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
                    description={"suggested_value": ""},  # Don't show secret, let user re-enter
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

    async def async_step_rtsp_credentials(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage RTSP/ONVIF credentials for Video Edge cameras."""
        if user_input is not None:
            # Update options with RTSP credentials
            new_options = {**self.config_entry.options}

            # Store credentials (even if empty to clear them)
            new_options[CONF_RTSP_USERNAME] = user_input.get(CONF_RTSP_USERNAME, "")
            new_options[CONF_RTSP_PASSWORD] = user_input.get(CONF_RTSP_PASSWORD, "")

            return self.async_create_entry(title="", data=new_options)

        # Get current credentials
        current_username = self.config_entry.options.get(CONF_RTSP_USERNAME, "")
        current_password = self.config_entry.options.get(CONF_RTSP_PASSWORD, "")

        data_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_RTSP_USERNAME,
                    description={"suggested_value": current_username},
                ): str,
                vol.Optional(
                    CONF_RTSP_PASSWORD,
                    description={"suggested_value": ""},  # Don't show password
                ): str,
            }
        )

        return self.async_show_form(
            step_id="rtsp_credentials",
            data_schema=data_schema,
            description_placeholders={
                "current_username": current_username or "Not configured",
                "current_password": self._mask_credential(current_password) if current_password else "Not configured",
            },
        )
