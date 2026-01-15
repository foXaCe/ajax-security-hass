"""Ajax switch platform for Home Assistant (refactored).

This module creates switches for Ajax device settings using the device handler architecture.
Each device type has its own handler that defines which switches to create.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import DOMAIN, MANUFACTURER
from .coordinator import AjaxDataCoordinator
from .devices import (
    ButtonHandler,
    DoorbellHandler,
    DoorContactHandler,
    FloodDetectorHandler,
    GlassBreakHandler,
    MotionDetectorHandler,
    RepeaterHandler,
    SirenHandler,
    SmokeDetectorHandler,
    SocketHandler,
    WireInputHandler,
)
from .models import AjaxDevice, DeviceType, SecurityState

_LOGGER = logging.getLogger(__name__)

# Mapping of device types to handlers
DEVICE_HANDLERS = {
    DeviceType.MOTION_DETECTOR: MotionDetectorHandler,
    DeviceType.COMBI_PROTECT: MotionDetectorHandler,
    DeviceType.DOOR_CONTACT: DoorContactHandler,
    DeviceType.WIRE_INPUT: WireInputHandler,
    DeviceType.SMOKE_DETECTOR: SmokeDetectorHandler,
    DeviceType.FLOOD_DETECTOR: FloodDetectorHandler,
    DeviceType.GLASS_BREAK: GlassBreakHandler,
    DeviceType.SIREN: SirenHandler,
    DeviceType.TRANSMITTER: SirenHandler,
    DeviceType.MULTI_TRANSMITTER: SirenHandler,
    DeviceType.SOCKET: SocketHandler,
    DeviceType.RELAY: SocketHandler,
    DeviceType.WALLSWITCH: SocketHandler,
    DeviceType.BUTTON: ButtonHandler,
    DeviceType.DOORBELL: DoorbellHandler,
    DeviceType.REPEATER: RepeaterHandler,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax switches from a config entry."""
    coordinator = entry.runtime_data

    entities: list[SwitchEntity] = []

    # Create switches for each device using handlers
    for space_id, space in coordinator.account.spaces.items():
        for device_id, device in space.devices.items():
            handler_class = DEVICE_HANDLERS.get(device.type)
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
                        device.type.value,
                    )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax switch(es)", len(entities))


