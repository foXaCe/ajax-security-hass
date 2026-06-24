"""Ajax REST API — Device read, command and update endpoints."""

from __future__ import annotations

import time
from typing import Any

from ._base import (
    _LOGGER,
    AjaxRestApiError,
    AjaxRestClientBase,
)


class _DevicesMixin(AjaxRestClientBase):
    """Device read, command and update endpoint methods (mixed into ``AjaxRestApi``)."""

    DEVICE_UPDATE_EXCLUDE_FIELDS = {
        # Identifiers (read-only)
        "id",
        "hubId",
        "spaceId",
        "serial",
        # Type info (read-only) - but NOT deviceType (required by API)
        "type",
        "deviceCategory",
        # Version info (read-only)
        "firmwareVersion",
        "hardwareVersion",
        # State/status fields (computed)
        "online",
        "state",
        "status",
        "batteryLevel",
        "batteryChargeLevelPercentage",
        "signalLevel",
        # Sensor state fields (read-only)
        "temperature",
        "tampered",
        "reedClosed",
        "extraContactClosed",
        "externalContactState",
        "externalContactTriggered",
        "estimatedArmingState",
        "issuesCount",
        "confirmsAlarm",
        "verifiesAlarm",
        # Timestamps (read-only)
        "createdAt",
        "updatedAt",
        "lastSeen",
        "lastSeenAt",
        # Computed/internal fields that cause API validation errors
        "wiringSchemeSpecificDetails",
        "malfunctions",
        "alerts",
        "problems",
        # Device capability/support flags (read-only)
        "capabilities",
        "selfMonitoringConfigs",
        "indicatorLightModeSupported",
        "rollerShutterSupported",
        # Hardware config fields (read-only)
        "color",
        # Note: assignedExtender is required by API, do not exclude
        "cmsDeviceIndex",
        "bypassState",
        # Note: deviceTransmissionPowerMode/Value must NOT be excluded -
        # API requires at least one to be present in PUT requests
    }

    async def async_get_devices(self, hub_id: str, enrich: bool = True) -> list[dict[str, Any]]:
        """Get all devices for a specific hub.

        Args:
            hub_id: Hub ID
            enrich: If True, returns full device details (default True)

        Returns:
            List of device dictionaries (with full details if enrich=True)
        """
        # Serve from the short-lived cache unless a cache bypass is pending
        # (bypass must reach the real endpoint to get fresh state).
        cache_key = (hub_id, enrich)
        if not self._cache_bypass_active():
            cached = self._devices_cache.get(cache_key)
            if cached and (time.time() - cached[0]) < self._devices_cache_ttl:
                return cached[1]

        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")
        endpoint = f"user/{self.user_id}/hubs/{hub_id}/devices"
        if enrich:
            endpoint += "?enrich=true"
        data = await self._request("GET", endpoint)
        self._devices_cache[cache_key] = (time.time(), data)
        return data  # type: ignore[no-any-return]

    async def async_get_device(self, hub_id: str, device_id: str) -> dict[str, Any]:
        """Get device details.

        Args:
            hub_id: Hub ID
            device_id: Device ID

        Returns:
            Device details dictionary
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")
        return await self._request("GET", f"user/{self.user_id}/hubs/{hub_id}/devices/{device_id}")  # type: ignore[no-any-return]

    async def async_get_device_state(self, device_id: str) -> dict[str, Any]:
        """Get device state.

        Args:
            device_id: Device ID

        Returns:
            Device state dictionary
        """
        return await self._request("GET", f"devices/{device_id}/state")  # type: ignore[no-any-return]

    async def async_send_device_command(self, hub_id: str, device_id: str, command: str, device_type: str) -> None:
        """Send command to device (Socket/Relay/WallSwitch).

        Uses the /command endpoint which is simpler and more reliable than PUT.
        Supported commands: SWITCH_ON, SWITCH_OFF

        Args:
            hub_id: Hub ID
            device_id: Device ID
            command: Command string (e.g., "SWITCH_ON", "SWITCH_OFF")
            device_type: Device type string (e.g., "WALL_SWITCH", "SOCKET", "RELAY")
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")

        endpoint = f"user/{self.user_id}/hubs/{hub_id}/devices/{device_id}/command"
        payload = {"command": command, "deviceType": device_type}
        _LOGGER.info(
            "Sending device command: POST %s with %s",
            endpoint,
            payload,
        )
        await self._request_no_response("POST", endpoint, payload)

    async def async_set_switch_state(self, hub_id: str, device_id: str, state: bool, device_type: str) -> None:
        """Set switch/relay/socket state.

        Uses the /command endpoint for reliable switch control.

        Args:
            hub_id: Hub ID
            device_id: Device ID
            state: True for on, False for off
            device_type: Device type string (e.g., "WallSwitch", "Socket", "Relay")
        """
        command = "SWITCH_ON" if state else "SWITCH_OFF"
        await self.async_send_device_command(hub_id, device_id, command, device_type)

    async def async_set_channel_state(
        self,
        hub_id: str,
        device_id: str,
        channel: int,
        state: bool,
        device_type: str,
    ) -> None:
        """Set multi-gang switch channel state.

        Uses SWITCH_ON/SWITCH_OFF command with additionalParam.channels for LightSwitch.
        The Ajax API expects a single channel per command.

        Args:
            hub_id: Hub ID
            device_id: Device ID
            channel: Channel index (0-based: 0 for channel 1, 1 for channel 2)
            state: True for on, False for off
            device_type: Device type string (e.g., "LightSwitchTwoGang")
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")

        endpoint = f"user/{self.user_id}/hubs/{hub_id}/devices/{device_id}/command"
        command = "SWITCH_ON" if state else "SWITCH_OFF"
        # Convert 0-based index to API format: 0 -> "CHANNEL_1", 1 -> "CHANNEL_2"
        channel_str = f"CHANNEL_{channel + 1}"
        payload = {
            "command": command,
            "deviceType": device_type,
            "additionalParam": {
                "additionalParamType": "CHANNELS",
                "channels": [channel_str],
            },
        }
        _LOGGER.info(
            "Sending channel command: POST %s with %s",
            endpoint,
            payload,
        )
        await self._request_no_response("POST", endpoint, payload)

    async def async_set_waterstop_state(self, hub_id: str, device_id: str, open_valve: bool) -> None:
        """Set WaterStop valve state.

        Uses the /command endpoint for valve control.
        WaterStop uses SWITCH_ON (open) / SWITCH_OFF (close) commands.

        Args:
            hub_id: Hub ID
            device_id: Device ID
            open_valve: True to open valve, False to close
        """
        # WaterStop uses same commands as switches: SWITCH_ON = open, SWITCH_OFF = close
        command = "SWITCH_ON" if open_valve else "SWITCH_OFF"
        await self.async_send_device_command(hub_id, device_id, command, "WaterStop")

    async def async_get_socket_power(self, device_id: str) -> dict[str, Any]:
        """Get socket power consumption.

        Args:
            device_id: Socket device ID

        Returns:
            Power consumption data
        """
        return await self._request("GET", f"devices/{device_id}/power")  # type: ignore[no-any-return]

    async def async_set_dimmer_brightness(
        self,
        hub_id: str,
        device_id: str,
        brightness: int,
    ) -> None:
        """Set dimmer brightness level.

        Args:
            hub_id: Hub ID
            device_id: Dimmer device ID
            brightness: Brightness level (0-100). 0 = off.
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")

        # Use command endpoint with BRIGHTNESS command
        await self._request_no_response(
            "POST",
            f"user/{self.user_id}/hubs/{hub_id}/devices/{device_id}/command",
            {
                "command": "BRIGHTNESS",
                "deviceType": "LightSwitchDimmer",
                "additionalParam": {
                    "additionalParamType": "BRIGHTNESS_STATUS",
                    "brightnessInPercentage": brightness,
                    "channels": ["CHANNEL_1"],
                    "brightnessType": "BRIGHTNESS_TYPE_ABSOLUTE",
                },
            },
        )

    async def async_update_device(
        self,
        hub_id: str,
        device_id: str,
        settings: dict[str, Any],
    ) -> None:
        """Update device settings.

        Args:
            hub_id: Hub ID
            device_id: Device ID
            settings: Dictionary of settings to update (e.g., {"alwaysActive": true})

        Raises:
            AjaxRestApiError: If the update fails
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")

        # First get current device data
        current_device = await self.async_get_device(hub_id, device_id)

        # Merge settings with current device data
        updated_device = {**current_device, **settings}

        # Remove read-only and problematic fields
        for field in self.DEVICE_UPDATE_EXCLUDE_FIELDS:
            updated_device.pop(field, None)

        _LOGGER.debug(
            "Updating device %s with fields: %s",
            device_id,
            list(updated_device.keys()),
        )

        await self._request_no_response(
            "PUT",
            f"user/{self.user_id}/hubs/{hub_id}/devices/{device_id}",
            updated_device,
        )

    async def async_update_device_nested(
        self,
        hub_id: str,
        device_id: str,
        settings: dict[str, Any],
    ) -> None:
        """Update device settings with deep merge for nested structures.

        This method properly handles nested settings like wiredDeviceSettings
        by merging them with existing values instead of replacing.

        Args:
            hub_id: Hub ID
            device_id: Device ID
            settings: Dictionary of settings to update, can include nested dicts

        Raises:
            AjaxRestApiError: If the update fails
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")

        # First get current device data
        current_device = await self.async_get_device(hub_id, device_id)

        # Deep merge settings with current device data
        updated_device = self._deep_merge(current_device, settings)

        # Remove read-only and problematic fields
        for field in self.DEVICE_UPDATE_EXCLUDE_FIELDS:
            updated_device.pop(field, None)

        _LOGGER.debug(
            "Updating device %s (nested) with fields: %s",
            device_id,
            list(updated_device.keys()),
        )

        await self._request_no_response(
            "PUT",
            f"user/{self.user_id}/hubs/{hub_id}/devices/{device_id}",
            updated_device,
        )

    def _deep_merge(self, base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        """Deep merge two dictionaries, preserving nested structures."""
        result = base.copy()
        for key, value in updates.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result
