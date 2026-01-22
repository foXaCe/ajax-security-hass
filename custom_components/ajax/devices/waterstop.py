"""WaterStop handler for Ajax WaterStop smart water valve.

Handles:
- WaterStop (smart water valve with leak protection)
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


class WaterStopHandler(AjaxDeviceHandler):
    """Handler for Ajax WaterStop smart water valve."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for WaterStop."""
        sensors = []

        # Tamper detection
        sensors.append(
            {
                "key": "tamper",
                "device_class": BinarySensorDeviceClass.TAMPER,
                "value_fn": lambda: self.device.attributes.get("tampered", False),
                "enabled_by_default": True,
            }
        )

        # Problem/malfunction indicator
        sensors.append(
            {
                "key": "problem",
                "translation_key": "problem",
                "device_class": BinarySensorDeviceClass.PROBLEM,
                "value_fn": lambda: bool(self.device.malfunctions),
                "enabled_by_default": True,
            }
        )

        # Temperature protection active
        if "tempProtectState" in self.device.attributes:
            sensors.append(
                {
                    "key": "temp_protect",
                    "translation_key": "waterstop_temp_protect",
                    "device_class": BinarySensorDeviceClass.COLD,
                    "value_fn": lambda: self.device.attributes.get("tempProtectState") == "ON",
                    "enabled_by_default": True,
                }
            )

        return sensors

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for WaterStop."""
        sensors = []

        # Battery level
        if "batteryChargeLevelPercentage" in self.device.attributes:
            sensors.append(
                {
                    "key": "battery",
                    "device_class": SensorDeviceClass.BATTERY,
                    "native_unit_of_measurement": PERCENTAGE,
                    "state_class": SensorStateClass.MEASUREMENT,
                    "value_fn": lambda: self.device.attributes.get("batteryChargeLevelPercentage"),
                    "enabled_by_default": True,
                }
            )

        # Temperature
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

        # Motor state
        if "motorState" in self.device.attributes:
            sensors.append(
                {
                    "key": "motor_state",
                    "translation_key": "waterstop_motor_state",
                    "device_class": "enum",
                    "options": ["on", "off"],
                    "value_fn": lambda: self.device.attributes.get("motorState", "OFF").lower(),
                    "enabled_by_default": False,
                    "entity_category": "diagnostic",
                }
            )

        # External power
        if "extPower" in self.device.attributes:
            sensors.append(
                {
                    "key": "external_power",
                    "translation_key": "waterstop_external_power",
                    "device_class": "enum",
                    "options": ["supply", "battery", "unknown"],
                    "value_fn": lambda: self.device.attributes.get("extPower", "UNKNOWN").lower(),
                    "enabled_by_default": True,
                    "entity_category": "diagnostic",
                }
            )

        # Prevention settings
        if "preventionEnable" in self.device.attributes:
            sensors.append(
                {
                    "key": "prevention_status",
                    "translation_key": "waterstop_prevention_status",
                    "device_class": "enum",
                    "options": ["enabled", "disabled"],
                    "value_fn": lambda: self.device.attributes.get("preventionEnable", "DISABLED").lower(),
                    "enabled_by_default": False,
                    "entity_category": "diagnostic",
                }
            )

        # Prevention period (days)
        if "preventionDaysPeriod" in self.device.attributes:
            sensors.append(
                {
                    "key": "prevention_period",
                    "translation_key": "waterstop_prevention_period",
                    "native_unit_of_measurement": "days",
                    "value_fn": lambda: self.device.attributes.get("preventionDaysPeriod"),
                    "enabled_by_default": False,
                    "entity_category": "diagnostic",
                }
            )

        # Firmware version
        if "firmwareVersion" in self.device.attributes:
            sensors.append(
                {
                    "key": "firmware_version",
                    "translation_key": "firmware_version",
                    "value_fn": lambda: self.device.attributes.get("firmwareVersion"),
                    "enabled_by_default": False,
                    "entity_category": "diagnostic",
                }
            )

        return sensors

    def get_valves(self) -> list[dict]:
        """Return valve entities for WaterStop."""
        return [
            {
                "key": "valve",
                "translation_key": "waterstop_valve",
                "value_fn": lambda: self.device.attributes.get("valveState") == "OPEN",
                "open_fn": lambda: {"action": "open_valve"},
                "close_fn": lambda: {"action": "close_valve"},
                "enabled_by_default": True,
            }
        ]
