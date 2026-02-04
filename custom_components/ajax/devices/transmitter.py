"""Transmitter handler for Ajax universal transmitter modules.

Handles:
- Transmitter (universal module for wired sensors)
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


class TransmitterHandler(AjaxDeviceHandler):
    """Handler for Ajax Transmitter universal modules.

    The Transmitter is a universal module that connects external wired sensors
    (door contacts, motion detectors, etc.) to the Ajax wireless system.
    """

    # Map customAlarmType to binary sensor device class
    ALARM_TYPE_DEVICE_CLASS = {
        "INTRUSION": BinarySensorDeviceClass.MOTION,
        "OPENING": BinarySensorDeviceClass.OPENING,
        "FIRE": BinarySensorDeviceClass.SMOKE,
        "FLOOD": BinarySensorDeviceClass.MOISTURE,
        "GAS": BinarySensorDeviceClass.GAS,
        "CO": BinarySensorDeviceClass.CO,
        "PANIC": BinarySensorDeviceClass.SAFETY,
        "MEDICAL": BinarySensorDeviceClass.SAFETY,
    }

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for Transmitter."""
        sensors = []

        # Get device class based on customAlarmType
        alarm_type = self.device.attributes.get("customAlarmType", "OPENING")
        device_class = self.ALARM_TYPE_DEVICE_CLASS.get(alarm_type, BinarySensorDeviceClass.OPENING)

        # External contact state (main sensor)
        sensors.append(
            {
                "key": "external_contact",
                "device_class": device_class,
                "translation_key": "external_contact",
                "value_fn": lambda: self.device.attributes.get("externalContactTriggered", False),
                "enabled_by_default": True,
            }
        )

        # Tamper detection
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
        """Return sensor entities for Transmitter."""
        sensors = []

        # Battery level
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

        # External contact mode (NC/NO)
        sensors.append(
            {
                "key": "contact_mode",
                "translation_key": "contact_mode",
                "value_fn": lambda: self.device.attributes.get("externalContactStateMode", "").upper(),
                "enabled_by_default": False,
            }
        )

        # Alarm type
        if "customAlarmType" in self.device.attributes:
            sensors.append(
                {
                    "key": "alarm_type",
                    "translation_key": "alarm_type",
                    "value_fn": lambda: self.device.attributes.get("customAlarmType", "").lower().replace("_", " "),
                    "enabled_by_default": False,
                }
            )

        # External contact alarm mode (IMPULSE/CONTINUOUS)
        if "externalContactAlarmMode" in self.device.attributes:
            sensors.append(
                {
                    "key": "alarm_mode",
                    "translation_key": "alarm_mode",
                    "value_fn": lambda: self.device.attributes.get("externalContactAlarmMode", "").lower(),
                    "enabled_by_default": False,
                }
            )

        # External device power supply mode
        if "externalDevicePowerSupplyMode" in self.device.attributes:
            sensors.append(
                {
                    "key": "power_supply_mode",
                    "translation_key": "power_supply_mode",
                    "value_fn": lambda: (
                        self.device.attributes.get("externalDevicePowerSupplyMode", "").lower().replace("_", " ")
                    ),
                    "enabled_by_default": False,
                }
            )

        # Arm delay
        if "armDelaySeconds" in self.device.attributes:
            sensors.append(
                {
                    "key": "arm_delay",
                    "translation_key": "arm_delay",
                    "native_unit_of_measurement": "s",
                    "value_fn": lambda: self.device.attributes.get("armDelaySeconds", 0),
                    "enabled_by_default": False,
                }
            )

        # Alarm delay
        if "alarmDelaySeconds" in self.device.attributes:
            sensors.append(
                {
                    "key": "alarm_delay",
                    "translation_key": "alarm_delay",
                    "native_unit_of_measurement": "s",
                    "value_fn": lambda: self.device.attributes.get("alarmDelaySeconds", 0),
                    "enabled_by_default": False,
                }
            )

        return sensors

    def get_switches(self) -> list[dict]:
        """Return switch entities for Transmitter."""
        switches = []

        # Always Active switch
        switches.append(
            {
                "key": "always_active",
                "translation_key": "always_active",
                "value_fn": lambda: self.device.attributes.get(
                    "external_contact_always_active", self.device.attributes.get("externalContactAlwaysActive", False)
                ),
                "api_key": "externalContactAlwaysActive",
                "enabled_by_default": True,
            }
        )

        # Night Mode switch
        switches.append(
            {
                "key": "night_mode",
                "translation_key": "night_mode",
                "value_fn": lambda: self.device.attributes.get("nightModeArm", False),
                "api_key": "nightModeArm",
                "enabled_by_default": True,
            }
        )

        # Accelerometer switch
        if "accelerometerAware" in self.device.attributes:
            switches.append(
                {
                    "key": "accelerometer",
                    "translation_key": "accelerometer",
                    "value_fn": lambda: self.device.attributes.get("accelerometerAware", False),
                    "api_key": "accelerometerAware",
                    "enabled_by_default": True,
                }
            )

        # Siren trigger for external contact
        switches.append(
            {
                "key": "siren_trigger_contact",
                "translation_key": "siren_trigger_contact",
                "value_fn": lambda: "EXTRA_CONTACT" in self.device.attributes.get("siren_triggers", []),
                "api_key": "sirenTriggers",
                "trigger_key": "EXTRA_CONTACT",
                "enabled_by_default": True,
            }
        )

        # Siren trigger for acceleration
        if "accelerometerAware" in self.device.attributes:
            switches.append(
                {
                    "key": "siren_trigger_acceleration",
                    "translation_key": "siren_trigger_acceleration",
                    "value_fn": lambda: "ACCELERATION" in self.device.attributes.get("siren_triggers", []),
                    "api_key": "sirenTriggers",
                    "trigger_key": "ACCELERATION",
                    "enabled_by_default": True,
                }
            )

        return switches
