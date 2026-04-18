"""Doorbell device handler for Ajax Doorbell.

Handles:
- Ajax Doorbell (video doorbell with button)
"""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass
from homeassistant.components.sensor import SensorDeviceClass

from .base import AjaxDeviceHandler


class DoorbellHandler(AjaxDeviceHandler):
    """Handler for Ajax Doorbell devices."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for doorbell.

        The doorbell press itself is exposed via the event platform
        (see get_events); we only expose tamper here.
        """
        return [self._tamper_binary_sensor()]

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for doorbell."""
        return [
            self._battery_sensor(),
            self._signal_strength_percent_sensor(),
            {
                "key": "last_ring",
                "translation_key": "last_ring",
                "device_class": SensorDeviceClass.TIMESTAMP,
                "value_fn": lambda: self.device.attributes.get("last_ring"),
                "enabled_by_default": True,
            },
        ]

    def get_events(self) -> list[dict]:
        """Return event entities for doorbell."""
        return [
            {
                "key": "doorbell_press",
                "translation_key": "doorbell_press",
                "device_class": EventDeviceClass.DOORBELL,
                "event_types": ["ring"],
                "enabled_by_default": True,
            },
        ]
