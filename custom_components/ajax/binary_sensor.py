"""Ajax binary sensor platform.

This module creates binary sensors for:
- Door/Window contact sensors
- Motion detectors
- Smoke detectors
- Leak/Water detectors
- Tamper detection
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AjaxDataCoordinator
from .models import AjaxDevice, DeviceType

_LOGGER = logging.getLogger(__name__)


# ==============================================================================
# Binary Sensor Descriptions
# ==============================================================================


@dataclass
class AjaxBinarySensorDescription(BinarySensorEntityDescription):
    """Description for Ajax binary sensors."""

    value_fn: Callable[[AjaxDevice], bool | None] | None = None
    should_create: Callable[[AjaxDevice], bool] | None = None
    enabled_by_default: bool = True


# Device-level binary sensor descriptions
BINARY_SENSORS: tuple[AjaxBinarySensorDescription, ...] = (
    AjaxBinarySensorDescription(
        key="door",
        translation_key="door",
        device_class=BinarySensorDeviceClass.DOOR,
        value_fn=lambda device: device.attributes.get("door_opened", False),
        should_create=lambda device: device.type in [DeviceType.DOOR_CONTACT, DeviceType.WIRE_INPUT]
        or "door_opened" in device.attributes,
        enabled_by_default=True,
    ),
    AjaxBinarySensorDescription(
        key="motion",
        translation_key="motion",
        device_class=BinarySensorDeviceClass.MOTION,
        value_fn=lambda device: device.is_triggered if device.type in [DeviceType.MOTION_DETECTOR, DeviceType.COMBI_PROTECT] else device.attributes.get("motion_detected", False),
        should_create=lambda device: device.type in [DeviceType.MOTION_DETECTOR, DeviceType.COMBI_PROTECT]
        or "motion_detected" in device.attributes,
        enabled_by_default=True,
    ),
    AjaxBinarySensorDescription(
        key="glass_break",
        translation_key="glass_break",
        device_class=BinarySensorDeviceClass.VIBRATION,
        value_fn=lambda device: device.attributes.get("glass_break_detected", False),
        should_create=lambda device: device.type in [DeviceType.GLASS_BREAK, DeviceType.COMBI_PROTECT]
        or "glass_break_detected" in device.attributes,
        enabled_by_default=True,
    ),
    AjaxBinarySensorDescription(
        key="smoke",
        translation_key="smoke",
        device_class=BinarySensorDeviceClass.SMOKE,
        value_fn=lambda device: device.attributes.get("smoke_detected", False),
        should_create=lambda device: device.type == DeviceType.SMOKE_DETECTOR
        or "smoke_detected" in device.attributes,
        enabled_by_default=True,
    ),
    AjaxBinarySensorDescription(
        key="moisture",
        translation_key="moisture",
        device_class=BinarySensorDeviceClass.MOISTURE,
        value_fn=lambda device: device.attributes.get("leak_detected", False),
        should_create=lambda device: device.type == DeviceType.FLOOD_DETECTOR
        or "leak_detected" in device.attributes,
        enabled_by_default=True,
    ),
    AjaxBinarySensorDescription(
        key="tamper",
        translation_key="tamper",
        device_class=BinarySensorDeviceClass.TAMPER,
        value_fn=lambda device: device.attributes.get("tampered", False),
        should_create=lambda device: "tampered" in device.attributes,
        enabled_by_default=False,  # Disabled by default as it's rarely triggered
    ),
    AjaxBinarySensorDescription(
        key="always_active",
        translation_key="always_active",
        icon="mdi:moon-waning-crescent",
        value_fn=lambda device: device.attributes.get("always_active", False),
        should_create=lambda device: (
            "always_active" in device.attributes
            and device.type in [
                DeviceType.MOTION_DETECTOR,
                DeviceType.DOOR_CONTACT,
                DeviceType.GLASS_BREAK,
                DeviceType.COMBI_PROTECT,
                DeviceType.SMOKE_DETECTOR,
                DeviceType.FLOOD_DETECTOR,
                DeviceType.TEMPERATURE_SENSOR,
            ]
        ),
        enabled_by_default=True,
    ),
    AjaxBinarySensorDescription(
        key="armed_in_night_mode",
        translation_key="armed_in_night_mode",
        icon="mdi:shield-moon",
        value_fn=lambda device: device.attributes.get("armed_in_night_mode", False),
        should_create=lambda device: (
            "armed_in_night_mode" in device.attributes
            and device.type in [
                DeviceType.MOTION_DETECTOR,
                DeviceType.DOOR_CONTACT,
                DeviceType.GLASS_BREAK,
                DeviceType.COMBI_PROTECT,
                DeviceType.SMOKE_DETECTOR,
                DeviceType.FLOOD_DETECTOR,
                DeviceType.TEMPERATURE_SENSOR,
            ]
        ),
        enabled_by_default=True,
    ),
    # Problem detection for all devices
    AjaxBinarySensorDescription(
        key="problem",
        translation_key="problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda device: device.malfunctions > 0,
        should_create=lambda device: True,  # Create for all devices
        enabled_by_default=True,
    ),
)


# ==============================================================================
# Setup
# ==============================================================================


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax binary sensors from a config entry."""
    coordinator: AjaxDataCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[BinarySensorEntity] = []

    if not coordinator.account:
        _LOGGER.warning("No Ajax account found, no binary sensors created")
        return

    # Create binary sensors for each device
    for space_id, space in coordinator.account.spaces.items():
        for device_id, device in space.devices.items():
            for description in BINARY_SENSORS:
                if description.should_create and description.should_create(device):
                    entities.append(
                        AjaxBinarySensor(
                            coordinator, entry, space_id, device_id, description
                        )
                    )
                    _LOGGER.debug(
                        "Creating %s binary sensor for device '%s'",
                        description.key,
                        device.name,
                    )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax binary sensor(s)", len(entities))
    else:
        _LOGGER.debug("No Ajax binary sensors created")


