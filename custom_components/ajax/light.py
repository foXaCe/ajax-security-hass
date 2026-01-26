"""Ajax light platform for dimmable switches.

This module creates light entities for:
- LightSwitchDimmer (dimmable wall switch)
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import DOMAIN, MANUFACTURER
from .coordinator import AjaxDataCoordinator
from .models import AjaxDevice, DeviceType

_LOGGER = logging.getLogger(__name__)

# Device types that support dimming
DIMMABLE_DEVICE_TYPES = {DeviceType.WALLSWITCH}

# Raw device types that are dimmers
DIMMER_RAW_TYPES = {"lightswitchdimmer", "light_switch_dimmer"}


def is_dimmer_device(device: AjaxDevice) -> bool:
    """Check if device is a LightSwitchDimmer."""
    raw_type = (device.raw_type or "").lower().replace("_", "")
    return raw_type in DIMMER_RAW_TYPES or "dimmer" in raw_type


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax light entities from a config entry."""
    coordinator = entry.runtime_data

    if coordinator.account is None:
        return

    entities: list[LightEntity] = []

    for space_id, space in coordinator.account.spaces.items():
        for device_id, device in space.devices.items():
            # Only create light entities for dimmer devices
            if device.type in DIMMABLE_DEVICE_TYPES and is_dimmer_device(device):
                entities.append(
                    AjaxDimmerLight(
                        coordinator=coordinator,
                        space_id=space_id,
                        device_id=device_id,
                    )
                )
                _LOGGER.debug(
                    "Created light entity for dimmer device: %s",
                    device.name,
                )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax light entit(ies)", len(entities))


class AjaxDimmerLight(CoordinatorEntity[AjaxDataCoordinator], LightEntity):
    """Representation of an Ajax dimmable light switch."""

    __slots__ = ("_space_id", "_device_id")

    _attr_has_entity_name = True
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        device_id: str,
    ) -> None:
        """Initialize the light."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_light"

    def _get_device(self) -> AjaxDevice | None:
        """Get the device from coordinator data."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None
        return space.devices.get(self._device_id)

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device info."""
        device = self._get_device()
        if not device:
            return None
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=device.name,
            manufacturer=MANUFACTURER,
            model=device.raw_type or "LightSwitch Dimmer",
            sw_version=device.firmware_version,
            via_device=(DOMAIN, self._space_id),
        )

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        device = self._get_device()
        return self.coordinator.last_update_success and device is not None and device.online

    @property
    def is_on(self) -> bool:
        """Return true if light is on."""
        device = self._get_device()
        if not device:
            return False
        # Check channelStatuses for ON state
        channel_statuses = device.attributes.get("channelStatuses", [])
        return "CHANNEL_1_ON" in channel_statuses

    @property
    def brightness(self) -> int | None:
        """Return the brightness of the light (0-255)."""
        device = self._get_device()
        if not device:
            return None
        # Ajax uses 0-100%, Home Assistant uses 0-255
        brightness_percent = device.attributes.get("actualBrightnessCh1", 0)
        if brightness_percent is None:
            return None
        return int((brightness_percent / 100) * 255)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light."""
        space = self.coordinator.get_space(self._space_id)
        device = self._get_device()
        if not space or not device:
            _LOGGER.error("Space or device not found for light %s", self._device_id)
            return

        if not space.hub_id:
            _LOGGER.error("Hub ID not found for space %s", self._space_id)
            return

        brightness = kwargs.get(ATTR_BRIGHTNESS)

        if brightness is not None:
            # Convert HA brightness (0-255) to Ajax percentage (0-100)
            brightness_percent = int((brightness / 255) * 100)
        else:
            # Use current brightness or 100%
            current = device.attributes.get("actualBrightnessCh1", 100)
            brightness_percent = current if current > 0 else 100

        # Save old state for rollback
        old_brightness = device.attributes.get("actualBrightnessCh1", 0)
        old_statuses = device.attributes.get("channelStatuses", [])

        # Optimistic update
        device.attributes["actualBrightnessCh1"] = brightness_percent
        device.attributes["channelStatuses"] = ["CHANNEL_1_ON"]
        self.async_write_ha_state()

        try:
            await self.coordinator.api.async_set_dimmer_brightness(
                hub_id=space.hub_id,
                device_id=self._device_id,
                brightness=brightness_percent,
            )
            _LOGGER.debug("Dimmer %s turned on at %d%%", self._device_id, brightness_percent)
        except Exception as err:
            _LOGGER.error("Failed to turn on dimmer %s: %s", self._device_id, err)
            # Rollback on error
            device.attributes["actualBrightnessCh1"] = old_brightness
            device.attributes["channelStatuses"] = old_statuses
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        space = self.coordinator.get_space(self._space_id)
        device = self._get_device()
        if not space or not device:
            _LOGGER.error("Space or device not found for light %s", self._device_id)
            return

        if not space.hub_id:
            _LOGGER.error("Hub ID not found for space %s", self._space_id)
            return

        # Save old state for rollback
        old_brightness = device.attributes.get("actualBrightnessCh1", 0)
        old_statuses = device.attributes.get("channelStatuses", [])

        # Optimistic update
        device.attributes["actualBrightnessCh1"] = 0
        device.attributes["channelStatuses"] = []
        self.async_write_ha_state()

        try:
            await self.coordinator.api.async_set_dimmer_brightness(
                hub_id=space.hub_id,
                device_id=self._device_id,
                brightness=0,
            )
            _LOGGER.debug("Dimmer %s turned off", self._device_id)
        except Exception as err:
            _LOGGER.error("Failed to turn off dimmer %s: %s", self._device_id, err)
            # Rollback on error
            device.attributes["actualBrightnessCh1"] = old_brightness
            device.attributes["channelStatuses"] = old_statuses
            self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
