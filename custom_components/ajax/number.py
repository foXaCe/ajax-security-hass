"""Ajax number platform for Home Assistant.

This module creates number entities for Ajax device settings like:
- accelerometerTiltDegrees: Tilt angle threshold (5-25)
- currentThresholdAmpere: Current limit threshold for sockets (1-16A)
- LightSwitchDimmer settings (touch sensitivity, brightness limits, etc.)
"""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import DOMAIN
from .coordinator import AjaxDataCoordinator
from .models import AjaxDevice, DeviceType, SecurityState

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

# LightSwitchDimmer number definitions - direct attribute mapping
DIMMER_NUMBER_DEFINITIONS = [
    {
        "key": "touch_sensitivity",
        "translation_key": "touch_sensitivity",
        "attr_key": "touchSensitivity",
        "min_value": 1,
        "max_value": 7,
        "step": 1,
        "api_key": "touchSensitivity",
        "entity_category": "config",
    },
    {
        "key": "brightness_change_speed",
        "translation_key": "brightness_change_speed",
        "attr_key": "brightnessChangeSpeed",
        "min_value": 0,
        "max_value": 20,
        "step": 1,
        "api_key": "brightnessChangeSpeed",
        "entity_category": "config",
    },
    {
        "key": "min_brightness",
        "translation_key": "min_brightness",
        "attr_key": "minBrightnessLimitCh1",
        "min_value": 0,
        "max_value": 100,
        "step": 1,
        "unit": PERCENTAGE,
        "api_key": "minBrightnessLimitCh1",
        "entity_category": "config",
    },
    {
        "key": "max_brightness",
        "translation_key": "max_brightness",
        "attr_key": "maxBrightnessLimitCh1",
        "min_value": 0,
        "max_value": 100,
        "step": 1,
        "unit": PERCENTAGE,
        "api_key": "maxBrightnessLimitCh1",
        "entity_category": "config",
    },
    {
        "key": "arm_brightness",
        "translation_key": "arm_brightness",
        "attr_key": "armActionBrightnessCh1",
        "min_value": 0,
        "max_value": 100,
        "step": 1,
        "unit": PERCENTAGE,
        "api_key": "armActionBrightnessCh1",
        "entity_category": "config",
    },
    {
        "key": "disarm_brightness",
        "translation_key": "disarm_brightness",
        "attr_key": "disarmActionBrightnessCh1",
        "min_value": 0,
        "max_value": 100,
        "step": 1,
        "unit": PERCENTAGE,
        "api_key": "disarmActionBrightnessCh1",
        "entity_category": "config",
    },
]


def is_dimmer_device(device: AjaxDevice) -> bool:
    """Check if device is a LightSwitchDimmer."""
    raw_type = (device.raw_type or "").lower().replace("_", "").replace(" ", "")
    return "lightswitchdimmer" in raw_type or raw_type == "dimmer"


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

            # DoorProtect Plus tilt degrees
            if device_type_raw in DEVICES_WITH_DOOR_PLUS_NUMBERS:
                entities.append(AjaxTiltDegreesNumber(coordinator, space_id, device_id))

            # Socket current threshold
            if device.type in DEVICES_WITH_CURRENT_THRESHOLD and "current_threshold" in device.attributes:
                entities.append(AjaxCurrentThresholdNumber(coordinator, space_id, device_id))

            # SocketOutlet LED brightness V2 (1-8 slider)
            if device.type in DEVICES_WITH_CURRENT_THRESHOLD:
                brightness = device.attributes.get("indicationBrightness")
                if isinstance(brightness, int):
                    entities.append(AjaxLedBrightnessV2Number(coordinator, space_id, device_id))

            # LightSwitchDimmer number entities
            if is_dimmer_device(device):
                for number_def in DIMMER_NUMBER_DEFINITIONS:
                    # Only create entity if the attribute exists
                    if number_def["attr_key"] in device.attributes:
                        entities.append(
                            AjaxDimmerNumber(
                                coordinator=coordinator,
                                space_id=space_id,
                                device_id=device_id,
                                number_def=number_def,
                            )
                        )
                        _LOGGER.debug(
                            "Created dimmer number '%s' for device: %s",
                            number_def["key"],
                            device.name,
                        )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax number entities", len(entities))


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


class AjaxDimmerNumber(CoordinatorEntity[AjaxDataCoordinator], NumberEntity):
    """Number entity for LightSwitchDimmer settings."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        device_id: str,
        number_def: dict,
    ) -> None:
        """Initialize the dimmer number entity."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._number_def = number_def

        self._attr_unique_id = f"{device_id}_{number_def['key']}"
        self._attr_translation_key = number_def["translation_key"]
        self._attr_native_min_value = number_def["min_value"]
        self._attr_native_max_value = number_def["max_value"]
        self._attr_native_step = number_def["step"]
        self._attr_native_unit_of_measurement = number_def.get("unit")

        if number_def.get("entity_category") == "config":
            self._attr_entity_category = EntityCategory.CONFIG
        elif number_def.get("entity_category") == "diagnostic":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

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
    def native_value(self) -> float | None:
        """Return current value from device attributes."""
        device = self._get_device()
        if not device:
            return None
        return device.attributes.get(self._number_def["attr_key"])

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from coordinator."""
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Set the number value."""
        space = self.coordinator.get_space(self._space_id)
        device = self._get_device()
        if not space or not device:
            raise HomeAssistantError("Device not found")

        if not space.hub_id:
            raise HomeAssistantError("Hub not found")

        api_key = self._number_def["api_key"]

        try:
            await self.coordinator.api.async_update_device(space.hub_id, self._device_id, {api_key: int(value)})
            _LOGGER.info(
                "Set %s=%d for device %s",
                api_key,
                int(value),
                self._device_id,
            )
            await self.coordinator.async_request_refresh()
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_to_change",
                translation_placeholders={
                    "entity": self._number_def["key"],
                    "error": str(err),
                },
            ) from err
