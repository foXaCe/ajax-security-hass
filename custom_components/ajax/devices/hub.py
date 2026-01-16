"""Hub device handler for Ajax Hub series.

Handles:
- Hub (basic hub)
- Hub 2 (2G/4G)
- Hub Plus (ethernet + 2G/3G/4G)
- Hub 2 Plus (ethernet + 2G/3G/4G/LTE)

The Hub creates an alarm control panel entity and various system status sensors.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
)

from .base import AjaxDeviceHandler


class HubHandler(AjaxDeviceHandler):
    """Handler for Ajax Hub devices."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for hub."""
        sensors = [
            {
                "key": "connection",
                "translation_key": "connection",
                "device_class": BinarySensorDeviceClass.CONNECTIVITY,
                "value_fn": lambda: self.device.attributes.get("online", False),
                "enabled_by_default": True,
            },
            {
                "key": "problem",
                "translation_key": "problem",
                "device_class": BinarySensorDeviceClass.PROBLEM,
                "value_fn": lambda: bool(self.device.malfunctions),
                "enabled_by_default": True,
            },
            # Note: No translation_key needed - HA provides automatic translation for TAMPER device_class
            {
                "key": "tamper",
                "device_class": BinarySensorDeviceClass.TAMPER,
                "value_fn": lambda: self.device.attributes.get("tampered", False),
                "enabled_by_default": True,
            },
        ]

        # External power status
        if "externally_powered" in self.device.attributes:
            sensors.append(
                {
                    "key": "external_power",
                    "translation_key": "external_power",
                    "device_class": BinarySensorDeviceClass.POWER,
                    "value_fn": lambda: self.device.attributes.get(
                        "externally_powered", False
                    ),
                    "enabled_by_default": True,
                }
            )

        # Battery connected
        if "battery_connected" in self.device.attributes:
            sensors.append(
                {
                    "key": "battery_connected",
                    "translation_key": "battery_connected",
                    "device_class": BinarySensorDeviceClass.BATTERY,
                    "value_fn": lambda: self.device.attributes.get(
                        "battery_connected", False
                    ),
                    "enabled_by_default": True,
                }
            )

        # GSM antenna
        if "gsm_antenna" in self.device.attributes:
            sensors.append(
                {
                    "key": "gsm_antenna",
                    "translation_key": "gsm_antenna",
                    "device_class": BinarySensorDeviceClass.CONNECTIVITY,
                    "value_fn": lambda: self.device.attributes.get(
                        "gsm_antenna", False
                    ),
                    "enabled_by_default": True,
                }
            )

        # Jeweller radio
        if "jeweller_radio" in self.device.attributes:
            sensors.append(
                {
                    "key": "jeweller_radio",
                    "translation_key": "jeweller_radio",
                    "device_class": BinarySensorDeviceClass.CONNECTIVITY,
                    "value_fn": lambda: self.device.attributes.get(
                        "jeweller_radio", False
                    ),
                    "enabled_by_default": True,
                }
            )

        # Wings radio (for curtain detectors)
        if "wings_radio" in self.device.attributes:
            sensors.append(
                {
                    "key": "wings_radio",
                    "translation_key": "wings_radio",
                    "device_class": BinarySensorDeviceClass.CONNECTIVITY,
                    "value_fn": lambda: self.device.attributes.get(
                        "wings_radio", False
                    ),
                    "enabled_by_default": True,
                }
            )

        return sensors

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for hub."""
        sensors = []

        # Hub battery level
        # Note: No translation_key needed - HA provides automatic translation for BATTERY device_class
        if "battery_level" in self.device.attributes:
            sensors.append(
                {
                    "key": "battery",
                    "device_class": SensorDeviceClass.BATTERY,
                    "native_unit_of_measurement": PERCENTAGE,
                    "state_class": SensorStateClass.MEASUREMENT,
                    "value_fn": lambda: self.device.attributes.get("battery_level"),
                    "enabled_by_default": True,
                }
            )

        # GSM signal level
        if "gsm_signal_level" in self.device.attributes:
            sensors.append(
                {
                    "key": "gsm_signal_level",
                    "translation_key": "gsm_signal_level",
                    "device_class": SensorDeviceClass.SIGNAL_STRENGTH,
                    "native_unit_of_measurement": SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
                    "state_class": SensorStateClass.MEASUREMENT,
                    "value_fn": lambda: self.device.attributes.get("gsm_signal_level"),
                    "enabled_by_default": True,
                }
            )

        # WiFi signal level
        if "wifi_signal_level" in self.device.attributes:
            sensors.append(
                {
                    "key": "wifi_signal_level",
                    "translation_key": "wifi_signal_level",
                    "device_class": SensorDeviceClass.SIGNAL_STRENGTH,
                    "native_unit_of_measurement": SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
                    "state_class": SensorStateClass.MEASUREMENT,
                    "value_fn": lambda: self.device.attributes.get("wifi_signal_level"),
                    "enabled_by_default": True,
                }
            )

        # Active connection type (Ethernet, WiFi, GSM, etc.)
        if "active_connection" in self.device.attributes:
            sensors.append(
                {
                    "key": "active_connection",
                    "translation_key": "active_connection",
                    "value_fn": lambda: self.device.attributes.get("active_connection"),
                    "enabled_by_default": True,
                }
            )

        # Network status
        if "network_status" in self.device.attributes:
            sensors.append(
                {
                    "key": "network_status",
                    "translation_key": "network_status",
                    "value_fn": lambda: self.device.attributes.get("network_status"),
                    "enabled_by_default": True,
                }
            )

        # GSM type (2G, 3G, 4G, 5G)
        if "gsm_type" in self.device.attributes:
            sensors.append(
                {
                    "key": "gsm_type",
                    "translation_key": "gsm_type",
                    "value_fn": lambda: (
                        self.device.attributes.get("gsm_type", "").lower()
                        if self.device.attributes.get("gsm_type")
                        else None
                    ),
                    "enabled_by_default": True,
                }
            )

        # Total devices count
        if "total_devices" in self.device.attributes:
            sensors.append(
                {
                    "key": "total_devices",
                    "translation_key": "total_devices",
                    "value_fn": lambda: self.device.attributes.get("total_devices"),
                    "state_class": SensorStateClass.MEASUREMENT,
                    "enabled_by_default": True,
                }
            )

        # Online devices count
        if "online_devices" in self.device.attributes:
            sensors.append(
                {
                    "key": "online_devices",
                    "translation_key": "online_devices",
                    "value_fn": lambda: self.device.attributes.get("online_devices"),
                    "state_class": SensorStateClass.MEASUREMENT,
                    "enabled_by_default": True,
                }
            )

        # Devices with malfunctions
        if "devices_with_malfunctions" in self.device.attributes:
            sensors.append(
                {
                    "key": "devices_with_malfunctions",
                    "translation_key": "devices_with_malfunctions",
                    "value_fn": lambda: self.device.attributes.get(
                        "devices_with_malfunctions"
                    ),
                    "state_class": SensorStateClass.MEASUREMENT,
                    "enabled_by_default": True,
                }
            )

        # Unread notifications
        if "unread_notifications" in self.device.attributes:
            sensors.append(
                {
                    "key": "unread_notifications",
                    "translation_key": "unread_notifications",
                    "value_fn": lambda: self.device.attributes.get(
                        "unread_notifications"
                    ),
                    "state_class": SensorStateClass.MEASUREMENT,
                    "enabled_by_default": True,
                }
            )

        # SIM status
        if "sim_status" in self.device.attributes:
            sensors.append(
                {
                    "key": "sim_status",
                    "translation_key": "sim_status",
                    "value_fn": lambda: str(self.device.attributes.get("sim_status")),
                    "enabled_by_default": True,
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

    def get_alarm_control_panels(self) -> list[dict]:
        """Return alarm control panel for hub.

        The hub is the only device that creates an alarm control panel.
        """
        return [
            {
                "key": "alarm",
                "name": f"{self.device.name} Alarm",
                "space_id": self.device.space_id,
            }
        ]
