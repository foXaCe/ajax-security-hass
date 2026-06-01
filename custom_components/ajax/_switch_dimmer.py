"""LightSwitchDimmer switch entities for Ajax.

``settingsSwitch``-based toggles (LED / child-lock / state-memory / current
threshold), the boolean attribute switch, and the calibration switch. Split
out of ``switch.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._ids import device_identifier
from .const import DOMAIN
from .coordinator import AjaxDataCoordinator
from .models import AjaxDevice

_LOGGER = logging.getLogger(__name__)

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
        switch_def: dict[str, Any],
    ) -> None:
        """Initialize the dimmer settings switch."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._switch_def = switch_def

        self._attr_unique_id = f"{self.coordinator.entry_id}_{device_id}_{switch_def['key']}"
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
        return DeviceInfo(identifiers={device_identifier(self.coordinator.entry_id, self._device_id)})

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        device = self._get_device()
        if not device:
            return False
        settings_list = device.attributes.get("settingsSwitch", [])
        return self._switch_def["settings_key"] in settings_list

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
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="device_not_found")

        if not space.hub_id:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="hub_not_found")

        settings_key = self._switch_def["settings_key"]
        old_settings = list(device.attributes.get("settingsSwitch", []))
        new_settings = list(old_settings)

        if value and settings_key not in new_settings:
            new_settings.append(settings_key)
        elif not value and settings_key in new_settings:
            new_settings.remove(settings_key)

        # Optimistic update — reserve against polling overwrite; the coordinator
        # honours is_optimistic("settingsSwitch") before rewriting from a poll.
        device.attributes["settingsSwitch"] = new_settings
        device.mark_optimistic("settingsSwitch", 15.0)
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
            # Rollback optimistic update + drop the guard so the refresh re-syncs.
            device.attributes["settingsSwitch"] = old_settings
            device.attributes.get("_optimistic_attrs", {}).pop("settingsSwitch", None)
            self.async_write_ha_state()
            await self.coordinator.async_request_refresh()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_to_change",
                translation_placeholders={"entity": self._switch_def["key"], "error": str(err)},
            ) from err


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

        self._attr_unique_id = f"{self.coordinator.entry_id}_{device_id}_{switch_key}"
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
        return DeviceInfo(identifiers={device_identifier(self.coordinator.entry_id, self._device_id)})

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        device = self._get_device()
        if not device:
            return False
        return device.attributes.get(self._attr_key, False)  # type: ignore[no-any-return]

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
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="device_not_found")

        if not space.hub_id:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="hub_not_found")

        # Optimistic update with rollback — reserve against polling overwrite;
        # the coordinator honours is_optimistic(attr_key) before rewriting.
        old_value = device.attributes.get(self._attr_key)
        device.attributes[self._attr_key] = value
        device.mark_optimistic(self._attr_key, 15.0)
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
            # Rollback optimistic update + drop the guard so the refresh re-syncs.
            device.attributes[self._attr_key] = old_value
            device.attributes.get("_optimistic_attrs", {}).pop(self._attr_key, None)
            self.async_write_ha_state()
            await self.coordinator.async_request_refresh()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_to_change",
                translation_placeholders={"entity": self._attr_key, "error": str(err)},
            ) from err


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

        self._attr_unique_id = f"{self.coordinator.entry_id}_{device_id}_dimmer_calibration"
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
        return DeviceInfo(identifiers={device_identifier(self.coordinator.entry_id, self._device_id)})

    @property
    def is_on(self) -> bool:
        """Return true if calibration is enabled."""
        device = self._get_device()
        if not device:
            return False
        dimmer_settings = device.attributes.get("dimmerSettings", {})
        return dimmer_settings.get("calibration") == "ENABLED"  # type: ignore[no-any-return]

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
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="device_not_found")

        if not space.hub_id:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="hub_not_found")

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
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="failed_to_change",
                translation_placeholders={"entity": "dimmer_calibration", "error": str(err)},
            ) from err
