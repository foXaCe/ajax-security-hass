"""Light handler for Ajax WallSwitch with dimmer.

Handles:
- WallSwitch (in light/dimmer mode)
- Smart lighting control with brightness adjustment

Features:
- ON/OFF control
- Brightness control (0-100%)
- Smooth transitions
- Power monitoring
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.light import ColorMode
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
)

from .base import AjaxDeviceHandler


class LightHandler(AjaxDeviceHandler):
    """Handler for Ajax Light/WallSwitch with dimmer."""

    def get_lights(self) -> list[dict]:
        """Return light entities for lighting control."""
        return [
            {
                "key": "light",
                "translation_key": "light",
                "value_fn": lambda: self.device.attributes.get("state") == "on",
                "brightness_fn": lambda: self._get_brightness(),
                "turn_on_fn": self._async_turn_on,
                "turn_off_fn": self._async_turn_off,
                "color_mode": ColorMode.BRIGHTNESS,
                "supported_color_modes": {ColorMode.BRIGHTNESS},
                "enabled_by_default": True,
            }
        ]

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for light."""
        sensors = [
            {
                "key": "problem",
                "translation_key": "problem",
                "device_class": BinarySensorDeviceClass.PROBLEM,
                "value_fn": lambda: bool(self.device.malfunctions),
                "enabled_by_default": True,
            }
        ]

        # Connection status
        sensors.append(
            {
                "key": "connection",
                "translation_key": "connection",
                "device_class": BinarySensorDeviceClass.CONNECTIVITY,
                "value_fn": lambda: self.device.online,
                "enabled_by_default": True,
            }
        )

        # External power status (if available)
        if "external_power" in self.device.attributes:
            sensors.append(
                {
                    "key": "external_power",
                    "translation_key": "external_power",
                    "device_class": BinarySensorDeviceClass.POWER,
                    "value_fn": lambda: self.device.attributes.get(
                        "external_power", False
                    ),
                    "enabled_by_default": True,
                }
            )

        return sensors

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for light monitoring."""
        sensors = []

        # Battery level (if battery powered)
        if "battery_level" in self.device.attributes:
            sensors.append(
                {
                    "key": "battery",
                    "translation_key": "battery",
                    "device_class": SensorDeviceClass.BATTERY,
                    "native_unit_of_measurement": PERCENTAGE,
                    "state_class": SensorStateClass.MEASUREMENT,
                    "value_fn": lambda: self.device.attributes.get("battery_level"),
                    "enabled_by_default": True,
                }
            )

        # Signal strength
        if "signal_strength" in self.device.attributes:
            sensors.append(
                {
                    "key": "signal_strength",
                    "translation_key": "signal_strength",
                    "device_class": SensorDeviceClass.SIGNAL_STRENGTH,
                    "native_unit_of_measurement": SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
                    "state_class": SensorStateClass.MEASUREMENT,
                    "value_fn": lambda: self.device.attributes.get("signal_strength"),
                    "enabled_by_default": False,
                }
            )

        # Current brightness level
        sensors.append(
            {
                "key": "brightness",
                "translation_key": "brightness",
                "native_unit_of_measurement": PERCENTAGE,
                "state_class": SensorStateClass.MEASUREMENT,
                "value_fn": lambda: self.device.attributes.get("brightness", 0),
                "enabled_by_default": False,
            }
        )

        return sensors

    def _get_brightness(self) -> int:
        """Get current brightness (0-255 for Home Assistant).

        Ajax API uses 0-100%, Home Assistant uses 0-255.
        """
        brightness_percent = self.device.attributes.get("brightness", 0)
        # Convert 0-100% to 0-255
        return int((brightness_percent / 100) * 255)

    async def _async_turn_on(self, brightness: int | None = None) -> None:
        """Turn the light on.

        Args:
            brightness: Optional brightness (0-255). If None, use 100%.
        """
        # Convert Home Assistant brightness (0-255) to Ajax percentage (0-100)
        if brightness is not None:
            brightness_percent = int((brightness / 255) * 100)
        else:
            brightness_percent = 100

        await self.device.coordinator.api.async_set_light_state(
            self.device.device_id, state=True, brightness=brightness_percent
        )
        await self.device.coordinator.async_request_refresh()

    async def _async_turn_off(self) -> None:
        """Turn the light off."""
        await self.device.coordinator.api.async_set_light_state(
            self.device.device_id, state=False
        )
        await self.device.coordinator.async_request_refresh()
