"""Button device handler for Ajax Button series.

Handles:
- Button (single button)
- DoubleButton (two buttons)
- SpaceControl (keyfob with multiple buttons)
"""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass
from homeassistant.components.sensor import SensorDeviceClass

from .base import AjaxDeviceHandler


class ButtonHandler(AjaxDeviceHandler):
    """Handler for Ajax Button devices."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for buttons."""
        return [self._tamper_binary_sensor()]

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for buttons."""
        sensors: list[dict] = [
            self._battery_sensor(),
            self._signal_strength_percent_sensor(),
        ]

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
                    "value_fn": lambda: (self.device.attributes.get("button_mode") or "").lower(),
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
                    "value_fn": lambda: (self.device.attributes.get("brightness") or "").lower(),
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
                    "value_fn": lambda: (self.device.attributes.get("false_press_filter") or "").lower(),
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
                "device_class": EventDeviceClass.BUTTON,
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
