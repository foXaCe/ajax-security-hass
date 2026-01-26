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
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature

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
                "value_fn": lambda: self.device.attributes.get("switchState", "").upper() == "BUTTON_PRESSED",
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
        sensors = []

        # Battery level
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

        # Temperature (MCP has built-in temperature sensor)
        if self.device.attributes.get("temperature") is not None:
            sensors.append(
                {
                    "key": "temperature",
                    "device_class": SensorDeviceClass.TEMPERATURE,
                    "native_unit_of_measurement": UnitOfTemperature.CELSIUS,
                    "state_class": SensorStateClass.MEASUREMENT,
                    "value_fn": lambda: self.device.attributes.get("temperature"),
                    "enabled_by_default": True,
                }
            )

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
                    "value_fn": lambda: self.device.attributes.get("color", "").lower(),
                    "enabled_by_default": False,
                }
            )

        return sensors

    def get_switches(self) -> list[dict]:
        """Return switch entities for MCP."""
        # MCP doesn't have configurable switches
        return []
