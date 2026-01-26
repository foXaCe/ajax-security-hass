"""Ajax select platform for Home Assistant.

This module creates select entities for Ajax device settings like:
- shockSensorSensitivity: Shock sensor sensitivity (Low, Normal, High)
- indicationBrightness: Socket LED brightness (Min, Max)
- LightSwitchDimmer settings (touch mode, dimmer curve, light source)
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import DOMAIN
from .coordinator import AjaxDataCoordinator
from .devices.siren import SirenHandler
from .models import AjaxDevice, DeviceType, SecurityState

_LOGGER = logging.getLogger(__name__)

# Device handlers that support get_selects()
SELECT_DEVICE_HANDLERS = {
    DeviceType.SIREN: SirenHandler,
}

# Device types that support DoorProtect Plus select settings
DEVICES_WITH_DOOR_PLUS_SELECTS = [
    "DoorProtectPlus",
    "DoorProtectPlusFibra",
    "DoorProtectSPlus",
]

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
DIMMER_SELECT_DEFINITIONS = [
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


def is_dimmer_device(device: AjaxDevice) -> bool:
    """Check if device is a LightSwitchDimmer."""
    raw_type = (device.raw_type or "").lower().replace("_", "").replace(" ", "")
    return "lightswitchdimmer" in raw_type or raw_type == "dimmer"


def is_lightswitch_device(device: AjaxDevice) -> bool:
    """Check if device is a LightSwitch (non-dimmer)."""
    raw_type = (device.raw_type or "").lower().replace("_", "").replace(" ", "")
    return "lightswitch" in raw_type and "dimmer" not in raw_type


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


def _get_dimmer_attr(device: AjaxDevice, attr_key: str):
    """Get nested attribute value from device."""
    if "." in attr_key:
        parts = attr_key.split(".")
        value = device.attributes
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None
        return value
    return device.attributes.get(attr_key)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax select entities from a config entry."""
    coordinator = entry.runtime_data

    if coordinator.account is None:
        return

    entities: list[SelectEntity] = []

    for space_id, space in coordinator.account.spaces.items():
        for device_id, device in space.devices.items():
            device_type = device.raw_type or ""

            # DoorProtect Plus shock sensitivity
            if device_type in DEVICES_WITH_DOOR_PLUS_SELECTS:
                entities.append(AjaxShockSensitivitySelect(coordinator, space_id, device_id))

            # Socket LED brightness (old model with MIN/MAX)
            if device.type == DeviceType.SOCKET and device.attributes.get("indicationBrightness") in ["MIN", "MAX"]:
                entities.append(AjaxLedBrightnessSelect(coordinator, space_id, device_id))

            # SocketOutlet indication mode
            if device.type == DeviceType.SOCKET and "indicationMode" in device.attributes:
                entities.append(AjaxIndicationModeSelect(coordinator, space_id, device_id))

            # LightSwitchDimmer select entities
            if is_dimmer_device(device):
                for select_def in DIMMER_SELECT_DEFINITIONS:
                    # Only create entity if the attribute exists
                    if _get_dimmer_attr(device, select_def["attr_key"]) is not None:
                        entities.append(AjaxDimmerSelect(coordinator, space_id, device_id, select_def))
                        _LOGGER.debug(
                            "Created dimmer select '%s' for device: %s",
                            select_def["key"],
                            device.name,
                        )

            # LightSwitch (non-dimmer) touch mode select
            if is_lightswitch_device(device) and "touchMode" in device.attributes:
                entities.append(AjaxDimmerSelect(coordinator, space_id, device_id, LIGHTSWITCH_TOUCH_MODE_SELECT))
                _LOGGER.debug(
                    "Created LightSwitch select 'touch_mode' for device: %s",
                    device.name,
                )

            # Handler-based selects (sirens, etc.)
            handler_class = SELECT_DEVICE_HANDLERS.get(device.type)
            if handler_class:
                handler = handler_class(device)
                if hasattr(handler, "get_selects"):
                    selects = handler.get_selects()
                    for select_desc in selects:
                        entities.append(AjaxHandlerSelect(coordinator, space_id, device_id, select_desc))

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax select entities", len(entities))


