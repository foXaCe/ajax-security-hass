"""Motion detector device handler for Ajax MotionProtect series.

Handles:
- MotionProtect
- MotionProtect Plus (with microwave sensor)
- MotionProtect Outdoor (with dual motion detection)
- MotionCam (with camera)
- CombiProtect (motion + glass break)
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


class MotionDetectorHandler(AjaxDeviceHandler):
    """Handler for Ajax MotionProtect motion detectors."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for motion detectors."""
        # Note: No translation_key needed - HA provides automatic translation for device_class
        sensors = [
            {
                "key": "motion",
                "device_class": BinarySensorDeviceClass.MOTION,
                # motion_detected is set by SSE/SQS events in real-time
                "value_fn": lambda: self.device.attributes.get(
                    "motion_detected", False
                ),
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

        # CombiProtect also has glass break detection
        if "glass_break_detected" in self.device.attributes:
            sensors.append(
                {
                    "key": "glass_break",
                    "translation_key": "glass_break",
                    "device_class": BinarySensorDeviceClass.SAFETY,
                    "value_fn": lambda: self.device.attributes.get(
                        "glass_break_detected", False
                    ),
                    "enabled_by_default": True,
                }
            )

        return sensors

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for motion detectors."""
        sensors = []

        # Battery level - always create
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

        # Temperature
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

        # Sensitivity
        if "sensitivity" in self.device.attributes:
            sensors.append(
                {
                    "key": "sensitivity",
                    "translation_key": "sensitivity",
                    "value_fn": lambda: {0: "low", 1: "normal", 2: "high"}.get(
                        self.device.attributes.get("sensitivity"),
                        self.device.attributes.get("sensitivity"),
                    ),
                    "enabled_by_default": True,
                }
            )

        return sensors

    def get_switches(self) -> list[dict]:
        """Return switch entities for motion detectors."""
        switches = []

        # Always Active switch
        switches.append(
            {
                "key": "always_active",
                "translation_key": "always_active",
                "value_fn": lambda: self.device.attributes.get("always_active", False),
                "api_key": "alwaysActive",
                "enabled_by_default": True,
            }
        )

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
