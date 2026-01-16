"""Flood/Water leak detector handler for Ajax LeaksProtect.

Handles:
- LeaksProtect (water leak detector)
- LeaksProtect with temperature sensor
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfTemperature,
)

from .base import AjaxDeviceHandler


class FloodDetectorHandler(AjaxDeviceHandler):
    """Handler for Ajax LeaksProtect flood/water leak detectors."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for flood detectors."""
        # Note: No translation_key needed - HA provides automatic translation for device_class
        return [
            {
                "key": "moisture",
                "device_class": BinarySensorDeviceClass.MOISTURE,
                # Note: Ajax API uses 'state' field - ALARM when leak detected
                "value_fn": lambda: self.device.attributes.get("state") == "ALARM",
                "enabled_by_default": True,
                "name": None,
            },
            # Note: "armed_in_night_mode" is now a switch, not a binary sensor
            {
                "key": "tamper",
                "device_class": BinarySensorDeviceClass.TAMPER,
                "value_fn": lambda: self.device.attributes.get("tampered", False),
                "enabled_by_default": True,
            },
        ]

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for flood detectors."""
        sensors = []

        # Battery level - always create (all LeaksProtect are battery powered)
        # Note: No translation_key needed - HA provides automatic translation for BATTERY device_class
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

        # Signal strength - always create
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

        # Temperature (some LeaksProtect models have temperature sensor)
        # Note: No translation_key needed - HA provides automatic translation for TEMPERATURE device_class
        if "temperature" in self.device.attributes:
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

        # Malfunctions
        if self.device.malfunctions:
            sensors.append(
                {
                    "key": "malfunctions",
                    "translation_key": "malfunctions",
                    "value_fn": lambda: ", ".join(self.device.malfunctions)
                    if self.device.malfunctions
                    else "None",
                    "enabled_by_default": True,
                }
            )

        # Firmware version
        if "firmware_version" in self.device.attributes:
            sensors.append(
                {
                    "key": "firmware_version",
                    "translation_key": "firmware_version",
                    "value_fn": lambda: self.device.attributes.get("firmware_version"),
                    "enabled_by_default": False,
                }
            )

        return sensors

    def get_switches(self) -> list[dict]:
        """Return switch entities for flood detectors."""
        switches = []

        # LED Indicator switch
        if "indicatorLightMode" in self.device.attributes:
            switches.append(
                {
                    "key": "indicator_light",
                    "translation_key": "indicator_light",
                    "value_fn": lambda: self.device.attributes.get("indicatorLightMode")
                    == "STANDARD",
                    "api_key": "indicatorLightMode",
                    "api_value_on": "STANDARD",
                    "api_value_off": "DONT_BLINK_ON_ALARM",
                    "enabled_by_default": True,
                }
            )

        # Night Mode switch
        switches.append(
            {
                "key": "night_mode",
                "translation_key": "night_mode",
                "value_fn": lambda: self.device.attributes.get("night_mode_arm", False),
                "api_key": "nightModeArm",
                "enabled_by_default": True,
            }
        )

        return switches
