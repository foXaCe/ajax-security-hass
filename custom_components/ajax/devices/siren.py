"""Siren device handler for Ajax HomeSiren series.

Handles:
- HomeSiren
- StreetSiren
- StreetSiren DoubleDeck
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


class SirenHandler(AjaxDeviceHandler):
    """Handler for Ajax HomeSiren sirens."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for sirens."""
        sensors = []

        # Tamper / Couvercle - only if device has tamper sensor (not None)
        # Note: No translation_key needed - HA provides automatic translation for TAMPER device_class
        if self.device.attributes.get("tampered") is not None:
            sensors.append(
                {
                    "key": "tamper",
                    "device_class": BinarySensorDeviceClass.TAMPER,
                    "value_fn": lambda: self.device.attributes.get("tampered", False),
                    "enabled_by_default": True,
                }
            )

        # Externally powered (StreetSiren only)
        if "externally_powered" in self.device.attributes:
            sensors.append(
                {
                    "key": "externally_powered",
                    "translation_key": "externally_powered",
                    "device_class": BinarySensorDeviceClass.PLUG,
                    "value_fn": lambda: self.device.attributes.get("externally_powered", False),
                    "enabled_by_default": True,
                }
            )

        return sensors

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for sirens."""
        sensors = []

        # Battery level - only create if device has battery
        # Note: No translation_key needed - HA provides automatic translation for BATTERY device_class
        if self.device.battery_level is not None:
            sensors.append(
                {
                    "key": "battery",
                    "device_class": SensorDeviceClass.BATTERY,
                    "native_unit_of_measurement": PERCENTAGE,
                    "state_class": SensorStateClass.MEASUREMENT,
                    "value_fn": lambda: self.device.battery_level,
                    "enabled_by_default": True,
                }
            )

        # Signal strength - only create if device has signal
        if self.device.signal_strength is not None:
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

        # Note: Volume and duration are now selects (controllable)

        return sensors

    def get_selects(self) -> list[dict]:
        """Return select entities for sirens."""
        selects = []

        # Siren volume level
        if "siren_volume_level" in self.device.attributes:
            selects.append(
                {
                    "key": "siren_volume",
                    "translation_key": "siren_volume",
                    "options": ["disabled", "quiet", "loud", "very_loud"],
                    "value_fn": lambda: str(self.device.attributes.get("siren_volume_level") or "VERY_LOUD").lower(),
                    "api_key": "v2sirenVolumeLevel",
                    "api_transform": lambda x: x.upper(),
                    "enabled_by_default": True,
                }
            )

        # Beep volume level
        if "beep_volume_level" in self.device.attributes:
            selects.append(
                {
                    "key": "beep_volume",
                    "translation_key": "beep_volume",
                    "options": ["quiet", "loud", "very_loud"],
                    "value_fn": lambda: str(self.device.attributes.get("beep_volume_level") or "LOUD").lower(),
                    "api_key": "beepVolumeLevel",
                    "api_transform": lambda x: x.upper(),
                    "enabled_by_default": True,
                }
            )

        # Alarm duration (in minutes)
        if "alarm_duration" in self.device.attributes:
            selects.append(
                {
                    "key": "alarm_duration",
                    "translation_key": "alarm_duration_select",
                    "options": ["1", "2", "3", "5", "10", "15"],
                    "value_fn": lambda: str(self.device.attributes.get("alarm_duration", 3)),
                    "api_key": "alarmDuration",
                    "api_transform": lambda x: int(x),
                    "enabled_by_default": True,
                }
            )

        return selects

    def get_switches(self) -> list[dict]:
        """Return switch entities for sirens and transmitters."""
        switches = []

        # Night Mode switch - only for devices that support it
        if "night_mode_arm" in self.device.attributes:
            switches.append(
                {
                    "key": "night_mode",
                    "translation_key": "night_mode",
                    "value_fn": lambda: self.device.attributes.get("night_mode_arm", False),
                    "api_key": "nightModeArm",
                    "enabled_by_default": True,
                }
            )

        # Beep on arm/disarm
        if "beep_on_arm_disarm" in self.device.attributes:
            switches.append(
                {
                    "key": "beep_on_arm_disarm",
                    "translation_key": "beep_on_arm_disarm",
                    "value_fn": lambda: self.device.attributes.get("beep_on_arm_disarm", False),
                    "api_key": "beepOnArmDisarm",
                    "enabled_by_default": True,
                }
            )

        # Beep on delay
        if "beep_on_delay" in self.device.attributes:
            switches.append(
                {
                    "key": "beep_on_delay",
                    "translation_key": "beep_on_delay",
                    "value_fn": lambda: self.device.attributes.get("beep_on_delay", False),
                    "api_key": "beepOnDelay",
                    "enabled_by_default": True,
                }
            )

        # Blink while armed (LED) - only for sirens
        if "led_indication" in self.device.attributes or "blink_while_armed" in self.device.attributes:
            switches.append(
                {
                    "key": "blink_while_armed",
                    "translation_key": "blink_while_armed",
                    "value_fn": lambda: self._get_blink_state(),
                    "api_key": "v2sirenIndicatorLightMode",
                    "api_value_on": "BLINK_WHILE_ARMED",
                    "api_value_off": "DISABLED",
                    "api_extra": {"blinkWhileArmed": True},
                    "api_extra_off": {"blinkWhileArmed": False},
                    "enabled_by_default": True,
                }
            )

        # Chimes enabled - only for sirens
        if "chimes_enabled" in self.device.attributes:
            switches.append(
                {
                    "key": "chimes",
                    "translation_key": "chimes",
                    "value_fn": lambda: self.device.attributes.get("chimes_enabled", False),
                    "api_key": "chimesEnabled",
                    "enabled_by_default": True,
                }
            )

        # Alert if moved (StreetSiren only)
        if "alert_if_moved" in self.device.attributes:
            switches.append(
                {
                    "key": "alert_if_moved",
                    "translation_key": "alert_if_moved",
                    "value_fn": lambda: self.device.attributes.get("alert_if_moved", False),
                    "api_key": "alertIfMoved",
                    "enabled_by_default": True,
                }
            )

        return switches

    def _get_blink_state(self) -> bool:
        """Get the blink while armed state."""
        led = self.device.attributes.get("led_indication")
        if isinstance(led, bool):
            return led
        if isinstance(led, str):
            return led == "BLINK_WHILE_ARMED"
        return self.device.attributes.get("blink_while_armed", False)
