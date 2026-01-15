"""Ajax number platform for Home Assistant.

This module creates number entities for Ajax device settings like:
- accelerometerTiltDegrees: Tilt angle threshold (5-25)
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import DOMAIN
from .coordinator import AjaxDataCoordinator
from .models import SecurityState

_LOGGER = logging.getLogger(__name__)

# Device types that support DoorProtect Plus number settings
DEVICES_WITH_DOOR_PLUS_NUMBERS = [
    "DoorProtectPlus",
    "DoorProtectPlusFibra",
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax number entities from a config entry."""
    coordinator = entry.runtime_data

    entities: list[NumberEntity] = []

    for space_id, space in coordinator.account.spaces.items():
        for device_id, device in space.devices.items():
            device_type = device.raw_type or ""

            if device_type in DEVICES_WITH_DOOR_PLUS_NUMBERS:
                # Tilt degrees
                entities.append(AjaxTiltDegreesNumber(coordinator, space_id, device_id))
                _LOGGER.debug(
                    "Created number entities for device: %s",
                    device.name,
                )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax number entit(ies)", len(entities))


class AjaxDoorPlusBaseNumber(CoordinatorEntity[AjaxDataCoordinator], NumberEntity):
    """Base class for DoorProtect Plus number entities."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER

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


class AjaxTiltDegreesNumber(AjaxDoorPlusBaseNumber):
    """Number entity for tilt angle threshold."""

    _attr_native_min_value = 5
    _attr_native_max_value = 25
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "°"

    def __init__(
        self, coordinator: AjaxDataCoordinator, space_id: str, device_id: str
    ) -> None:
        super().__init__(coordinator, space_id, device_id)
        self._attr_unique_id = f"{device_id}_tilt_degrees"
        self._attr_translation_key = "tilt_degrees"
        self._attr_icon = "mdi:angle-acute"

    @property
    def native_value(self) -> float | None:
        device = self._get_device()
        if not device:
            return None
        return device.attributes.get("accelerometer_tilt_degrees", 5)

    async def async_set_native_value(self, value: float) -> None:
        """Set the tilt degrees threshold."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            raise HomeAssistantError("space_not_found")

        if space.security_state != SecurityState.DISARMED:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="system_armed",
            )

        try:
            await self.coordinator.api.async_update_device(
                space.hub_id, self._device_id, {"accelerometerTiltDegrees": int(value)}
            )
            _LOGGER.info(
                "Set accelerometerTiltDegrees=%d for device %s",
                int(value),
                self._device_id,
            )
            await self.coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_to_change",
                translation_placeholders={
                    "entity": "accelerometer tilt degrees",
                    "error": err,
                },
            ) from err
