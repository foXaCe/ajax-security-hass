"""Ajax alarm control panel platform."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import DOMAIN, MANUFACTURER
from .coordinator import AjaxDataCoordinator
from .models import GroupState, SecurityState

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax alarm control panels from a config entry."""
    coordinator = entry.runtime_data

    entities: list[AlarmControlPanelEntity] = []

    if coordinator.account:
        for space_id, space in coordinator.account.spaces.items():
            # Create main alarm control panel for the space (hub)
            entities.append(AjaxAlarmControlPanel(coordinator, entry, space_id))

            # Create alarm control panel for each group if groups mode is enabled
            if space.group_mode_enabled and space.groups:
                for group_id, _group in space.groups.items():
                    entities.append(AjaxGroupAlarmControlPanel(coordinator, entry, space_id, group_id))
                _LOGGER.info(
                    "Added %d group alarm panels for space %s",
                    len(space.groups),
                    space.name,
                )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax alarm control panel(s)", len(entities))
    else:
        _LOGGER.info("No Ajax spaces found, no alarm panels created (yet)")


class AjaxAlarmControlPanel(CoordinatorEntity[AjaxDataCoordinator], AlarmControlPanelEntity):
    """Representation of an Ajax alarm control panel (one per space/hub).

    Note: This entity overrides the default availability behavior to always
    remain available, even during temporary API failures. This prevents
    automations from being triggered incorrectly when the entity transitions
    from "unavailable" back to its normal state.
    """

    __slots__ = ("_entry", "_space_id", "_device_info_updated")

    _attr_has_entity_name = True
    _attr_name = None  # Use device name as entity name (main entity)
    # Ajax exposes partial arming only at group level (see AjaxGroupAlarmControlPanel),
    # so the space-level panel only advertises AWAY and NIGHT.
    _attr_supported_features = AlarmControlPanelEntityFeature.ARM_AWAY | AlarmControlPanelEntityFeature.ARM_NIGHT
    _attr_code_arm_required = False
    _attr_available = True  # Always available - keep last known state on API errors

    def __init__(self, coordinator: AjaxDataCoordinator, entry: AjaxConfigEntry, space_id: str) -> None:
        """Initialize the alarm control panel."""
        super().__init__(coordinator)
        self._entry = entry
        self._space_id = space_id
        self._attr_unique_id = f"{entry.entry_id}_alarm_{space_id}"

    def _build_hub_info(self) -> tuple[str, str | None, str | None] | None:
        """Extract model name, sw_version, hw_version from hub details.

        Returns (model_name, sw_version, hw_version) or None if no space.
        """
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None

        hub_subtype = space.hub_details.get("hubSubtype", "Security Hub") if space.hub_details else "Security Hub"
        hub_subtype_formatted = hub_subtype.replace("_", " ").title()

        hub_color = space.hub_details.get("color", "") if space.hub_details else ""
        color_name = str(hub_color).title() if hub_color else ""

        model_name = f"{hub_subtype_formatted} ({color_name})" if color_name else hub_subtype_formatted

        sw_version: str | None = None
        hw_version: str | None = None

        if space.hub_details and space.hub_details.get("firmware"):
            firmware = space.hub_details["firmware"]
            if firmware.get("version"):
                sw_version = firmware["version"]

        if space.hub_details and space.hub_details.get("hardwareVersions"):
            pcb_version = space.hub_details["hardwareVersions"].get("pcb")
            if pcb_version:
                hw_version = f"PCB rev.{pcb_version}"

        return model_name, sw_version, hw_version

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None

        hub_info = self._build_hub_info()
        model_name, sw_version, hw_version = hub_info if hub_info else ("Security Hub", None, None)

        return DeviceInfo(
            identifiers={(DOMAIN, self._space_id)},
            name=space.name,
            manufacturer=MANUFACTURER,
            model=model_name,
            sw_version=sw_version,
            hw_version=hw_version,
        )

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        """Return the state of the alarm."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None

        # Map Ajax SecurityState to Home Assistant AlarmControlPanelState
        state_map = {
            SecurityState.DISARMED: AlarmControlPanelState.DISARMED,
            SecurityState.ARMED: AlarmControlPanelState.ARMED_AWAY,
            SecurityState.NIGHT_MODE: AlarmControlPanelState.ARMED_NIGHT,
            SecurityState.PARTIALLY_ARMED: AlarmControlPanelState.ARMED_HOME,
            SecurityState.AWAITING_EXIT_TIMER: AlarmControlPanelState.ARMING,
            SecurityState.AWAITING_CONFIRMATION: AlarmControlPanelState.PENDING,
            SecurityState.ARMING_INCOMPLETE: AlarmControlPanelState.ARMING,
            SecurityState.TRIGGERED: AlarmControlPanelState.TRIGGERED,
        }

        # Unknown states return None (HA displays "unknown") rather than
        # a misleading DISARMED default for an alarm panel.
        return state_map.get(space.security_state)

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Send disarm command."""
        _LOGGER.info("Disarming Ajax alarm for space %s", self._space_id)

        # Optimistic update - change state immediately
        space = self.coordinator.get_space(self._space_id)
        if space:
            space.security_state = SecurityState.DISARMED
            self.async_write_ha_state()

        try:
            await self.coordinator.async_disarm_space(self._space_id)
        except Exception as err:
            _LOGGER.error("Failed to disarm: %s", err)
            # Revert on error - refresh from API with cache bypass
            await self.coordinator.async_request_refresh_bypass_cache()
            raise

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Send arm away command."""
        _LOGGER.info("Arming Ajax alarm (away) for space %s", self._space_id)

        # Optimistic update - change state immediately
        space = self.coordinator.get_space(self._space_id)
        if space:
            space.security_state = SecurityState.ARMED
            self.async_write_ha_state()

        try:
            await self.coordinator.async_arm_space(self._space_id)
        except Exception as err:
            _LOGGER.error("Failed to arm: %s", err)
            # Revert on error - refresh from API with cache bypass
            await self.coordinator.async_request_refresh_bypass_cache()
            raise

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        """Send arm night command."""
        _LOGGER.info("Arming Ajax alarm (night) for space %s", self._space_id)

        # Optimistic update - change state immediately
        space = self.coordinator.get_space(self._space_id)
        if space:
            space.security_state = SecurityState.NIGHT_MODE
            self.async_write_ha_state()

        try:
            await self.coordinator.async_arm_night_mode(self._space_id)
        except Exception as err:
            _LOGGER.error("Failed to arm night mode: %s", err)
            # Revert on error - refresh from API with cache bypass
            await self.coordinator.async_request_refresh_bypass_cache()
            raise

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass, update device info in registry."""
        await super().async_added_to_hass()
        self._update_device_registry()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Update device registry once on first update (for existing entities)
        if not getattr(self, "_device_info_updated", False):
            self._device_info_updated = True
            self._update_device_registry()
        self.async_write_ha_state()

    def _update_device_registry(self) -> None:
        """Update hub device info in registry."""
        hub_info = self._build_hub_info()
        if not hub_info:
            _LOGGER.debug("No space or hub_details for %s", self._space_id)
            return

        device_registry = dr.async_get(self.hass)
        device_entry = device_registry.async_get_device(identifiers={(DOMAIN, self._space_id)})
        if not device_entry:
            _LOGGER.debug("No device entry found for %s", self._space_id)
            return

        model_name, sw_version, hw_version = hub_info

        _LOGGER.info(
            "Updating hub device: model=%s, sw=%s, hw=%s",
            model_name,
            sw_version,
            hw_version,
        )

        device_registry.async_update_device(
            device_entry.id,
            model=model_name,
            sw_version=sw_version,
            hw_version=hw_version,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return {}

        attributes: dict[str, Any] = {
            "space_id": space.id,
            "space_name": space.name,
            "hub_id": space.hub_id,
            "unread_notifications": space.unread_notifications,
            "total_devices": len(space.devices),
            "online_devices": len(space.get_online_devices()),
            "devices_with_malfunctions": len(space.get_devices_with_malfunctions()),
            "bypassed_devices": len(space.get_bypassed_devices()),
        }

        # Add changed_by attribute from latest arm/disarm notification
        if space.notifications:
            for notification in space.notifications:
                # Check for arm/disarm events
                if notification.title in [
                    "armed",
                    "disarmed",
                    "night_mode_on",
                    "partially_armed",
                ]:
                    if notification.user_name:
                        attributes["changed_by"] = notification.user_name
                    break

        # Add room information
        if space.rooms:
            attributes["rooms"] = {
                room_id: {
                    "name": room.name,
                    "device_count": len(room.device_ids),
                }
                for room_id, room in space.rooms.items()
            }

        return attributes


class AjaxGroupAlarmControlPanel(CoordinatorEntity[AjaxDataCoordinator], AlarmControlPanelEntity):
    """Representation of an Ajax group alarm control panel.

    Each group in the Ajax system gets its own alarm control panel entity,
    allowing users to arm/disarm individual groups independently.
    """

    __slots__ = ("_entry", "_space_id", "_group_id")

    _attr_has_entity_name = True
    _attr_supported_features = AlarmControlPanelEntityFeature.ARM_AWAY
    _attr_code_arm_required = False
    _attr_available = True  # Always available - keep last known state on API errors

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        entry: AjaxConfigEntry,
        space_id: str,
        group_id: str,
    ) -> None:
        """Initialize the group alarm control panel."""
        super().__init__(coordinator)
        self._entry = entry
        self._space_id = space_id
        self._group_id = group_id
        self._attr_unique_id = f"{entry.entry_id}_group_alarm_{group_id}"

        # Get initial group data for name
        group = coordinator.get_group(space_id, group_id)
        self._attr_name = group.name if group else "Group"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information - link to the hub device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._space_id)},
        )

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        """Return the state of the group alarm."""
        group = self.coordinator.get_group(self._space_id, self._group_id)
        if not group:
            return None

        # Map GroupState to Home Assistant AlarmControlPanelState
        state_map = {
            GroupState.ARMED: AlarmControlPanelState.ARMED_AWAY,
            GroupState.DISARMED: AlarmControlPanelState.DISARMED,
            GroupState.NONE: AlarmControlPanelState.DISARMED,
        }

        return state_map.get(group.state)

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Send disarm command for this group."""
        _LOGGER.info("Disarming Ajax group %s in space %s", self._group_id, self._space_id)

        # Optimistic update
        group = self.coordinator.get_group(self._space_id, self._group_id)
        if group:
            group.state = GroupState.DISARMED
            self.async_write_ha_state()

        try:
            await self.coordinator.async_disarm_group(self._space_id, self._group_id)
        except Exception as err:
            _LOGGER.error("Failed to disarm group: %s", err)
            await self.coordinator.async_request_refresh_bypass_cache()
            raise

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Send arm command for this group."""
        _LOGGER.info("Arming Ajax group %s in space %s", self._group_id, self._space_id)

        # Optimistic update
        group = self.coordinator.get_group(self._space_id, self._group_id)
        if group:
            group.state = GroupState.ARMED
            self.async_write_ha_state()

        try:
            await self.coordinator.async_arm_group(self._space_id, self._group_id)
        except Exception as err:
            _LOGGER.error("Failed to arm group: %s", err)
            await self.coordinator.async_request_refresh_bypass_cache()
            raise

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        group = self.coordinator.get_group(self._space_id, self._group_id)
        if not group:
            return {}

        return {
            "group_id": group.id,
            "group_name": group.name,
            "space_id": self._space_id,
            "bulk_arm_involved": group.bulk_arm_involved,
            "bulk_disarm_involved": group.bulk_disarm_involved,
        }
