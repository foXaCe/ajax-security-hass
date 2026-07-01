"""Ajax switch platform for Home Assistant (refactored).

This module creates switches for Ajax device settings using the device handler architecture.
Each device type has its own handler that defines which switches to create.
"""

from __future__ import annotations

import logging

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AjaxConfigEntry
from ._discovery import connect_new_entity_signal

# Entity classes live in dedicated modules; re-imported here for the platform
# setup below and for the public import surface (tests import from .switch).
from ._switch_dimmer import (
    DIMMER_SETTINGS_SWITCHES,
    AjaxDimmerBoolSwitch,
    AjaxDimmerCalibrationSwitch,
    AjaxDimmerSettingsSwitch,
)
from ._switch_entity import AjaxSwitch
from .const import SIGNAL_NEW_DEVICE
from .devices import LightSwitchHandler, get_device_handler, is_dimmer_device, is_lightswitch_device

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax switches from a config entry."""
    coordinator = entry.runtime_data

    if coordinator.account is None:
        return

    entities: list[SwitchEntity] = []

    # Create switches for each device
    for space_id, space in coordinator.account.spaces.items():
        for device_id, device in space.devices.items():
            # Handle LightSwitchDimmer separately with static definitions
            if is_dimmer_device(device):
                # Settings-based switches (settingsSwitch list)
                if "settingsSwitch" in device.attributes:
                    for switch_def in DIMMER_SETTINGS_SWITCHES:
                        entities.append(
                            AjaxDimmerSettingsSwitch(
                                coordinator=coordinator,
                                space_id=space_id,
                                device_id=device_id,
                                switch_def=switch_def,
                            )
                        )
                        _LOGGER.debug(
                            "Created dimmer switch '%s' for device: %s",
                            switch_def["key"],
                            device.name,
                        )

                # Night mode switch (boolean attribute)
                if "nightModeArm" in device.attributes:
                    entities.append(
                        AjaxDimmerBoolSwitch(
                            coordinator=coordinator,
                            space_id=space_id,
                            device_id=device_id,
                            switch_key="night_mode",
                            attr_key="nightModeArm",
                            api_key="nightModeArm",
                        )
                    )

                # Dimmer calibration switch (nested in dimmerSettings)
                dimmer_settings = device.attributes.get("dimmerSettings", {})
                if dimmer_settings and "calibration" in dimmer_settings:
                    entities.append(
                        AjaxDimmerCalibrationSwitch(
                            coordinator=coordinator,
                            space_id=space_id,
                            device_id=device_id,
                        )
                    )
                continue

            # Standard handler-based switches for other devices
            handler_class = get_device_handler(device)
            if handler_class:
                handler = handler_class(device)
                switches = handler.get_switches()

                for switch_desc in switches:
                    entities.append(
                        AjaxSwitch(
                            coordinator=coordinator,
                            space_id=space_id,
                            device_id=device_id,
                            switch_key=switch_desc["key"],
                            switch_desc=switch_desc,
                        )
                    )
                    _LOGGER.debug(
                        "Created switch '%s' for device: %s (type: %s)",
                        switch_desc["key"],
                        device.name,
                        device.type.value if device.type else "unknown",
                    )

            # Add LightSwitch settings switches (non-dimmer) - LED, child lock, etc.
            if is_lightswitch_device(device):
                lightswitch_handler = LightSwitchHandler(device)
                settings_switches = lightswitch_handler.get_switches()
                for switch_desc in settings_switches:
                    entities.append(
                        AjaxSwitch(
                            coordinator=coordinator,
                            space_id=space_id,
                            device_id=device_id,
                            switch_key=switch_desc["key"],
                            switch_desc=switch_desc,
                        )
                    )
                    _LOGGER.debug(
                        "Created LightSwitch settings switch '%s' for device: %s",
                        switch_desc["key"],
                        device.name,
                    )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax switch(es)", len(entities))

    def _build_device(space_id: str, device_id: str) -> list[tuple[str, SwitchEntity]]:
        """Build switch entities for a newly-discovered device.

        Returns every candidate ``(unique_id, entity)``; the discovery
        helper drops the ones already present in the entity registry.
        """
        space = coordinator.get_space(space_id)
        device = space.devices.get(device_id) if space else None
        if not device:
            return []

        pairs: list[tuple[str, SwitchEntity]] = []

        if is_dimmer_device(device):
            if "settingsSwitch" in device.attributes:
                for switch_def in DIMMER_SETTINGS_SWITCHES:
                    pairs.append(
                        (
                            f"{device_id}_{switch_def['key']}",
                            AjaxDimmerSettingsSwitch(coordinator, space_id, device_id, switch_def),
                        )
                    )
            if "nightModeArm" in device.attributes:
                pairs.append(
                    (
                        f"{device_id}_night_mode",
                        AjaxDimmerBoolSwitch(
                            coordinator=coordinator,
                            space_id=space_id,
                            device_id=device_id,
                            switch_key="night_mode",
                            attr_key="nightModeArm",
                            api_key="nightModeArm",
                        ),
                    )
                )
            dimmer_settings = device.attributes.get("dimmerSettings", {})
            if dimmer_settings and "calibration" in dimmer_settings:
                pairs.append(
                    (
                        f"{device_id}_dimmer_calibration",
                        AjaxDimmerCalibrationSwitch(coordinator, space_id, device_id),
                    )
                )
        else:
            handler_class = get_device_handler(device)
            if handler_class:
                handler = handler_class(device)
                for switch_desc in handler.get_switches():
                    pairs.append(
                        (
                            f"{device_id}_{switch_desc['key']}",
                            AjaxSwitch(coordinator, space_id, device_id, switch_desc["key"], switch_desc),
                        )
                    )

            if is_lightswitch_device(device):
                lightswitch_handler = LightSwitchHandler(device)
                for switch_desc in lightswitch_handler.get_switches():
                    pairs.append(
                        (
                            f"{device_id}_{switch_desc['key']}",
                            AjaxSwitch(coordinator, space_id, device_id, switch_desc["key"], switch_desc),
                        )
                    )

        return pairs

    connect_new_entity_signal(
        hass,
        entry,
        SIGNAL_NEW_DEVICE,
        SWITCH_DOMAIN,
        async_add_entities,
        _build_device,
        label="switch entit(ies)",
    )
