"""Pin the Energy Dashboard contract for Ajax Socket sensors.

HA's Energy panel only picks up a sensor when all three of
``device_class=ENERGY``, ``state_class=TOTAL_INCREASING`` and a
``native_unit_of_measurement`` from ``UnitOfEnergy`` are set together.
If any one is removed (e.g. someone switches the state class to
``MEASUREMENT``), the sensor silently disappears from the panel.
Pinning the trio here turns that regression into a failing test
instead of a feature that quietly breaks in production.
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import UnitOfElectricCurrent, UnitOfEnergy, UnitOfPower

from custom_components.ajax.devices.socket import SocketHandler
from custom_components.ajax.models import AjaxDevice, DeviceType


def _socket(attributes: dict) -> SocketHandler:
    device = AjaxDevice(
        id="dev1",
        name="Socket Test",
        type=DeviceType.SOCKET,
        space_id="space1",
        hub_id="hub1",
        attributes=attributes,
    )
    return SocketHandler(device)


def test_socket_energy_sensor_is_long_term_stats_compatible() -> None:
    handler = _socket({"energy": 1234})
    energy = next(s for s in handler.get_sensors() if s["key"] == "energy")

    assert energy["device_class"] == SensorDeviceClass.ENERGY
    assert energy["state_class"] == SensorStateClass.TOTAL_INCREASING
    # The coordinator normalises the raw Wh reading into kWh before storing it.
    assert energy["native_unit_of_measurement"] == UnitOfEnergy.KILO_WATT_HOUR


def test_socket_energy_sensor_accepts_raw_api_key() -> None:
    """The Ajax API may send ``powerConsumedWattsPerHour`` instead of ``energy``."""
    handler = _socket({"powerConsumedWattsPerHour": 5678})
    energy = next(s for s in handler.get_sensors() if s["key"] == "energy")
    assert energy["value_fn"]() == 5678


def test_socket_power_sensor_is_measurement() -> None:
    """Instant power is a measurement, not a cumulative total."""
    handler = _socket({"power": 42})
    power = next(s for s in handler.get_sensors() if s["key"] == "power")

    assert power["device_class"] == SensorDeviceClass.POWER
    assert power["state_class"] == SensorStateClass.MEASUREMENT
    assert power["native_unit_of_measurement"] == UnitOfPower.WATT


def test_socket_current_sensor_unit_matches_coordinator_scaling() -> None:
    """The coordinator stores ``current`` in Amperes (mA / 1000), so the
    declared unit must be AMPERE — declaring MILLIAMPERE was a 1000x error."""
    handler = _socket({"current": 0.5})
    current = next(s for s in handler.get_sensors() if s["key"] == "current")

    assert current["device_class"] == SensorDeviceClass.CURRENT
    assert current["native_unit_of_measurement"] == UnitOfElectricCurrent.AMPERE
