"""Button device handler for Ajax Button series.

Handles:
- Button (single button)
- DoubleButton (two buttons)
- SpaceControl (keyfob with multiple buttons)
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE

from .base import AjaxDeviceHandler


class ButtonHandler(AjaxDeviceHandler):
    """Handler for Ajax Button devices."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for buttons."""
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
        """Return sensor entities for buttons."""
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

        # Last button action (single_press, double_press, long_press, etc.)
        sensors.append(
            {
                "key": "last_action",
                "translation_key": "last_action",
                "icon": "mdi:gesture-tap-button",
                "value_fn": lambda: self.device.attributes.get("last_action"),
                "enabled_by_default": True,
            }
        )

        return sensors

    def get_switches(self) -> list[dict]:
        """Return switch entities for buttons."""
        # Buttons typically don't have configurable switches
        return []
