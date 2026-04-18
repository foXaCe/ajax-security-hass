"""Ajax button platform."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import DOMAIN, MANUFACTURER
from .coordinator import AjaxDataCoordinator

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1


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
    """Representation of an Ajax panic button.

    Disabled by default to avoid accidental taps that would trigger a
    real alarm. Users must explicitly enable the entity.
    """

    __slots__ = ("_entry", "_space_id")

    # No device_class: IDENTIFY is semantically "flash the device to find it",
    # which does not match the destructive nature of a panic trigger.
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: AjaxDataCoordinator, entry: AjaxConfigEntry, space_id: str) -> None:
        """Initialize the panic button."""
        super().__init__(coordinator)
        self._entry = entry
        self._space_id = space_id

        self._attr_unique_id = f"{entry.entry_id}_panic_{space_id}"
        self._attr_translation_key = "panic"
        self._attr_has_entity_name = True

    async def async_press(self) -> None:
        """Handle the button press."""
        _LOGGER.info("Panic button pressed for space %s", self._space_id)

        try:
            await self.coordinator.async_press_panic_button(self._space_id)
        except Exception as err:
            _LOGGER.error("Failed to trigger panic: %s", err)
            raise

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None

        return DeviceInfo(
            identifiers={(DOMAIN, self._space_id)},
            name=space.name,
            manufacturer=MANUFACTURER,
            model="Security Hub",
        )
