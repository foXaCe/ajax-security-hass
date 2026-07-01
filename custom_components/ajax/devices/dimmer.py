"""Dimmer handler for Ajax LightSwitchDimmer.

Handles:
- LightSwitchDimmer (dimmable wall switch with touch panel)

Features:
- ON/OFF control with brightness
- Touch sensitivity settings
- Dimmer curve settings
- Protection status monitoring
- Arm/disarm brightness actions
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import (
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
)

from .base import AjaxDeviceHandler


class DimmerHandler(AjaxDeviceHandler):
    """Handler for Ajax LightSwitchDimmer devices."""

    def get_binary_sensors(self) -> list[dict[str, Any]]:
        """Return binary sensor entities for dimmer."""
        # Note: LightSwitchDimmer has no tamper sensor
        sensors = [
            self._problem_binary_sensor(),
        ]

        # Current limit protection
        sensors.append(
            {
                "key": "current_limit",
                "translation_key": "current_limit",
                "device_class": BinarySensorDeviceClass.SAFETY,
                "value_fn": lambda: "CURRENT_LIMIT_ON" in self.device.attributes.get("protectStatuses", []),
                "enabled_by_default": True,
            }
        )

        # Temperature limit protection
        sensors.append(
            {
                "key": "temperature_limit",
                "translation_key": "temperature_limit",
                "device_class": BinarySensorDeviceClass.HEAT,
                "value_fn": lambda: "TEMPERATURE_LIMIT_ON" in self.device.attributes.get("protectStatuses", []),
                "enabled_by_default": True,
            }
        )

        # Data channel status
        if "dataChannelOk" in self.device.attributes:
            sensors.append(
                {
                    "key": "data_channel_ok",
                    "translation_key": "data_channel_ok",
                    "device_class": BinarySensorDeviceClass.CONNECTIVITY,
                    "value_fn": lambda: self.device.attributes.get("dataChannelOk") == "OK",
                    "enabled_by_default": True,
                }
            )

        return sensors

    def get_sensors(self) -> list[dict[str, Any]]:
        """Return sensor entities for dimmer."""
        sensors: list[dict[str, Any]] = [self._signal_strength_percent_sensor()]
        if "temperature" in self.device.attributes:
            sensors.append(self._temperature_sensor())

        # Current brightness level (read-only sensor for display)
        sensors.append(
            {
                "key": "current_brightness",
                "translation_key": "current_brightness",
                "native_unit_of_measurement": PERCENTAGE,
                "state_class": SensorStateClass.MEASUREMENT,
                "value_fn": lambda: self.device.attributes.get("actualBrightnessCh1"),
                "enabled_by_default": False,
                "entity_category": "diagnostic",
            }
        )

        # Data channel signal quality
        if "dataChannelSignalQuality" in self.device.attributes:
            sensors.append(
                {
                    "key": "data_channel_quality",
                    "translation_key": "data_channel_quality",
                    "value_fn": lambda: (
                        (self.device.attributes.get("dataChannelSignalQuality") or "").lower().replace("_", " ") or None
                    ),
                    "enabled_by_default": False,
                    "entity_category": "diagnostic",
                }
            )

        if self.device.firmware_version:
            sensors.append(self._firmware_version_sensor())

        return sensors
