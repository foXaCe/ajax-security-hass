"""Ajax valve platform for Home Assistant.

This module creates valve entities for Ajax WaterStop devices.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.valve import DOMAIN as VALVE_DOMAIN, ValveEntity, ValveEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from ._discovery import connect_new_entity_signal
from ._ids import device_identifier
from .const import DOMAIN, MANUFACTURER, SIGNAL_NEW_DEVICE
from .coordinator import AjaxDataCoordinator
from .devices import WaterStopHandler
from .models import AjaxDevice, DeviceType

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1

# Mapping of device types to handlers
DEVICE_HANDLERS = {
    DeviceType.WATERSTOP: WaterStopHandler,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax valves from a config entry."""
    coordinator = entry.runtime_data

    if coordinator.account is None:
        return

    entities: list[ValveEntity] = []

    # Create valves for each device using handlers
    for space_id, space in coordinator.account.spaces.items():
        for device_id, device in space.devices.items():
            handler_class = DEVICE_HANDLERS.get(device.type)
            if handler_class:
                handler = handler_class(device)
                valves = handler.get_valves()

                for valve_desc in valves:
                    entities.append(
                        AjaxValve(
                            coordinator=coordinator,
                            space_id=space_id,
                            device_id=device_id,
                            valve_key=valve_desc["key"],
                            valve_desc=valve_desc,
                        )
                    )
                    _LOGGER.debug(
                        "Created valve '%s' for device: %s (type: %s)",
                        valve_desc["key"],
                        device.name,
                        device.type.value,
                    )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax valve(s)", len(entities))

    def _build_valves(space_id: str, device_id: str) -> list[tuple[str, ValveEntity]]:
        """Build valve entities for a newly-discovered WaterStop device."""
        space = coordinator.get_space(space_id)
        device = space.devices.get(device_id) if space else None
        if not device:
            return []

        handler_class = DEVICE_HANDLERS.get(device.type)
        if not handler_class:
            return []

        handler = handler_class(device)
        return [
            (
                f"{device_id}_{valve_desc['key']}",
                AjaxValve(
                    coordinator=coordinator,
                    space_id=space_id,
                    device_id=device_id,
                    valve_key=valve_desc["key"],
                    valve_desc=valve_desc,
                ),
            )
            for valve_desc in handler.get_valves()
        ]

    connect_new_entity_signal(
        hass,
        entry,
        SIGNAL_NEW_DEVICE,
        VALVE_DOMAIN,
        async_add_entities,
        _build_valves,
        label="valve entit(ies)",
    )


class AjaxValve(CoordinatorEntity[AjaxDataCoordinator], ValveEntity):
    """Representation of an Ajax valve (WaterStop)."""

    _attr_has_entity_name = True
    _attr_supported_features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE
    _attr_reports_position = False

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        device_id: str,
        valve_key: str,
        valve_desc: dict[str, Any],
    ) -> None:
        """Initialize the Ajax valve."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._valve_key = valve_key
        self._valve_desc = valve_desc

        # Set unique ID
        self._attr_unique_id = f"{self.coordinator.entry_id}_{device_id}_{valve_key}"

        # Set translation key
        self._attr_translation_key = valve_desc.get("translation_key", valve_key)

        # Set enabled by default
        if "enabled_by_default" in valve_desc:
            self._attr_entity_registry_enabled_default = valve_desc["enabled_by_default"]

    @property
    def is_open(self) -> bool | None:
        """Return true if the valve is open."""
        device = self._get_device()
        if not device:
            return None

        value_fn = self._valve_desc.get("value_fn")
        if value_fn:
            try:
                return value_fn()  # type: ignore[no-any-return]
            except Exception as err:
                _LOGGER.error(
                    "Error getting value for valve %s: %s",
                    self._valve_key,
                    err,
                )
                return None
        return None

    @property
    def is_closed(self) -> bool | None:
        """Return true if the valve is closed."""
        is_open = self.is_open
        if is_open is None:
            return None
        return not is_open

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if not self.coordinator.last_update_success:
            return False
        device = self._get_device()
        if not device:
            return False
        return device.online

    async def async_open_valve(self, **kwargs: Any) -> None:
        """Open the valve."""
        await self._set_valve_state(open_valve=True)

    async def async_close_valve(self, **kwargs: Any) -> None:
        """Close the valve."""
        await self._set_valve_state(open_valve=False)

    async def _set_valve_state(self, open_valve: bool) -> None:
        """Set the valve state via API."""
        space = self.coordinator.get_space(self._space_id)
        device = self._get_device()
        if not space or not device:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="device_not_found")

        if not space.hub_id:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="hub_not_found")

        # Optimistic update. mark_optimistic protects ``valveState`` from being
        # overwritten by a poll arriving before the valve reports its new state.
        old_value = device.attributes.get("valveState")
        device.attributes["valveState"] = "OPEN" if open_valve else "CLOSED"
        device.mark_optimistic("valveState", 15.0)
        self.async_write_ha_state()

        try:
            # Use the WaterStop command endpoint
            await self.coordinator.api.async_set_waterstop_state(
                space.hub_id,
                self._device_id,
                open_valve,
            )
            _LOGGER.info(
                "Set WaterStop valve=%s for device %s",
                "OPEN" if open_valve else "CLOSED",
                self._device_id,
            )
        except Exception as err:
            _LOGGER.error(
                "Failed to set valve state for device %s: %s",
                self._device_id,
                err,
            )
            # Revert optimistic update on error (drop key if previously unset) and
            # clear the guard so the next poll can correct the restored value.
            if old_value is None:
                device.attributes.pop("valveState", None)
            else:
                device.attributes["valveState"] = old_value
            device.attributes.get("_optimistic_attrs", {}).pop("valveState", None)
            self.async_write_ha_state()
            await self.coordinator.async_request_refresh()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_to_change",
                translation_placeholders={"entity": self._valve_key, "error": str(err)},
            ) from err

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        device = self._get_device()
        if not device:
            return {}

        attrs = {
            "device_type": device.raw_type,
            "device_id": self._device_id,
        }

        # Add motor state if available
        motor_state = device.attributes.get("motorState")
        if isinstance(motor_state, str) and motor_state:
            attrs["motor_state"] = motor_state.lower()

        return attrs

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information."""
        device = self._get_device()
        if not device:
            return None

        return DeviceInfo(
            identifiers={device_identifier(self.coordinator.entry_id, self._device_id)},
            name=device.name,
            manufacturer=MANUFACTURER,
            model="WaterStop",
            via_device=device_identifier(self.coordinator.entry_id, self._space_id),
            sw_version=device.attributes.get("firmwareVersion"),
            suggested_area=device.room_name,
        )

    def _get_device(self) -> AjaxDevice | None:
        """Get the device from coordinator data."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None
        return space.devices.get(self._device_id)
