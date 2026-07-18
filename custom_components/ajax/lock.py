"""Ajax lock platform for Home Assistant.

This module creates lock entities for Ajax LockBridge Jeweller devices.
State comes from the enriched device record (lockStatus / doorStatus) and
real-time SSE/SQS events. Locking and unlocking are supported via the
dedicated Ajax ``LOCK_SMART_LOCK`` / ``UNLOCK_SMART_LOCK`` commands. A
successful command also applies an optimistic state update (#88) so HA
reflects it immediately instead of waiting for the next poll.

Note: Yale cloud locks are automatically filtered out in the coordinator
since they don't send SSE events and only return minimal API data.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN, LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from ._discovery import connect_new_entity_signal
from ._ids import device_identifier
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

    def _build_lock(space_id: str, smart_lock_id: str) -> list[tuple[str, LockEntity]]:
        """Build the lock entity for a newly-discovered smart lock."""
        return [
            (
                f"{smart_lock_id}_lock",
                AjaxLock(coordinator=coordinator, space_id=space_id, smart_lock_id=smart_lock_id),
            )
        ]

    connect_new_entity_signal(
        hass,
        entry,
        SIGNAL_NEW_SMART_LOCK,
        LOCK_DOMAIN,
        async_add_entities,
        _build_lock,
        label="lock entit(ies)",
    )


class AjaxLock(CoordinatorEntity[AjaxDataCoordinator], LockEntity):
    """Representation of an Ajax smart lock."""

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
        self._attr_unique_id = f"{self.coordinator.entry_id}_{smart_lock_id}_lock"
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
    def available(self) -> bool:
        """Return if entity is available."""
        if not self.coordinator.last_update_success:
            return False
        return self._get_smart_lock() is not None

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the lock via the Ajax ``LOCK_SMART_LOCK`` command."""
        await self._async_send_lock_command("LOCK_SMART_LOCK", "lock")

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the lock via the Ajax ``UNLOCK_SMART_LOCK`` command."""
        await self._async_send_lock_command("UNLOCK_SMART_LOCK", "unlock")

    async def _async_send_lock_command(self, command: str, verb: str) -> None:
        """Send a lock/unlock command to the smart lock.

        On success, applies an optimistic state update so HA reflects the
        command immediately instead of waiting for the next poll / event.
        """
        space = self.coordinator.get_space(self._space_id)
        smart_lock = self._get_smart_lock()
        if not space or not smart_lock:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="device_not_found")
        if not space.hub_id:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="hub_not_found")

        device_type = smart_lock.raw_data.get("deviceType") or "SmartLock"
        try:
            await self.coordinator.api.async_send_device_command(
                space.hub_id, self._smart_lock_id, command, device_type
            )
        except Exception as err:
            _LOGGER.error("Failed to %s %s: %s", verb, smart_lock.name, err)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_to_change",
                translation_placeholders={"entity": smart_lock.name, "error": str(err)},
            ) from err

        # Optimistic update (#88): the physical lock reacts immediately but no
        # realtime event arrives while disarmed, so without this the HA state
        # waits for the next poll. Stamping last_event_time reuses the existing
        # 30s freshness guard in _apply_smart_lock_rest_state, so a stale REST
        # payload cannot bounce the state back; a real SSE/SQS event or the
        # first poll after the window remains the final authority.
        smart_lock.is_locked = command == "LOCK_SMART_LOCK"
        smart_lock.last_event_tag = f"{verb}_command"
        smart_lock.last_event_time = datetime.now(UTC)
        self.async_write_ha_state()

        await self.coordinator.async_request_refresh()

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
            identifiers={device_identifier(self.coordinator.entry_id, self._smart_lock_id)},
            name=smart_lock.name,
            manufacturer=MANUFACTURER,
            model="LockBridge Jeweller",
            via_device=device_identifier(self.coordinator.entry_id, self._space_id),
        )

    def _get_smart_lock(self) -> AjaxSmartLock | None:
        """Get the smart lock from coordinator data."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None
        return space.smart_locks.get(self._smart_lock_id)
