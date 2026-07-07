"""Ajax number platform.

Thin platform module: ``async_setup_entry`` plus the discovery builder.
The static setup reuses the builder so entity-construction logic exists in
exactly one place. Number definitions and entity classes live in
``_number_entities`` and are re-exported here for backwards compatibility
(tests and older imports).
"""

from __future__ import annotations

import logging

from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN, NumberEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AjaxConfigEntry
from ._discovery import connect_new_entity_signal
from ._number_entities import (
    DEVICES_WITH_CURRENT_THRESHOLD,
    DEVICES_WITH_DOOR_PLUS_NUMBERS,
    DIMMER_NUMBER_DEFINITIONS,
    LIGHTSWITCH_TOUCH_SENSITIVITY_NUMBER,
    AjaxCurrentThresholdNumber,
    AjaxDimmerNumber,
    AjaxDoorPlusBaseNumber,
    AjaxLedBrightnessV2Number,
    AjaxTiltDegreesNumber,
)
from .const import DOMAIN, SIGNAL_NEW_DEVICE
from .devices import is_dimmer_device, is_lightswitch_device

__all__ = [
    "DEVICES_WITH_CURRENT_THRESHOLD",
    "DEVICES_WITH_DOOR_PLUS_NUMBERS",
    "DIMMER_NUMBER_DEFINITIONS",
    "DOMAIN",
    "LIGHTSWITCH_TOUCH_SENSITIVITY_NUMBER",
    "AjaxCurrentThresholdNumber",
    "AjaxDimmerNumber",
    "AjaxDoorPlusBaseNumber",
    "AjaxLedBrightnessV2Number",
    "AjaxTiltDegreesNumber",
    "async_setup_entry",
]

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax number entities from a config entry."""
    coordinator = entry.runtime_data

    if coordinator.account is None:
        return

    def _build_device(space_id: str, device_id: str) -> list[tuple[str, NumberEntity]]:
        """Build number entities for a newly-discovered device.

        Returns every candidate ``(unique_id, entity)``; the discovery
        helper drops the ones already present in the entity registry.
        """
        space = coordinator.get_space(space_id)
        device = space.devices.get(device_id) if space else None
        if not device:
            return []

        pairs: list[tuple[str, NumberEntity]] = []

        device_type_raw = device.raw_type or ""
        if device_type_raw in DEVICES_WITH_DOOR_PLUS_NUMBERS:
            pairs.append(
                (
                    f"{coordinator.entry_id}_{device_id}_tilt_degrees",
                    AjaxTiltDegreesNumber(coordinator, space_id, device_id),
                )
            )

        if device.type in DEVICES_WITH_CURRENT_THRESHOLD and "current_threshold" in device.attributes:
            pairs.append(
                (
                    f"{coordinator.entry_id}_{device_id}_current_threshold",
                    AjaxCurrentThresholdNumber(coordinator, space_id, device_id),
                )
            )

        if device.type in DEVICES_WITH_CURRENT_THRESHOLD:
            brightness = device.attributes.get("indicationBrightness")
            if isinstance(brightness, int):
                pairs.append(
                    (
                        f"{coordinator.entry_id}_{device_id}_led_brightness",
                        AjaxLedBrightnessV2Number(coordinator, space_id, device_id),
                    )
                )

        if is_dimmer_device(device):
            for number_def in DIMMER_NUMBER_DEFINITIONS:
                if number_def["attr_key"] in device.attributes:
                    pairs.append(
                        (
                            f"{coordinator.entry_id}_{device_id}_{number_def['key']}",
                            AjaxDimmerNumber(coordinator, space_id, device_id, number_def),
                        )
                    )

        if is_lightswitch_device(device) and "touchSensitivity" in device.attributes:
            pairs.append(
                (
                    f"{coordinator.entry_id}_{device_id}_touch_sensitivity",
                    AjaxDimmerNumber(
                        coordinator=coordinator,
                        space_id=space_id,
                        device_id=device_id,
                        number_def=LIGHTSWITCH_TOUCH_SENSITIVITY_NUMBER,
                    ),
                )
            )

        return pairs

    # Static setup: reuse the discovery builder.
    entities: list[NumberEntity] = [
        entity
        for space_id, space in coordinator.account.spaces.items()
        for device_id in space.devices
        for _uid, entity in _build_device(space_id, device_id)
    ]

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax number entities", len(entities))

    connect_new_entity_signal(
        hass,
        entry,
        SIGNAL_NEW_DEVICE,
        NUMBER_DOMAIN,
        async_add_entities,
        _build_device,
        label="number entit(ies)",
    )
