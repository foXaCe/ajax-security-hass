"""Ajax select platform for Home Assistant.

This module creates select entities for Ajax device settings like:
- shockSensorSensitivity: Shock sensor sensitivity (Désactivé, Faible, Normal, Élevé)
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import DOMAIN
from .coordinator import AjaxDataCoordinator
from .models import SecurityState

_LOGGER = logging.getLogger(__name__)

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
                entities.append(
                    AjaxShockSensitivitySelect(coordinator, space_id, device_id)
                )
                _LOGGER.debug(
                    "Created select entities for device: %s",
                    device.name,
                )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax select entit(ies)", len(entities))


class AjaxDoorPlusBaseSelect(CoordinatorEntity[AjaxDataCoordinator], SelectEntity):
    """Base class for DoorProtect Plus select entities."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: AjaxDataCoordinator, space_id: str, device_id: str
    ) -> None:
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

    _attr_options = list(SHOCK_SENSITIVITY_OPTIONS.values())

    def __init__(
        self, coordinator: AjaxDataCoordinator, space_id: str, device_id: str
    ) -> None:
        super().__init__(coordinator, space_id, device_id)
        self._attr_unique_id = f"{device_id}_shock_sensitivity"
        self._attr_translation_key = "shock_sensitivity"
        self._attr_icon = "mdi:vibrate"

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