class AjaxSwitch(CoordinatorEntity[AjaxDataCoordinator], SwitchEntity):
    """Representation of an Ajax switch."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        device_id: str,
        switch_key: str,
        switch_desc: dict,
    ) -> None:
        """Initialize the Ajax switch."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._switch_key = switch_key
        self._switch_desc = switch_desc

        # Set unique ID
        self._attr_unique_id = f"{device_id}_{switch_key}"

        # Set entity name - use custom name if provided (for multi-gang channels),
        # otherwise use translation key
        if "name" in switch_desc:
            self._attr_name = switch_desc["name"]
            self._attr_translation_key = None
        else:
            self._attr_translation_key = switch_desc.get("translation_key", switch_key)
        self._attr_has_entity_name = True

        # Set icon if provided
        if "icon" in switch_desc:
            self._attr_icon = switch_desc["icon"]

        # Set enabled by default
        if "enabled_by_default" in switch_desc:
            self._attr_entity_registry_enabled_default = switch_desc[
                "enabled_by_default"
            ]

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        device = self._get_device()
        if not device:
            return None

        # Use value_fn from switch description
        value_fn = self._switch_desc.get("value_fn")
        if value_fn:
            try:
                return value_fn()
            except Exception as err:
                _LOGGER.error(
                    "Error getting value for switch %s: %s",
                    self._switch_key,
                    err,
                )
                return None
        return None

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        device = self._get_device()
        if not device:
            return False
        return device.online

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._set_value(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._set_value(False)

    async def _set_value(self, value: bool) -> None:
        """Set the switch value via API."""
        space = self.coordinator.get_space(self._space_id)
        device = self._get_device()
        if not space or not device:
            _LOGGER.error("Space or device not found for switch %s", self._switch_key)
            return

        # Handle Socket/Relay/WallSwitch using /command endpoint
        if device.type in (DeviceType.SOCKET, DeviceType.RELAY, DeviceType.WALLSWITCH):
            # Check if this is a multi-gang channel switch
            channel = self._switch_desc.get("channel")
            _LOGGER.debug(
                "Switch %s: key=%s, channel=%s, value=%s, switch_desc=%s",
                device.name,
                self._switch_key,
                channel,
                value,
                self._switch_desc,
            )
            if channel:
                await self._set_channel_value(space, device, channel, value)
                return

            # Standard single switch (Socket, Relay, single-gang WallSwitch)
            # Optimistic update
            old_value = device.attributes.get("is_on")
            device.attributes["is_on"] = value
            self.async_write_ha_state()

            try:
                # Use raw_type from API (exact device type like LIGHT_SWITCH_ONE_GANG)
                # instead of generic mapping (WALL_SWITCH, SOCKET, RELAY)
                device_type_str = device.raw_type
                _LOGGER.debug(
                    "Using device raw_type for command: %s (device: %s)",
                    device_type_str,
                    self._device_id,
                )

                await self.coordinator.api.async_set_switch_state(
                    space.hub_id,
                    self._device_id,
                    value,
                    device_type_str,
                )
                _LOGGER.info(
                    "Set %s state=%s for device %s via /command",
                    device_type_str,
                    "ON" if value else "OFF",
                    self._device_id,
                )
            except Exception as err:
                _LOGGER.error(
                    "Failed to set relay/socket state for device %s: %s",
                    self._device_id,
                    err,
                )
                # Revert optimistic update on error
                device.attributes["is_on"] = old_value
                self.async_write_ha_state()
                await self.coordinator.async_request_refresh()
            return

        api_key = self._switch_desc.get("api_key")
        if not api_key:
            _LOGGER.error("No api_key defined for switch %s", self._switch_key)
            return

        # Handle trigger-type switches (sirenTriggers list)
        trigger_key = self._switch_desc.get("trigger_key")
        if trigger_key:
            await self._set_trigger_value(space, device, trigger_key, value)
            return

        # Handle standard boolean or value-based switches
        if value:
            api_value = self._switch_desc.get("api_value_on", True)
            api_extra = self._switch_desc.get("api_extra", {})
        else:
            api_value = self._switch_desc.get("api_value_off", False)
            api_extra = self._switch_desc.get("api_extra_off", {})

        if space.security_state != SecurityState.DISARMED:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="system_armed",
            )

        # Optimistic update: update local state immediately
        # Map API key to attribute key (camelCase -> snake_case)
        attr_key_map = {
            "nightModeArm": "night_mode_arm",
            "alwaysActive": "always_active",
            "extraContactAware": "extra_contact_aware",
            "shockSensorAware": "shock_sensor_aware",
            "accelerometerAware": "accelerometer_aware",
            "ignoreSimpleImpact": "ignore_simple_impact",
            "beepOnArming": "beep_on_arming",
            "beepOnEntryDelay": "beep_on_entry_delay",
            "blinkWhileArmed": "blink_while_armed",
            "chimesEnabled": "chimes_enabled",
            "indicatorLightEnabled": "indicator_light_enabled",
        }
        attr_key = attr_key_map.get(api_key, api_key)
        old_value = device.attributes.get(attr_key)
        device.attributes[attr_key] = api_value
        self.async_write_ha_state()

        # Build the payload
        # Check if this setting needs to be nested (e.g., wiredDeviceSettings)
        api_nested_key = self._switch_desc.get("api_nested_key")
        if api_nested_key:
            payload = {api_nested_key: {api_key: api_value}}
        else:
            payload = {api_key: api_value}
        payload.update(api_extra)

        try:
            # Use nested update for settings inside nested structures (e.g., wiredDeviceSettings)
            if api_nested_key:
                await self.coordinator.api.async_update_device_nested(
                    space.hub_id, self._device_id, payload
                )
            else:
                await self.coordinator.api.async_update_device(
                    space.hub_id, self._device_id, payload
                )
            _LOGGER.info(
                "Set %s=%s for device %s",
                api_key,
                api_value,
                self._device_id,
            )
        except Exception as err:
            _LOGGER.error(
                "Failed to set %s for device %s: %s",
                api_key,
                self._device_id,
                err,
            )
            # Revert optimistic update on error
            device.attributes[attr_key] = old_value
            self.async_write_ha_state()
            await self.coordinator.async_request_refresh()

    async def _set_trigger_value(
        self, space, device, trigger_key: str, enabled: bool
    ) -> None:
        """Set a trigger value in the sirenTriggers list."""
        current_triggers = list(device.attributes.get("siren_triggers", []))

        if enabled and trigger_key not in current_triggers:
            current_triggers.append(trigger_key)
        elif not enabled and trigger_key in current_triggers:
            current_triggers.remove(trigger_key)

        # Optimistic update
        device.attributes["siren_triggers"] = current_triggers
        self.async_write_ha_state()

        try:
            await self.coordinator.api.async_update_device(
                space.hub_id, self._device_id, {"sirenTriggers": current_triggers}
            )
            _LOGGER.info(
                "Set sirenTriggers=%s for device %s",
                current_triggers,
                self._device_id,
            )
        except Exception as err:
            _LOGGER.error("Failed to set sirenTriggers: %s", err)
            await self.coordinator.async_request_refresh()

    async def _set_channel_value(
        self, space, device, channel: int, value: bool
    ) -> None:
        """Set a channel value for multi-gang LightSwitch devices."""
        # channel is 0-based (0, 1), but attribute keys are 1-based (channel_1_on, channel_2_on)
        attr_key = f"channel_{channel + 1}_on"
        old_value = device.attributes.get(attr_key)

        # Optimistic update
        device.attributes[attr_key] = value
        self.async_write_ha_state()

        try:
            device_type_str = device.raw_type
            await self.coordinator.api.async_set_channel_state(
                space.hub_id,
                self._device_id,
                channel,
                value,
                device_type_str,
            )
            _LOGGER.info(
                "Set %s channel %d=%s for device %s",
                device_type_str,
                channel,
                "ON" if value else "OFF",
                self._device_id,
            )
        except Exception as err:
            _LOGGER.error(
                "Failed to set channel %d for device %s: %s",
                channel,
                self._device_id,
                err,
            )
            # Revert optimistic update on error
            device.attributes[attr_key] = old_value
            self.async_write_ha_state()
            await self.coordinator.async_request_refresh()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        device = self._get_device()
        if not device:
            return {}

        return {
            "device_type": device.raw_type,
            "device_id": self._device_id,
        }

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        device = self._get_device()
        if not device:
            return {}

        info = {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": device.name,
            "manufacturer": MANUFACTURER,
            "model": device.raw_type,
            "via_device": (DOMAIN, self._space_id),
            "sw_version": device.firmware_version,
            "hw_version": device.hardware_version,
        }
        # Auto-assign device to HA Area based on Ajax room
        if device.room_name:
            info["suggested_area"] = device.room_name
        return info

    def _get_device(self) -> AjaxDevice | None:
        """Get the device from coordinator data."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None
        return space.devices.get(self._device_id)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
