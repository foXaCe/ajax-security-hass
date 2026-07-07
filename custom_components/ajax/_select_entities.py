"""Entity classes and descriptor tables for the Ajax select platform.

Split out of ``select.py`` (which keeps only ``async_setup_entry`` and the
discovery builder). Contains the option tables (shock sensitivity, LED
brightness, indication mode, dimmer definitions) and the entity classes.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._ids import device_identifier
from .const import DOMAIN
from .coordinator import AjaxDataCoordinator
from .devices.door_contact import DOOR_PLUS_DEVICE_TYPES
from .devices.siren import SirenHandler
from .models import AjaxDevice, DeviceType, SecurityState

_LOGGER = logging.getLogger(__name__)

# Device handlers that support get_selects()
SELECT_DEVICE_HANDLERS = {
    DeviceType.SIREN: SirenHandler,
}

# Device types that support DoorProtect Plus select settings
DEVICES_WITH_DOOR_PLUS_SELECTS = DOOR_PLUS_DEVICE_TYPES

# Shock sensitivity options mapping (value -> translation key)
SHOCK_SENSITIVITY_OPTIONS = {
    0: "low",
    4: "normal",
    7: "high",
}
SHOCK_SENSITIVITY_VALUES = {v: k for k, v in SHOCK_SENSITIVITY_OPTIONS.items()}

# LED brightness options for Socket (lowercase for HA translation keys)
LED_BRIGHTNESS_OPTIONS = ["min", "max"]

# Indication mode options for SocketOutlet
INDICATION_MODE_OPTIONS = {
    "ENABLED": "always",
    "DISABLED": "off",
    "IF_ON": "if_on",
}
INDICATION_MODE_VALUES = {v: k for k, v in INDICATION_MODE_OPTIONS.items()}

# LightSwitchDimmer select definitions
# Values are heterogeneous (str / list / dict) by design — typing the whole
# constant as dict[str, Any] avoids 20+ mypy `Collection[str]` complaints on
# every consumer that pulls ``select_def["attr_key"]`` as a str.
DIMMER_SELECT_DEFINITIONS: list[dict[str, Any]] = [
    {
        "key": "touch_mode",
        "translation_key": "touch_mode",
        "attr_key": "touchMode",
        "options": ["touch_mode_toggle_and_slider", "touch_mode_toggle", "touch_mode_blocked"],
        "api_key": "touchMode",
        "api_options": {
            "touch_mode_toggle_and_slider": "TOUCH_MODE_TOGGLE_AND_SLIDER",
            "touch_mode_toggle": "TOUCH_MODE_TOGGLE",
            "touch_mode_blocked": "TOUCH_MODE_BLOCKED",
        },
    },
    {
        "key": "dimmer_curve",
        "translation_key": "dimmer_curve",
        "attr_key": "dimmerSettings.curveType",
        "options": ["curve_type_auto", "curve_type_linear", "curve_type_logarithmic"],
        "api_nested_key": "dimmerSettings",
        "api_key": "curveType",
        "api_options": {
            "curve_type_auto": "CURVE_TYPE_AUTO",
            "curve_type_linear": "CURVE_TYPE_LINEAR",
            "curve_type_logarithmic": "CURVE_TYPE_LOGARITHMIC",
        },
    },
    {
        "key": "light_source",
        "translation_key": "light_source",
        "attr_key": "dimmerSettings.lightSource",
        "options": ["light_source_auto", "light_source_leading_edge", "light_source_trailing_edge"],
        "api_nested_key": "dimmerSettings",
        "api_key": "lightSource",
        "api_options": {
            "light_source_auto": "LIGHT_SOURCE_AUTO",
            "light_source_leading_edge": "LIGHT_SOURCE_LEADING_EDGE",
            "light_source_trailing_edge": "LIGHT_SOURCE_TRAILING_EDGE",
        },
    },
]


# LightSwitch touch mode select definition
LIGHTSWITCH_TOUCH_MODE_SELECT = {
    "key": "touch_mode",
    "translation_key": "touch_mode",
    "attr_key": "touchMode",
    "api_key": "touchMode",
    "options": ["touch_mode_toggle_and_slider", "touch_mode_toggle", "touch_mode_blocked"],
    "api_options": {
        "touch_mode_toggle_and_slider": "TOUCH_MODE_TOGGLE_AND_SLIDER",
        "touch_mode_toggle": "TOUCH_MODE_TOGGLE",
        "touch_mode_blocked": "TOUCH_MODE_BLOCKED",
    },
}


def _get_dimmer_attr(device: AjaxDevice, attr_key: str) -> Any:
    """Get nested attribute value from device."""
    if "." in attr_key:
        parts = attr_key.split(".")
        value: Any = device.attributes
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None
        return value
    return device.attributes.get(attr_key)


class AjaxDoorPlusBaseSelect(CoordinatorEntity[AjaxDataCoordinator], SelectEntity):
    """Base class for DoorProtect Plus select entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AjaxDataCoordinator, space_id: str, device_id: str) -> None:
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id

    def _get_device(self) -> AjaxDevice | None:
        space = self.coordinator.get_space(self._space_id)
        return space.devices.get(self._device_id) if space else None

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        device = self._get_device()
        return device.online if device else False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={device_identifier(self.coordinator.entry_id, self._device_id)})

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class AjaxShockSensitivitySelect(AjaxDoorPlusBaseSelect):
    """Select entity for shock sensor sensitivity."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(SHOCK_SENSITIVITY_OPTIONS.values())

    def __init__(self, coordinator: AjaxDataCoordinator, space_id: str, device_id: str) -> None:
        super().__init__(coordinator, space_id, device_id)
        self._attr_unique_id = f"{self.coordinator.entry_id}_{device_id}_shock_sensitivity"
        self._attr_translation_key = "shock_sensitivity"

    @property
    def current_option(self) -> str | None:
        device = self._get_device()
        if not device:
            return None
        value = device.attributes.get("shock_sensor_sensitivity")
        # Return None for unmapped values so HA displays "unknown" rather
        # than forcing a wrong option.
        return SHOCK_SENSITIVITY_OPTIONS.get(value)  # type: ignore[arg-type]

    async def async_select_option(self, option: str) -> None:
        """Change the shock sensor sensitivity."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="space_not_found")

        value = SHOCK_SENSITIVITY_VALUES.get(option, 0)

        if space.security_state != SecurityState.DISARMED:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="system_armed",
            )

        if not space.hub_id:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="hub_not_found")

        try:
            await self.coordinator.api.async_update_device(
                space.hub_id, self._device_id, {"shockSensorSensitivity": value}
            )
            _LOGGER.info(
                "Set shockSensorSensitivity=%d (%s) for device %s",
                value,
                option,
                self._device_id,
            )
            await self.coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_to_change",
                translation_placeholders={
                    "entity": "shock sensitivity level",
                    "error": str(err),
                },
            ) from err


