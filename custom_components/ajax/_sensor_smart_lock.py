"""Smart-lock Ajax sensors (battery, signal).

``AjaxSmartLockSensor``. Split out of ``sensor.py`` (platform module keeps
only ``async_setup_entry`` + re-exports).
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorEntity,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._ids import device_identifier
from .const import MANUFACTURER
from .coordinator import AjaxDataCoordinator
from .models import (
    AjaxSmartLock,
)

_LOGGER = logging.getLogger(__name__)


class AjaxSmartLockSensor(CoordinatorEntity[AjaxDataCoordinator], SensorEntity):
    """Sensor for Ajax smart lock - shows who last locked/unlocked."""

    _attr_has_entity_name = True
    _attr_translation_key = "smart_lock_last_changed_by"

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        smart_lock_id: str,
    ) -> None:
        """Initialize the smart lock sensor."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._smart_lock_id = smart_lock_id
        self._attr_unique_id = f"{self.coordinator.entry_id}_{smart_lock_id}_last_changed_by"

    @property
    def native_value(self) -> str | None:
        """Return who last locked/unlocked the smart lock."""
        smart_lock = self._get_smart_lock()
        if not smart_lock:
            return None
        return smart_lock.last_changed_by

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not self.coordinator.last_update_success:
            return False
        return self._get_smart_lock() is not None

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information (same device as the lock entity)."""
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
