"""LightSwitch handler for Ajax LightSwitch devices (non-dimmer).

Handles:
- LightSwitchTwoWay (single channel two-way switch)
- LightSwitchTwoGang (two channel switch)
- LightSwitchTwoChannelTwoWay (two channel two-way switch)

Features:
- ON/OFF control per channel
- Touch sensitivity settings
- LED indicator settings
- Protection status monitoring
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


class LightSwitchHandler(AjaxDeviceHandler):
    """Handler for Ajax LightSwitch devices (non-dimmer)."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for LightSwitch."""
        # Note: LightSwitch devices have no tamper sensor
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
        if "protectStatuses" in self.device.attributes:
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
        """Return sensor entities for LightSwitch."""
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
        """Return switch entities for LightSwitch settings."""
        switches = []

        # Multi-gang channel switches are handled by SocketHandler
        # Here we only handle settings switches

        # LED indicator enabled
        if "settingsSwitch" in self.device.attributes:
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

            # Child lock (if available)
            if "CHILD_LOCK_ENABLED" in self.device.attributes.get("settingsSwitch", []) or True:
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

        return switches

    def get_numbers(self) -> list[dict]:
        """Return number entities for LightSwitch settings."""
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

        return numbers

    def get_selects(self) -> list[dict]:
        """Return select entities for LightSwitch settings."""
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

        return selects
