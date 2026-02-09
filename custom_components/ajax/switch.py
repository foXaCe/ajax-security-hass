"""Ajax switch platform for Home Assistant (refactored).

This module creates switches for Ajax device settings using the device handler architecture.
Each device type has its own handler that defines which switches to create.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
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
    LifeQualityHandler,
    LightSwitchHandler,
    ManualCallPointHandler,
    MotionDetectorHandler,
    RepeaterHandler,
    SirenHandler,
    SmokeDetectorHandler,
    SocketHandler,
    TransmitterHandler,
    WireInputHandler,
)
from .models import AjaxDevice, DeviceType, SecurityState

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1

# Mapping of device types to handlers (excluding dimmer - handled separately)
DEVICE_HANDLERS = {
    DeviceType.MOTION_DETECTOR: MotionDetectorHandler,
    DeviceType.COMBI_PROTECT: MotionDetectorHandler,
    DeviceType.DOOR_CONTACT: DoorContactHandler,
    DeviceType.WIRE_INPUT: WireInputHandler,
    DeviceType.SMOKE_DETECTOR: SmokeDetectorHandler,
    DeviceType.FLOOD_DETECTOR: FloodDetectorHandler,
    DeviceType.MANUAL_CALL_POINT: ManualCallPointHandler,
    DeviceType.GLASS_BREAK: GlassBreakHandler,
    DeviceType.SIREN: SirenHandler,
    DeviceType.SPEAKERPHONE: SirenHandler,
    DeviceType.TRANSMITTER: TransmitterHandler,
    DeviceType.MULTI_TRANSMITTER: SirenHandler,
    DeviceType.KEYPAD: SirenHandler,
    DeviceType.SOCKET: SocketHandler,
    DeviceType.RELAY: SocketHandler,
    DeviceType.WALLSWITCH: SocketHandler,
    DeviceType.BUTTON: ButtonHandler,
    DeviceType.REMOTE_CONTROL: ButtonHandler,
    DeviceType.DOORBELL: DoorbellHandler,
    DeviceType.REPEATER: RepeaterHandler,
    DeviceType.LIFE_QUALITY: LifeQualityHandler,
}

# LightSwitchDimmer switch definitions (settingsSwitch-based)
DIMMER_SETTINGS_SWITCHES = [
    {
        "key": "led_indicator",
        "translation_key": "led_indicator",
        "settings_key": "LED_INDICATOR_ENABLED",
    },
    {
        "key": "child_lock",
        "translation_key": "child_lock",
        "settings_key": "CHILD_LOCK_ENABLED",
    },
    {
        "key": "state_memory",
        "translation_key": "state_memory",
        "settings_key": "STATE_MEMORY_ENABLED",
    },
    {
        "key": "current_threshold",
        "translation_key": "current_threshold",
        "settings_key": "CURRENT_THRESHOLD_ENABLED",
    },
]


def is_dimmer_device(device: AjaxDevice) -> bool:
    """Check if device is a LightSwitchDimmer."""
    raw_type = (device.raw_type or "").lower().replace("_", "").replace(" ", "")
    return "lightswitchdimmer" in raw_type or raw_type == "dimmer"


def is_lightswitch_device(device: AjaxDevice) -> bool:
    """Check if device is a LightSwitch (non-dimmer)."""
    raw_type = (device.raw_type or "").lower().replace("_", "").replace(" ", "")
    # Match LightSwitchTwoWay, LightSwitchTwoGang, LightSwitchTwoChannelTwoWay, etc.
    return "lightswitch" in raw_type and "dimmer" not in raw_type


def get_device_handler(device: AjaxDevice):
    """Get the appropriate handler for a device.

    Dimmer devices are handled separately, not via handlers.
    """
    # Dimmer devices are handled separately
    if is_dimmer_device(device):
        return None

    # Standard mapping
    return DEVICE_HANDLERS.get(device.type)


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
                handler = handler_class(device)  # type: ignore[abstract]
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


class AjaxSwitch(CoordinatorEntity[AjaxDataCoordinator], SwitchEntity):
    """Representation of an Ajax switch."""

    __slots__ = ("_space_id", "_device_id", "_switch_key", "_switch_desc")

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

        # Set entity category if provided
        self._attr_entity_category = switch_desc.get("entity_category", EntityCategory.CONFIG)

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
            self._attr_entity_registry_enabled_default = switch_desc["enabled_by_default"]

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

        if not space.hub_id:
            _LOGGER.error("Hub ID not found for space %s", self._space_id)
            return

        # Handle Socket/Relay/WallSwitch using /command endpoint
        # BUT only if this is NOT a configuration switch (no api_key means it's the main on/off switch)
        if device.type in (DeviceType.SOCKET, DeviceType.RELAY, DeviceType.WALLSWITCH) and not self._switch_desc.get(
            "api_key"
        ):
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
            if channel is not None:
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
                device_type_str = device.raw_type or device.type.value
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

        # Handle settings-type switches (settingsSwitch list for LightSwitch)
        settings_key = self._switch_desc.get("settings_key")
        if settings_key:
            await self._set_settings_value(space, device, settings_key, value)
            return

        # Handle standard boolean or value-based switches
        if value:
            api_value = self._switch_desc.get("api_value_on", True)
            api_extra = self._switch_desc.get("api_extra", {})
        else:
            api_value = self._switch_desc.get("api_value_off", False)
            api_extra = self._switch_desc.get("api_extra_off", {})

        # Check security state unless bypass_security_check is set
        # (for device config switches like FireProtect alarm settings)
        if not self._switch_desc.get("bypass_security_check", False) and space.security_state != SecurityState.DISARMED:
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
            "externalContactAlwaysActive": "external_contact_always_active",
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
                await self.coordinator.api.async_update_device_nested(space.hub_id, self._device_id, payload)
            else:
                await self.coordinator.api.async_update_device(space.hub_id, self._device_id, payload)
            _LOGGER.info(
                "Set %s=%s for device %s",
                api_key,
                api_value,
                self._device_id,
            )
            # Notify other entities that depend on this attribute
            self.coordinator.async_update_listeners()
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

    async def _set_trigger_value(self, space, device, trigger_key: str, enabled: bool) -> None:
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

    async def _set_settings_value(self, space, device, settings_key: str, enabled: bool) -> None:
        """Set a settings value in the settingsSwitch list (for LightSwitch devices)."""
        current_settings = list(device.attributes.get("settingsSwitch", []))

        if enabled and settings_key not in current_settings:
            current_settings.append(settings_key)
        elif not enabled and settings_key in current_settings:
            current_settings.remove(settings_key)

        # Optimistic update
        device.attributes["settingsSwitch"] = current_settings
        self.async_write_ha_state()

        try:
            await self.coordinator.api.async_update_device(
                space.hub_id, self._device_id, {"settingsSwitch": current_settings}
            )
            _LOGGER.info(
                "Set settingsSwitch=%s for device %s",
                current_settings,
                self._device_id,
            )
        except Exception as err:
            _LOGGER.error("Failed to set settingsSwitch: %s", err)
            await self.coordinator.async_request_refresh()

    async def _set_channel_value(self, space, device, channel: int, value: bool) -> None:
        """Set a channel value for multi-gang LightSwitch devices."""
        import time

        # channel is 0-based (0, 1), but attribute keys are 1-based (channel_1_on, channel_2_on)
        attr_key = f"channel_{channel + 1}_on"
        old_value = device.attributes.get(attr_key)

        # Optimistic update - also update channelStatuses to keep in sync
        device.attributes[attr_key] = value
        channel_str_on = f"CHANNEL_{channel + 1}_ON"
        current_statuses = list(device.attributes.get("channelStatuses", []))
        if value and channel_str_on not in current_statuses:
            current_statuses.append(channel_str_on)
        elif not value and channel_str_on in current_statuses:
            current_statuses.remove(channel_str_on)
        device.attributes["channelStatuses"] = current_statuses

        # Mark device as having pending optimistic update (prevent polling overwrite)
        # Use 15 seconds to give enough time for the device to report state
        device.attributes["_optimistic_until"] = time.time() + 15.0

        self.async_write_ha_state()
        # Notify other channel switches of state change
        self.coordinator.async_update_listeners()

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
            # Revert channelStatuses
            if value and channel_str_on in current_statuses:
                current_statuses.remove(channel_str_on)
            elif not value and channel_str_on not in current_statuses:
                current_statuses.append(channel_str_on)
            device.attributes["channelStatuses"] = current_statuses
            device.attributes.pop("_optimistic_until", None)
            self.async_write_ha_state()
            self.coordinator.async_update_listeners()

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
    def device_info(self) -> DeviceInfo | None:
        """Return device information."""
        device = self._get_device()
        if not device:
            return None

        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=device.name,
            manufacturer=MANUFACTURER,
            model=device.raw_type,
            via_device=(DOMAIN, self._space_id),
            sw_version=device.firmware_version,
            hw_version=device.hardware_version,
            suggested_area=device.room_name,
        )

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


class AjaxDimmerSettingsSwitch(CoordinatorEntity[AjaxDataCoordinator], SwitchEntity):
    """Switch entity for LightSwitchDimmer settings (settingsSwitch list)."""

    __slots__ = ("_space_id", "_device_id", "_switch_def")

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        device_id: str,
        switch_def: dict,
    ) -> None:
        """Initialize the dimmer settings switch."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._switch_def = switch_def

        self._attr_unique_id = f"{device_id}_{switch_def['key']}"
        self._attr_translation_key = switch_def["translation_key"]

    def _get_device(self) -> AjaxDevice | None:
        """Get current device from coordinator."""
        space = self.coordinator.get_space(self._space_id)
        return space.devices.get(self._device_id) if space else None

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        device = self._get_device()
        return self.coordinator.last_update_success and device is not None and device.online

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(identifiers={(DOMAIN, self._device_id)})

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        device = self._get_device()
        if not device:
            return False
        settings_list = device.attributes.get("settingsSwitch", [])
        return self._switch_def["settings_key"] in settings_list

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from coordinator."""
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._set_value(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._set_value(False)

    async def _set_value(self, value: bool) -> None:
        """Set the switch value."""
        space = self.coordinator.get_space(self._space_id)
        device = self._get_device()
        if not space or not device:
            raise HomeAssistantError("Device not found")

        if not space.hub_id:
            raise HomeAssistantError("Hub not found")

        settings_key = self._switch_def["settings_key"]
        old_settings = list(device.attributes.get("settingsSwitch", []))
        new_settings = list(old_settings)

        if value and settings_key not in new_settings:
            new_settings.append(settings_key)
        elif not value and settings_key in new_settings:
            new_settings.remove(settings_key)

        # Optimistic update
        device.attributes["settingsSwitch"] = new_settings
        self.async_write_ha_state()

        try:
            await self.coordinator.api.async_update_device(
                space.hub_id, self._device_id, {"settingsSwitch": new_settings}
            )
            _LOGGER.info(
                "Set settingsSwitch=%s for device %s",
                new_settings,
                self._device_id,
            )
        except Exception as err:
            _LOGGER.error("Failed to set settingsSwitch: %s", err)
            # Rollback optimistic update
            device.attributes["settingsSwitch"] = old_settings
            self.async_write_ha_state()
            await self.coordinator.async_request_refresh()


class AjaxDimmerBoolSwitch(CoordinatorEntity[AjaxDataCoordinator], SwitchEntity):
    """Switch entity for LightSwitchDimmer boolean attributes."""

    __slots__ = ("_space_id", "_device_id", "_attr_key", "_api_key")

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        device_id: str,
        switch_key: str,
        attr_key: str,
        api_key: str,
    ) -> None:
        """Initialize the dimmer boolean switch."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._attr_key = attr_key
        self._api_key = api_key

        self._attr_unique_id = f"{device_id}_{switch_key}"
        self._attr_translation_key = switch_key

    def _get_device(self) -> AjaxDevice | None:
        """Get current device from coordinator."""
        space = self.coordinator.get_space(self._space_id)
        return space.devices.get(self._device_id) if space else None

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        device = self._get_device()
        return self.coordinator.last_update_success and device is not None and device.online

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(identifiers={(DOMAIN, self._device_id)})

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        device = self._get_device()
        if not device:
            return False
        return device.attributes.get(self._attr_key, False)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from coordinator."""
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._set_value(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._set_value(False)

    async def _set_value(self, value: bool) -> None:
        """Set the switch value."""
        space = self.coordinator.get_space(self._space_id)
        device = self._get_device()
        if not space or not device:
            raise HomeAssistantError("Device not found")

        if not space.hub_id:
            raise HomeAssistantError("Hub not found")

        # Optimistic update with rollback
        old_value = device.attributes.get(self._attr_key)
        device.attributes[self._attr_key] = value
        self.async_write_ha_state()

        try:
            await self.coordinator.api.async_update_device(space.hub_id, self._device_id, {self._api_key: value})
            _LOGGER.info(
                "Set %s=%s for device %s",
                self._api_key,
                value,
                self._device_id,
            )
        except Exception as err:
            _LOGGER.error("Failed to set %s: %s", self._api_key, err)
            # Rollback optimistic update
            device.attributes[self._attr_key] = old_value
            self.async_write_ha_state()
            await self.coordinator.async_request_refresh()


class AjaxDimmerCalibrationSwitch(CoordinatorEntity[AjaxDataCoordinator], SwitchEntity):
    """Switch entity for LightSwitchDimmer calibration (nested in dimmerSettings)."""

    __slots__ = ("_space_id", "_device_id")

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        device_id: str,
    ) -> None:
        """Initialize the dimmer calibration switch."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id

        self._attr_unique_id = f"{device_id}_dimmer_calibration"
        self._attr_translation_key = "dimmer_calibration"

    def _get_device(self) -> AjaxDevice | None:
        """Get current device from coordinator."""
        space = self.coordinator.get_space(self._space_id)
        return space.devices.get(self._device_id) if space else None

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        device = self._get_device()
        return self.coordinator.last_update_success and device is not None and device.online

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(identifiers={(DOMAIN, self._device_id)})

    @property
    def is_on(self) -> bool:
        """Return true if calibration is enabled."""
        device = self._get_device()
        if not device:
            return False
        dimmer_settings = device.attributes.get("dimmerSettings", {})
        return dimmer_settings.get("calibration") == "ENABLED"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from coordinator."""
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on calibration."""
        await self._set_value(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off calibration."""
        await self._set_value(False)

    async def _set_value(self, value: bool) -> None:
        """Set the calibration value."""
        space = self.coordinator.get_space(self._space_id)
        device = self._get_device()
        if not space or not device:
            raise HomeAssistantError("Device not found")

        if not space.hub_id:
            raise HomeAssistantError("Hub not found")

        api_value = "ENABLED" if value else "DISABLED"
        payload = {"dimmerSettings": {"calibration": api_value}}

        try:
            await self.coordinator.api.async_update_device_nested(space.hub_id, self._device_id, payload)
            _LOGGER.info(
                "Set dimmerSettings.calibration=%s for device %s",
                api_value,
                self._device_id,
            )
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to set dimmer calibration: %s", err)
            await self.coordinator.async_request_refresh()
