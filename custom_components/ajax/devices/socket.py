"""Smart socket/relay handler for Ajax Socket and Relay.

Handles:
- Socket (smart socket with power monitoring)
- Relay (relay for controlling lights/appliances)
- WallSwitch (smart wall switch)
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
)

from .base import AjaxDeviceHandler


class SocketHandler(AjaxDeviceHandler):
    """Handler for Ajax Socket/Relay smart devices."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for sockets/relays."""
        sensors = [
            {
                "key": "problem",
                "translation_key": "problem",
                "device_class": BinarySensorDeviceClass.PROBLEM,
                "value_fn": lambda: bool(self.device.malfunctions),
                "enabled_by_default": True,
            }
        ]

        # External power status (some models)
        if "external_power" in self.device.attributes:
            sensors.append(
                {
                    "key": "external_power",
                    "translation_key": "external_power",
                    "device_class": BinarySensorDeviceClass.POWER,
                    "value_fn": lambda: self.device.attributes.get(
                        "external_power", False
                    ),
                    "enabled_by_default": True,
                }
            )

        return sensors

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for sockets/relays."""
        sensors = []

        # Signal strength
        if "signal_strength" in self.device.attributes:
            sensors.append(
                {
                    "key": "signal_strength",
                    "translation_key": "signal_strength",
                    "device_class": SensorDeviceClass.SIGNAL_STRENGTH,
                    "native_unit_of_measurement": SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
                    "state_class": SensorStateClass.MEASUREMENT,
                    "value_fn": lambda: self.device.attributes.get("signal_strength"),
                    "enabled_by_default": True,
                }
            )

        # Power consumption (Socket with power monitoring)
        if "power" in self.device.attributes:
            sensors.append(
                {
                    "key": "power",
                    "translation_key": "power",
                    "device_class": SensorDeviceClass.POWER,
                    "native_unit_of_measurement": UnitOfPower.WATT,
                    "state_class": SensorStateClass.MEASUREMENT,
                    "value_fn": lambda: self.device.attributes.get("power"),
                    "enabled_by_default": True,
                }
            )

        # Energy consumption (Socket with power monitoring)
        if "energy" in self.device.attributes:
            sensors.append(
                {
                    "key": "energy",
                    "translation_key": "energy",
                    "device_class": SensorDeviceClass.ENERGY,
                    "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
                    "state_class": SensorStateClass.TOTAL_INCREASING,
                    "value_fn": lambda: self.device.attributes.get("energy"),
                    "enabled_by_default": True,
                }
            )

        # Voltage (Socket with power monitoring)
        if "voltage" in self.device.attributes:
            sensors.append(
                {
                    "key": "voltage",
                    "translation_key": "voltage",
                    "device_class": SensorDeviceClass.VOLTAGE,
                    "native_unit_of_measurement": UnitOfElectricPotential.VOLT,
                    "state_class": SensorStateClass.MEASUREMENT,
                    "value_fn": lambda: self.device.attributes.get("voltage"),
                    "enabled_by_default": False,
                }
            )

        # Current (Socket with power monitoring)
        if "current" in self.device.attributes:
            sensors.append(
                {
                    "key": "current",
                    "translation_key": "current",
                    "device_class": SensorDeviceClass.CURRENT,
                    "native_unit_of_measurement": UnitOfElectricCurrent.AMPERE,
                    "state_class": SensorStateClass.MEASUREMENT,
                    "value_fn": lambda: self.device.attributes.get("current"),
                    "enabled_by_default": False,
                }
            )

        # Malfunctions
        if self.device.malfunctions:
            sensors.append(
                {
                    "key": "malfunctions",
                    "translation_key": "malfunctions",
                    "value_fn": lambda: ", ".join(self.device.malfunctions)
                    if self.device.malfunctions
                    else "None",
                    "enabled_by_default": True,
                }
            )

        # Firmware version
        if "firmware_version" in self.device.attributes:
            sensors.append(
                {
                    "key": "firmware_version",
                    "translation_key": "firmware_version",
                    "value_fn": lambda: self.device.attributes.get("firmware_version"),
                    "enabled_by_default": False,
                }
            )

        return sensors

    def get_switches(self) -> list[dict]:
        """Return switch entities for sockets/relays."""
        # Check if this is a multi-gang LightSwitch device
        if self.device.attributes.get("is_multi_gang"):
            return self._get_multi_gang_switches()

        # Standard single switch (Socket, Relay, single-gang WallSwitch)
        return [
            {
                "key": "socket",
                "translation_key": "socket",
                "entity_category": None,
                "value_fn": lambda: self.device.attributes.get("is_on", False),
                "turn_on_fn": lambda: {"action": "turn_on"},
                "turn_off_fn": lambda: {"action": "turn_off"},
                "enabled_by_default": True,
            }
        ]

    def _get_multi_gang_switches(self) -> list[dict]:
        """Return switch entities for multi-gang LightSwitch devices."""
        switches = []

        # Channel 1 (API uses 0-indexed channels)
        channel_1_name = self.device.attributes.get("channel_1_name", "Channel 1")
        switches.append(
            {
                "key": "channel_1",
                "name": channel_1_name,
                "value_fn": lambda: self.device.attributes.get("channel_1_on", False),
                "icon": "mdi:light-switch",
                "enabled_by_default": True,
                "entity_category": None,
                "channel": 0,
            }
        )

        # Channel 2 (API uses 0-indexed channels)
        channel_2_name = self.device.attributes.get("channel_2_name", "Channel 2")
        switches.append(
            {
                "key": "channel_2",
                "name": channel_2_name,
                "value_fn": lambda: self.device.attributes.get("channel_2_on", False),
                "icon": "mdi:light-switch",
                "enabled_by_default": True,
                "entity_category": None,
                "channel": 1,
            }
        )

        return switches
