"""Ajax lock platform for Home Assistant.

This module creates lock entities for Ajax LockBridge Jeweller devices.
Lock entities are READ-ONLY: state comes from SSE/SQS events only.
There is no API to lock/unlock remotely.

Note: Yale cloud locks are automatically filtered out in the coordinator
since they don't send SSE events and only return minimal API data.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN, LockEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import DOMAIN, MANUFACTURER, SIGNAL_NEW_SMART_LOCK
from .coordinator import AjaxDataCoordinator
from .models import AjaxSmartLock

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax locks from a config entry."""
    coordinator = entry.runtime_data

    if coordinator.account is None:
        return

    entities: list[LockEntity] = []

    for space_id, space in coordinator.account.spaces.items():
        for smart_lock_id, smart_lock in space.smart_locks.items():
            entities.append(
                AjaxLock(
                    coordinator=coordinator,
                    space_id=space_id,
                    smart_lock_id=smart_lock_id,
                )
            )
            _LOGGER.debug(
                "Created lock entity for smart lock: %s (%s)",
                smart_lock.name,
                smart_lock_id,
            )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax lock(s)", len(entities))

    @callback
    def _async_add_new_smart_lock(space_id: str, smart_lock_id: str) -> None:
        """Add new lock entity when a smart lock is discovered from SSE/SQS."""
        # Check entity registry to avoid duplicates (more robust than manual set tracking)
        ent_reg = er.async_get(hass)
        unique_id = f"{smart_lock_id}_lock"
        if ent_reg.async_get_entity_id(LOCK_DOMAIN, DOMAIN, unique_id):
            return
        async_add_entities([AjaxLock(coordinator=coordinator, space_id=space_id, smart_lock_id=smart_lock_id)])
        _LOGGER.info("Dynamically added lock entity for smart lock: %s", smart_lock_id)

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_SMART_LOCK, _async_add_new_smart_lock))


class AjaxLock(CoordinatorEntity[AjaxDataCoordinator], LockEntity):
    """Representation of an Ajax smart lock (read-only)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        smart_lock_id: str,
    ) -> None:
        """Initialize the Ajax lock."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._smart_lock_id = smart_lock_id
        self._attr_unique_id = f"{smart_lock_id}_lock"
        self._attr_translation_key = "smart_lock"
        self._attr_name = None

    @property
    def is_locked(self) -> bool | None:
        """Return true if the lock is locked.

        Returns None if state is unknown (no event received yet).
        """
        smart_lock = self._get_smart_lock()
        if not smart_lock:
            return None
        return smart_lock.is_locked

    @property
    def is_locking(self) -> bool:
        """Return true if the lock is locking. Not applicable for read-only."""
        return False

    @property
    def is_unlocking(self) -> bool:
        """Return true if the lock is unlocking. Not applicable for read-only."""
        return False

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._get_smart_lock() is not None

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the lock. Not supported — read-only entity."""
        _LOGGER.warning("Locking is not supported for Ajax smart locks (no API available)")

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the lock. Not supported — read-only entity."""
        _LOGGER.warning("Unlocking is not supported for Ajax smart locks (no API available)")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        smart_lock = self._get_smart_lock()
        if not smart_lock:
            return {}

        attrs: dict[str, Any] = {}

        if smart_lock.last_changed_by:
            attrs["last_changed_by"] = smart_lock.last_changed_by
        if smart_lock.last_event_tag:
            attrs["last_event"] = smart_lock.last_event_tag
        if smart_lock.last_event_time:
            attrs["last_event_time"] = smart_lock.last_event_time.isoformat()

        return attrs

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information."""
        smart_lock = self._get_smart_lock()
        if not smart_lock:
            return None

        return DeviceInfo(
            identifiers={(DOMAIN, self._smart_lock_id)},
            name=smart_lock.name,
            manufacturer=MANUFACTURER,
            model="LockBridge Jeweller",
            via_device=(DOMAIN, self._space_id),
        )

    def _get_smart_lock(self) -> AjaxSmartLock | None:
        """Get the smart lock from coordinator data."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None
        return space.smart_locks.get(self._smart_lock_id)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
