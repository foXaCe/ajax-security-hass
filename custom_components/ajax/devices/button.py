"""Button device handler for Ajax Button series.

Handles:
- Button (single button)
- DoubleButton (two buttons)
- SpaceControl (keyfob with multiple buttons)
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventDeviceClass
from homeassistant.components.sensor import SensorDeviceClass

from .base import AjaxDeviceHandler


def _enum_or_none(raw: Any, options: list[str]) -> str | None:
    """Lowercase an Ajax enum token, returning None when missing or unmapped.

    A ``SensorDeviceClass.ENUM`` sensor errors on any value outside ``options``;
    Ajax may send a null (or a firmware-added) token for a present key, so snap
    anything not declared to None (HA renders it as ``unknown``).
    """
    value = (raw or "").lower()
    return value if value in options else None


class ButtonHandler(AjaxDeviceHandler):
    """Handler for Ajax Button devices."""

    def get_binary_sensors(self) -> list[dict[str, Any]]:
        """Return binary sensor entities for buttons."""
        return [self._tamper_binary_sensor()]

    def get_sensors(self) -> list[dict[str, Any]]:
        """Return sensor entities for buttons."""
        sensors: list[dict[str, Any]] = [
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
                    "value_fn": lambda: _enum_or_none(
                        self.device.attributes.get("button_mode"),
                        ["panic_button", "smart_button", "interconnect_delay"],
                    ),
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
                    "value_fn": lambda: _enum_or_none(self.device.attributes.get("brightness"), ["off", "low", "high"]),
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
                    "value_fn": lambda: _enum_or_none(
                        self.device.attributes.get("false_press_filter"),
                        ["long_push", "double_click", "disabled"],
                    ),
                    "enabled_by_default": True,
                }
            )

        return sensors

    def get_events(self) -> list[dict[str, Any]]:
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

    def get_switches(self) -> list[dict[str, Any]]:
        """Return switch entities for buttons."""
        # Buttons typically don't have configurable switches
        return []
