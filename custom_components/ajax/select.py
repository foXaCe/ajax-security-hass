"""Ajax select platform.

Thin platform module: ``async_setup_entry`` plus the discovery builder.
The static setup reuses the builder so entity-construction logic exists in
exactly one place. Option tables and entity classes live in
``_select_entities`` and are re-exported here for backwards compatibility
(tests and older imports).
"""

from __future__ import annotations

import logging

from homeassistant.components.select import DOMAIN as SELECT_DOMAIN, SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AjaxConfigEntry
from ._discovery import connect_new_entity_signal
from ._select_entities import (
    DEVICES_WITH_DOOR_PLUS_SELECTS,
    DIMMER_SELECT_DEFINITIONS,
    INDICATION_MODE_OPTIONS,
    INDICATION_MODE_VALUES,
    LED_BRIGHTNESS_OPTIONS,
    LIGHTSWITCH_TOUCH_MODE_SELECT,
    SELECT_DEVICE_HANDLERS,
    SHOCK_SENSITIVITY_OPTIONS,
    SHOCK_SENSITIVITY_VALUES,
    AjaxDimmerSelect,
    AjaxDoorPlusBaseSelect,
    AjaxHandlerSelect,
    AjaxIndicationModeSelect,
    AjaxLedBrightnessSelect,
    AjaxShockSensitivitySelect,
    _get_dimmer_attr,
)
from .const import DOMAIN, SIGNAL_NEW_DEVICE
from .devices import is_dimmer_device, is_lightswitch_device
from .models import DeviceType

__all__ = [
    "DEVICES_WITH_DOOR_PLUS_SELECTS",
    "DIMMER_SELECT_DEFINITIONS",
    "DOMAIN",
    "INDICATION_MODE_OPTIONS",
    "INDICATION_MODE_VALUES",
    "LED_BRIGHTNESS_OPTIONS",
    "LIGHTSWITCH_TOUCH_MODE_SELECT",
    "SELECT_DEVICE_HANDLERS",
    "SHOCK_SENSITIVITY_OPTIONS",
    "SHOCK_SENSITIVITY_VALUES",
    "AjaxDimmerSelect",
    "AjaxDoorPlusBaseSelect",
    "AjaxHandlerSelect",
    "AjaxIndicationModeSelect",
    "AjaxLedBrightnessSelect",
    "AjaxShockSensitivitySelect",
    "async_setup_entry",
]

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax select entities from a config entry."""
    coordinator = entry.runtime_data

    if coordinator.account is None:
        return

    def _build_device(space_id: str, device_id: str) -> list[tuple[str, SelectEntity]]:
        """Build select entities for a newly-discovered device.

        Returns every candidate ``(unique_id, entity)``; the discovery
        helper drops the ones already present in the entity registry.
        """
        space = coordinator.get_space(space_id)
        device = space.devices.get(device_id) if space else None
        if not device:
            return []

        pairs: list[tuple[str, SelectEntity]] = []

        device_type = device.raw_type or ""
        if device_type in DEVICES_WITH_DOOR_PLUS_SELECTS:
            pairs.append(
                (
                    f"{entry.entry_id}_{device_id}_shock_sensitivity",
                    AjaxShockSensitivitySelect(coordinator, space_id, device_id),
                )
            )

        if device.type == DeviceType.SOCKET and device.attributes.get("indicationBrightness") in ["MIN", "MAX"]:
            pairs.append(
                (
                    f"{entry.entry_id}_{device_id}_led_brightness",
                    AjaxLedBrightnessSelect(coordinator, space_id, device_id),
                )
            )

        if device.type == DeviceType.SOCKET and "indicationMode" in device.attributes:
            pairs.append(
                (
                    f"{entry.entry_id}_{device_id}_indication_mode",
                    AjaxIndicationModeSelect(coordinator, space_id, device_id),
                )
            )

        if is_dimmer_device(device):
            for select_def in DIMMER_SELECT_DEFINITIONS:
                if _get_dimmer_attr(device, select_def["attr_key"]) is not None:
                    pairs.append(
                        (
                            f"{entry.entry_id}_{device_id}_{select_def['key']}",
                            AjaxDimmerSelect(coordinator, space_id, device_id, select_def),
                        )
                    )

        if is_lightswitch_device(device) and "touchMode" in device.attributes:
            pairs.append(
                (
                    f"{entry.entry_id}_{device_id}_{LIGHTSWITCH_TOUCH_MODE_SELECT['key']}",
                    AjaxDimmerSelect(coordinator, space_id, device_id, LIGHTSWITCH_TOUCH_MODE_SELECT),
                )
            )

        handler_class = SELECT_DEVICE_HANDLERS.get(device.type)
        if handler_class:
            handler = handler_class(device)
            if hasattr(handler, "get_selects"):
                for select_desc in handler.get_selects():
                    pairs.append(
                        (
                            f"{entry.entry_id}_{device_id}_{select_desc['key']}",
                            AjaxHandlerSelect(coordinator, space_id, device_id, select_desc),
                        )
                    )

        return pairs

    # Static setup: reuse the discovery builder.
    entities: list[SelectEntity] = [
        entity
        for space_id, space in coordinator.account.spaces.items()
        for device_id in space.devices
        for _uid, entity in _build_device(space_id, device_id)
    ]

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax select entities", len(entities))

    connect_new_entity_signal(
        hass,
        entry,
        SIGNAL_NEW_DEVICE,
        SELECT_DOMAIN,
        async_add_entities,
        _build_device,
        label="select entit(ies)",
    )
