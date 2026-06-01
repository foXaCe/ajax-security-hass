"""Generic Ajax settings switch entity.

The handler-driven ``AjaxSwitch`` covers socket / relay / wallswitch / combi
config switches (command routing, optimistic update, availability). Split out
of ``switch.py`` so that module is just platform setup + discovery.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._ids import device_identifier
from .const import DOMAIN, MANUFACTURER
from .coordinator import AjaxDataCoordinator
from .models import AjaxDevice, AjaxSpace, DeviceType, SecurityState

_LOGGER = logging.getLogger(__name__)


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
        switch_desc: dict[str, Any],
    ) -> None:
        """Initialize the Ajax switch."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._switch_key = switch_key
        self._switch_desc = switch_desc

        # Set unique ID
        self._attr_unique_id = f"{self.coordinator.entry_id}_{device_id}_{switch_key}"

        # Set entity category if provided (accept enum or string for back-compat)
        cat = switch_desc.get("entity_category", EntityCategory.CONFIG)
        if isinstance(cat, str):
            cat = EntityCategory.DIAGNOSTIC if cat == "diagnostic" else EntityCategory.CONFIG
        self._attr_entity_category = cat

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
                return value_fn()  # type: ignore[no-any-return]
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
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="device_not_found")

        if not space.hub_id:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="hub_not_found")

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
            # Optimistic update. mark_optimistic protects ``is_on`` from being
            # overwritten by a poll arriving before the device reports its new
            # switchState/socketState (otherwise the switch bounces back).
            old_value = device.attributes.get("is_on")
            device.attributes["is_on"] = value
            device.mark_optimistic("is_on", 15.0)
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
                # Revert optimistic update on error and clear the guard so the
                # next poll can correct the restored value immediately.
                device.attributes["is_on"] = old_value
                device.attributes.get("_optimistic_attrs", {}).pop("is_on", None)
                self.async_write_ha_state()
                await self.coordinator.async_request_refresh()
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="failed_to_change",
                    translation_placeholders={"entity": self._switch_key, "error": str(err)},
                ) from err
            return

        api_key = self._switch_desc.get("api_key")
        if not api_key:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_api_key")

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
            # Siren "blink while armed" switch writes via v2sirenIndicatorLightMode
            # but its state is read from led_indication (poll target) — map the
            # optimistic write there so the toggle doesn't bounce back.
            "v2sirenIndicatorLightMode": "led_indication",
        }
        attr_key = attr_key_map.get(api_key, api_key)
        old_value = device.attributes.get(attr_key)
        device.attributes[attr_key] = api_value
        # Reserve against polling overwrite while the API call is in-flight
        device.mark_optimistic(attr_key, 15.0)
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
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_to_change",
                translation_placeholders={"entity": self._switch_key, "error": str(err)},
            ) from err

    async def _set_trigger_value(self, space: AjaxSpace, device: AjaxDevice, trigger_key: str, enabled: bool) -> None:
        """Set a trigger value in the sirenTriggers list."""
        old_triggers = list(device.attributes.get("siren_triggers", []))
        current_triggers = list(old_triggers)

        if enabled and trigger_key not in current_triggers:
            current_triggers.append(trigger_key)
        elif not enabled and trigger_key in current_triggers:
            current_triggers.remove(trigger_key)

        # Optimistic update
        device.attributes["siren_triggers"] = current_triggers
        device.mark_optimistic("siren_triggers", 15.0)
        self.async_write_ha_state()

        try:
            api_nested_key = self._switch_desc.get("api_nested_key")
            if api_nested_key:
                payload = {api_nested_key: {"sirenTriggers": current_triggers}}
                await self.coordinator.api.async_update_device_nested(space.hub_id, self._device_id, payload)  # type: ignore[arg-type]
            else:
                await self.coordinator.api.async_update_device(
                    space.hub_id,  # type: ignore[arg-type]
                    self._device_id,
                    {"sirenTriggers": current_triggers},
                )
            _LOGGER.info(
                "Set sirenTriggers=%s for device %s",
                current_triggers,
                self._device_id,
            )
        except Exception as err:
            _LOGGER.error("Failed to set sirenTriggers: %s", err)
            device.attributes["siren_triggers"] = old_triggers
            self.async_write_ha_state()
            await self.coordinator.async_request_refresh()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_to_change",
                translation_placeholders={"entity": self._switch_key, "error": str(err)},
            ) from err

    async def _set_settings_value(self, space: AjaxSpace, device: AjaxDevice, settings_key: str, enabled: bool) -> None:
        """Set a settings value in the settingsSwitch list (for LightSwitch devices)."""
        old_settings = list(device.attributes.get("settingsSwitch", []))
        current_settings = list(old_settings)

        if enabled and settings_key not in current_settings:
            current_settings.append(settings_key)
        elif not enabled and settings_key in current_settings:
            current_settings.remove(settings_key)

        # Optimistic update
        device.attributes["settingsSwitch"] = current_settings
        device.mark_optimistic("settingsSwitch", 15.0)
        self.async_write_ha_state()

        try:
            await self.coordinator.api.async_update_device(
                space.hub_id,  # type: ignore[arg-type]
                self._device_id,
                {"settingsSwitch": current_settings},
            )
            _LOGGER.info(
                "Set settingsSwitch=%s for device %s",
                current_settings,
                self._device_id,
            )
        except Exception as err:
            _LOGGER.error("Failed to set settingsSwitch: %s", err)
            device.attributes["settingsSwitch"] = old_settings
            self.async_write_ha_state()
            await self.coordinator.async_request_refresh()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_to_change",
                translation_placeholders={"entity": self._switch_key, "error": str(err)},
            ) from err

    async def _set_channel_value(self, space: AjaxSpace, device: AjaxDevice, channel: int, value: bool) -> None:
        """Set a channel value for multi-gang LightSwitch devices."""
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
        # Use 15 seconds to give enough time for the device to report state.
        # A multi-gang LightSwitch is ONE shared device object, so track the
        # optimistic expiry PER channel and derive the device-wide guard from the
        # latest channel expiry. This prevents a rollback on one channel from
        # clearing the guard while another channel's update is still in flight.
        expiry = time.time() + 15.0
        channel_expiry: dict[int, float] = device.attributes.get("_channel_optimistic_until", {})
        channel_expiry[channel] = expiry
        device.attributes["_channel_optimistic_until"] = channel_expiry
        device.attributes["_optimistic_until"] = max(channel_expiry.values())

        self.async_write_ha_state()
        # Notify other channel switches of state change
        self.coordinator.async_update_listeners()

        try:
            device_type_str = device.raw_type
            await self.coordinator.api.async_set_channel_state(
                space.hub_id,  # type: ignore[arg-type]
                self._device_id,
                channel,
                value,
                device_type_str,  # type: ignore[arg-type]
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
            # Only clear THIS channel's optimistic guard; preserve the device-wide
            # guard if another channel still has an in-flight optimistic update,
            # otherwise a poll could overwrite that channel's optimistic state.
            channel_expiry = device.attributes.get("_channel_optimistic_until", {})
            channel_expiry.pop(channel, None)
            if channel_expiry:
                device.attributes["_channel_optimistic_until"] = channel_expiry
                device.attributes["_optimistic_until"] = max(channel_expiry.values())
            else:
                device.attributes.pop("_channel_optimistic_until", None)
                device.attributes.pop("_optimistic_until", None)
            self.async_write_ha_state()
            self.coordinator.async_update_listeners()
            await self.coordinator.async_request_refresh()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_to_change",
                translation_placeholders={"entity": self._switch_key, "error": str(err)},
            ) from err

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
            identifiers={device_identifier(self.coordinator.entry_id, self._device_id)},
            name=device.name,
            manufacturer=MANUFACTURER,
            model=device.raw_type,
            via_device=device_identifier(self.coordinator.entry_id, self._space_id),
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
