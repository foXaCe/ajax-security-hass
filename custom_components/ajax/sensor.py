"""Ajax sensor platform.

Thin platform module: ``async_setup_entry`` + dynamic discovery wiring.
The entity classes live in ``_sensor_space`` / ``_sensor_device`` /
``_sensor_hub`` / ``_sensor_smart_lock`` and are re-exported here (tests
and downstreams import them from this module).
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
    SensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AjaxConfigEntry
from ._discovery import connect_new_entity_signal
from ._sensor_device import AjaxDeviceSensor, AjaxVideoEdgeSensor
from ._sensor_hub import AjaxHubSensor, _get_hub_sensors
from ._sensor_smart_lock import AjaxSmartLockSensor
from ._sensor_space import (
    SPACE_SENSORS,
    AjaxSpaceSensor,
    AjaxSpaceSensorDescription,
    _format_time_ago,
    format_event_text,
    format_hub_type,
    format_signal_level,
    format_timezone,
    get_last_event_attributes,
    get_last_event_text,
)
from .const import SIGNAL_NEW_DEVICE, SIGNAL_NEW_SMART_LOCK, SIGNAL_NEW_SPACE, SIGNAL_NEW_VIDEO_EDGE
from .devices import VideoEdgeHandler, get_device_handler

__all__ = [
    "SPACE_SENSORS",
    "AjaxDeviceSensor",
    "AjaxHubSensor",
    "AjaxSmartLockSensor",
    "AjaxSpaceSensor",
    "AjaxSpaceSensorDescription",
    "AjaxVideoEdgeSensor",
    "_format_time_ago",
    "_get_hub_sensors",
    "async_setup_entry",
    "format_event_text",
    "format_hub_type",
    "format_signal_level",
    "format_timezone",
    "get_last_event_attributes",
    "get_last_event_text",
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
    """Set up Ajax sensors from a config entry."""
    coordinator = entry.runtime_data

    entities: list[SensorEntity] = []
    seen_unique_ids: set[str] = set()

    if not coordinator.account:
        _LOGGER.warning("No Ajax account found, no sensors created")
        return

    # Create space-level sensors for each space (hub)
    for space_id, space in coordinator.account.spaces.items():
        for description in SPACE_SENSORS:
            if description.should_create and not description.should_create(space):
                continue
            # recent_events is now created from REST API state changes, not just SQS
            # So we always create it
            entities.append(AjaxSpaceSensor(coordinator, entry, space_id, description))

    # Create device-level sensors using handlers
    for space_id, space in coordinator.account.spaces.items():
        for device_id, device in space.devices.items():
            handler_class = get_device_handler(device)
            if handler_class:
                handler = handler_class(device)
                # Get device-specific sensors + common sensors (room, etc.)
                sensors = handler.get_sensors() + handler.get_common_sensors()

                for sensor_desc in sensors:
                    unique_id = f"{device_id}_{sensor_desc['key']}"

                    # Skip if we already created this entity in this setup
                    if unique_id in seen_unique_ids:
                        _LOGGER.debug(
                            "Skipping duplicate unique_id %s for device %s",
                            unique_id,
                            device.name,
                        )
                        continue
                    seen_unique_ids.add(unique_id)

                    entities.append(
                        AjaxDeviceSensor(
                            coordinator=coordinator,
                            space_id=space_id,
                            device_id=device_id,
                            sensor_key=sensor_desc["key"],
                            sensor_desc=sensor_desc,
                        )
                    )
                    _LOGGER.debug(
                        "Created sensor '%s' for device: %s (type: %s)",
                        sensor_desc["key"],
                        device.name,
                        device.type.value,
                    )

        # Create sensors for video edges (surveillance cameras)
        all_video_edges = space.video_edges
        for ve_id, video_edge in all_video_edges.items():
            handler = VideoEdgeHandler(video_edge, all_video_edges)  # type: ignore[assignment]
            sensors = handler.get_sensors()

            for sensor_desc in sensors:
                unique_id = f"{ve_id}_{sensor_desc['key']}"

                if unique_id in seen_unique_ids:
                    continue
                seen_unique_ids.add(unique_id)

                entities.append(
                    AjaxVideoEdgeSensor(
                        coordinator=coordinator,
                        space_id=space_id,
                        video_edge_id=ve_id,
                        sensor_key=sensor_desc["key"],
                        sensor_desc=sensor_desc,
                    )
                )
                _LOGGER.debug(
                    "Created video edge sensor '%s' for: %s",
                    sensor_desc["key"],
                    video_edge.name,
                )

        # Create hub-level sensors from hub_details
        if space.hub_details:
            hub_id = space.hub_id or space_id
            hub_sensors = _get_hub_sensors(space)

            for sensor_desc in hub_sensors:
                unique_id = f"{hub_id}_{sensor_desc['key']}"

                if unique_id in seen_unique_ids:
                    continue
                seen_unique_ids.add(unique_id)

                entities.append(
                    AjaxHubSensor(
                        coordinator=coordinator,
                        space_id=space_id,
                        sensor_key=sensor_desc["key"],
                        sensor_desc=sensor_desc,
                    )
                )
                _LOGGER.debug(
                    "Created hub sensor '%s' for space: %s",
                    sensor_desc["key"],
                    space.name,
                )

        # Create smart lock sensors (last_changed_by)
        for smart_lock_id, smart_lock in space.smart_locks.items():
            unique_id = f"{smart_lock_id}_last_changed_by"

            if unique_id in seen_unique_ids:
                continue
            seen_unique_ids.add(unique_id)

            entities.append(
                AjaxSmartLockSensor(
                    coordinator=coordinator,
                    space_id=space_id,
                    smart_lock_id=smart_lock_id,
                )
            )
            _LOGGER.debug(
                "Created last_changed_by sensor for smart lock: %s",
                smart_lock.name,
            )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax sensor(s)", len(entities))

    def _build_device(space_id: str, device_id: str) -> list[tuple[str, SensorEntity]]:
        """Build sensors for a newly-discovered regular device."""
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
                AjaxDeviceSensor(
                    coordinator=coordinator,
                    space_id=space_id,
                    device_id=device_id,
                    sensor_key=sensor_desc["key"],
                    sensor_desc=sensor_desc,
                ),
            )
            for sensor_desc in handler.get_sensors() + handler.get_common_sensors()
        ]

    def _build_video_edge(space_id: str, video_edge_id: str) -> list[tuple[str, SensorEntity]]:
        """Build sensors for a newly-discovered Video Edge device."""
        space = coordinator.get_space(space_id)
        if space is None:
            return []
        video_edge = space.video_edges.get(video_edge_id)
        if not video_edge:
            return []
        handler = VideoEdgeHandler(video_edge, space.video_edges)
        return [
            (
                f"{video_edge_id}_{sensor_desc['key']}",
                AjaxVideoEdgeSensor(
                    coordinator=coordinator,
                    space_id=space_id,
                    video_edge_id=video_edge_id,
                    sensor_key=sensor_desc["key"],
                    sensor_desc=sensor_desc,
                ),
            )
            for sensor_desc in handler.get_sensors()
        ]

    def _build_smart_lock(space_id: str, smart_lock_id: str) -> list[tuple[str, SensorEntity]]:
        """Build the last-changed-by sensor for a newly-discovered smart lock."""
        return [
            (
                f"{smart_lock_id}_last_changed_by",
                AjaxSmartLockSensor(coordinator, space_id, smart_lock_id),
            )
        ]

    def _build_space(space_id: str, _obj_id: str) -> list[tuple[str, SensorEntity]]:
        """Build space + hub sensors for a hub added after startup (#multi-hub)."""
        space = coordinator.get_space(space_id)
        if space is None:
            return []
        pairs: list[tuple[str, SensorEntity]] = [
            (
                f"{space_id}_{description.key}",
                AjaxSpaceSensor(coordinator, entry, space_id, description),
            )
            for description in SPACE_SENSORS
            if not (description.should_create and not description.should_create(space))
        ]
        if space.hub_details:
            hub_id = space.hub_id or space_id
            pairs.extend(
                (
                    f"{hub_id}_{sensor_desc['key']}",
                    AjaxHubSensor(
                        coordinator=coordinator,
                        space_id=space_id,
                        sensor_key=sensor_desc["key"],
                        sensor_desc=sensor_desc,
                    ),
                )
                for sensor_desc in _get_hub_sensors(space)
            )
        return pairs

    connect_new_entity_signal(
        hass,
        entry,
        SIGNAL_NEW_SPACE,
        SENSOR_DOMAIN,
        async_add_entities,
        _build_space,
        label="space/hub sensor(s)",
    )
    connect_new_entity_signal(
        hass,
        entry,
        SIGNAL_NEW_DEVICE,
        SENSOR_DOMAIN,
        async_add_entities,
        _build_device,
        label="sensor(s)",
    )
    connect_new_entity_signal(
        hass,
        entry,
        SIGNAL_NEW_VIDEO_EDGE,
        SENSOR_DOMAIN,
        async_add_entities,
        _build_video_edge,
        label="Video Edge sensor(s)",
    )
    connect_new_entity_signal(
        hass,
        entry,
        SIGNAL_NEW_SMART_LOCK,
        SENSOR_DOMAIN,
        async_add_entities,
        _build_smart_lock,
        label="smart lock sensor(s)",
    )


# ==============================================================================
# Space-level Sensors
# ==============================================================================