class AjaxDoorPlusBaseSelect(CoordinatorEntity[AjaxDataCoordinator], SelectEntity):
    """Base class for DoorProtect Plus select entities."""

    __slots__ = ("_space_id", "_device_id")

    _attr_has_entity_name = True

    def __init__(self, coordinator: AjaxDataCoordinator, space_id: str, device_id: str) -> None:
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id

    def _get_device(self):
        space = self.coordinator.get_space(self._space_id)
        return space.devices.get(self._device_id) if space else None

    @property
    def available(self) -> bool:
        device = self._get_device()
        return device.online if device else False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._device_id)})

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class AjaxShockSensitivitySelect(AjaxDoorPlusBaseSelect):
    """Select entity for shock sensor sensitivity."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(SHOCK_SENSITIVITY_OPTIONS.values())

    def __init__(self, coordinator: AjaxDataCoordinator, space_id: str, device_id: str) -> None:
        super().__init__(coordinator, space_id, device_id)
        self._attr_unique_id = f"{device_id}_shock_sensitivity"
        self._attr_translation_key = "shock_sensitivity"

    @property
    def current_option(self) -> str | None:
        device = self._get_device()
        if not device:
            return None
        value = device.attributes.get("shock_sensor_sensitivity", 0)
        return SHOCK_SENSITIVITY_OPTIONS.get(value, "low")

    async def async_select_option(self, option: str) -> None:
        """Change the shock sensor sensitivity."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            raise HomeAssistantError("space_not_found")

        value = SHOCK_SENSITIVITY_VALUES.get(option, 0)

        if space.security_state != SecurityState.DISARMED:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="system_armed",
            )

        if not space.hub_id:
            raise HomeAssistantError("hub_not_found")

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

    __slots__ = ("_space_id", "_device_id")

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = LED_BRIGHTNESS_OPTIONS

    def __init__(self, coordinator: AjaxDataCoordinator, space_id: str, device_id: str) -> None:
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_led_brightness"
        self._attr_translation_key = "led_brightness"

    def _get_device(self):
        space = self.coordinator.get_space(self._space_id)
        return space.devices.get(self._device_id) if space else None

    @property
    def available(self) -> bool:
        device = self._get_device()
        if not device or not device.online:
            return False
        # Hide when LED indication is disabled
        return device.attributes.get("indicationEnabled", False)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._device_id)})

    @property
    def current_option(self) -> str | None:
        device = self._get_device()
        if not device:
            return None
        # API returns uppercase (MIN/MAX), convert to lowercase for HA
        value = device.attributes.get("indicationBrightness", "MAX")
        return value.lower() if value else "max"

    async def async_select_option(self, option: str) -> None:
        """Change the LED brightness."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            raise HomeAssistantError("space_not_found")

        if not space.hub_id:
            raise HomeAssistantError("hub_not_found")

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

    __slots__ = ("_space_id", "_device_id")

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(INDICATION_MODE_OPTIONS.values())

    def __init__(self, coordinator: AjaxDataCoordinator, space_id: str, device_id: str) -> None:
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_indication_mode"
        self._attr_translation_key = "indication_mode"

    def _get_device(self):
        space = self.coordinator.get_space(self._space_id)
        return space.devices.get(self._device_id) if space else None

    @property
    def available(self) -> bool:
        device = self._get_device()
        return device.online if device else False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._device_id)})

    @property
    def current_option(self) -> str | None:
        device = self._get_device()
        if not device:
            return None
        api_value = device.attributes.get("indicationMode", "ENABLED")
        return INDICATION_MODE_OPTIONS.get(api_value, "always")

    async def async_select_option(self, option: str) -> None:
        """Change the indication mode."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            raise HomeAssistantError("space_not_found")

        if not space.hub_id:
            raise HomeAssistantError("hub_not_found")

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
        select_def: dict,
    ) -> None:
        """Initialize the dimmer select entity."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._select_def = select_def

        self._attr_unique_id = f"{device_id}_{select_def['key']}"
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
        return DeviceInfo(identifiers={(DOMAIN, self._device_id)})

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
                return option
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from coordinator."""
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Set the select option."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            raise HomeAssistantError("space_not_found")

        if not space.hub_id:
            raise HomeAssistantError("hub_not_found")

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

    __slots__ = ("_space_id", "_device_id", "_select_desc")

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        device_id: str,
        select_desc: dict,
    ) -> None:
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._select_desc = select_desc

        self._attr_unique_id = f"{device_id}_{select_desc['key']}"
        self._attr_translation_key = select_desc.get("translation_key", select_desc["key"])
        self._attr_options = select_desc.get("options", [])

    def _get_device(self):
        space = self.coordinator.get_space(self._space_id)
        return space.devices.get(self._device_id) if space else None

    @property
    def available(self) -> bool:
        device = self._get_device()
        return device.online if device else False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._device_id)})

    @property
    def current_option(self) -> str | None:
        value_fn = self._select_desc.get("value_fn")
        if value_fn:
            return value_fn()
        return None

    async def async_select_option(self, option: str) -> None:
        """Change the select option."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            raise HomeAssistantError("space_not_found")

        if not space.hub_id:
            raise HomeAssistantError("hub_not_found")

        api_key = self._select_desc.get("api_key")
        if not api_key:
            raise HomeAssistantError("No API key configured for this select")

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
