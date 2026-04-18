"""Base device handler for Ajax devices.

This module defines the base class that all device-specific handlers inherit from.
Each handler knows which Home Assistant entities (sensors, binary sensors, switches, etc.)
should be created for that specific device type.

It also exposes small helpers (`_battery_sensor`, `_tamper_binary_sensor`,
`_temperature_sensor`, `_signal_strength_percent_sensor`,
`_firmware_version_sensor`) so device handlers can declare common entities
consistently instead of copy-pasting dict literals in every subclass.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfTemperature

if TYPE_CHECKING:
    from ..models import AjaxDevice


class AjaxDeviceHandler(ABC):
    """Base class for Ajax device type handlers.

    Each device type (MotionProtect, DoorProtect, etc.) has its own handler
    that defines which entities should be created for that device.
    """

    def __init__(self, device: AjaxDevice) -> None:
        """Initialize the device handler.

        Args:
            device: The Ajax device data model
        """
        self.device = device

    def get_common_sensors(self) -> list[dict]:
        """Return common sensor entities for all devices.

        These sensors are available on most/all device types.
        """
        sensors = []

        # Room sensor - shows which room the device is in
        if self.device.room_name:
            sensors.append(
                {
                    "key": "room",
                    "translation_key": "room",
                    "value_fn": lambda: self.device.room_name,
                    "enabled_by_default": True,
                }
            )

        return sensors

    @abstractmethod
    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entity descriptions for this device.

        Returns:
            List of dicts with keys:
                - key: Unique key for the sensor
                - name: Display name
                - device_class: BinarySensorDeviceClass
                - value_fn: Function to get the value from device
                - enabled_by_default: Whether enabled by default
        """
        return []

    @abstractmethod
    def get_sensors(self) -> list[dict]:
        """Return sensor entity descriptions for this device.

        Returns:
            List of dicts with keys:
                - key: Unique key for the sensor
                - name: Display name
                - device_class: SensorDeviceClass
                - native_unit_of_measurement: Optional unit
                - state_class: Optional SensorStateClass
                - value_fn: Function to get the value from device
                - enabled_by_default: Whether enabled by default
        """
        return []

    def get_switches(self) -> list[dict]:
        """Return switch entity descriptions for this device.

        Returns:
            List of dicts with keys:
                - key: Unique key for the switch
                - name: Display name
                - value_fn: Function to get the state from device
                - turn_on_fn: Function to turn on
                - turn_off_fn: Function to turn off
                - icon: Optional icon
                - enabled_by_default: Whether enabled by default
        """
        return []

    def get_buttons(self) -> list[dict]:
        """Return button entity descriptions for this device.

        Returns:
            List of dicts with keys:
                - key: Unique key for the button
                - name: Display name
                - press_fn: Function to press the button
                - enabled_by_default: Whether enabled by default
        """
        return []

    def get_events(self) -> list[dict]:
        """Return event entity descriptions for this device.

        Returns:
            List of dicts with keys:
                - key: Unique key for the event entity
                - translation_key: Translation key
                - event_types: List of possible event type strings
                - enabled_by_default: Whether enabled by default
        """
        return []

    def get_alarm_control_panels(self) -> list[dict]:
        """Return alarm control panel descriptions for this device.

        Usually only the Hub device creates an alarm control panel.

        Returns:
            List of dicts with alarm panel configuration
        """
        return []

    # ---------------------------------------------------------------------
    # Helpers: common entity descriptors shared by most handlers.
    # Returning a dict (not appending) lets each handler control ordering
    # and optional customisation (enabled_by_default, extra flags).
    # ---------------------------------------------------------------------

    def _battery_sensor(self, enabled_by_default: bool = True) -> dict:
        """Build a common battery percentage sensor descriptor."""
        return {
            "key": "battery",
            "device_class": SensorDeviceClass.BATTERY,
            "native_unit_of_measurement": PERCENTAGE,
            "state_class": SensorStateClass.MEASUREMENT,
            "value_fn": lambda: self.device.battery_level,
            "enabled_by_default": enabled_by_default,
        }

    def _tamper_binary_sensor(self, enabled_by_default: bool = True) -> dict:
        """Build a common tamper binary-sensor descriptor."""
        return {
            "key": "tamper",
            "device_class": BinarySensorDeviceClass.TAMPER,
            "value_fn": lambda: self.device.attributes.get("tampered", False),
            "enabled_by_default": enabled_by_default,
        }

    def _temperature_sensor(
        self,
        attr: str = "temperature",
        enabled_by_default: bool = True,
    ) -> dict:
        """Build a common temperature sensor descriptor (in °C)."""
        return {
            "key": "temperature",
            "device_class": SensorDeviceClass.TEMPERATURE,
            "native_unit_of_measurement": UnitOfTemperature.CELSIUS,
            "state_class": SensorStateClass.MEASUREMENT,
            "value_fn": lambda: self.device.attributes.get(attr),
            "enabled_by_default": enabled_by_default,
        }

    def _signal_strength_percent_sensor(self, enabled_by_default: bool = True) -> dict:
        """Build a common Jeweller signal-strength sensor (percentage scale)."""
        return {
            "key": "signal_strength",
            "translation_key": "signal_strength",
            "native_unit_of_measurement": PERCENTAGE,
            "state_class": SensorStateClass.MEASUREMENT,
            "value_fn": lambda: self.device.signal_strength,
            "enabled_by_default": enabled_by_default,
        }

    def _firmware_version_sensor(self, enabled_by_default: bool = False) -> dict:
        """Build a firmware-version diagnostic sensor descriptor."""
        return {
            "key": "firmware_version",
            "translation_key": "firmware_version",
            "value_fn": lambda: self.device.firmware_version,
            "enabled_by_default": enabled_by_default,
            "entity_category": "diagnostic",
        }

    def _problem_binary_sensor(self, enabled_by_default: bool = True) -> dict:
        """Build a PROBLEM binary-sensor based on device.malfunctions."""
        return {
            "key": "problem",
            "translation_key": "problem",
            "device_class": BinarySensorDeviceClass.PROBLEM,
            "value_fn": lambda: bool(self.device.malfunctions),
            "enabled_by_default": enabled_by_default,
        }
