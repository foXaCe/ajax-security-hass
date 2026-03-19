"""Ajax event platform.

Creates event entities for button presses and doorbell rings.
Events are fired from SQS/SSE real-time messages.
"""

from __future__ import annotations

import logging

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import DOMAIN, MANUFACTURER
from .coordinator import AjaxDataCoordinator
from .devices import get_device_handler
from .models import VIDEO_EDGE_MODEL_NAMES, VideoEdgeType

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

        # Create event entities for Video Edge doorbells
        for ve_id, video_edge in space.video_edges.items():
            if video_edge.video_edge_type == VideoEdgeType.DOORBELL:
                unique_id = f"{ve_id}_doorbell_press"
                if unique_id in seen_unique_ids:
                    continue
                seen_unique_ids.add(unique_id)

                event_desc = {
                    "key": "doorbell_press",
                    "translation_key": "doorbell_press",
                    "device_class": EventDeviceClass.DOORBELL,
                    "event_types": ["ring"],
                    "enabled_by_default": True,
                }
                entity = AjaxEventEntity(
                    coordinator=coordinator,
                    space_id=space_id,
                    device_id=ve_id,
                    event_key="doorbell_press",
                    event_desc=event_desc,
                )
                entities.append(entity)
                coordinator._event_entities[ve_id] = entity
                _LOGGER.debug(
                    "Created event entity 'doorbell_press' for video edge: %s",
                    video_edge.name,
                )

    async_add_entities(entities)
    if entities:
        _LOGGER.info("Added %d Ajax event entit(ies)", len(entities))


class AjaxEventEntity(CoordinatorEntity[AjaxDataCoordinator], EventEntity):
    """Event entity for Ajax button/doorbell devices."""

    __slots__ = ("_space_id", "_device_id", "_event_key", "_event_desc")

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
        self._attr_device_class = event_desc.get("device_class")
        self._attr_event_types = event_desc["event_types"]
        # Fallback name if translation_key is not resolved
        self._attr_name = event_desc.get("name")
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
            # Check video_edges (for doorbell)
            video_edge = space.video_edges.get(self._device_id)
            if video_edge:
                model_name = VIDEO_EDGE_MODEL_NAMES.get(video_edge.video_edge_type, "Video Edge")
                return DeviceInfo(
                    identifiers={(DOMAIN, self._device_id)},
                    name=video_edge.name,
                    manufacturer=MANUFACTURER,
                    model=model_name,
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
