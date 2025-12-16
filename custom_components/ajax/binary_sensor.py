"""Ajax binary sensor platform (refactored).

This module creates binary sensors for Ajax devices using the device handler architecture.
Each device type (MotionProtect, DoorProtect, etc.) has its own handler that defines
which binary sensors to create.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AjaxDataCoordinator
from .devices import (
    DoorContactHandler,
    FloodDetectorHandler,
    GlassBreakHandler,
    HubHandler,
    MotionDetectorHandler,
    SirenHandler,
    SmokeDetectorHandler,
    SocketHandler,
)
from .models import AjaxDevice, DeviceType

_LOGGER = logging.getLogger(__name__)

# Mapping of device types to handlers
DEVICE_HANDLERS = {
    DeviceType.MOTION_DETECTOR: MotionDetectorHandler,
    DeviceType.COMBI_PROTECT: MotionDetectorHandler,
    DeviceType.DOOR_CONTACT: DoorContactHandler,
    DeviceType.WIRE_INPUT: DoorContactHandler,
    DeviceType.SMOKE_DETECTOR: SmokeDetectorHandler,
    DeviceType.FLOOD_DETECTOR: FloodDetectorHandler,
    DeviceType.GLASS_BREAK: GlassBreakHandler,
    DeviceType.SOCKET: SocketHandler,
    DeviceType.RELAY: SocketHandler,
    DeviceType.WALLSWITCH: SocketHandler,
    DeviceType.SIREN: SirenHandler,
    DeviceType.SPEAKERPHONE: SirenHandler,
    DeviceType.HUB: HubHandler,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax binary sensor platform."""
    coordinator: AjaxDataCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []

    # Create binary sensors for all devices using handlers
    for space_id, space in coordinator.account.spaces.items():
        for device_id, device in space.devices.items():
            handler_class = DEVICE_HANDLERS.get(device.type)
            if handler_class:
                handler = handler_class(device)
                binary_sensors = handler.get_binary_sensors()

                for sensor_desc in binary_sensors:
                    entities.append(
                        AjaxBinarySensor(
                            coordinator=coordinator,
                            space_id=space_id,
                            device_id=device_id,
                            sensor_key=sensor_desc["key"],
                            sensor_desc=sensor_desc,
                        )
                    )
                    _LOGGER.debug(
                        "Created binary sensor '%s' for device: %s (type: %s)",
                        sensor_desc["key"],
                        device.name,
                        device.type.value,
                    )

    async_add_entities(entities)
    if entities:
        _LOGGER.info("Added %d Ajax binary sensor(s)", len(entities))


class AjaxBinarySensor(CoordinatorEntity[AjaxDataCoordinator], BinarySensorEntity):
    """Representation of an Ajax binary sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        device_id: str,
        sensor_key: str,
        sensor_desc: dict,
    ) -> None:
        """Initialize the Ajax binary sensor."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._sensor_key = sensor_key
        self._sensor_desc = sensor_desc

        # Set unique ID
        self._attr_unique_id = f"{device_id}_{sensor_key}"

        # Set translation key
        self._attr_translation_key = sensor_desc.get("translation_key", sensor_key)

        # Set device class if provided
        if "device_class" in sensor_desc:
            self._attr_device_class = sensor_desc["device_class"]

        # Set icon if provided
        if "icon" in sensor_desc:
            self._attr_icon = sensor_desc["icon"]

        # Set enabled by default
        if "enabled_by_default" in sensor_desc:
            self._attr_entity_registry_enabled_default = sensor_desc[
                "enabled_by_default"
            ]

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        device = self._get_device()
        if not device:
            return None

        # Use value_fn from sensor description
        value_fn = self._sensor_desc.get("value_fn")
        if value_fn:
            try:
                return value_fn()
            except Exception as err:
                _LOGGER.error(
                    "Error getting value for sensor %s: %s",
                    self._sensor_key,
                    err,
                )
                return None
        return None

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        device = self._get_device()
        if not device:
            return False

        # Most sensors require device to be online
        return device.attributes.get("online", True)

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass, update device info in registry."""
        await super().async_added_to_hass()
        self._update_device_registry()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Update device registry once on first update (for existing entities)
        if not getattr(self, "_device_info_updated", False):
            self._device_info_updated = True
            self._update_device_registry()
        self.async_write_ha_state()

    def _update_device_registry(self) -> None:
        """Update device info in registry with model, firmware, and color."""
        device = self._get_device()
        if not device:
            return

        device_registry = dr.async_get(self.hass)
        device_entry = device_registry.async_get_device(
            identifiers={(DOMAIN, self._device_id)}
        )
        if not device_entry:
            return

        # Get model name with color
        model_name = self._get_device_model_name(device.type.value)
        if device.device_color:
            color_name = {
                "WHITE": "Blanc",
                "White": "Blanc",
                "BLACK": "Noir",
                "Black": "Noir",
            }.get(str(device.device_color), str(device.device_color))
            model_name = f"{model_name} ({color_name})"

        device_registry.async_update_device(
            device_entry.id,
            model=model_name,
            sw_version=device.firmware_version,
            hw_version=device.hardware_version,
        )

    def _get_device_model_name(self, device_type_value: str) -> str:
        """Get translated device model name.

        Args:
            device_type_value: Device type enum value (e.g., "door_contact")

        Returns:
            Translated model name in French
        """
        translations = {
            "motion_detector": "Détecteur de mouvement",
            "door_contact": "Capteur de porte",
            "glass_break": "Détecteur bris de glace",
            "combi_protect": "Détecteur combiné",
            "smoke_detector": "Détecteur de fumée",
            "flood_detector": "Détecteur d'inondation",
            "temperature_sensor": "Capteur de température",
            "keypad": "Clavier",
            "remote_control": "Télécommande",
            "button": "Bouton",
            "siren": "Sirène",
            "transmitter": "Transmetteur",
            "repeater": "Répéteur",
            "wire_input": "Module d'entrée filaire",
            "line_splitter": "Répartiteur de ligne",
            "socket": "Prise connectée",
            "relay": "Relais",
            "thermostat": "Thermostat",
            "life_quality": "Capteur qualité de l'air",
            "camera": "Caméra",
            "hub": "Hub",
            "unknown": "Appareil inconnu",
        }
        return translations.get(
            device_type_value, device_type_value.replace("_", " ").title()
        )

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        device = self._get_device()
        if not device:
            return {}

        # Get room name if available
        room_name = None
        if device.room_id:
            space = self.coordinator.get_space(self._space_id)
            if space and device.room_id in space.rooms:
                room_name = space.rooms[device.room_id].name

        # Include room name in device name if available
        device_display_name = (
            f"{room_name} - {device.name}" if room_name else device.name
        )

        # Get model name with color
        model_name = self._get_device_model_name(device.type.value)
        if device.device_color:
            color_name = {
                "WHITE": "Blanc",
                "White": "Blanc",
                "BLACK": "Noir",
                "Black": "Noir",
            }.get(str(device.device_color), str(device.device_color))
            model_name = f"{model_name} ({color_name})"

        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"Ajax {device_display_name}",
            "manufacturer": "Ajax Systems",
            "model": model_name,
            "via_device": (DOMAIN, self._space_id),
            "sw_version": device.firmware_version,
            "hw_version": device.hardware_version,
        }

    def _get_device(self) -> AjaxDevice | None:
        """Get the device from coordinator data."""
        space = self.coordinator.account.spaces.get(self._space_id)
        if not space:
            return None
        return space.devices.get(self._device_id)
