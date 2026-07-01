"""Hub-level Ajax sensors.

``_get_hub_sensors`` (hub_details extraction table) and ``AjaxHubSensor``.
Split out of ``sensor.py`` (platform module keeps only ``async_setup_entry``
+ re-exports).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._ids import device_identifier
from .coordinator import AjaxDataCoordinator
from .models import (
    AjaxSpace,
)

_LOGGER = logging.getLogger(__name__)


def _get_hub_sensors(space: AjaxSpace) -> list[dict[str, Any]]:
    """Get hub sensor definitions based on available hub_details fields."""
    sensors = []
    hub_details = space.hub_details or {}

    # Hub battery is provided by the SPACE_SENSORS list (AjaxSpaceSensor,
    # translation_key "hub_battery"). Defining it here too produced a duplicate
    # unique_id ({entry}_{hub_id}_hub_battery, since space_id == hub_id) and HA
    # silently dropped one of the two entities — keep it solely in SPACE_SENSORS.

    # GSM signal level
    gsm = hub_details.get("gsm", {})
    if gsm and "signalLevel" in gsm:
        sensors.append(
            {
                "key": "gsm_signal",
                "translation_key": "gsm_signal_level",
                "value_fn": lambda hd=hub_details: (
                    hd.get("gsm", {}).get("signalLevel", "").lower() if hd.get("gsm", {}).get("signalLevel") else None
                ),
                "enabled_by_default": True,
            }
        )

    # GSM network status (2G, 3G, 4G)
    if gsm and "networkStatus" in gsm:
        sensors.append(
            {
                "key": "gsm_network",
                "translation_key": "gsm_type",
                "value_fn": lambda hd=hub_details: (
                    hd.get("gsm", {}).get("networkStatus", "").lower()
                    if hd.get("gsm", {}).get("networkStatus")
                    else None
                ),
                "enabled_by_default": True,
            }
        )

    # SIM card state
    if gsm and "simCardState" in gsm:
        sensors.append(
            {
                "key": "sim_status",
                "translation_key": "sim_status",
                "value_fn": lambda hd=hub_details: (
                    hd.get("gsm", {}).get("simCardState", "").lower() if hd.get("gsm", {}).get("simCardState") else None
                ),
                "enabled_by_default": True,
            }
        )

    # Active channels (connection types)
    # IMPORTANT: sorted() is required - API returns channels in random order
    # causing state changes every poll (e.g., "GSM, ETHERNET" <-> "ETHERNET, GSM")
    # See: https://github.com/foXaCe/ajax-security-hass/issues/76
    if "activeChannels" in hub_details:
        sensors.append(
            {
                "key": "active_connection",
                "translation_key": "active_connection",
                "value_fn": lambda hd=hub_details: (
                    ", ".join(sorted(hd.get("activeChannels", []))) if hd.get("activeChannels") else None
                ),
                "enabled_by_default": True,
            }
        )

    # Firmware version
    firmware = hub_details.get("firmware", {})
    if firmware and "version" in firmware:
        sensors.append(
            {
                "key": "hub_firmware",
                "translation_key": "firmware_version",
                "value_fn": lambda hd=hub_details: hd.get("firmware", {}).get("version"),
                "enabled_by_default": False,
            }
        )

    return sensors


class AjaxHubSensor(CoordinatorEntity[AjaxDataCoordinator], SensorEntity):
    """Representation of an Ajax Hub sensor.

    This is for hub-level sensors that come from space.hub_details,
    not from a device in space.devices.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        sensor_key: str,
        sensor_desc: dict[str, Any],
    ) -> None:
        """Initialize the Ajax hub sensor."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._sensor_key = sensor_key
        self._sensor_desc = sensor_desc

        # Get space for hub_id
        space = coordinator.get_space(space_id)
        hub_id = space.hub_id if space else space_id

        # Set unique ID
        self._attr_unique_id = f"{self.coordinator.entry_id}_{hub_id}_{sensor_key}"

        # Set device class if provided
        if "device_class" in sensor_desc:
            self._attr_device_class = sensor_desc["device_class"]

        # Set translation key
        if "translation_key" in sensor_desc:
            self._attr_translation_key = sensor_desc["translation_key"]
        elif "device_class" not in sensor_desc:
            self._attr_translation_key = sensor_key

        # Set unit of measurement
        if "native_unit_of_measurement" in sensor_desc:
            self._attr_native_unit_of_measurement = sensor_desc["native_unit_of_measurement"]

        # Set state class
        if "state_class" in sensor_desc:
            self._attr_state_class = sensor_desc["state_class"]

        # Set enabled by default
        if "enabled_by_default" in sensor_desc:
            self._attr_entity_registry_enabled_default = sensor_desc["enabled_by_default"]

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        space = self.coordinator.get_space(self._space_id)
        if not space or not space.hub_details:
            return None

        value_fn = self._sensor_desc.get("value_fn")
        if value_fn:
            try:
                return value_fn(space.hub_details)
            except Exception as err:
                _LOGGER.error(
                    "Error getting value for hub sensor %s: %s",
                    self._sensor_key,
                    err,
                )
                return None
        return None

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not self.coordinator.last_update_success:
            return False
        space = self.coordinator.get_space(self._space_id)
        return space is not None and space.hub_details is not None

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information linking to the hub/space device."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None

        # Link to the space device (hub)
        return DeviceInfo(
            identifiers={device_identifier(self.coordinator.entry_id, self._space_id)},
        )


# ==============================================================================
# Smart Lock Sensors
# ==============================================================================
