"""Ajax binary sensor platform.

Thin platform module: ``async_setup_entry`` plus the discovery builders.
The static setup reuses the builders so entity-construction logic exists
in exactly one place. The entity classes live in
``_binary_sensor_entities`` and are re-exported here for backwards
compatibility (tests and older imports).
"""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    DOMAIN as BINARY_SENSOR_DOMAIN,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AjaxConfigEntry
from ._binary_sensor_entities import (
    AjaxBinarySensor,
    AjaxHubBinarySensor,
    AjaxSmartLockBinarySensor,
    AjaxVideoEdgeBinarySensor,
)
from ._discovery import connect_new_entity_signal
from .const import SIGNAL_NEW_DEVICE, SIGNAL_NEW_SMART_LOCK, SIGNAL_NEW_SPACE, SIGNAL_NEW_VIDEO_EDGE
from .devices import VideoEdgeHandler, get_device_handler

__all__ = [
    "AjaxBinarySensor",
    "AjaxHubBinarySensor",
    "AjaxSmartLockBinarySensor",
    "AjaxVideoEdgeBinarySensor",
    "async_setup_entry",
]

_LOGGER = logging.getLogger(__name__)
# Read-only coordinator-driven platform: no per-entity I/O to throttle
# (Quality Scale parallel-updates: 0 = unlimited is the recommendation).
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax binary sensor platform."""
    coordinator = entry.runtime_data

    if coordinator.account is None:
        return

    def _build_device(space_id: str, device_id: str) -> list[tuple[str, BinarySensorEntity]]:
        """Build binary sensors for a newly-discovered regular device."""
        space = coordinator.get_space(space_id)
        device = space.devices.get(device_id) if space else None
        if not device:
            return []
        handler_class = get_device_handler(device)
        if not handler_class:
            return []
        handler = handler_class(device)
        return [
            (
                f"{device_id}_{sensor_desc['key']}",
                AjaxBinarySensor(
                    coordinator=coordinator,
                    space_id=space_id,
                    device_id=device_id,
                    sensor_key=sensor_desc["key"],
                    sensor_desc=sensor_desc,
                ),
            )
            for sensor_desc in handler.get_binary_sensors()
        ]

    def _build_video_edge(space_id: str, video_edge_id: str) -> list[tuple[str, BinarySensorEntity]]:
        """Build binary sensors for a newly-discovered Video Edge device."""
        space = coordinator.get_space(space_id)
        if space is None:
            return []
        video_edge = space.video_edges.get(video_edge_id)
        if not video_edge:
            return []
        handler = VideoEdgeHandler(video_edge, space.video_edges)
        pairs: list[tuple[str, BinarySensorEntity]] = []
        for sensor_desc in handler.get_binary_sensors():
            # Use target_video_edge_id if present (for NVR channels linked to
            # cameras) — attaches the entity to the camera device, not the NVR.
            target_ve_id = sensor_desc.get("target_video_edge_id") or video_edge_id
            pairs.append(
                (
                    f"{target_ve_id}_{sensor_desc['key']}",
                    AjaxVideoEdgeBinarySensor(
                        coordinator=coordinator,
                        space_id=space_id,
                        video_edge_id=target_ve_id,
                        sensor_key=sensor_desc["key"],
                        sensor_desc=sensor_desc,
                    ),
                )
            )
        return pairs

    def _build_smart_lock_door(space_id: str, smart_lock_id: str) -> list[tuple[str, BinarySensorEntity]]:
        """Build the door binary sensor for a newly-discovered smart lock."""
        return [
            (
                f"{smart_lock_id}_door",
                AjaxSmartLockBinarySensor(coordinator=coordinator, space_id=space_id, smart_lock_id=smart_lock_id),
            )
        ]

    def _build_space(space_id: str, _obj_id: str) -> list[tuple[str, BinarySensorEntity]]:
        """Build hub binary sensors for a hub added after startup (#multi-hub)."""
        space = coordinator.get_space(space_id)
        if space is None or not space.hub_details:
            return []
        hub_id = space.hub_id
        return [
            (
                f"{hub_id}_{sensor_key}",
                AjaxHubBinarySensor(coordinator=coordinator, space_id=space_id, sensor_key=sensor_key),
            )
            for sensor_key in AjaxHubBinarySensor.HUB_BINARY_SENSORS
        ]

    # Static setup: reuse the discovery builders so the construction logic
    # exists once. The seen-set dedup matters for NVR-linked cameras: the
    # same target unique_id can be produced from several source video edges.
    entities: list[BinarySensorEntity] = []
    seen_unique_ids: set[str] = set()

    for space_id, space in coordinator.account.spaces.items():
        built: list[tuple[str, BinarySensorEntity]] = []
        for device_id in space.devices:
            built.extend(_build_device(space_id, device_id))
        for ve_id in space.video_edges:
            built.extend(_build_video_edge(space_id, ve_id))
        for sl_id in space.smart_locks:
            built.extend(_build_smart_lock_door(space_id, sl_id))
        built.extend(_build_space(space_id, space_id))

        for unique_id, entity in built:
            if unique_id in seen_unique_ids:
                continue
            seen_unique_ids.add(unique_id)
            entities.append(entity)

    async_add_entities(entities)
    if entities:
        _LOGGER.info("Added %d Ajax binary sensor(s)", len(entities))

    connect_new_entity_signal(
        hass,
        entry,
        SIGNAL_NEW_SPACE,
        BINARY_SENSOR_DOMAIN,
        async_add_entities,
        _build_space,
        label="hub binary sensor(s)",
    )
    connect_new_entity_signal(
        hass,
        entry,
        SIGNAL_NEW_DEVICE,
        BINARY_SENSOR_DOMAIN,
        async_add_entities,
        _build_device,
        label="binary sensor(s)",
    )
    connect_new_entity_signal(
        hass,
        entry,
        SIGNAL_NEW_VIDEO_EDGE,
        BINARY_SENSOR_DOMAIN,
        async_add_entities,
        _build_video_edge,
        label="Video Edge binary sensor(s)",
    )
    connect_new_entity_signal(
        hass,
        entry,
        SIGNAL_NEW_SMART_LOCK,
        BINARY_SENSOR_DOMAIN,
        async_add_entities,
        _build_smart_lock_door,
        label="smart lock door sensor(s)",
    )
