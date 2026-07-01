"""Device- and video-edge-level Ajax sensors.

``AjaxDeviceSensor`` (handler-driven) and ``AjaxVideoEdgeSensor``. Split out
of ``sensor.py`` (platform module keeps only ``async_setup_entry`` +
re-exports).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._ids import device_identifier
from .const import MANUFACTURER
from .coordinator import AjaxDataCoordinator
from .devices.base import resolve_entity_category
from .models import (
    VIDEO_EDGE_MODEL_NAMES,
    AjaxDevice,
    AjaxVideoEdge,
)

_LOGGER = logging.getLogger(__name__)


class AjaxDeviceSensor(CoordinatorEntity[AjaxDataCoordinator], SensorEntity):
    """Representation of an Ajax device sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        device_id: str,
        sensor_key: str,
        sensor_desc: dict[str, Any],
    ) -> None:
        """Initialize the Ajax device sensor."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._sensor_key = sensor_key
        self._sensor_desc = sensor_desc

        self._attr_unique_id = f"{self.coordinator.entry_id}_{device_id}_{sensor_key}"

        # Set device class if provided
        if "device_class" in sensor_desc:
            self._attr_device_class = sensor_desc["device_class"]

        # Set options for enum sensors (required for translations)
        if "options" in sensor_desc:
            self._attr_options = sensor_desc["options"]

        # Set translation key only if explicitly provided
        # If device_class is set and no translation_key, HA will use automatic naming
        if "translation_key" in sensor_desc:
            self._attr_translation_key = sensor_desc["translation_key"]
        elif "device_class" not in sensor_desc:
            # No device_class, use sensor_key as fallback translation key
            self._attr_translation_key = sensor_key

        if "native_unit_of_measurement" in sensor_desc:
            self._attr_native_unit_of_measurement = sensor_desc["native_unit_of_measurement"]

        if "state_class" in sensor_desc:
            self._attr_state_class = sensor_desc["state_class"]

        if "enabled_by_default" in sensor_desc:
            self._attr_entity_registry_enabled_default = sensor_desc["enabled_by_default"]

        if "entity_category" in sensor_desc:
            self._attr_entity_category = resolve_entity_category(sensor_desc["entity_category"])

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        device = self._get_device()
        if not device:
            return None

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
        if not self.coordinator.last_update_success:
            return False
        device = self._get_device()
        if not device:
            return False
        return bool(device.online)

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information."""
        device = self._get_device()
        if not device:
            return None

        return DeviceInfo(
            identifiers={device_identifier(self.coordinator.entry_id, self._device_id)},
            name=device.name,
            manufacturer=MANUFACTURER,
            model=device.raw_type,
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


# ==============================================================================
# Video Edge Sensors
# ==============================================================================
class AjaxVideoEdgeSensor(CoordinatorEntity[AjaxDataCoordinator], SensorEntity):
    """Representation of an Ajax Video Edge sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        video_edge_id: str,
        sensor_key: str,
        sensor_desc: dict[str, Any],
    ) -> None:
        """Initialize the Ajax video edge sensor."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._video_edge_id = video_edge_id
        self._sensor_key = sensor_key
        self._sensor_desc = sensor_desc

        # Set unique ID
        self._attr_unique_id = f"{self.coordinator.entry_id}_{video_edge_id}_{sensor_key}"

        # Set translation key
        self._attr_translation_key = sensor_desc.get("translation_key", sensor_key)

        # Set entity category if provided (diagnostic, config, etc.)
        if "entity_category" in sensor_desc:
            self._attr_entity_category = resolve_entity_category(sensor_desc["entity_category"])

        # Set enabled by default
        if "enabled_by_default" in sensor_desc:
            self._attr_entity_registry_enabled_default = sensor_desc["enabled_by_default"]

        # Set device class if provided
        if "device_class" in sensor_desc:
            self._attr_device_class = sensor_desc["device_class"]

        # Set options for enum sensors (required for translations)
        if "options" in sensor_desc:
            self._attr_options = sensor_desc["options"]

        # Set native unit of measurement
        if "native_unit_of_measurement" in sensor_desc:
            self._attr_native_unit_of_measurement = sensor_desc["native_unit_of_measurement"]

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
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
        if not self.coordinator.last_update_success:
            return False
        video_edge = self._get_video_edge()
        if video_edge is None:
            return False
        return video_edge.online

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        extra_fn = self._sensor_desc.get("extra_state_attributes_fn")
        if extra_fn:
            try:
                return extra_fn()  # type: ignore[no-any-return]
            except Exception as err:
                _LOGGER.error(
                    "Error getting extra attributes for video edge sensor %s: %s",
                    self._sensor_key,
                    err,
                )
                return None
        return None

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


# ==============================================================================
# Hub-level Sensors (from hub_details)
# ==============================================================================
