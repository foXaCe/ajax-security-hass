"""Doorbell device handler for Ajax Doorbell.

Handles:
- Ajax Doorbell (video doorbell with button)
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE

from .base import AjaxDeviceHandler


class DoorbellHandler(AjaxDeviceHandler):
    """Handler for Ajax Doorbell devices."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for doorbell."""
        sensors = [
            # Doorbell ring sensor - indicates when someone pressed the doorbell
            {
                "key": "doorbell_ring",
                "translation_key": "doorbell_ring",
                "device_class": BinarySensorDeviceClass.OCCUPANCY,
                "value_fn": lambda: self.device.attributes.get("doorbell_ring", False),
                "enabled_by_default": True,
            },
            # Tamper sensor
            {
                "key": "tamper",
                "device_class": BinarySensorDeviceClass.TAMPER,
                "value_fn": lambda: self.device.attributes.get("tampered", False),
                "enabled_by_default": True,
            },
        ]

        return sensors

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for doorbell."""
        sensors = []

        # Battery level
        sensors.append(
            {
                "key": "battery",
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
                "native_unit_of_measurement": PERCENTAGE,
                "state_class": SensorStateClass.MEASUREMENT,
                "value_fn": lambda: self.device.signal_strength
                if self.device.signal_strength is not None
                else None,
                "enabled_by_default": True,
            }
        )

        # Last ring timestamp
        sensors.append(
            {
                "key": "last_ring",
                "translation_key": "last_ring",
                "device_class": SensorDeviceClass.TIMESTAMP,
                "value_fn": lambda: self.device.attributes.get("last_ring"),
                "enabled_by_default": True,
            }
        )

        return sensors

    def get_switches(self) -> list[dict]:
        """Return switch entities for doorbell."""
        # Doorbell typically doesn't have configurable switches
        return []
