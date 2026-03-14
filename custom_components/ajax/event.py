"""Ajax event platform.

Creates event entities for button presses and doorbell rings.
Events are fired from SQS/SSE real-time messages.
"""

from __future__ import annotations

import logging

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import DOMAIN, MANUFACTURER
from .coordinator import AjaxDataCoordinator
from .devices import get_device_handler

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax event platform."""
    coordinator = entry.runtime_data

    if coordinator.account is None:
        return

    entities: list[AjaxEventEntity] = []
    seen_unique_ids: set[str] = set()

    for space_id, space in coordinator.account.spaces.items():
        for device_id, device in space.devices.items():
            handler_class = get_device_handler(device)
            if handler_class:
                handler = handler_class(device)
                events = handler.get_events()

                for event_desc in events:
                    unique_id = f"{device_id}_{event_desc['key']}"

                    if unique_id in seen_unique_ids:
                        continue
                    seen_unique_ids.add(unique_id)

                    entity = AjaxEventEntity(
                        coordinator=coordinator,
                        space_id=space_id,
                        device_id=device_id,
                        event_key=event_desc["key"],
                        event_desc=event_desc,
                    )
                    entities.append(entity)
                    # Register in coordinator for SQS/SSE event firing
                    coordinator._event_entities[device_id] = entity
                    _LOGGER.debug(
                        "Created event entity '%s' for device: %s",
                        event_desc["key"],
                        device.name,
                    )

    async_add_entities(entities)
    if entities:
        _LOGGER.info("Added %d Ajax event entit(ies)", len(entities))


class AjaxEventEntity(CoordinatorEntity[AjaxDataCoordinator], EventEntity):
    """Event entity for Ajax button/doorbell devices."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        device_id: str,
        event_key: str,
        event_desc: dict,
    ) -> None:
        """Initialize the event entity."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._event_key = event_key
        self._event_desc = event_desc

        self._attr_unique_id = f"{device_id}_{event_key}"
        self._attr_translation_key = event_desc.get("translation_key", event_key)
        self._attr_event_types = event_desc["event_types"]
        self._attr_entity_registry_enabled_default = event_desc.get("enabled_by_default", True)

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device info."""
        if self.coordinator.account is None:
            return None

        for space in self.coordinator.account.spaces.values():
            device = space.devices.get(self._device_id)
            if device:
                return DeviceInfo(
                    identifiers={(DOMAIN, self._device_id)},
                    name=device.name,
                    manufacturer=MANUFACTURER,
                    model=device.type.value,
                )
        return None

    @callback
    def fire(self, event_type: str) -> None:
        """Fire an event."""
        if event_type in self._attr_event_types:
            self._trigger_event(event_type)
            self.async_write_ha_state()
            _LOGGER.debug(
                "Event fired: %s -> %s",
                self._device_id,
                event_type,
            )
