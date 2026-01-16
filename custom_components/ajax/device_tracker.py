"""Ajax device tracker platform for Home Assistant.

This module creates device trackers for Ajax hubs with GPS geofence data,
allowing them to be displayed on the Home Assistant map.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.device_tracker import SourceType
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import DOMAIN, MANUFACTURER
from .coordinator import AjaxDataCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax device trackers from a config entry."""
    coordinator = entry.runtime_data

    entities: list[AjaxHubTracker] = []

    # Create device tracker for each space/hub with geofence data
    for space_id, space in coordinator.account.spaces.items():
        if space.hub_details:
            geofence = space.hub_details.get("geoFence") or {}
            if geofence.get("latitude") and geofence.get("longitude"):
                entities.append(AjaxHubTracker(coordinator, space_id))
                _LOGGER.debug(
                    "Created device tracker for hub: %s (lat: %s, lon: %s)",
                    space.name,
                    geofence.get("latitude"),
                    geofence.get("longitude"),
                )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax device tracker(s)", len(entities))


class AjaxHubTracker(CoordinatorEntity[AjaxDataCoordinator], TrackerEntity):
    """Device tracker for Ajax Hub location."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
    ) -> None:
        """Initialize the device tracker."""
        super().__init__(coordinator)
        self._space_id = space_id

        self._attr_unique_id = f"{space_id}_location"
        self._attr_translation_key = "position"
        self._attr_has_entity_name = True
        self._attr_icon = "mdi:map-marker-radius"

    @property
    def source_type(self) -> SourceType:
        """Return the source type."""
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        space = self.coordinator.get_space(self._space_id)
        if space and space.hub_details:
            geofence = space.hub_details.get("geoFence", {})
            lat = geofence.get("latitude")
            if lat:
                try:
                    return float(lat)
                except (ValueError, TypeError):
                    pass
        return None

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        space = self.coordinator.get_space(self._space_id)
        if space and space.hub_details:
            geofence = space.hub_details.get("geoFence", {})
            lon = geofence.get("longitude")
            if lon:
                try:
                    return float(lon)
                except (ValueError, TypeError):
                    pass
        return None

    @property
    def location_accuracy(self) -> int:
        """Return the location accuracy of the device (geofence radius)."""
        space = self.coordinator.get_space(self._space_id)
        if space and space.hub_details:
            geofence = space.hub_details.get("geoFence", {})
            radius = geofence.get("radiusMeters")
            if radius:
                try:
                    return int(radius)
                except (ValueError, TypeError):
                    pass
        return 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        space = self.coordinator.get_space(self._space_id)
        if not space or not space.hub_details:
            return {}

        geofence = space.hub_details.get("geoFence", {})
        return {
            "radius_meters": geofence.get("radiusMeters"),
            "space_id": self._space_id,
            "hub_id": space.hub_id,
        }

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return {}

        return {
            "identifiers": {(DOMAIN, self._space_id)},
            "name": "Ajax Hub" if space.name == "Hub" else space.name,
            "manufacturer": MANUFACTURER,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
