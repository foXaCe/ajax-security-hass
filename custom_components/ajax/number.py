"""Ajax number platform for Home Assistant.

This module creates number entities for Ajax device settings like:
- accelerometerTiltDegrees: Tilt angle threshold (5-25)
- currentThresholdAmpere: Current limit threshold for sockets (1-16A)
"""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import DOMAIN
from .coordinator import AjaxDataCoordinator
from .models import DeviceType, SecurityState

_LOGGER = logging.getLogger(__name__)

# Device types that support DoorProtect Plus number settings
DEVICES_WITH_DOOR_PLUS_NUMBERS = [
    "DoorProtectPlus",
    "DoorProtectPlusFibra",
    "DoorProtectSPlus",
]

# Device types that support current threshold
DEVICES_WITH_CURRENT_THRESHOLD = [
    DeviceType.SOCKET,
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax number entities from a config entry."""
    coordinator = entry.runtime_data

    if coordinator.account is None:
        return

    entities: list[NumberEntity] = []

    for space_id, space in coordinator.account.spaces.items():
        for device_id, device in space.devices.items():
            device_type_raw = device.raw_type or ""

            if device_type_raw in DEVICES_WITH_DOOR_PLUS_NUMBERS:
                # Tilt degrees
                entities.append(AjaxTiltDegreesNumber(coordinator, space_id, device_id))
                _LOGGER.debug(
                    "Created tilt degrees number entity for device: %s",
                    device.name,
                )

            # Current threshold for Socket devices
            if device.type in DEVICES_WITH_CURRENT_THRESHOLD and "current_threshold" in device.attributes:
                entities.append(AjaxCurrentThresholdNumber(coordinator, space_id, device_id))
                _LOGGER.debug(
                    "Created current threshold number entity for device: %s",
                    device.name,
                )

            # LED brightness V2 for SocketOutlet (1-8 slider)
            if device.type in DEVICES_WITH_CURRENT_THRESHOLD and "indicationBrightness" in device.attributes:
                # Only create V2 brightness if it's an integer (not MIN/MAX string)
                brightness = device.attributes.get("indicationBrightness")
                if isinstance(brightness, int):
                    entities.append(AjaxLedBrightnessV2Number(coordinator, space_id, device_id))
                    _LOGGER.debug(
                        "Created LED brightness number entity for device: %s",
                        device.name,
                    )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax number entit(ies)", len(entities))


class AjaxDoorPlusBaseNumber(CoordinatorEntity[AjaxDataCoordinator], NumberEntity):
    """Base class for DoorProtect Plus number entities."""

    __slots__ = ("_space_id", "_device_id")

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER

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


class AjaxTiltDegreesNumber(AjaxDoorPlusBaseNumber):
    """Number entity for tilt angle threshold."""

    _attr_native_min_value = 5
    _attr_native_max_value = 25
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "°"

    def __init__(self, coordinator: AjaxDataCoordinator, space_id: str, device_id: str) -> None:
        super().__init__(coordinator, space_id, device_id)
        self._attr_unique_id = f"{device_id}_tilt_degrees"
        self._attr_translation_key = "tilt_degrees"
        self._attr_entity_category = EntityCategory.CONFIG

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
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="space_not_found",
            )

        if space.security_state != SecurityState.DISARMED:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="system_armed",
            )

        if not space.hub_id:
            raise HomeAssistantError("hub_not_found")

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
                    "error": str(err),
                },
            ) from err


class AjaxCurrentThresholdNumber(CoordinatorEntity[AjaxDataCoordinator], NumberEntity):
    """Number entity for socket current threshold (protection limit)."""

    __slots__ = ("_space_id", "_device_id")

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 1
    _attr_native_max_value = 16
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: AjaxDataCoordinator, space_id: str, device_id: str) -> None:
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_current_threshold"
        self._attr_translation_key = "current_threshold"

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
    def native_value(self) -> float | None:
        device = self._get_device()
        if not device:
            return None
        return device.attributes.get("current_threshold")

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Set the current threshold."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="space_not_found",
            )

        if not space.hub_id:
            raise HomeAssistantError("hub_not_found")

        try:
            await self.coordinator.api.async_update_device(
                space.hub_id, self._device_id, {"currentThresholdAmpere": int(value)}
            )
            _LOGGER.info(
                "Set currentThresholdAmpere=%d for device %s",
                int(value),
                self._device_id,
            )
            await self.coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_to_change",
                translation_placeholders={
                    "entity": "current threshold",
                    "error": str(err),
                },
            ) from err


class AjaxLedBrightnessV2Number(CoordinatorEntity[AjaxDataCoordinator], NumberEntity):
    """Number entity for SocketOutlet LED brightness (1-8 scale)."""

    __slots__ = ("_space_id", "_device_id")

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 1
    _attr_native_max_value = 8
    _attr_native_step = 1
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: AjaxDataCoordinator, space_id: str, device_id: str) -> None:
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_led_brightness"
        self._attr_translation_key = "led_brightness_level"

    def _get_device(self):
        space = self.coordinator.get_space(self._space_id)
        return space.devices.get(self._device_id) if space else None

    @property
    def available(self) -> bool:
        device = self._get_device()
        if not device or not device.online:
            return False
        # Only available when indication mode is not DISABLED
        return device.attributes.get("indicationMode") != "DISABLED"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._device_id)})

    @property
    def native_value(self) -> float | None:
        device = self._get_device()
        if not device:
            return None
        return device.attributes.get("indicationBrightness", 8)

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Set the LED brightness level."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="space_not_found",
            )

        if not space.hub_id:
            raise HomeAssistantError("hub_not_found")

        try:
            await self.coordinator.api.async_update_device(
                space.hub_id, self._device_id, {"indicationBrightnessV2": int(value)}
            )
            _LOGGER.info(
                "Set indicationBrightnessV2=%d for device %s",
                int(value),
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
