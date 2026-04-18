"""Repeater device handler for Ajax Rex series.

Handles:
- Rex (range extender)
- Rex2 (range extender v2)
"""

from __future__ import annotations

from .base import AjaxDeviceHandler


class RepeaterHandler(AjaxDeviceHandler):
    """Handler for Ajax Rex/Repeater devices."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for repeaters."""
        return [self._tamper_binary_sensor()]

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for repeaters."""
        return [
            self._battery_sensor(),
            self._signal_strength_percent_sensor(),
        ]
