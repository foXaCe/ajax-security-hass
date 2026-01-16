"""Door/Window contact sensor handler for Ajax DoorProtect series.

Handles:
- DoorProtect
- DoorProtect Plus (with tilt sensor and temperature)
- Wired input modules with door contacts
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


class DoorContactHandler(AjaxDeviceHandler):
    """Handler for Ajax DoorProtect door/window contact sensors."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for door contacts."""
        sensors = []

        # Main opening sensor - always create it even if attribute doesn't exist yet
        # The attribute will be populated by SQS notifications
        # Note: No translation_key needed - HA provides automatic translation for OPENING device_class
        sensors.append(
            {
                "key": "door",
                "device_class": BinarySensorDeviceClass.OPENING,
                "value_fn": lambda: self.device.attributes.get("door_opened", False),
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
                    "device_class": BinarySensorDeviceClass.OPENING,
                    "value_fn": lambda: self.device.attributes.get(
                        "external_contact_opened", False
                    ),
                    "enabled_by_default": True,
                }
            )

        # Note: "armed_in_night_mode" is now a switch, not a binary sensor

        # Tamper / Couvercle - inverted: False = closed (OK), True = open (problem)
        # Note: No translation_key needed - HA provides automatic translation for TAMPER device_class
        sensors.append(
            {
                "key": "tamper",
                "device_class": BinarySensorDeviceClass.TAMPER,
                "value_fn": lambda: self.device.attributes.get("tampered", False),
                "enabled_by_default": True,
            }
        )

        # Tilt sensor / Capteur d'inclinaison (DoorProtect Plus)
        # Only create if accelerometerAware is True (feature enabled on device)
        if self.device.attributes.get("accelerometer_aware", False):
            sensors.append(
                {
                    "key": "tilt",
                    "translation_key": "tilt",
                    "device_class": BinarySensorDeviceClass.MOVING,
                    "value_fn": lambda: self.device.attributes.get(
                        "tilt_detected", self.device.attributes.get("tilt", False)
                    ),
                    "enabled_by_default": True,
                }
            )

        # Shock sensor / Capteur de choc (DoorProtect Plus)
        # Only create if shockSensorAware is True (feature enabled on device)
        if self.device.attributes.get("shock_sensor_aware", False):
            sensors.append(
                {
                    "key": "shock",
                    "translation_key": "shock",
                    "device_class": BinarySensorDeviceClass.VIBRATION,
                    "value_fn": lambda: self.device.attributes.get(
                        "shock_detected", self.device.attributes.get("shock", False)
                    ),
                    "enabled_by_default": True,
                }
            )

        return sensors

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for door contacts."""
        sensors = []

        # Battery level - always create even if None, will be updated by notifications
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

        # Signal strength - always create even if None, will be updated by notifications
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

        # Temperature (DoorProtect Plus)
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

        # Note: firmware_version and hardware_version are available on device_info
        # so we don't need separate sensors for them

        # Connection type / Connexion via Jeweller
        if "connection_type" in self.device.attributes:
            sensors.append(
                {
                    "key": "connection_type",
                    "translation_key": "connection_type",
                    "value_fn": lambda: self.device.attributes.get("connection_type"),
                    "enabled_by_default": True,
                }
            )

        # Operating mode / Mode de fonctionnement
        if "operating_mode" in self.device.attributes:
            sensors.append(
                {
                    "key": "operating_mode",
                    "translation_key": "operating_mode",
                    "value_fn": lambda: self.device.attributes.get("operating_mode"),
                    "enabled_by_default": True,
                }
            )

        # Battery state / État de la batterie (normal/faible/critique)
        if self.device.battery_state is not None:
            sensors.append(
                {
                    "key": "battery_state",
                    "translation_key": "battery_state",
                    "value_fn": lambda: self.device.battery_state,
                    "enabled_by_default": True,
                }
            )

        return sensors

    def get_switches(self) -> list[dict]:
        """Return switch entities for door contacts."""
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

        # DoorProtect Plus specific switches
        raw_type = self.device.raw_type or ""
        if "Plus" in raw_type:
            # External contact switch
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

            # Shock sensor switch
            switches.append(
                {
                    "key": "shock_sensor",
                    "translation_key": "shock_sensor",
                    "value_fn": lambda: self.device.attributes.get(
                        "shock_sensor_aware", False
                    ),
                    "api_key": "shockSensorAware",
                    "enabled_by_default": True,
                }
            )

            # Ignore simple impact switch
            switches.append(
                {
                    "key": "ignore_impact",
                    "translation_key": "ignore_impact",
                    "value_fn": lambda: self.device.attributes.get(
                        "ignore_simple_impact", False
                    ),
                    "api_key": "ignoreSimpleImpact",
                    "enabled_by_default": True,
                }
            )

            # Tilt sensor switch
            switches.append(
                {
                    "key": "tilt_sensor",
                    "translation_key": "tilt_sensor",
                    "value_fn": lambda: self.device.attributes.get(
                        "accelerometer_aware", False
                    ),
                    "api_key": "accelerometerAware",
                    "enabled_by_default": True,
                }
            )

            # Siren trigger switches
            switches.append(
                {
                    "key": "siren_trigger_reed",
                    "translation_key": "siren_trigger_reed",
                    "value_fn": lambda: "REED"
                    in self.device.attributes.get("siren_triggers", []),
                    "api_key": "sirenTriggers",
                    "trigger_key": "REED",
                    "enabled_by_default": True,
                }
            )

            switches.append(
                {
                    "key": "siren_trigger_shock",
                    "translation_key": "siren_trigger_shock",
                    "value_fn": lambda: "SHOCK"
                    in self.device.attributes.get("siren_triggers", []),
                    "api_key": "sirenTriggers",
                    "trigger_key": "SHOCK",
                    "enabled_by_default": True,
                }
            )

            switches.append(
                {
                    "key": "siren_trigger_tilt",
                    "translation_key": "siren_trigger_tilt",
                    "value_fn": lambda: "TILT"
                    in self.device.attributes.get("siren_triggers", []),
                    "api_key": "sirenTriggers",
                    "trigger_key": "TILT",
                    "enabled_by_default": True,
                }
            )

        return switches


class WireInputHandler(DoorContactHandler):
    """Handler for MultiTransmitter wired input devices.

    These are wired devices connected to a MultiTransmitter, so they don't have:
    - Battery (powered by wire)
    - Signal strength (wired connection)

    TWO_EOL wiring scheme supports tamper detection via contactOneDetails.
    """

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for wired inputs.

        TWO_EOL wiring scheme provides tamper detection via contactOneDetails.
        """
        # Note: No translation_key needed - HA provides automatic translation for device_class
        sensors = [
            {
                "key": "door",
                "device_class": BinarySensorDeviceClass.OPENING,
                "value_fn": lambda: self.device.attributes.get("door_opened", False),
                "enabled_by_default": True,
            }
        ]

        # TWO_EOL wiring scheme has tamper detection (contactOneDetails)
        if self.device.attributes.get("wiring_type") == "TWO_EOL":
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
        """Return sensor entities for wired inputs (no battery/signal)."""
        # Wired devices don't have battery or signal - skip those sensors
        # Only return temperature if available
        sensors = []

        # Note: No translation_key needed - HA provides automatic translation for TEMPERATURE device_class
        if "temperature" in self.device.attributes:
            from homeassistant.components.sensor import (
                SensorDeviceClass,
                SensorStateClass,
            )
            from homeassistant.const import UnitOfTemperature

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

        return sensors

    def get_switches(self) -> list[dict]:
        """Return switch entities for wired inputs."""
        switches = []

        # Always Active switch - nested in wiredDeviceSettings for WireInput
        switches.append(
            {
                "key": "always_active",
                "translation_key": "always_active",
                "value_fn": lambda: self.device.attributes.get("always_active", False),
                "api_key": "alwaysActive",
                "api_nested_key": "wiredDeviceSettings",
                "enabled_by_default": True,
            }
        )

        # Night Mode switch - nested in wiredDeviceSettings for WireInput
        switches.append(
            {
                "key": "night_mode",
                "translation_key": "night_mode",
                "value_fn": lambda: self.device.attributes.get("night_mode_arm", False),
                "api_key": "nightModeArm",
                "api_nested_key": "wiredDeviceSettings",
                "enabled_by_default": True,
            }
        )

        return switches
