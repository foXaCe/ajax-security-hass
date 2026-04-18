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
                    _LOGGER.debug(
                        "Created event entity '%s' for device: %s",
                        event_desc["key"],
                        device.name,
                    )

        # Create event entities for Video Edge cameras
        for ve_id, video_edge in space.video_edges.items():
            if video_edge.video_edge_type == VideoEdgeType.NVR:
                continue

            ve_events = []

            # Doorbell ring event
            if video_edge.video_edge_type == VideoEdgeType.DOORBELL:
                ve_events.append(
                    {
                        "key": "doorbell_press",
                        "translation_key": "doorbell_press",
                        "device_class": EventDeviceClass.DOORBELL,
                        "event_types": ["ring"],
                        "enabled_by_default": True,
                    }
                )

            # AI detection event (all cameras including doorbell)
            ve_events.append(
                {
                    "key": "detection",
                    "translation_key": "camera_detection",
                    "device_class": EventDeviceClass.MOTION,
                    "event_types": [
                        "motion",
                        "human",
                        "vehicle",
                        "pet",
                        "line_crossing",
                    ],
                    "enabled_by_default": True,
                }
            )

            for event_desc in ve_events:
                unique_id = f"{ve_id}_{event_desc['key']}"
                if unique_id in seen_unique_ids:
                    continue
                seen_unique_ids.add(unique_id)

                entity = AjaxEventEntity(
                    coordinator=coordinator,
                    space_id=space_id,
                    device_id=ve_id,
                    event_key=event_desc["key"],
                    event_desc=event_desc,
                )
                entities.append(entity)
                _LOGGER.debug(
                    "Created event entity '%s' for video edge: %s",
                    event_desc["key"],
                    video_edge.name,
                )

        # Create event entities for smart locks
        for sl_id, smart_lock in space.smart_locks.items():
            event_desc = {
                "key": "smart_lock_event",
                "translation_key": "smart_lock_event",
                "device_class": EventDeviceClass.DOORBELL,
                "event_types": ["doorbell_pressed", "door_left_open"],
                "enabled_by_default": True,
            }
            unique_id = f"{sl_id}_smart_lock_event"
            if unique_id in seen_unique_ids:
                continue
            seen_unique_ids.add(unique_id)

            entity = AjaxEventEntity(
                coordinator=coordinator,
                space_id=space_id,
                device_id=sl_id,
                event_key="smart_lock_event",
                event_desc=event_desc,
            )
            entities.append(entity)
            _LOGGER.debug(
                "Created event entity 'smart_lock_event' for smart lock: %s",
                smart_lock.name,
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

    async def async_added_to_hass(self) -> None:
        """Register entity in coordinator dispatch map."""
        await super().async_added_to_hass()
        self.coordinator._event_entities[self._attr_unique_id] = self

    async def async_will_remove_from_hass(self) -> None:
        """Remove entity from coordinator dispatch map to avoid stale refs."""
        self.coordinator._event_entities.pop(self._attr_unique_id, None)
        await super().async_will_remove_from_hass()

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
            # Check smart_locks
            smart_lock = space.smart_locks.get(self._device_id)
            if smart_lock:
                return DeviceInfo(
                    identifiers={(DOMAIN, self._device_id)},
                    name=smart_lock.name,
                    manufacturer=MANUFACTURER,
                    model="LockBridge Jeweller",
                )
        return None

    @callback
    def fire(self, event_type: str, event_attributes: dict | None = None) -> None:
        """Fire an event."""
        if event_type in self._attr_event_types:
            self._trigger_event(event_type, event_attributes)
            if self.hass is not None:
                self.async_write_ha_state()
            _LOGGER.debug(
                "Event fired: %s -> %s",
                self._device_id,
                event_type,
            )
