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
        # Note: No translation_key needed - HA provides automatic translation for TAMPER device_class
        sensors = [
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
        """Return sensor entities for buttons."""
        sensors = []

        # Battery level
        # Note: No translation_key needed - HA provides automatic translation for BATTERY device_class
        sensors.append(
            {
                "key": "battery",
                "device_class": SensorDeviceClass.BATTERY,
                "native_unit_of_measurement": PERCENTAGE,
                "state_class": SensorStateClass.MEASUREMENT,
                "value_fn": lambda: self.device.battery_level if self.device.battery_level is not None else None,
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
                "value_fn": lambda: self.device.signal_strength if self.device.signal_strength is not None else None,
                "enabled_by_default": True,
            }
        )

        # Last button action (single_press, double_press, long_press, etc.)
        sensors.append(
            {
                "key": "last_action",
                "translation_key": "last_action",
                "value_fn": lambda: self.device.attributes.get("last_action"),
                "enabled_by_default": True,
            }
        )

        # Button mode (PANIC_BUTTON, SMART_BUTTON, INTERCONNECT_DELAY)
        if "button_mode" in self.device.attributes:
            sensors.append(
                {
                    "key": "button_mode",
                    "translation_key": "button_mode",
                    "device_class": SensorDeviceClass.ENUM,
                    "options": ["panic_button", "smart_button", "interconnect_delay"],
                    "value_fn": lambda: self.device.attributes.get("button_mode", "").lower(),
                    "enabled_by_default": True,
                }
            )

        # LED brightness (OFF, LOW, HIGH)
        if "brightness" in self.device.attributes:
            sensors.append(
                {
                    "key": "button_brightness",
                    "translation_key": "button_brightness",
                    "device_class": SensorDeviceClass.ENUM,
                    "options": ["off", "low", "high"],
                    "value_fn": lambda: self.device.attributes.get("brightness", "").lower(),
                    "enabled_by_default": True,
                }
            )

        # False press filter (LONG_PUSH, DOUBLE_CLICK, DISABLED)
        if "false_press_filter" in self.device.attributes:
            sensors.append(
                {
                    "key": "false_press_filter",
                    "translation_key": "false_press_filter",
                    "device_class": SensorDeviceClass.ENUM,
                    "options": ["long_push", "double_click", "disabled"],
                    "value_fn": lambda: self.device.attributes.get("false_press_filter", "").lower(),
                    "enabled_by_default": True,
                }
            )

        return sensors

    def get_events(self) -> list[dict]:
        """Return event entities for buttons."""
        return [
            {
                "key": "button_press",
                "translation_key": "button_press",
                "event_types": [
                    "single_press",
                    "double_press",
                    "long_press",
                    "panic",
                    "emergency",
                ],
                "enabled_by_default": True,
            },
        ]

    def get_switches(self) -> list[dict]:
        """Return switch entities for buttons."""
        # Buttons typically don't have configurable switches
        return []
