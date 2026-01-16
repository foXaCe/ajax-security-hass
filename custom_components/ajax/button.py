"""Ajax button platform."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.core import HomeAssistant
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
    """Set up Ajax buttons from a config entry."""
    coordinator = entry.runtime_data

    # Create a panic button for each space
    entities = []

    if coordinator.account:
        for space_id, _space in coordinator.account.spaces.items():
            entities.append(AjaxPanicButton(coordinator, entry, space_id))

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax button(s)", len(entities))
    else:
        _LOGGER.info("No Ajax spaces found, no buttons created (yet)")


class AjaxPanicButton(CoordinatorEntity[AjaxDataCoordinator], ButtonEntity):
    """Representation of an Ajax panic button."""

    _attr_device_class = ButtonDeviceClass.IDENTIFY
    _attr_icon = "mdi:alarm-light"

    def __init__(
        self, coordinator: AjaxDataCoordinator, entry: AjaxConfigEntry, space_id: str
    ) -> None:
        """Initialize the panic button."""
        super().__init__(coordinator)
        self._entry = entry
        self._space_id = space_id

        self._attr_unique_id = f"{entry.entry_id}_panic_{space_id}"
        self._attr_translation_key = "panic"
        self._attr_has_entity_name = True

    async def async_press(self) -> None:
        """Handle the button press."""
        _LOGGER.warning("PANIC BUTTON pressed by user!")

        try:
            await self.coordinator.async_press_panic_button(self._space_id)
        except Exception as err:
            _LOGGER.error("Failed to trigger panic: %s", err)
            raise

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return {}

        return {
            "identifiers": {(DOMAIN, self._space_id)},
            "name": space.name,
            "manufacturer": MANUFACTURER,
            "model": "Security Hub",
            "sw_version": None,  # TODO: Add hub firmware version when available
        }
