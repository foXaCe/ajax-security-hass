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
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
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
                    "value_fn": lambda: self.device.attributes.get("external_power", False),
                    "enabled_by_default": True,
                }
            )

        return sensors

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for sockets/relays."""
        sensors = []

        # Signal strength (percentage based on signalLevel)
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

        # Temperature (Socket has internal temperature sensor)
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

        # Power consumption (Socket with power monitoring)
        # Check both normalized and raw attribute names
        if "power" in self.device.attributes or "powerConsumptionWatts" in self.device.attributes:
            sensors.append(
                {
                    "key": "power",
                    "translation_key": "power",
                    "device_class": SensorDeviceClass.POWER,
                    "native_unit_of_measurement": UnitOfPower.WATT,
                    "state_class": SensorStateClass.MEASUREMENT,
                    "value_fn": lambda: self.device.attributes.get(
                        "power", self.device.attributes.get("powerConsumptionWatts")
                    ),
                    "enabled_by_default": True,
                }
            )

        # Energy consumption (Socket with power monitoring)
        # Check both normalized and raw attribute names
        if "energy" in self.device.attributes or "powerConsumedWattsPerHour" in self.device.attributes:
            sensors.append(
                {
                    "key": "energy",
                    "translation_key": "energy",
                    "device_class": SensorDeviceClass.ENERGY,
                    "native_unit_of_measurement": UnitOfEnergy.WATT_HOUR,
                    "state_class": SensorStateClass.TOTAL_INCREASING,
                    "value_fn": lambda: self.device.attributes.get(
                        "energy", self.device.attributes.get("powerConsumedWattsPerHour")
                    ),
                    "enabled_by_default": True,
                }
            )

        # Voltage (Socket with power monitoring)
        # Check both normalized and raw attribute names
        if "voltage" in self.device.attributes or "voltageVolts" in self.device.attributes:
            sensors.append(
                {
                    "key": "voltage",
                    "translation_key": "voltage",
                    "device_class": SensorDeviceClass.VOLTAGE,
                    "native_unit_of_measurement": UnitOfElectricPotential.VOLT,
                    "state_class": SensorStateClass.MEASUREMENT,
                    "value_fn": lambda: self.device.attributes.get(
                        "voltage", self.device.attributes.get("voltageVolts")
                    ),
                    "enabled_by_default": True,
                }
            )

        # Current (Socket with power monitoring)
        # Check both normalized and raw attribute names (convert mA to A)
        if (
            "current" in self.device.attributes
            or "currentMilliAmpere" in self.device.attributes
            or "currentMilliAmpers" in self.device.attributes
        ):
            sensors.append(
                {
                    "key": "current",
                    "translation_key": "current",
                    "device_class": SensorDeviceClass.CURRENT,
                    "native_unit_of_measurement": UnitOfElectricCurrent.MILLIAMPERE,
                    "state_class": SensorStateClass.MEASUREMENT,
                    "value_fn": lambda: self.device.attributes.get(
                        "current",
                        self.device.attributes.get(
                            "currentMilliAmpere", self.device.attributes.get("currentMilliAmpers")
                        ),
                    ),
                    "enabled_by_default": True,
                }
            )

        # Malfunctions
        if self.device.malfunctions:
            sensors.append(
                {
                    "key": "malfunctions",
                    "translation_key": "malfunctions",
                    "value_fn": lambda: (
                        ", ".join(str(m) for m in self.device.malfunctions)
                        if isinstance(self.device.malfunctions, list)
                        else str(self.device.malfunctions)
                        if self.device.malfunctions
                        else "None"
                    ),
                    "enabled_by_default": True,
                }
            )

        # Firmware version (uses device.firmware_version, populated by coordinator)
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
        """Return switch entities for sockets/relays."""
        # Check if this is a LightSwitch device with channel(s)
        # LightSwitchTwoWay has only channel 1, LightSwitchTwoGang/TwoChannelTwoWay have both
        if self.device.attributes.get("has_channel_1") or self.device.attributes.get("has_channel_2"):
            return self._get_multi_gang_switches()

        # Standard single switch (Socket, Relay, single-gang WallSwitch)
        switches = [
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

        # LED indication (Socket)
        if "indicationEnabled" in self.device.attributes:
            switches.append(
                {
                    "key": "indication_enabled",
                    "translation_key": "indication_enabled",
                    "value_fn": lambda: self.device.attributes.get("indicationEnabled", False),
                    "api_key": "indicationEnabled",
                    "api_value_on": True,
                    "api_value_off": False,
                    "enabled_by_default": True,
                    "bypass_security_check": True,
                }
            )

        # Current protection (Socket)
        if "currentProtectionEnabled" in self.device.attributes:
            switches.append(
                {
                    "key": "current_protection",
                    "translation_key": "current_protection",
                    "value_fn": lambda: self.device.attributes.get("currentProtectionEnabled", False),
                    "api_key": "currentProtectionEnabled",
                    "api_value_on": True,
                    "api_value_off": False,
                    "enabled_by_default": True,
                    "bypass_security_check": True,
                }
            )

        # Voltage protection (Socket)
        if "voltageProtectionEnabled" in self.device.attributes:
            switches.append(
                {
                    "key": "voltage_protection",
                    "translation_key": "voltage_protection",
                    "value_fn": lambda: self.device.attributes.get("voltageProtectionEnabled", False),
                    "api_key": "voltageProtectionEnabled",
                    "api_value_on": True,
                    "api_value_off": False,
                    "enabled_by_default": True,
                    "bypass_security_check": True,
                }
            )

        return switches

    def _get_multi_gang_switches(self) -> list[dict]:
        """Return switch entities for multi-gang LightSwitch devices."""
        switches = []

        # Channel 1 (API uses 0-indexed channels)
        if self.device.attributes.get("has_channel_1", True):
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

        # Channel 2 (API uses 0-indexed channels) - only if device has second channel
        if self.device.attributes.get("has_channel_2", False):
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
