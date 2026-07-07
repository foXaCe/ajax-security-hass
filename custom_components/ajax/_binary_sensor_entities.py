"""Entity classes for the Ajax binary_sensor platform.

Split out of ``binary_sensor.py`` (which keeps only ``async_setup_entry``
and the discovery builders). Four sibling classes:

* ``AjaxBinarySensor`` — regular devices, descriptor-driven (handlers).
* ``AjaxVideoEdgeBinarySensor`` — surveillance cameras / NVR channels.
* ``AjaxHubBinarySensor`` — hub-level sensors from ``space.hub_details``.
* ``AjaxSmartLockBinarySensor`` — smart-lock door open/close state.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._ids import device_identifier
from .const import MANUFACTURER
from .coordinator import AjaxDataCoordinator
from .devices.base import resolve_entity_category
from .models import (
    VIDEO_EDGE_MODEL_NAMES,
    AjaxDevice,
    AjaxSmartLock,
    AjaxVideoEdge,
)

_LOGGER = logging.getLogger(__name__)


class AjaxBinarySensor(CoordinatorEntity[AjaxDataCoordinator], BinarySensorEntity):
    """Representation of an Ajax binary sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        device_id: str,
        sensor_key: str,
        sensor_desc: dict[str, Any],
    ) -> None:
        """Initialize the Ajax binary sensor."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._sensor_key = sensor_key
        self._sensor_desc = sensor_desc

        # Set unique ID
        self._attr_unique_id = f"{self.coordinator.entry_id}_{device_id}_{sensor_key}"

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

        # Set enabled by default
        if "enabled_by_default" in sensor_desc:
            self._attr_entity_registry_enabled_default = sensor_desc["enabled_by_default"]

        if "entity_category" in sensor_desc:
            self._attr_entity_category = resolve_entity_category(sensor_desc["entity_category"])

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
                return value_fn()  # type: ignore[no-any-return]
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
        if not self.coordinator.last_update_success:
            return False
        device = self._get_device()
        if not device:
            return False
        return bool(device.online)

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
            identifiers={device_identifier(self.coordinator.entry_id, self._device_id)}
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
    def device_info(self) -> DeviceInfo | None:
        """Return device information."""
        device = self._get_device()
        if not device:
            return None

        # Get model name - use raw_type from API (e.g., "DoorProtect Plus")
        model_name = device.raw_type or device.type.value.replace("_", " ").title()
        if device.device_color:
            # Keep color as-is from API (WHITE/BLACK are product colors)
            color = str(device.device_color).title()
            model_name = f"{model_name} ({color})"

        return DeviceInfo(
            identifiers={device_identifier(self.coordinator.entry_id, self._device_id)},
            name=device.name,
            manufacturer=MANUFACTURER,
            model=model_name,
            via_device=device_identifier(self.coordinator.entry_id, self._space_id),
            sw_version=device.firmware_version,
            hw_version=device.hardware_version,
            suggested_area=device.room_name,
        )

    def _get_device(self) -> AjaxDevice | None:
        """Get the device from coordinator data."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None
        return space.devices.get(self._device_id)


