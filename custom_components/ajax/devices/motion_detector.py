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
                "value_fn": lambda: self.device.attributes.get("motion_detected", False),
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
                    "value_fn": lambda: self.device.attributes.get("glass_break_detected", False),
                    "enabled_by_default": True,
                }
            )

        return sensors

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for motion detectors."""
        sensors: list[dict] = [
            self._battery_sensor(),
            self._signal_strength_percent_sensor(),
        ]
        if "temperature" in self.device.attributes:
            sensors.append(self._temperature_sensor())

        # Sensitivity
        if "sensitivity" in self.device.attributes:
            sensors.append(
                {
                    "key": "sensitivity",
                    "translation_key": "sensitivity",
                    "value_fn": lambda: self._get_sensitivity_label(),
                    "enabled_by_default": True,
                }
            )

        return sensors

    def _get_sensitivity_label(self) -> str:
        """Get human-readable sensitivity label, safe for unknown values."""
        _map = {0: "low", 1: "normal", 2: "high"}
        raw = self.device.attributes.get("sensitivity", 0)
        try:
            return _map.get(int(raw), str(raw))
        except (ValueError, TypeError):
            return str(raw)

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
                    "value_fn": lambda: self.device.attributes.get("indicatorLightMode") == "STANDARD",
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

        # Siren trigger for motion
        switches.append(
            {
                "key": "siren_trigger_motion",
                "translation_key": "siren_trigger_motion",
                "value_fn": lambda: "MOTION" in self.device.attributes.get("siren_triggers", []),
                "api_key": "sirenTriggers",
                "trigger_key": "MOTION",
                "enabled_by_default": True,
            }
        )

        return switches