# ==============================================================================
# Binary Sensor Entity
# ==============================================================================


class AjaxBinarySensor(CoordinatorEntity[AjaxDataCoordinator], BinarySensorEntity):
    """Representation of an Ajax binary sensor."""

    entity_description: AjaxBinarySensorDescription

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        entry: ConfigEntry,
        space_id: str,
        device_id: str,
        description: AjaxBinarySensorDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._space_id = space_id
        self._device_id = device_id
        self._entry = entry

        # Get initial device data
        device = coordinator.get_device(space_id, device_id)
        device_name = device.name if device else "Unknown"

        # Set entity attributes
        self._attr_has_entity_name = True
        self._attr_translation_key = description.translation_key
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{description.key}"
        self._attr_entity_registry_enabled_default = description.enabled_by_default

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        device = self.coordinator.get_device(self._space_id, self._device_id)
        if not device or not self.entity_description.value_fn:
            return None

        return self.entity_description.value_fn(device)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if not self.coordinator.last_update_success:
            return False

        device = self.coordinator.get_device(self._space_id, self._device_id)
        if not device:
            return False

        # Binary sensors are unavailable if device is offline
        return device.online

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        device = self.coordinator.get_device(self._space_id, self._device_id)
        if not device:
            return {}

        # For hub devices, use the space identifier to merge with space-level sensors
        # This prevents duplicate hub devices in Home Assistant
        if device.type == DeviceType.HUB:
            return {
                "identifiers": {(DOMAIN, self._space_id)},
                "name": f"Ajax Hub - {device.name}",
                "manufacturer": "Ajax Systems",
                "model": device.type.value.replace("_", " ").title(),
                "sw_version": device.firmware_version,
                "hw_version": device.hardware_version,
            }

        # For non-hub devices, use device identifier
        # Get room name if available
        room_name = None
        if device.room_id:
            space = self.coordinator.get_space(self._space_id)
            if space and device.room_id in space.rooms:
                room_name = space.rooms[device.room_id].name

        # Include room name in device name if available
        device_display_name = f"{room_name} - {device.name}" if room_name else device.name

        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"Ajax {device_display_name}",
            "manufacturer": "Ajax Systems",
            "model": device.type.value.replace("_", " ").title(),
            "via_device": (DOMAIN, self._space_id),
            "sw_version": device.firmware_version,
            "hw_version": device.hardware_version,
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        device = self.coordinator.get_device(self._space_id, self._device_id)
        if not device:
            return {}

        attributes = {
            "device_id": device.id,
            "device_type": device.type.value,
            "space_id": device.space_id,
            "hub_id": device.hub_id,
            "online": device.online,
            "bypassed": device.bypassed,
        }

        # Add room information if available
        if device.room_id:
            room = self.coordinator.get_room(self._space_id, device.room_id)
            if room:
                attributes["room_id"] = room.id
                attributes["room_name"] = room.name

        # Add motion detection timestamp if available
        if (
            self.entity_description.key == "motion"
            and "motion_detected_at" in device.attributes
        ):
            attributes["last_motion"] = device.attributes["motion_detected_at"]

        # Add battery and signal info for motion sensors
        if self.entity_description.key == "motion":
            if device.battery_level is not None:
                attributes["battery_level"] = device.battery_level
            if device.signal_strength is not None:
                attributes["signal_strength"] = device.signal_strength
            if "temperature" in device.attributes:
                attributes["temperature"] = device.attributes["temperature"]

        # Add malfunction details for hub problem sensor
        if self.entity_description.key == "problem" and device.type == DeviceType.HUB:
            # Get all devices in this space with malfunctions
            space = self.coordinator.account.spaces.get(self._space_id)
            if space:
                devices_with_problems = []
                for dev_id, dev in space.devices.items():
                    if dev.malfunctions > 0:
                        problem_info = {
                            "device_id": dev.id,
                            "device_name": dev.name,
                            "device_type": dev.type.value,
                            "malfunction_count": dev.malfunctions,
                        }
                        # Add room info if available
                        if dev.room_id:
                            room = self.coordinator.get_room(self._space_id, dev.room_id)
                            if room:
                                problem_info["room_name"] = room.name
                        devices_with_problems.append(problem_info)

                attributes["total_malfunctions"] = device.malfunctions
                attributes["devices_with_problems"] = len(devices_with_problems)
                if devices_with_problems:
                    attributes["problem_devices"] = devices_with_problems

        return attributes
