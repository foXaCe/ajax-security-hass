"""Smoke detector handler for Ajax FireProtect series.

Handles:
- FireProtect (smoke detector)
- FireProtect Plus (smoke + CO + temperature)
- FireProtect 2 (smoke + CO + temperature + SirenControl)
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


class SmokeDetectorHandler(AjaxDeviceHandler):
    """Handler for Ajax FireProtect smoke detectors."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for smoke detectors."""
        # Note: No translation_key needed - HA provides automatic translation for device_class
        sensors = [
            {
                "key": "smoke",
                "device_class": BinarySensorDeviceClass.SMOKE,
                # Note: Ajax API uses 'state' field - ALARM when smoke detected
                "value_fn": lambda: self.device.attributes.get("state") == "ALARM",
                "enabled_by_default": True,
            },
            # Note: "armed_in_night_mode" is now a switch, not a binary sensor
            {
                "key": "tamper",
                "device_class": BinarySensorDeviceClass.TAMPER,
                "value_fn": lambda: self.device.attributes.get("tampered", False),
                "enabled_by_default": True,
            },
        ]

        # CO detector (FireProtect Plus, FireProtect 2)
        # CO alarm is separate from smoke - check for CO-specific state
        if self.device.raw_type in ["FireProtect2", "FireProtectPlus"]:
            sensors.append(
                {
                    "key": "co",
                    "device_class": BinarySensorDeviceClass.CO,
                    # CO alarm indicated by specific state or attribute
                    "value_fn": lambda: self.device.attributes.get("co_alarm", False),
                    "enabled_by_default": True,
                }
            )

        return sensors

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for smoke detectors."""
        sensors = []

        # Battery level - always create (all FireProtect are battery powered)
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

        # Temperature (FireProtect Plus, FireProtect 2)
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

        # CO level (FireProtect Plus, FireProtect 2)
        # Note: No translation_key needed - HA provides automatic translation for CO device_class
        if "co_level" in self.device.attributes:
            sensors.append(
                {
                    "key": "co_level",
                    "device_class": SensorDeviceClass.CO,
                    "native_unit_of_measurement": "ppm",
                    "state_class": SensorStateClass.MEASUREMENT,
                    "value_fn": lambda: self.device.attributes.get("co_level"),
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
        """Return switch entities for smoke detectors."""
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

        return switches
