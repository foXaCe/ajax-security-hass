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

        # Note: "armed_in_night_mode" is now a switch, not a binary sensor

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

        # Beep on arm/disarm - read-only (Ajax API doesn't allow modification)
        if "beep_on_arm_disarm" in self.device.attributes:
            sensors.append(
                {
                    "key": "beep_on_arm",
                    "translation_key": "beep_on_arm",
                    "value_fn": lambda: self.device.attributes.get(
                        "beep_on_arm_disarm", False
                    ),
                    "enabled_by_default": True,
                }
            )

        # Beep on delay - read-only (Ajax API doesn't allow modification)
        if "beep_on_delay" in self.device.attributes:
            sensors.append(
                {
                    "key": "beep_on_delay",
                    "translation_key": "beep_on_delay",
                    "value_fn": lambda: self.device.attributes.get(
                        "beep_on_delay", False
                    ),
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

        # Alarm volume level (sirenVolumeLevel from API)
        if "siren_volume_level" in self.device.attributes:
            sensors.append(
                {
                    "key": "alarm_volume_level",
                    "translation_key": "alarm_volume_level",
                    "value_fn": lambda: self._format_volume(
                        self.device.attributes.get("siren_volume_level")
                    ),
                    "enabled_by_default": True,
                }
            )

        # Beep volume level
        if "beep_volume_level" in self.device.attributes:
            sensors.append(
                {
                    "key": "beep_volume_level",
                    "translation_key": "beep_volume_level",
                    "value_fn": lambda: self._format_volume(
                        self.device.attributes.get("beep_volume_level")
                    ),
                    "enabled_by_default": True,
                }
            )

        # Alarm duration
        if "alarm_duration" in self.device.attributes:
            sensors.append(
                {
                    "key": "alarm_duration",
                    "translation_key": "alarm_duration",
                    "value_fn": lambda: self._format_duration(
                        self.device.attributes.get("alarm_duration")
                    ),
                    "enabled_by_default": True,
                }
            )

        # Note: LED indication is now a switch (switch.sirene_clignoter_quand_arme)

        return sensors

    def _format_volume(self, volume: str | None) -> str | None:
        """Format volume level to translation key."""
        if volume is None:
            return None
        # Return lowercase keys for translation
        return volume.lower() if volume else None

    def _format_duration(self, duration: int | str | None) -> str | None:
        """Format alarm duration to translation key or readable format."""
        if duration is None:
            return None
        # If it's a number (seconds), format as readable duration
        if isinstance(duration, (int, float)):
            seconds = int(duration)
            if seconds >= 60:
                minutes = seconds // 60
                return f"{minutes} min"
            return f"{seconds}s"
        # Return lowercase key for translation (e.g., "continuous")
        return str(duration).lower() if duration else None

    def get_switches(self) -> list[dict]:
        """Return switch entities for sirens and transmitters."""
        switches = []

        # Night Mode switch - only for devices that support it
        if "night_mode_arm" in self.device.attributes:
            switches.append(
                {
                    "key": "night_mode",
                    "translation_key": "night_mode",
                    "value_fn": lambda: self.device.attributes.get(
                        "night_mode_arm", False
                    ),
                    "api_key": "nightModeArm",
                    "enabled_by_default": True,
                }
            )

        # Note: beep_on_arm and beep_on_delay are now binary_sensors (read-only via Ajax API)

        # Blink while armed (LED) - only for sirens
        if (
            "led_indication" in self.device.attributes
            or "blink_while_armed" in self.device.attributes
        ):
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
                    "value_fn": lambda: self.device.attributes.get(
                        "chimes_enabled", False
                    ),
                    "api_key": "chimesEnabled",
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
