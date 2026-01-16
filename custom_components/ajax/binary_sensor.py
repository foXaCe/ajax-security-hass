"""Ajax binary sensor platform (refactored).

This module creates binary sensors for Ajax devices using the device handler architecture.
Each device type (MotionProtect, DoorProtect, etc.) has its own handler that defines
which binary sensors to create.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import DOMAIN, MANUFACTURER
from .coordinator import AjaxDataCoordinator
from .devices import (
    ButtonHandler,
    DoorbellHandler,
    DoorContactHandler,
    FloodDetectorHandler,
    GlassBreakHandler,
    HubHandler,
    MotionDetectorHandler,
    RepeaterHandler,
    SirenHandler,
    SmokeDetectorHandler,
    SocketHandler,
    VideoEdgeHandler,
    WireInputHandler,
)
from .models import AjaxDevice, AjaxVideoEdge, DeviceType

_LOGGER = logging.getLogger(__name__)

# Mapping of device types to handlers
DEVICE_HANDLERS = {
    DeviceType.MOTION_DETECTOR: MotionDetectorHandler,
    DeviceType.COMBI_PROTECT: MotionDetectorHandler,
    DeviceType.DOOR_CONTACT: DoorContactHandler,
    DeviceType.WIRE_INPUT: WireInputHandler,
    DeviceType.SMOKE_DETECTOR: SmokeDetectorHandler,
    DeviceType.FLOOD_DETECTOR: FloodDetectorHandler,
    DeviceType.GLASS_BREAK: GlassBreakHandler,
    DeviceType.SOCKET: SocketHandler,
    DeviceType.RELAY: SocketHandler,
    DeviceType.WALLSWITCH: SocketHandler,
    DeviceType.SIREN: SirenHandler,
    DeviceType.SPEAKERPHONE: SirenHandler,
    DeviceType.TRANSMITTER: SirenHandler,
    DeviceType.MULTI_TRANSMITTER: SirenHandler,
    DeviceType.KEYPAD: SirenHandler,
    DeviceType.BUTTON: ButtonHandler,
    DeviceType.DOORBELL: DoorbellHandler,
    DeviceType.REPEATER: RepeaterHandler,
    DeviceType.HUB: HubHandler,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax binary sensor platform."""
    coordinator = entry.runtime_data

    entities = []
    seen_unique_ids: set[str] = set()

    # Create binary sensors for all devices using handlers
    for space_id, space in coordinator.account.spaces.items():
        for device_id, device in space.devices.items():
            handler_class = DEVICE_HANDLERS.get(device.type)
            if handler_class:
                handler = handler_class(device)
                binary_sensors = handler.get_binary_sensors()

                for sensor_desc in binary_sensors:
                    unique_id = f"{device_id}_{sensor_desc['key']}"

                    # Skip if we already created this entity in this setup
                    if unique_id in seen_unique_ids:
                        _LOGGER.debug(
                            "Skipping duplicate unique_id %s for device %s",
                            unique_id,
                            device.name,
                        )
                        continue
                    seen_unique_ids.add(unique_id)

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

        # Create binary sensors for video edges (surveillance cameras)
        for ve_id, video_edge in space.video_edges.items():
            handler = VideoEdgeHandler(video_edge)
            binary_sensors = handler.get_binary_sensors()

            for sensor_desc in binary_sensors:
                unique_id = f"{ve_id}_{sensor_desc['key']}"

                if unique_id in seen_unique_ids:
                    continue
                seen_unique_ids.add(unique_id)

                entities.append(
                    AjaxVideoEdgeBinarySensor(
                        coordinator=coordinator,
                        space_id=space_id,
                        video_edge_id=ve_id,
                        sensor_key=sensor_desc["key"],
                        sensor_desc=sensor_desc,
                    )
                )
                _LOGGER.debug(
                    "Created video edge binary sensor '%s' for: %s",
                    sensor_desc["key"],
                    video_edge.name,
                )

        # Create hub-level binary sensors from hub_details
        if space.hub_details:
            hub_id = space.hub_id

            # Create all hub binary sensors
            for sensor_key in AjaxHubBinarySensor.HUB_BINARY_SENSORS:
                unique_id = f"{hub_id}_{sensor_key}"
                if unique_id not in seen_unique_ids:
                    seen_unique_ids.add(unique_id)
                    entities.append(
                        AjaxHubBinarySensor(
                            coordinator=coordinator,
                            space_id=space_id,
                            sensor_key=sensor_key,
                        )
                    )
                    _LOGGER.debug(
                        "Created hub binary sensor '%s' for space: %s",
                        sensor_key,
                        space.name,
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

        # Set device class if provided
        if "device_class" in sensor_desc:
            self._attr_device_class = sensor_desc["device_class"]

        # Set translation key only if explicitly provided
        # If device_class is set and no translation_key, HA will use automatic naming
        if "translation_key" in sensor_desc:
            self._attr_translation_key = sensor_desc["translation_key"]
        elif "device_class" not in sensor_desc:
            # No device_class, use sensor_key as fallback translation key
            self._attr_translation_key = sensor_key

        # Set icon if provided
        if "icon" in sensor_desc:
            self._attr_icon = sensor_desc["icon"]

        # Set enabled by default
        if "enabled_by_default" in sensor_desc:
            self._attr_entity_registry_enabled_default = sensor_desc[
                "enabled_by_default"
            ]

        # Force name on entity. Mainly used to assign None (=device name)
        if "name" in sensor_desc:
            self._attr_name = sensor_desc["name"]

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

        # Get model name - use raw_type from API (e.g., "DoorProtect Plus")
        model_name = device.raw_type or device.type.value.replace("_", " ").title()
        if device.device_color:
            # Keep color as-is from API (WHITE/BLACK are product colors)
            color = str(device.device_color).title()
            model_name = f"{model_name} ({color})"

        device_registry.async_update_device(
            device_entry.id,
            model=model_name,
            sw_version=device.firmware_version,
            hw_version=device.hardware_version,
        )

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        device = self._get_device()
        if not device:
            return {}

        # Get model name - use raw_type from API (e.g., "DoorProtect Plus")
        model_name = device.raw_type or device.type.value.replace("_", " ").title()
        if device.device_color:
            # Keep color as-is from API (WHITE/BLACK are product colors)
            color = str(device.device_color).title()
            model_name = f"{model_name} ({color})"

        info = {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": device.name,
            "manufacturer": MANUFACTURER,
            "model": model_name,
            "via_device": (DOMAIN, self._space_id),
            "sw_version": device.firmware_version,
            "hw_version": device.hardware_version,
        }
        # Auto-assign device to HA Area based on Ajax room
        if device.room_name:
            info["suggested_area"] = device.room_name
        return info

    def _get_device(self) -> AjaxDevice | None:
        """Get the device from coordinator data."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None
        return space.devices.get(self._device_id)


class AjaxVideoEdgeBinarySensor(
    CoordinatorEntity[AjaxDataCoordinator], BinarySensorEntity
):
    """Representation of an Ajax Video Edge binary sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        video_edge_id: str,
        sensor_key: str,
        sensor_desc: dict,
    ) -> None:
        """Initialize the Ajax video edge binary sensor."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._video_edge_id = video_edge_id
        self._sensor_key = sensor_key
        self._sensor_desc = sensor_desc

        # Set unique ID
        self._attr_unique_id = f"{video_edge_id}_{sensor_key}"

        # Set translation key
        self._attr_translation_key = sensor_desc.get("translation_key", sensor_key)

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
        video_edge = self._get_video_edge()
        if not video_edge:
            return None

        value_fn = self._sensor_desc.get("value_fn")
        if value_fn:
            try:
                return value_fn()
            except Exception as err:
                _LOGGER.error(
                    "Error getting value for video edge sensor %s: %s",
                    self._sensor_key,
                    err,
                )
                return None
        return None

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        video_edge = self._get_video_edge()
        if video_edge is None:
            return False
        return video_edge.online

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        video_edge = self._get_video_edge()
        if not video_edge:
            return {}

        model_name = video_edge.video_edge_type.value
        if video_edge.color:
            model_name = f"{model_name} ({video_edge.color.title()})"

        info = {
            "identifiers": {(DOMAIN, self._video_edge_id)},
            "name": video_edge.name,
            "manufacturer": MANUFACTURER,
            "model": model_name,
            "via_device": (DOMAIN, self._space_id),
            "sw_version": video_edge.firmware_version,
        }
        if video_edge.room_name:
            info["suggested_area"] = video_edge.room_name
        return info

    def _get_video_edge(self) -> AjaxVideoEdge | None:
        """Get the video edge from coordinator data."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None
        return space.video_edges.get(self._video_edge_id)


