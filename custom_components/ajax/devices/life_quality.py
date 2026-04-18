"""LifeQuality air quality sensor handler.

Handles:
- LifeQuality (CO2, temperature, humidity sensor)
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    CONCENTRATION_PARTS_PER_MILLION,
    PERCENTAGE,
    UnitOfTemperature,
)

from .base import AjaxDeviceHandler


class LifeQualityHandler(AjaxDeviceHandler):
    """Handler for Ajax LifeQuality air quality sensors."""

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for LifeQuality."""
        sensors = []

        # Tamper detection
        sensors.append(
            {
                "key": "tamper",
                "device_class": BinarySensorDeviceClass.TAMPER,
                "value_fn": lambda: self.device.attributes.get("tampered", False),
                "enabled_by_default": True,
            }
        )

        # CO2 problem (high CO2 level)
        sensors.append(
            {
                "key": "co2_problem",
                "device_class": BinarySensorDeviceClass.PROBLEM,
                "translation_key": "co2_problem",
                "value_fn": lambda: self._is_co2_problem(),
                "enabled_by_default": True,
            }
        )

        # Temperature problem (out of comfort range)
        sensors.append(
            {
                "key": "temperature_problem",
                "device_class": BinarySensorDeviceClass.PROBLEM,
                "translation_key": "temperature_problem",
                "value_fn": lambda: self._is_temperature_problem(),
                "enabled_by_default": True,
            }
        )

        # Humidity problem (out of comfort range)
        sensors.append(
            {
                "key": "humidity_problem",
                "device_class": BinarySensorDeviceClass.PROBLEM,
                "translation_key": "humidity_problem",
                "value_fn": lambda: self._is_humidity_problem(),
                "enabled_by_default": True,
            }
        )

        return sensors

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for LifeQuality."""
        sensors = []

        # CO2 level (actualCO2 is in ppm)
        sensors.append(
            {
                "key": "co2",
                "device_class": SensorDeviceClass.CO2,
                "native_unit_of_measurement": CONCENTRATION_PARTS_PER_MILLION,
                "state_class": SensorStateClass.MEASUREMENT,
                "value_fn": lambda: self.device.attributes.get("actualCO2"),
                "enabled_by_default": True,
            }
        )

        # Temperature (actualTemperature is in 0.1°C, divide by 10)
        sensors.append(
            {
                "key": "temperature",
                "device_class": SensorDeviceClass.TEMPERATURE,
                "native_unit_of_measurement": UnitOfTemperature.CELSIUS,
                "state_class": SensorStateClass.MEASUREMENT,
                "value_fn": lambda: self._get_temperature(),
                "enabled_by_default": True,
            }
        )

        # Humidity (actualHumidity is in 0.1%, divide by 10)
        sensors.append(
            {
                "key": "humidity",
                "device_class": SensorDeviceClass.HUMIDITY,
                "native_unit_of_measurement": PERCENTAGE,
                "state_class": SensorStateClass.MEASUREMENT,
                "value_fn": lambda: self._get_humidity(),
                "enabled_by_default": True,
            }
        )

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

        # Calibration state
        if "calibrationState" in self.device.attributes:
            sensors.append(
                {
                    "key": "calibration_state",
                    "translation_key": "calibration_state",
                    "value_fn": lambda: (
                        (self.device.attributes.get("calibrationState") or "").lower().replace("_", " ")
                    ),
                    "enabled_by_default": False,
                }
            )

        return sensors

    def get_switches(self) -> list[dict]:
        """Return switch entities for LifeQuality."""
        switches = []

        # LED Indicator switch
        if "indication" in self.device.attributes:
            switches.append(
                {
                    "key": "indicator_light",
                    "translation_key": "indicator_light",
                    "value_fn": lambda: self.device.attributes.get("indication") != "CO2_OFF",
                    "api_key": "indication",
                    "api_value_on": "CO2_ON",
                    "api_value_off": "CO2_OFF",
                    "enabled_by_default": True,
                }
            )

        return switches

    def _get_temperature(self) -> float | None:
        """Get temperature in Celsius (API returns 0.1°C units)."""
        actual_temp = self.device.attributes.get("actualTemperature")
        if isinstance(actual_temp, (int, float)):
            return round(actual_temp / 10.0, 1)
        # Fallback to integer temperature if available
        fallback = self.device.attributes.get("temperature")
        return fallback if isinstance(fallback, (int, float)) else None

    def _get_humidity(self) -> float | None:
        """Get humidity in % (API returns 0.1% units)."""
        actual_humidity = self.device.attributes.get("actualHumidity")
        if isinstance(actual_humidity, (int, float)):
            return round(actual_humidity / 10.0, 1)
        return None

    def _is_co2_problem(self) -> bool:
        """Check if CO2 level is above comfort threshold."""
        co2 = self.device.attributes.get("actualCO2")
        max_comfort = self.device.attributes.get("maxComfortCO2", 1000)
        if co2 is not None and max_comfort is not None:
            return co2 > max_comfort
        return False

    def _is_temperature_problem(self) -> bool:
        """Check if temperature is outside comfort range.

        actualTemperature is in 0.1°C (e.g. 215 = 21.5°C) while
        min/maxComfortTemperature are in whole °C — convert before compare.
        """
        temp_c = self._get_temperature()
        min_comfort = self.device.attributes.get("minComfortTemperature")
        max_comfort = self.device.attributes.get("maxComfortTemperature")
        if temp_c is not None and isinstance(min_comfort, (int, float)) and isinstance(max_comfort, (int, float)):
            return temp_c < min_comfort or temp_c > max_comfort
        return False

    def _is_humidity_problem(self) -> bool:
        """Check if humidity is outside comfort range."""
        humidity = self.device.attributes.get("actualHumidity")
        # Humidity thresholds are in %, actualHumidity is in 0.1% (e.g., 470 = 47.0%)
        min_comfort = self.device.attributes.get("minComfortHumidity")
        max_comfort = self.device.attributes.get("maxComfortHumidity")
        if humidity is not None and min_comfort is not None and max_comfort is not None:
            # Convert thresholds to same unit as actualHumidity (0.1%)
            return humidity < (min_comfort * 10) or humidity > (max_comfort * 10)
        return False