class AjaxVideoEdgeBinarySensor(CoordinatorEntity[AjaxDataCoordinator], BinarySensorEntity):
    """Representation of an Ajax Video Edge binary sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        video_edge_id: str,
        sensor_key: str,
        sensor_desc: dict[str, Any],
    ) -> None:
        """Initialize the Ajax video edge binary sensor."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._video_edge_id = video_edge_id
        self._sensor_key = sensor_key
        self._sensor_desc = sensor_desc

        # Set unique ID
        self._attr_unique_id = f"{self.coordinator.entry_id}_{video_edge_id}_{sensor_key}"

        # Set translation key
        self._attr_translation_key = sensor_desc.get("translation_key", sensor_key)

        # Set device class if provided
        if "device_class" in sensor_desc:
            self._attr_device_class = sensor_desc["device_class"]

        # Set enabled by default
        if "enabled_by_default" in sensor_desc:
            self._attr_entity_registry_enabled_default = sensor_desc["enabled_by_default"]

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        video_edge = self._get_video_edge()
        if not video_edge:
            return None

        value_fn = self._sensor_desc.get("value_fn")
        if value_fn:
            try:
                return value_fn()  # type: ignore[no-any-return]
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
        if not self.coordinator.last_update_success:
            return False
        video_edge = self._get_video_edge()
        if video_edge is None:
            return False
        return video_edge.online

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes (e.g., linked_nvr for cameras recorded by NVR)."""
        attrs = self._sensor_desc.get("extra_state_attributes")
        return attrs if attrs else None

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information."""
        video_edge = self._get_video_edge()
        if not video_edge:
            return None

        # Use human-readable model name
        model_name = VIDEO_EDGE_MODEL_NAMES.get(video_edge.video_edge_type, video_edge.video_edge_type.value)
        if video_edge.color:
            model_name = f"{model_name} ({video_edge.color.title()})"

        # Determine via_device: if camera is recorded by NVR, link to NVR
        # Otherwise link to hub (space_id)
        via_device_id = self._space_id
        nvr_id = self._get_recording_nvr_id()
        if nvr_id:
            via_device_id = nvr_id

        return DeviceInfo(
            identifiers={device_identifier(self.coordinator.entry_id, self._video_edge_id)},
            name=video_edge.name,
            manufacturer=MANUFACTURER,
            model=model_name,
            via_device=device_identifier(self.coordinator.entry_id, via_device_id),
            sw_version=video_edge.firmware_version,
            suggested_area=video_edge.room_name,
        )

    def _get_video_edge(self) -> AjaxVideoEdge | None:
        """Get the video edge from coordinator data."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None
        return space.video_edges.get(self._video_edge_id)

    def _get_recording_nvr_id(self) -> str | None:
        """Return the NVR that records this camera (if any)."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None
        return space.get_recording_nvr_id(self._video_edge_id)


class AjaxHubBinarySensor(CoordinatorEntity[AjaxDataCoordinator], BinarySensorEntity):
    """Representation of an Ajax Hub binary sensor.

    This is for hub-level sensors that come from space.hub_details,
    not from a device in space.devices.
    """

    _attr_has_entity_name = True

    # Hub binary sensor definitions
    HUB_BINARY_SENSORS: dict[str, dict[str, Any]] = {
        "tamper": {
            "device_class": BinarySensorDeviceClass.TAMPER,
            "value_key": "tampered",
        },
        "external_power": {
            "device_class": BinarySensorDeviceClass.PLUG,
            "translation_key": "external_power",
            "value_fn": lambda hd: hd.get("externallyPowered"),
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
        self._sensor_config: dict[str, Any] = self.HUB_BINARY_SENSORS.get(sensor_key, {})

        # Get space for hub_id
        space = coordinator.get_space(space_id)
        hub_id = space.hub_id if space else space_id

        # Set unique ID
        self._attr_unique_id = f"{self.coordinator.entry_id}_{hub_id}_{sensor_key}"

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
                return value_fn(space.hub_details)  # type: ignore[no-any-return]
            except Exception:
                return None

        value_key = self._sensor_config.get("value_key")
        if value_key:
            return space.hub_details.get(value_key, False)  # type: ignore[no-any-return]
        return None

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not self.coordinator.last_update_success:
            return False
        space = self.coordinator.get_space(self._space_id)
        return space is not None and space.hub_details is not None

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information linking to the hub/space device."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None

        # Link to the space device (hub)
        return DeviceInfo(
            identifiers={device_identifier(self.coordinator.entry_id, self._space_id)},
        )


class AjaxSmartLockBinarySensor(CoordinatorEntity[AjaxDataCoordinator], BinarySensorEntity):
    """Binary sensor for Ajax smart lock door open/close state."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.DOOR

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        smart_lock_id: str,
    ) -> None:
        """Initialize the smart lock door sensor."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._smart_lock_id = smart_lock_id
        self._attr_unique_id = f"{self.coordinator.entry_id}_{smart_lock_id}_door"
        self._attr_translation_key = "smart_lock_door"

    @property
    def is_on(self) -> bool | None:
        """Return true if the door is open."""
        smart_lock = self._get_smart_lock()
        if not smart_lock:
            return None
        return smart_lock.is_door_open

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not self.coordinator.last_update_success:
            return False
        return self._get_smart_lock() is not None

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information (same device as the lock entity)."""
        smart_lock = self._get_smart_lock()
        if not smart_lock:
            return None

        return DeviceInfo(
            identifiers={device_identifier(self.coordinator.entry_id, self._smart_lock_id)},
            name=smart_lock.name,
            manufacturer=MANUFACTURER,
            model="LockBridge Jeweller",
            via_device=device_identifier(self.coordinator.entry_id, self._space_id),
        )

    def _get_smart_lock(self) -> AjaxSmartLock | None:
        """Get the smart lock from coordinator data."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None
        return space.smart_locks.get(self._smart_lock_id)