class AjaxHubBinarySensor(CoordinatorEntity[AjaxDataCoordinator], BinarySensorEntity):
    """Representation of an Ajax Hub binary sensor.

    This is for hub-level sensors that come from space.hub_details,
    not from a device in space.devices.
    """

    _attr_has_entity_name = True

    # Hub binary sensor definitions
    HUB_BINARY_SENSORS = {
        "tamper": {
            "device_class": BinarySensorDeviceClass.TAMPER,
            "value_key": "tampered",
        },
        "external_power": {
            "device_class": BinarySensorDeviceClass.PLUG,
            "translation_key": "external_power",
            "value_fn": lambda hd: hd.get("battery", {}).get("state") != "DISCHARGED"
            if hd.get("battery")
            else None,
        },
    }

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        sensor_key: str,
    ) -> None:
        """Initialize the Ajax hub binary sensor."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._sensor_key = sensor_key
        self._sensor_config = self.HUB_BINARY_SENSORS.get(sensor_key, {})

        # Get space for hub_id
        space = coordinator.get_space(space_id)
        hub_id = space.hub_id if space else space_id

        # Set unique ID
        self._attr_unique_id = f"{hub_id}_{sensor_key}"

        # Set device class
        if "device_class" in self._sensor_config:
            self._attr_device_class = self._sensor_config["device_class"]

        # Set translation key if provided
        if "translation_key" in self._sensor_config:
            self._attr_translation_key = self._sensor_config["translation_key"]

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        space = self.coordinator.get_space(self._space_id)
        if not space or not space.hub_details:
            return None

        # Support both value_key (direct key) and value_fn (function)
        value_fn = self._sensor_config.get("value_fn")
        if value_fn:
            try:
                return value_fn(space.hub_details)
            except Exception:
                return None

        value_key = self._sensor_config.get("value_key")
        if value_key:
            return space.hub_details.get(value_key, False)
        return None

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        space = self.coordinator.get_space(self._space_id)
        return space is not None and space.hub_details is not None

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information linking to the hub/space device."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return {}

        # Link to the space device (hub)
        return {
            "identifiers": {(DOMAIN, self._space_id)},
        }
