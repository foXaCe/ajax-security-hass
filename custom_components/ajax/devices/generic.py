"""Generic handler for recognised Ajax devices without a dedicated handler.

Covers device types that ``DEVICE_TYPE_MAP`` can parse but for which no
specialised module exists yet (Thermostat, standalone temperature sensors,
Fibra LineSplitter). Without this fallback those devices were silently
skipped by every entity platform — no entities, no log line.

The generic set is the standard Jeweller diagnostics every Ajax device
reports (battery, tamper, signal strength, firmware, malfunctions) plus a
temperature sensor when the device exposes one.
"""

from __future__ import annotations

from typing import Any

from .base import AjaxDeviceHandler


class GenericHandler(AjaxDeviceHandler):
    """Fallback handler exposing the standard Jeweller diagnostics."""

    def get_binary_sensors(self) -> list[dict[str, Any]]:
        """Return tamper + malfunction binary sensors."""
        return [
            self._tamper_binary_sensor(),
            self._problem_binary_sensor(),
        ]

    def get_sensors(self) -> list[dict[str, Any]]:
        """Return battery / signal / firmware (+ temperature when reported)."""
        sensors = [
            self._battery_sensor(),
            self._signal_strength_percent_sensor(),
            self._firmware_version_sensor(),
        ]
        if self.device.attributes.get("temperature") is not None:
            sensors.append(self._temperature_sensor())
        return sensors
