"""Smoke detector handler for Ajax FireProtect series.

Handles:
- FireProtect (smoke detector)
- FireProtect Plus (smoke + CO + temperature)
- FireProtect 2 and all variants (smoke + CO + temperature + steam detection)
  - FireProtect2, FireProtect2Plus, FireProtect2Sb, FireProtect2PlusSb
  - FireProtect2Hrb, FireProtect2Hsb, FireProtect2Crb, FireProtect2Csb
  - FireProtect2Hcrb, FireProtect2Hcsb, and other regional variants
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
                # Check both REST API state and SSE event attribute
                "value_fn": lambda: (
                    self.device.attributes.get("state") == "ALARM"
                    or self.device.attributes.get("smoke_detected", False)
                ),
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

        # CO detector (FireProtect Plus, FireProtect 2 and all variants)
        # CO alarm is separate from smoke - check for CO-specific state
        # FireProtect2 variants: FireProtect2Plus, FireProtect2Sb, FireProtect2Crb, etc.
        raw_type = self.device.raw_type or ""
        has_co = (
            raw_type.startswith("FireProtect2")
            or raw_type == "FireProtectPlus"
            or raw_type == "FIRE_PROTECT_2_BASE"
            or "coAlarm" in self.device.attributes
        )
        if has_co:
            sensors.append(
                {
                    "key": "co",
                    "device_class": BinarySensorDeviceClass.CO,
                    # Check both REST API attribute and SSE event attribute
                    "value_fn": lambda: (
                        self.device.attributes.get("co_alarm", False)
                        or self.device.attributes.get("co_detected", False)
                        or self.device.attributes.get("coAlarm") == "CO_ALARM_DETECTED"
                    ),
                    "enabled_by_default": True,
                }
            )

        # Steam detector (FireProtect 2 variants with steam detection)
        has_steam = "steamAlarm" in self.device.attributes
        if has_steam:
            sensors.append(
                {
                    "key": "steam",
                    "translation_key": "steam",
                    "device_class": BinarySensorDeviceClass.MOISTURE,
                    "value_fn": lambda: (
                        self.device.attributes.get("steamAlarm") == "STEAM_ALARM_DETECTED"
                        or self.device.attributes.get("steam_detected", False)
                    ),
                    "enabled_by_default": True,
                }
            )

        # High temperature alarm (FireProtect 2 variants)
        if "temperatureAlarmDetected" in self.device.attributes:
            sensors.append(
                {
                    "key": "high_temperature",
                    "translation_key": "high_temperature",
                    "device_class": BinarySensorDeviceClass.HEAT,
                    "value_fn": lambda: self.device.attributes.get("temperatureAlarmDetected", False),
                    "enabled_by_default": True,
                }
            )

        # Rapid temperature rise alarm (FireProtect 2 variants)
        if "highTemperatureDiffDetected" in self.device.attributes:
            sensors.append(
                {
                    "key": "rapid_temperature_rise",
                    "translation_key": "rapid_temperature_rise",
                    "device_class": BinarySensorDeviceClass.HEAT,
                    "value_fn": lambda: self.device.attributes.get("highTemperatureDiffDetected", False),
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
                "value_fn": lambda: self.device.battery_level if self.device.battery_level is not None else None,
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
                "value_fn": lambda: self.device.signal_strength if self.device.signal_strength is not None else None,
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
                    "value_fn": lambda: (
                        ", ".join(str(m) for m in self.device.malfunctions)
                        if isinstance(self.device.malfunctions, list)
                        else str(self.device.malfunctions)
                        if self.device.malfunctions
                        else "None"
                    ),
                    "enabled_by_default": True,
                }
            )

        # Firmware version (uses device.firmware_version, populated by coordinator)
        if self.device.firmware_version:
            sensors.append(
                {
                    "key": "firmware_version",
                    "translation_key": "firmware_version",
                    "value_fn": lambda: self.device.firmware_version,
                    "enabled_by_default": False,
                    "entity_category": "diagnostic",
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
                    "value_fn": lambda: self.device.attributes.get("indicatorLightMode") == "STANDARD",
                    "api_key": "indicatorLightMode",
                    "api_value_on": "STANDARD",
                    "api_value_off": "DONT_BLINK_ON_ALARM",
                    "enabled_by_default": True,
                }
            )

        # CO alarm enable (FireProtect 2 variants)
        # bypass_security_check: these are device config, not security commands
        if "coAlarmEnable" in self.device.attributes:
            switches.append(
                {
                    "key": "co_alarm_enabled",
                    "translation_key": "co_alarm_enabled",
                    "value_fn": lambda: self.device.attributes.get("coAlarmEnable") == "CO_ALARM_ENABLED",
                    "api_key": "coAlarmEnable",
                    "api_value_on": "CO_ALARM_ENABLED",
                    "api_value_off": "CO_ALARM_DISABLED",
                    "enabled_by_default": True,
                    "bypass_security_check": True,
                }
            )

        # Temperature alarm enable (FireProtect 2 variants)
        if "tempAlarmEnable" in self.device.attributes:
            switches.append(
                {
                    "key": "temp_alarm_enabled",
                    "translation_key": "temp_alarm_enabled",
                    "value_fn": lambda: self.device.attributes.get("tempAlarmEnable") == "TEMP_ALARM_ENABLED",
                    "api_key": "tempAlarmEnable",
                    "api_value_on": "TEMP_ALARM_ENABLED",
                    "api_value_off": "TEMP_ALARM_DISABLED",
                    "enabled_by_default": True,
                    "bypass_security_check": True,
                }
            )

        # Rapid temperature rise alarm enable (FireProtect 2 variants)
        if "tempDiffAlarmEnable" in self.device.attributes:
            switches.append(
                {
                    "key": "temp_diff_alarm_enabled",
                    "translation_key": "temp_diff_alarm_enabled",
                    "value_fn": lambda: self.device.attributes.get("tempDiffAlarmEnable") == "TEMP_DIFF_ALARM_ENABLED",
                    "api_key": "tempDiffAlarmEnable",
                    "api_value_on": "TEMP_DIFF_ALARM_ENABLED",
                    "api_value_off": "TEMP_DIFF_ALARM_DISABLED",
                    "enabled_by_default": True,
                    "bypass_security_check": True,
                }
            )

        # Siren triggers for FireProtect2 variants
        siren_triggers = self.device.attributes.get("siren_triggers", [])
        if siren_triggers:
            # Smoke trigger
            if "SMOKE" in siren_triggers or "smokeAlarm" in self.device.attributes:
                switches.append(
                    {
                        "key": "siren_trigger_smoke",
                        "translation_key": "siren_trigger_smoke",
                        "value_fn": lambda: "SMOKE" in self.device.attributes.get("siren_triggers", []),
                        "api_key": "sirenTriggers",
                        "trigger_key": "SMOKE",
                        "enabled_by_default": True,
                        "bypass_security_check": True,
                    }
                )

            # CO trigger
            if "CO" in siren_triggers or "CCO" in siren_triggers or "coAlarm" in self.device.attributes:
                switches.append(
                    {
                        "key": "siren_trigger_co",
                        "translation_key": "siren_trigger_co",
                        "value_fn": lambda: (
                            "CO" in self.device.attributes.get("siren_triggers", [])
                            or "CCO" in self.device.attributes.get("siren_triggers", [])
                        ),
                        "api_key": "sirenTriggers",
                        "trigger_key": "CO",
                        "enabled_by_default": True,
                        "bypass_security_check": True,
                    }
                )

            # Temperature trigger
            if "TEMPERATURE" in siren_triggers:
                switches.append(
                    {
                        "key": "siren_trigger_temperature",
                        "translation_key": "siren_trigger_temperature",
                        "value_fn": lambda: "TEMPERATURE" in self.device.attributes.get("siren_triggers", []),
                        "api_key": "sirenTriggers",
                        "trigger_key": "TEMPERATURE",
                        "enabled_by_default": True,
                        "bypass_security_check": True,
                    }
                )

            # Temperature diff trigger
            if "TEMPERATURE_DIFF" in siren_triggers:
                switches.append(
                    {
                        "key": "siren_trigger_temp_diff",
                        "translation_key": "siren_trigger_temp_diff",
                        "value_fn": lambda: "TEMPERATURE_DIFF" in self.device.attributes.get("siren_triggers", []),
                        "api_key": "sirenTriggers",
                        "trigger_key": "TEMPERATURE_DIFF",
                        "enabled_by_default": True,
                        "bypass_security_check": True,
                    }
                )

        return switches