class AjaxLedBrightnessSelect(CoordinatorEntity[AjaxDataCoordinator], SelectEntity):
    """Select entity for Socket LED brightness."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = LED_BRIGHTNESS_OPTIONS

    def __init__(self, coordinator: AjaxDataCoordinator, space_id: str, device_id: str) -> None:
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._attr_unique_id = f"{self.coordinator.entry_id}_{device_id}_led_brightness"
        self._attr_translation_key = "led_brightness"

    def _get_device(self) -> AjaxDevice | None:
        space = self.coordinator.get_space(self._space_id)
        return space.devices.get(self._device_id) if space else None

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        device = self._get_device()
        if not device or not device.online:
            return False
        # Hide when LED indication is disabled
        return device.attributes.get("indicationEnabled", False)  # type: ignore[no-any-return]

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={device_identifier(self.coordinator.entry_id, self._device_id)})

    @property
    def current_option(self) -> str | None:
        device = self._get_device()
        if not device:
            return None
        # API returns uppercase (MIN/MAX), convert to lowercase for HA
        value = device.attributes.get("indicationBrightness")
        if not isinstance(value, str):
            return None
        lowered = value.lower()
        return lowered if lowered in LED_BRIGHTNESS_OPTIONS else None

    async def async_select_option(self, option: str) -> None:
        """Change the LED brightness."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="space_not_found")

        if not space.hub_id:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="hub_not_found")

        # Convert lowercase HA option to uppercase for API
        api_value = option.upper()

        try:
            await self.coordinator.api.async_update_device(
                space.hub_id, self._device_id, {"indicationBrightness": api_value}
            )
            _LOGGER.info(
                "Set indicationBrightness=%s for device %s",
                api_value,
                self._device_id,
            )
            await self.coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_to_change",
                translation_placeholders={
                    "entity": "LED brightness",
                    "error": str(err),
                },
            ) from err

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class AjaxIndicationModeSelect(CoordinatorEntity[AjaxDataCoordinator], SelectEntity):
    """Select entity for SocketOutlet indication mode (LED backlight mode)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(INDICATION_MODE_OPTIONS.values())

    def __init__(self, coordinator: AjaxDataCoordinator, space_id: str, device_id: str) -> None:
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._attr_unique_id = f"{self.coordinator.entry_id}_{device_id}_indication_mode"
        self._attr_translation_key = "indication_mode"

    def _get_device(self) -> AjaxDevice | None:
        space = self.coordinator.get_space(self._space_id)
        return space.devices.get(self._device_id) if space else None

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        device = self._get_device()
        return device.online if device else False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={device_identifier(self.coordinator.entry_id, self._device_id)})

    @property
    def current_option(self) -> str | None:
        device = self._get_device()
        if not device:
            return None
        api_value = device.attributes.get("indicationMode")
        return INDICATION_MODE_OPTIONS.get(api_value)  # type: ignore[arg-type]

    async def async_select_option(self, option: str) -> None:
        """Change the indication mode."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="space_not_found")

        if not space.hub_id:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="hub_not_found")

        api_value = INDICATION_MODE_VALUES.get(option, "ENABLED")

        try:
            await self.coordinator.api.async_update_device(space.hub_id, self._device_id, {"indicationMode": api_value})
            _LOGGER.info(
                "Set indicationMode=%s for device %s",
                api_value,
                self._device_id,
            )
            await self.coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_to_change",
                translation_placeholders={
                    "entity": "indication mode",
                    "error": str(err),
                },
            ) from err

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class AjaxDimmerSelect(CoordinatorEntity[AjaxDataCoordinator], SelectEntity):
    """Select entity for LightSwitchDimmer settings."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        device_id: str,
        select_def: dict[str, Any],
    ) -> None:
        """Initialize the dimmer select entity."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._select_def = select_def

        self._attr_unique_id = f"{self.coordinator.entry_id}_{device_id}_{select_def['key']}"
        self._attr_translation_key = select_def["translation_key"]
        self._attr_options = select_def["options"]

    def _get_device(self) -> AjaxDevice | None:
        """Get current device from coordinator."""
        space = self.coordinator.get_space(self._space_id)
        return space.devices.get(self._device_id) if space else None

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        device = self._get_device()
        return self.coordinator.last_update_success and device is not None and device.online

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(identifiers={device_identifier(self.coordinator.entry_id, self._device_id)})

    @property
    def current_option(self) -> str | None:
        """Return current option from device attributes."""
        device = self._get_device()
        if not device:
            return None
        value = _get_dimmer_attr(device, self._select_def["attr_key"])
        if value is None:
            return None
        # Convert API value to HA option
        value_lower = str(value).lower()
        for option in self._select_def["options"]:
            if option == value_lower:
                return option  # type: ignore[no-any-return]
        return None

    async def async_select_option(self, option: str) -> None:
        """Set the select option."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="space_not_found")

        if not space.hub_id:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="hub_not_found")

        # Get API value from option
        api_options = self._select_def.get("api_options", {})
        api_value = api_options.get(option, option)
        api_key = self._select_def["api_key"]
        api_nested_key = self._select_def.get("api_nested_key")

        # Build payload
        if api_nested_key:
            payload = {api_nested_key: {api_key: api_value}}
        else:
            payload = {api_key: api_value}

        try:
            if api_nested_key:
                await self.coordinator.api.async_update_device_nested(space.hub_id, self._device_id, payload)
            else:
                await self.coordinator.api.async_update_device(space.hub_id, self._device_id, payload)
            _LOGGER.info(
                "Set %s=%s for device %s",
                api_key,
                api_value,
                self._device_id,
            )
            await self.coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_to_change",
                translation_placeholders={
                    "entity": self._select_def["key"],
                    "error": str(err),
                },
            ) from err


class AjaxHandlerSelect(CoordinatorEntity[AjaxDataCoordinator], SelectEntity):
    """Generic select entity created from device handler definitions (for sirens, etc.)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        device_id: str,
        select_desc: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._select_desc = select_desc

        self._attr_unique_id = f"{self.coordinator.entry_id}_{device_id}_{select_desc['key']}"
        self._attr_translation_key = select_desc.get("translation_key", select_desc["key"])
        self._attr_options = select_desc.get("options", [])

    def _get_device(self) -> AjaxDevice | None:
        space = self.coordinator.get_space(self._space_id)
        return space.devices.get(self._device_id) if space else None

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        device = self._get_device()
        return device.online if device else False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={device_identifier(self.coordinator.entry_id, self._device_id)})

    @property
    def current_option(self) -> str | None:
        value_fn = self._select_desc.get("value_fn")
        if value_fn:
            return value_fn()  # type: ignore[no-any-return]
        return None

    async def async_select_option(self, option: str) -> None:
        """Change the select option."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="space_not_found")

        if not space.hub_id:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="hub_not_found")

        api_key = self._select_desc.get("api_key")
        if not api_key:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_api_key")

        # Transform the option value if needed
        api_options = self._select_desc.get("api_options")
        if api_options:
            # Use api_options mapping (HA value -> API value)
            api_value = api_options.get(option, option)
        else:
            api_transform = self._select_desc.get("api_transform")
            api_value = api_transform(option) if api_transform else option

        # Build payload (handle nested keys like dimmerSettings.curveType)
        api_nested_key = self._select_desc.get("api_nested_key")
        if api_nested_key:
            payload = {api_nested_key: {api_key: api_value}}
        else:
            payload = {api_key: api_value}

        try:
            # Use nested update for settings inside nested structures (e.g., dimmerSettings)
            if api_nested_key:
                await self.coordinator.api.async_update_device_nested(space.hub_id, self._device_id, payload)
            else:
                await self.coordinator.api.async_update_device(space.hub_id, self._device_id, payload)
            _LOGGER.info(
                "Set %s=%s for device %s",
                api_key,
                api_value,
                self._device_id,
            )
            await self.coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_to_change",
                translation_placeholders={
                    "entity": self._select_desc.get("key", "select"),
                    "error": str(err),
                },
            ) from err

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
