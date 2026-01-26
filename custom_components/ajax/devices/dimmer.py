"""Dimmer handler for Ajax LightSwitchDimmer.

Handles:
- LightSwitchDimmer (dimmable wall switch with touch panel)

Features:
- ON/OFF control with brightness
- Touch sensitivity settings
- Dimmer curve settings
- Protection status monitoring
- Arm/disarm brightness actions
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


class DimmerHandler(AjaxDeviceHandler):
    """Handler for Ajax LightSwitchDimmer devices."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for dimmer."""
        # Note: LightSwitchDimmer has no tamper sensor
        sensors = [
            {
                "key": "problem",
                "translation_key": "problem",
                "device_class": BinarySensorDeviceClass.PROBLEM,
                "value_fn": lambda: bool(self.device.malfunctions),
                "enabled_by_default": True,
            },
        ]

        # Current limit protection
        sensors.append(
            {
                "key": "current_limit",
                "translation_key": "current_limit",
                "device_class": BinarySensorDeviceClass.SAFETY,
                "value_fn": lambda: "CURRENT_LIMIT_ON" in self.device.attributes.get("protectStatuses", []),
                "enabled_by_default": True,
            }
        )

        # Temperature limit protection
        sensors.append(
            {
                "key": "temperature_limit",
                "translation_key": "temperature_limit",
                "device_class": BinarySensorDeviceClass.HEAT,
                "value_fn": lambda: "TEMPERATURE_LIMIT_ON" in self.device.attributes.get("protectStatuses", []),
                "enabled_by_default": True,
            }
        )

        # Data channel status
        if "dataChannelOk" in self.device.attributes:
            sensors.append(
                {
                    "key": "data_channel_ok",
                    "translation_key": "data_channel_ok",
                    "device_class": BinarySensorDeviceClass.CONNECTIVITY,
                    "value_fn": lambda: self.device.attributes.get("dataChannelOk") == "OK",
                    "enabled_by_default": True,
                }
            )

        return sensors

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for dimmer."""
        sensors = []

        # Signal strength
        sensors.append(
            {
                "key": "signal_strength",
                "translation_key": "signal_strength",
                "native_unit_of_measurement": PERCENTAGE,
                "state_class": SensorStateClass.MEASUREMENT,
                "value_fn": lambda: self.device.signal_strength,
                "enabled_by_default": True,
            }
        )

        # Temperature (internal)
        if "temperature" in self.device.attributes or self.device.attributes.get("temperature") is not None:
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

        # Current brightness level (read-only sensor for display)
        sensors.append(
            {
                "key": "current_brightness",
                "translation_key": "current_brightness",
                "native_unit_of_measurement": PERCENTAGE,
                "state_class": SensorStateClass.MEASUREMENT,
                "value_fn": lambda: self.device.attributes.get("actualBrightnessCh1", 0),
                "enabled_by_default": False,
                "entity_category": "diagnostic",
            }
        )

        # Data channel signal quality
        if "dataChannelSignalQuality" in self.device.attributes:
            sensors.append(
                {
                    "key": "data_channel_quality",
                    "translation_key": "data_channel_quality",
                    "value_fn": lambda: self.device.attributes.get("dataChannelSignalQuality", "")
                    .lower()
                    .replace("_", " "),
                    "enabled_by_default": False,
                    "entity_category": "diagnostic",
                }
            )

        # Firmware version
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
        """Return switch entities for dimmer settings."""
        switches = []

        # LED indicator enabled
        switches.append(
            {
                "key": "led_indicator",
                "translation_key": "led_indicator",
                "value_fn": lambda: "LED_INDICATOR_ENABLED" in self.device.attributes.get("settingsSwitch", []),
                "api_key": "settingsSwitch",
                "settings_key": "LED_INDICATOR_ENABLED",
                "enabled_by_default": True,
                "bypass_security_check": True,
            }
        )

        # Child lock
        switches.append(
            {
                "key": "child_lock",
                "translation_key": "child_lock",
                "value_fn": lambda: "CHILD_LOCK_ENABLED" in self.device.attributes.get("settingsSwitch", []),
                "api_key": "settingsSwitch",
                "settings_key": "CHILD_LOCK_ENABLED",
                "enabled_by_default": True,
                "bypass_security_check": True,
            }
        )

        # State memory (remember state after power outage)
        switches.append(
            {
                "key": "state_memory",
                "translation_key": "state_memory",
                "value_fn": lambda: "STATE_MEMORY_ENABLED" in self.device.attributes.get("settingsSwitch", []),
                "api_key": "settingsSwitch",
                "settings_key": "STATE_MEMORY_ENABLED",
                "enabled_by_default": True,
                "bypass_security_check": True,
            }
        )

        # Current threshold protection
        switches.append(
            {
                "key": "current_threshold",
                "translation_key": "current_threshold",
                "value_fn": lambda: "CURRENT_THRESHOLD_ENABLED" in self.device.attributes.get("settingsSwitch", []),
                "api_key": "settingsSwitch",
                "settings_key": "CURRENT_THRESHOLD_ENABLED",
                "enabled_by_default": True,
                "bypass_security_check": True,
            }
        )

        # Night mode arm
        if "nightModeArm" in self.device.attributes:
            switches.append(
                {
                    "key": "night_mode",
                    "translation_key": "night_mode",
                    "value_fn": lambda: self.device.attributes.get("nightModeArm", False),
                    "api_key": "nightModeArm",
                    "api_value_on": True,
                    "api_value_off": False,
                    "enabled_by_default": True,
                    "bypass_security_check": True,
                }
            )

        # Dimmer calibration
        dimmer_settings = self.device.attributes.get("dimmerSettings", {})
        if dimmer_settings:
            switches.append(
                {
                    "key": "dimmer_calibration",
                    "translation_key": "dimmer_calibration",
                    "value_fn": lambda: self.device.attributes.get("dimmerSettings", {}).get("calibration")
                    == "ENABLED",
                    "api_nested_key": "dimmerSettings",
                    "api_key": "calibration",
                    "api_value_on": "ENABLED",
                    "api_value_off": "DISABLED",
                    "enabled_by_default": False,
                    "entity_category": "config",
                    "bypass_security_check": True,
                }
            )

        return switches

    def get_numbers(self) -> list[dict]:
        """Return number entities for dimmer settings."""
        numbers = []

        # Touch sensitivity (1-7)
        if "touchSensitivity" in self.device.attributes:
            numbers.append(
                {
                    "key": "touch_sensitivity",
                    "translation_key": "touch_sensitivity",
                    "min_value": 1,
                    "max_value": 7,
                    "step": 1,
                    "value_fn": lambda: self.device.attributes.get("touchSensitivity", 4),
                    "api_key": "touchSensitivity",
                    "enabled_by_default": True,
                    "entity_category": "config",
                }
            )

        # Brightness change speed (0-20)
        if "brightnessChangeSpeed" in self.device.attributes:
            numbers.append(
                {
                    "key": "brightness_change_speed",
                    "translation_key": "brightness_change_speed",
                    "min_value": 0,
                    "max_value": 20,
                    "step": 1,
                    "value_fn": lambda: self.device.attributes.get("brightnessChangeSpeed", 10),
                    "api_key": "brightnessChangeSpeed",
                    "enabled_by_default": True,
                    "entity_category": "config",
                }
            )

        # Min brightness limit (0-100%)
        if "minBrightnessLimitCh1" in self.device.attributes:
            numbers.append(
                {
                    "key": "min_brightness",
                    "translation_key": "min_brightness",
                    "min_value": 0,
                    "max_value": 100,
                    "step": 1,
                    "native_unit_of_measurement": PERCENTAGE,
                    "value_fn": lambda: self.device.attributes.get("minBrightnessLimitCh1", 0),
                    "api_key": "minBrightnessLimitCh1",
                    "enabled_by_default": True,
                    "entity_category": "config",
                }
            )

        # Max brightness limit (0-100%)
        if "maxBrightnessLimitCh1" in self.device.attributes:
            numbers.append(
                {
                    "key": "max_brightness",
                    "translation_key": "max_brightness",
                    "min_value": 0,
                    "max_value": 100,
                    "step": 1,
                    "native_unit_of_measurement": PERCENTAGE,
                    "value_fn": lambda: self.device.attributes.get("maxBrightnessLimitCh1", 100),
                    "api_key": "maxBrightnessLimitCh1",
                    "enabled_by_default": True,
                    "entity_category": "config",
                }
            )

        # Arm action brightness (0-100%)
        if "armActionBrightnessCh1" in self.device.attributes:
            numbers.append(
                {
                    "key": "arm_brightness",
                    "translation_key": "arm_brightness",
                    "min_value": 0,
                    "max_value": 100,
                    "step": 1,
                    "native_unit_of_measurement": PERCENTAGE,
                    "value_fn": lambda: self.device.attributes.get("armActionBrightnessCh1", 0),
                    "api_key": "armActionBrightnessCh1",
                    "enabled_by_default": True,
                    "entity_category": "config",
                }
            )

        # Disarm action brightness (0-100%)
        if "disarmActionBrightnessCh1" in self.device.attributes:
            numbers.append(
                {
                    "key": "disarm_brightness",
                    "translation_key": "disarm_brightness",
                    "min_value": 0,
                    "max_value": 100,
                    "step": 1,
                    "native_unit_of_measurement": PERCENTAGE,
                    "value_fn": lambda: self.device.attributes.get("disarmActionBrightnessCh1", 0),
                    "api_key": "disarmActionBrightnessCh1",
                    "enabled_by_default": True,
                    "entity_category": "config",
                }
            )

        return numbers

    def get_selects(self) -> list[dict]:
        """Return select entities for dimmer settings."""
        selects = []

        # Touch mode
        if "touchMode" in self.device.attributes:
            selects.append(
                {
                    "key": "touch_mode",
                    "translation_key": "touch_mode",
                    "options": [
                        "touch_mode_toggle_and_slider",
                        "touch_mode_toggle",
                        "touch_mode_blocked",
                    ],
                    "value_fn": lambda: self.device.attributes.get("touchMode", "").lower(),
                    "api_key": "touchMode",
                    "api_options": {
                        "touch_mode_toggle_and_slider": "TOUCH_MODE_TOGGLE_AND_SLIDER",
                        "touch_mode_toggle": "TOUCH_MODE_TOGGLE",
                        "touch_mode_blocked": "TOUCH_MODE_BLOCKED",
                    },
                    "enabled_by_default": True,
                    "entity_category": "config",
                }
            )

        # Dimmer curve type
        dimmer_settings = self.device.attributes.get("dimmerSettings", {})
        if dimmer_settings and "curveType" in dimmer_settings:
            selects.append(
                {
                    "key": "dimmer_curve",
                    "translation_key": "dimmer_curve",
                    "options": [
                        "curve_type_auto",
                        "curve_type_linear",
                        "curve_type_logarithmic",
                    ],
                    "value_fn": lambda: self.device.attributes.get("dimmerSettings", {}).get("curveType", "").lower(),
                    "api_nested_key": "dimmerSettings",
                    "api_key": "curveType",
                    "api_options": {
                        "curve_type_auto": "CURVE_TYPE_AUTO",
                        "curve_type_linear": "CURVE_TYPE_LINEAR",
                        "curve_type_logarithmic": "CURVE_TYPE_LOGARITHMIC",
                    },
                    "enabled_by_default": True,
                    "entity_category": "config",
                }
            )

        # Light source type
        if dimmer_settings and "lightSource" in dimmer_settings:
            selects.append(
                {
                    "key": "light_source",
                    "translation_key": "light_source",
                    "options": [
                        "light_source_auto",
                        "light_source_leading_edge",
                        "light_source_trailing_edge",
                    ],
                    "value_fn": lambda: self.device.attributes.get("dimmerSettings", {}).get("lightSource", "").lower(),
                    "api_nested_key": "dimmerSettings",
                    "api_key": "lightSource",
                    "api_options": {
                        "light_source_auto": "LIGHT_SOURCE_AUTO",
                        "light_source_leading_edge": "LIGHT_SOURCE_LEADING_EDGE",
                        "light_source_trailing_edge": "LIGHT_SOURCE_TRAILING_EDGE",
                    },
                    "enabled_by_default": True,
                    "entity_category": "config",
                }
            )

        return selects
