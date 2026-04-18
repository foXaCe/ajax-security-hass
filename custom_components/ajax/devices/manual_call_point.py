"""Manual Call Point (MCP) device handler for Ajax fire alarm buttons.

Handles:
- ManualCallPoint (fire alarm button)
- SwitchBaseMcpFire

The MCP is a manual fire alarm trigger button. When pressed, it triggers
a fire alarm. The button state is tracked via switchState attribute.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import (
    SensorDeviceClass,
)

from .base import AjaxDeviceHandler


class ManualCallPointHandler(AjaxDeviceHandler):
    """Handler for Ajax Manual Call Point (MCP) fire alarm buttons."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for MCP."""
        sensors = [
            # Fire alarm triggered (button pressed)
            {
                "key": "fire_alarm",
                "translation_key": "mcp_fire_alarm",
                "device_class": BinarySensorDeviceClass.SAFETY,
                "value_fn": lambda: (self.device.attributes.get("switchState") or "").upper() == "BUTTON_PRESSED",
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
        """Return sensor entities for MCP."""
        sensors: list[dict] = [
            self._battery_sensor(),
            self._signal_strength_percent_sensor(),
        ]
        if self.device.attributes.get("temperature") is not None:
            sensors.append(self._temperature_sensor())

        # Button state as text sensor
        sensors.append(
            {
                "key": "switch_state",
                "translation_key": "mcp_switch_state",
                "device_class": SensorDeviceClass.ENUM,
                "options": ["button_unpressed", "button_pressed"],
                "value_fn": lambda: self.device.attributes.get("switchState", "BUTTON_UNPRESSED").lower(),
                "enabled_by_default": True,
            }
        )

        # Device color
        if self.device.attributes.get("color"):
            sensors.append(
                {
                    "key": "device_color",
                    "translation_key": "device_color",
                    "device_class": SensorDeviceClass.ENUM,
                    "options": ["red", "blue", "white", "black"],
                    "value_fn": lambda: (self.device.attributes.get("color") or "").lower(),
                    "enabled_by_default": False,
                }
            )

        return sensors

    def get_switches(self) -> list[dict]:
        """Return switch entities for MCP."""
        # MCP doesn't have configurable switches
        return []
