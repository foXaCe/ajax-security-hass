"""Repeater device handler for Ajax Rex series.

Handles:
- Rex (range extender)
- Rex2 (range extender v2)
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE

from .base import AjaxDeviceHandler


class RepeaterHandler(AjaxDeviceHandler):
    """Handler for Ajax Rex/Repeater devices."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for repeaters."""
        sensors = [
            # Tamper sensor
            {
                "key": "tamper",
                "translation_key": "tamper",
                "device_class": BinarySensorDeviceClass.TAMPER,
                "icon": "mdi:lock-open-alert",
                "value_fn": lambda: self.device.attributes.get("tampered", False),
                "enabled_by_default": True,
            },
        ]

        return sensors

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for repeaters."""
        sensors = []

        # Battery level
        sensors.append(
            {
                "key": "battery",
                "translation_key": "battery",
                "device_class": SensorDeviceClass.BATTERY,
                "native_unit_of_measurement": PERCENTAGE,
                "state_class": SensorStateClass.MEASUREMENT,
                "value_fn": lambda: self.device.battery_level
                if self.device.battery_level is not None
                else None,
                "enabled_by_default": True,
            }
        )

        # Signal strength
        sensors.append(
            {
                "key": "signal_strength",
                "translation_key": "signal_strength",
                "icon": "mdi:signal",
                "native_unit_of_measurement": PERCENTAGE,
                "state_class": SensorStateClass.MEASUREMENT,
                "value_fn": lambda: self.device.signal_strength
                if self.device.signal_strength is not None
                else None,
                "enabled_by_default": True,
            }
        )

        return sensors

    def get_switches(self) -> list[dict]:
        """Return switch entities for repeaters."""
        # Repeaters don't have configurable switches
        return []
