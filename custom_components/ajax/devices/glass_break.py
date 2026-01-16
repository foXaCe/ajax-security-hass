"""Glass break detector handler for Ajax GlassProtect series.

Handles:
- GlassProtect
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


class GlassBreakHandler(AjaxDeviceHandler):
    """Handler for Ajax GlassProtect glass break detectors."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for glass break detectors."""
        sensors = []

        # Main glass break sensor
        # Note: Ajax API doesn't provide real-time glass break detection when disarmed.
        # The 'state' field only shows ALARM when armed and glass break triggers alarm.
        sensors.append(
            {
                "key": "glass_break",
                "translation_key": "glass_break",
                "device_class": BinarySensorDeviceClass.SAFETY,
                "value_fn": lambda: self.device.attributes.get("state") == "ALARM",
                "enabled_by_default": True,
                "name": None,
            }
        )

        # External contact (for connecting wired sensors)
        # Only create if extraContactAware is True (feature enabled on device)
        if self.device.attributes.get("extra_contact_aware", False):
            sensors.append(
                {
                    "key": "external_contact",
                    "translation_key": "external_contact",
                    "device_class": BinarySensorDeviceClass.DOOR,
                    "value_fn": lambda: self.device.attributes.get(
                        "external_contact_opened", False
                    ),
                    "enabled_by_default": True,
                }
            )

        # Note: "armed_in_night_mode" is now a switch, not a binary sensor

        # Tamper / Couvercle
        # Note: No translation_key needed - HA provides automatic translation for TAMPER device_class
        sensors.append(
            {
                "key": "tamper",
                "device_class": BinarySensorDeviceClass.TAMPER,
                "value_fn": lambda: self.device.attributes.get("tampered", False),
                "enabled_by_default": True,
            }
        )

        return sensors

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for glass break detectors."""
        sensors = []

        # Battery level
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

        # Signal strength
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

        # Sensitivity (0=Low, 1=Normal, 2=High)
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
        """Return switch entities for glass break detectors."""
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

        # External contact switch (enable/disable the feature)
        switches.append(
            {
                "key": "external_contact_enabled",
                "translation_key": "external_contact_enabled",
                "value_fn": lambda: self.device.attributes.get(
                    "extra_contact_aware", False
                ),
                "api_key": "extraContactAware",
                "enabled_by_default": True,
            }
        )

        # Siren trigger for glass break
        switches.append(
            {
                "key": "siren_trigger_glass",
                "translation_key": "siren_trigger_glass",
                "value_fn": lambda: "GLASS"
                in self.device.attributes.get("siren_triggers", []),
                "api_key": "sirenTriggers",
                "trigger_key": "GLASS",
                "enabled_by_default": True,
            }
        )

        return switches
