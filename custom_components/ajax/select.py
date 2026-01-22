"""Ajax select platform for Home Assistant.

This module creates select entities for Ajax device settings like:
- shockSensorSensitivity: Shock sensor sensitivity (Désactivé, Faible, Normal, Élevé)
- indicationBrightness: Socket LED brightness (Min, Max)
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import DOMAIN
from .coordinator import AjaxDataCoordinator
from .devices.siren import SirenHandler
from .models import DeviceType, SecurityState

_LOGGER = logging.getLogger(__name__)

# Device handlers that support get_selects()
SELECT_DEVICE_HANDLERS = {
    DeviceType.SIREN: SirenHandler,
}

# Device types that support DoorProtect Plus select settings
DEVICES_WITH_DOOR_PLUS_SELECTS = [
    "DoorProtectPlus",
    "DoorProtectPlusFibra",
]

# Shock sensitivity options mapping (value -> translation key)
# Ajax API values: 0=low, 4=normal, 7=high (confirmed via testing)
SHOCK_SENSITIVITY_OPTIONS = {
    0: "low",
    4: "normal",
    7: "high",
}

# Reverse mapping (key -> value)
SHOCK_SENSITIVITY_VALUES = {v: k for k, v in SHOCK_SENSITIVITY_OPTIONS.items()}

# LED brightness options for Socket (lowercase for HA translation keys)
LED_BRIGHTNESS_OPTIONS = ["min", "max"]

# Indication mode options for SocketOutlet (ENABLED=Always, DISABLED=Off, IF_ON=If activated)
INDICATION_MODE_OPTIONS = {
    "ENABLED": "always",
    "DISABLED": "off",
    "IF_ON": "if_on",
}
INDICATION_MODE_VALUES = {v: k for k, v in INDICATION_MODE_OPTIONS.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax select entities from a config entry."""
    coordinator = entry.runtime_data

    entities: list[SelectEntity] = []

    for space_id, space in coordinator.account.spaces.items():
        for device_id, device in space.devices.items():
            device_type = device.raw_type or ""

            if device_type in DEVICES_WITH_DOOR_PLUS_SELECTS:
                # Shock sensor sensitivity
                entities.append(AjaxShockSensitivitySelect(coordinator, space_id, device_id))
                _LOGGER.debug(
                    "Created select entities for device: %s",
                    device.name,
                )

            # Socket LED brightness (old Socket model with MIN/MAX)
            if device.type == DeviceType.SOCKET and device.attributes.get("indicationBrightness") in ["MIN", "MAX"]:
                entities.append(AjaxLedBrightnessSelect(coordinator, space_id, device_id))
                _LOGGER.debug(
                    "Created LED brightness select for device: %s",
                    device.name,
                )

            # SocketOutlet indication mode (ENABLED/DISABLED/IF_ON)
            if device.type == DeviceType.SOCKET and "indicationMode" in device.attributes:
                entities.append(AjaxIndicationModeSelect(coordinator, space_id, device_id))
                _LOGGER.debug(
                    "Created indication mode select for device: %s",
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
                        _LOGGER.debug(
                            "Created select '%s' for device: %s (type: %s)",
                            select_desc.get("key"),
                            device.name,
                            device.type.value if device.type else "unknown",
                        )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax select entit(ies)", len(entities))


class AjaxDoorPlusBaseSelect(CoordinatorEntity[AjaxDataCoordinator], SelectEntity):
    """Base class for DoorProtect Plus select entities."""

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
    def device_info(self) -> dict[str, Any]:
        return {"identifiers": {(DOMAIN, self._device_id)}}

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
                    "error": err,
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
    def device_info(self) -> dict[str, Any]:
        return {"identifiers": {(DOMAIN, self._device_id)}}

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
                    "error": err,
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
    def device_info(self) -> dict[str, Any]:
        return {"identifiers": {(DOMAIN, self._device_id)}}

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


class AjaxHandlerSelect(CoordinatorEntity[AjaxDataCoordinator], SelectEntity):
    """Generic select entity created from device handler definitions."""

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
    def device_info(self) -> dict[str, Any]:
        return {"identifiers": {(DOMAIN, self._device_id)}}

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

        api_key = self._select_desc.get("api_key")
        if not api_key:
            raise HomeAssistantError("No API key configured for this select")

        # Transform the option value if needed
        api_transform = self._select_desc.get("api_transform")
        api_value = api_transform(option) if api_transform else option

        try:
            await self.coordinator.api.async_update_device(space.hub_id, self._device_id, {api_key: api_value})
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
