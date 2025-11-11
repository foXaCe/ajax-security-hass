"""Config flow for Ajax integration."""
from __future__ import annotations

import hashlib
import logging
from typing import Any
import uuid

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .api import AjaxApi, AjaxApiError, AjaxAuthError, Ajax2FARequiredError
from .const import (
    DOMAIN,
    CONF_DEVICE_ID,
    CONF_PERSISTENT_NOTIFICATION,
    CONF_NOTIFICATION_FILTER,
    NOTIFICATION_FILTER_NONE,
    NOTIFICATION_FILTER_ALARMS_ONLY,
    NOTIFICATION_FILTER_SECURITY_EVENTS,
    NOTIFICATION_FILTER_ALL,
)

_LOGGER = logging.getLogger(__name__)

CONF_TOTP = "totp_code"

def get_user_data_schema(hass: HomeAssistant) -> vol.Schema:
    """Get the user data schema with translated notification filter options."""
    return vol.Schema(
        {
            vol.Required(CONF_EMAIL): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(CONF_PERSISTENT_NOTIFICATION, default=False): bool,
            vol.Optional(CONF_NOTIFICATION_FILTER, default=NOTIFICATION_FILTER_NONE): vol.In(
                {
                    NOTIFICATION_FILTER_NONE: NOTIFICATION_FILTER_NONE,
                    NOTIFICATION_FILTER_ALARMS_ONLY: NOTIFICATION_FILTER_ALARMS_ONLY,
                    NOTIFICATION_FILTER_SECURITY_EVENTS: NOTIFICATION_FILTER_SECURITY_EVENTS,
                    NOTIFICATION_FILTER_ALL: NOTIFICATION_FILTER_ALL,
                }
            ),
        }
    )


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    Note: password in data is already hashed by async_step_user.
    """
    # Generate a unique device ID if not provided
    device_id = data.get(CONF_DEVICE_ID)
    if not device_id:
        device_id = f"homeassistant_{uuid.uuid4().hex[:16]}"

    # Password is already hashed by async_step_user
    # Create API client with hashed password
    api = AjaxApi(
        email=data[CONF_EMAIL],
        password=data[CONF_PASSWORD],  # Already hashed
        device_id=device_id,
        password_is_hashed=True,
    )

    # Try to authenticate
    try:
        login_result = await api.async_login()
    except AjaxAuthError as err:
        raise InvalidAuth from err
    except AjaxApiError as err:
        raise CannotConnect from err
    finally:
        await api.close()

    # Return info that you want to store in the config entry
    return {
        "title": f"Ajax - {login_result.get('user_name', data[CONF_EMAIL])}",
        "device_id": device_id,
        "user_id": login_result.get("user_id"),
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ajax."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._user_input: dict[str, Any] = {}
        self._spaces: list[dict[str, Any]] = []
        self._info: dict[str, Any] = {}
        self._totp_request_id: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Hash password immediately and replace in user_input
            # This ensures we only store the hash, never the plain password
            password_hash = hashlib.sha256(user_input[CONF_PASSWORD].encode()).hexdigest()
            user_input[CONF_PASSWORD] = password_hash

            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Ajax2FARequiredError as err:
                # Store request_id and user input (with hashed password), then go to TOTP step
                self._totp_request_id = err.request_id
                self._user_input = user_input
                return await self.async_step_totp()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                # Set unique ID based on user ID
                await self.async_set_unique_id(info["user_id"])
                self._abort_if_unique_id_configured()

                # Store user input (with hashed password) and info for next step
                self._user_input = user_input
                self._info = info

                # Get list of spaces for selection
                try:
                    from .api import AjaxApi
                    api = AjaxApi(user_input[CONF_EMAIL], user_input[CONF_PASSWORD], info["device_id"], password_is_hashed=True)
                    await api.async_login()
                    self._spaces = await api.async_get_spaces()
                    await api.close()

                    # If only one space, skip selection step
                    if len(self._spaces) == 1:
                        return await self.async_step_select_spaces({
                            "spaces": [self._spaces[0]["id"]]
                        })

                    # Go to space selection step
                    return await self.async_step_select_spaces()

                except Exception as err:
                    _LOGGER.exception("Error fetching spaces: %s", err)
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=get_user_data_schema(self.hass),
            errors=errors,
        )

    async def async_step_totp(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the TOTP (two-factor authentication) step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            totp_code = user_input.get(CONF_TOTP, "").strip()

            if not totp_code:
                errors["base"] = "invalid_totp"
            else:
                # Generate device ID if not already present
                device_id = self._user_input.get(CONF_DEVICE_ID)
                if not device_id:
                    device_id = f"homeassistant_{uuid.uuid4().hex[:16]}"

                # Create API client and try TOTP login
                api = AjaxApi(
                    email=self._user_input[CONF_EMAIL],
                    password=self._user_input[CONF_PASSWORD],
                    device_id=device_id,
                    password_is_hashed=True,
                )

                try:
                    login_result = await api.async_login_with_totp(
                        self._totp_request_id, totp_code
                    )

                    # Login successful, store info
                    self._info = {
                        "title": f"Ajax - {login_result.get('user_name', self._user_input[CONF_EMAIL])}",
                        "device_id": device_id,
                        "user_id": login_result.get("user_id"),
                    }

                    # Set unique ID based on user ID
                    await self.async_set_unique_id(self._info["user_id"])
                    self._abort_if_unique_id_configured()

                    # Get list of spaces for selection
                    try:
                        self._spaces = await api.async_get_spaces()
                        await api.close()

                        # If only one space, skip selection step
                        if len(self._spaces) == 1:
                            return await self.async_step_select_spaces({
                                "spaces": [self._spaces[0]["id"]]
                            })

                        # Go to space selection step
                        return await self.async_step_select_spaces()

                    except Exception as err:
                        _LOGGER.exception("Error fetching spaces: %s", err)
                        errors["base"] = "cannot_connect"

                except AjaxAuthError:
                    errors["base"] = "invalid_totp"
                except AjaxApiError:
                    errors["base"] = "cannot_connect"
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("Unexpected exception during TOTP login")
                    errors["base"] = "unknown"
                finally:
                    await api.close()

        # Show TOTP input form
        return self.async_show_form(
            step_id="totp",
            data_schema=vol.Schema({
                vol.Required(CONF_TOTP): str,
            }),
            errors=errors,
            description_placeholders={
                "email": self._user_input.get(CONF_EMAIL, ""),
            },
        )

    async def async_step_select_spaces(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle space selection step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_spaces = user_input.get("spaces", [])

            if not selected_spaces:
                errors["base"] = "no_spaces_selected"
            else:
                # Create config entry with selected spaces
                return self.async_create_entry(
                    title=self._info["title"],
                    data={
                        CONF_EMAIL: self._user_input[CONF_EMAIL],
                        CONF_PASSWORD: self._user_input[CONF_PASSWORD],
                        CONF_DEVICE_ID: self._info["device_id"],
                        "selected_spaces": selected_spaces,
                    },
                    options={
                        CONF_PERSISTENT_NOTIFICATION: self._user_input.get(CONF_PERSISTENT_NOTIFICATION, False),
                        CONF_NOTIFICATION_FILTER: self._user_input.get(CONF_NOTIFICATION_FILTER, NOTIFICATION_FILTER_NONE),
                    },
                )

        # Build space selection schema
        space_options = {space["id"]: space["name"] for space in self._spaces}

        return self.async_show_form(
            step_id="select_spaces",
            data_schema=vol.Schema({
                vol.Required("spaces", default=list(space_options.keys())): cv.multi_select(space_options),
            }),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Ajax integration."""

    def __init__(self) -> None:
        """Initialize options flow."""
        self._spaces: list[dict[str, Any]] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            # Check if spaces selection changed
            if "spaces" in user_input:
                # Update config entry data with new space selection
                new_data = dict(self.config_entry.data)
                new_data["selected_spaces"] = user_input.pop("spaces")

                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=new_data,
                )

            return self.async_create_entry(title="", data=user_input)

        # Fetch available spaces
        try:
            from .api import AjaxApi
            api = AjaxApi(
                self.config_entry.data[CONF_EMAIL],
                self.config_entry.data[CONF_PASSWORD],
                self.config_entry.data[CONF_DEVICE_ID],
                password_is_hashed=True,
            )
            await api.async_login()
            self._spaces = await api.async_get_spaces()
            await api.close()
        except Exception as err:
            _LOGGER.exception("Error fetching spaces in options: %s", err)
            self._spaces = []

        # Get currently selected spaces (default to all if not set)
        selected_spaces = self.config_entry.data.get("selected_spaces")
        if not selected_spaces and self._spaces:
            # Legacy support: if no selected_spaces, default to all
            selected_spaces = [space["id"] for space in self._spaces]

        # Build options schema
        space_options = {space["id"]: space["name"] for space in self._spaces}

        options_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_PERSISTENT_NOTIFICATION,
                    default=self.config_entry.options.get(
                        CONF_PERSISTENT_NOTIFICATION, False
                    ),
                ): bool,
                vol.Optional(
                    CONF_NOTIFICATION_FILTER,
                    default=self.config_entry.options.get(
                        CONF_NOTIFICATION_FILTER, NOTIFICATION_FILTER_NONE
                    ),
                ): vol.In(
                    {
                        NOTIFICATION_FILTER_NONE: NOTIFICATION_FILTER_NONE,
                        NOTIFICATION_FILTER_ALARMS_ONLY: NOTIFICATION_FILTER_ALARMS_ONLY,
                        NOTIFICATION_FILTER_SECURITY_EVENTS: NOTIFICATION_FILTER_SECURITY_EVENTS,
                        NOTIFICATION_FILTER_ALL: NOTIFICATION_FILTER_ALL,
                    }
                ),
            }
        )

        # Add space selection if spaces are available
        if space_options:
            options_schema = options_schema.extend({
                vol.Optional(
                    "spaces",
                    default=selected_spaces or list(space_options.keys()),
                ): cv.multi_select(space_options),
            })

        return self.async_show_form(step_id="init", data_schema=options_schema)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
