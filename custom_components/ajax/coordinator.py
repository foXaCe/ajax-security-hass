"""Ajax data coordinator for Home Assistant.

This coordinator manages:
- Real-time streaming updates from Ajax API
- Space, Room, Device, and Notification data
- State synchronization between Ajax and Home Assistant
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AjaxApi, AjaxApiError, AjaxAuthError
from .const import DOMAIN, UPDATE_INTERVAL
from .models import (
    AjaxAccount,
    AjaxDevice,
    AjaxNotification,
    AjaxRoom,
    AjaxSpace,
    DeviceType,
    NotificationType,
    SecurityState,
)

_LOGGER = logging.getLogger(__name__)


class AjaxDataCoordinator(DataUpdateCoordinator[AjaxAccount]):
    """Coordinator to manage Ajax data updates.

    Architecture:
        AjaxAccount (User)
        └── AjaxSpace (Hub/System)
            ├── Security State (Armed/Disarmed/etc)
            ├── Rooms (Zones/Pieces)
            │   └── Devices in room
            ├── Devices (All)
            │   ├── Sensors
            │   ├── Controls
            │   └── Cameras
            └── Notifications (Events)
    """

    def __init__(self, hass: HomeAssistant, api: AjaxApi) -> None:
        """Initialize the coordinator."""
        self.api = api
        self.account: AjaxAccount | None = None
        self._streaming_tasks: dict[str, asyncio.Task] = {}  # space_id -> streaming task

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    async def _async_update_data(self) -> AjaxAccount:
        """Fetch data from Ajax API.

        This is called periodically, but we also use streaming for real-time updates.
        """
        try:
            # Initialize account if needed
            if self.account is None:
                await self._async_init_account()

            # Update spaces
            await self._async_update_spaces()

            # Update devices for each space
            for space_id in self.account.spaces.keys():
                await self._async_update_devices(space_id)

            # Update notifications (last 50)
            for space_id in self.account.spaces.keys():
                await self._async_update_notifications(space_id, limit=50)

            # Start real-time streaming tasks for each space (if not already started)
            await self._async_start_streaming_tasks()

            _LOGGER.debug(
                "Updated Ajax data: %d spaces, %d devices",
                len(self.account.spaces),
                self.account.get_total_devices(),
            )

            return self.account

        except AjaxAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except AjaxApiError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err

    async def _async_start_streaming_tasks(self) -> None:
        """Start real-time streaming tasks for all spaces."""
        if not self.account:
            return

        for space_id in self.account.spaces.keys():
            # Skip if already streaming for this space
            if space_id in self._streaming_tasks and not self._streaming_tasks[space_id].done():
                continue

            # Create and start streaming task
            task = asyncio.create_task(self._async_stream_space(space_id))
            self._streaming_tasks[space_id] = task
            _LOGGER.info("Started real-time streaming for space %s", space_id)

    async def _async_stream_space(self, space_id: str) -> None:
        """Stream updates for a specific space in the background."""
        try:
            async for success in self.api.async_stream_space_updates(space_id):
                # Process the update
                await self._async_process_stream_update(space_id, success)

        except asyncio.CancelledError:
            _LOGGER.info("Streaming cancelled for space %s", space_id)
            raise
        except Exception as err:
            _LOGGER.error("Error in streaming task for space %s: %s", space_id, err)
            # Wait a bit before coordinator tries to restart it
            await asyncio.sleep(5)

    async def _async_process_stream_update(self, space_id: str, success) -> None:
        """Process a single stream update."""
        try:
            # Check if it's a snapshot (initial data) or an update
            if success.HasField("snapshot"):
                _LOGGER.debug("Received snapshot for space %s", space_id)
                # Snapshot is the full space data - we already have it from polling
                # Just trigger a refresh to ensure consistency
                self.async_set_updated_data(self.account)

            elif success.HasField("update"):
                # Single update
                await self._async_handle_single_update(space_id, success.update)

            elif success.HasField("updates"):
                # Multiple updates
                for update in success.updates.updates:
                    await self._async_handle_single_update(space_id, update)

        except Exception as err:
            _LOGGER.error("Error processing stream update for space %s: %s", space_id, err)

    async def _async_handle_single_update(self, space_id: str, update) -> None:
        """Handle a single update from the stream."""
        try:
            update_type = str(update.space_update_type).split("_")[-1]

            _LOGGER.debug(
                "Stream update for space %s: type=%s",
                space_id,
                update_type
            )

            # Security mode update (MOST IMPORTANT FOR YOUR USE CASE)
            if update.HasField("security_mode"):
                security_mode = update.security_mode
                space = self.account.spaces.get(space_id)
                if space:
                    try:
                        # The security_mode object has: regular_mode, group_mode, displayed_security_state
                        # regular_mode contains: space_state (the actual mode) and transition (current state)
                        mode_value = None

                        if hasattr(security_mode, "regular_mode"):
                            mode_value = security_mode.regular_mode
                            _LOGGER.debug("Using regular_mode: %s", mode_value)
                        elif hasattr(security_mode, "displayed_security_state"):
                            mode_value = security_mode.displayed_security_state
                            _LOGGER.debug("Using displayed_security_state: %s", mode_value)

                        if mode_value is not None:
                            # Extract the space_state which contains the actual security mode
                            # space_state is an enum with numeric values:
                            # 1 = ARMED, 2 = DISARMED, 3 = NIGHT_MODE, etc.
                            if hasattr(mode_value, "space_state"):
                                state_value = mode_value.space_state
                                state_int = int(state_value)
                                _LOGGER.debug("Security space_state: %s (int: %d)", state_value, state_int)

                                # Map numeric security mode to our internal state
                                # Based on Ajax API: 1=ARMED, 2=DISARMED, 3=NIGHT_MODE
                                if state_int == 2:
                                    new_state = SecurityState.DISARMED
                                elif state_int == 3:
                                    new_state = SecurityState.NIGHT_MODE
                                elif state_int == 1:
                                    new_state = SecurityState.ARMED
                                else:
                                    _LOGGER.warning("Unknown security state value: %d (%s)", state_int, state_value)
                                    new_state = None

                                # Only update if state changed
                                if new_state and space.security_state != new_state:
                                    space.security_state = new_state
                                    _LOGGER.info(
                                        "Real-time security state update for space %s: %s",
                                        space_id,
                                        space.security_state
                                    )
                                    self.async_set_updated_data(self.account)
                            else:
                                _LOGGER.error("regular_mode has no space_state attribute. Available: %s", dir(mode_value))
                        else:
                            _LOGGER.error("security_mode has no usable mode attribute. Available: %s", dir(security_mode))
                    except Exception as mode_err:
                        _LOGGER.error("Error parsing security mode: %s", mode_err, exc_info=True)

            # Device update
            elif update.HasField("device"):
                # Full refresh for device changes (less critical, can use polling)
                _LOGGER.debug("Device update received, will refresh on next poll")

            # Room update
            elif update.HasField("room"):
                _LOGGER.debug("Room update received")

            # Group update
            elif update.HasField("group"):
                _LOGGER.debug("Group update received")

        except Exception as err:
            _LOGGER.error("Error handling update: %s", err, exc_info=True)

    async def _async_init_account(self) -> None:
        """Initialize the account data."""
        _LOGGER.debug("Initializing Ajax account")

        # Get account info
        account_data = await self.api.async_get_account()

        self.account = AjaxAccount(
            user_id=account_data.get("user_hex_id", ""),
            name=account_data.get("name", "Unknown"),
            email=account_data.get("email", ""),
        )

        _LOGGER.info("Initialized account for %s", self.account.name)

    async def _async_update_spaces(self) -> None:
        """Update all spaces (hubs/systems)."""
        _LOGGER.debug("Updating spaces")

        spaces_data = await self.api.async_get_spaces()

        for space_data in spaces_data:
            space_id = space_data.get("id")
            if not space_id:
                continue

            # Create or update space
            if space_id not in self.account.spaces:
                space = AjaxSpace(
                    id=space_id,
                    name=space_data.get("name", "Unknown Space"),
                    hub_id=space_data.get("hub_id"),
                )
                self.account.spaces[space_id] = space
                _LOGGER.info("Added new space: %s", space.name)
            else:
                # Update existing space
                space = self.account.spaces[space_id]
                space.name = space_data.get("name", space.name)

            # Update security state if available
            if "security_state" in space_data:
                space.security_state = self._parse_security_state(
                    space_data["security_state"]
                )

            # Update notification count
            if "new_notifications_count" in space_data:
                space.unread_notifications = space_data["new_notifications_count"]

    async def _async_update_devices(self, space_id: str) -> None:
        """Update devices for a specific space."""
        _LOGGER.debug("Updating devices for space %s", space_id)

        space = self.account.spaces.get(space_id)
        if not space:
            return

        devices_data = await self.api.async_get_devices(space_id)

        for device_data in devices_data:
            device_id = device_data.get("id")
            if not device_id:
                continue

            # Parse device type
            device_type = self._parse_device_type(device_data.get("type", "unknown"))

            # Create or update device
            if device_id not in space.devices:
                device = AjaxDevice(
                    id=device_id,
                    name=device_data.get("name", "Unknown Device"),
                    type=device_type,
                    space_id=space_id,
                    hub_id=device_data.get("hub_id", space.hub_id or ""),
                    room_id=device_data.get("room_id"),
                    group_id=device_data.get("group_id"),
                )
                space.devices[device_id] = device
                _LOGGER.info("Added new device: %s (%s)", device.name, device.type.value)
            else:
                device = space.devices[device_id]

            # Update device attributes
            device.online = device_data.get("online", True)
            device.bypassed = device_data.get("bypassed", False)
            device.malfunctions = device_data.get("malfunctions", 0)
            device.battery_level = device_data.get("battery_level")
            device.battery_state = device_data.get("battery_state")
            device.signal_strength = device_data.get("signal_strength")
            device.firmware_version = device_data.get("firmware_version")
            device.hardware_version = device_data.get("hardware_version")
            device.states = device_data.get("states", [])

            # Update device metadata
            device.device_color = device_data.get("device_color")
            device.device_label = device_data.get("device_label")
            device.device_marketing_id = device_data.get("device_marketing_id")

            # Update device attributes dict
            if "attributes" in device_data:
                device.attributes.update(device_data["attributes"])

            # For Hub devices, try to get additional data via streamHubObject
            if device.type == DeviceType.HUB:
                try:
                    _LOGGER.debug("Fetching HubObject data for hub %s", device.id)
                    hub_obj_data = await self.api.async_stream_hub_object(device.id)
                    if hub_obj_data:
                        _LOGGER.info("Received HubObject data: %s", hub_obj_data)
                        # Merge hub_obj_data into device attributes
                        if "attributes" not in device_data:
                            device.attributes = {}
                        device.attributes.update(hub_obj_data)
                    else:
                        _LOGGER.warning("No HubObject data received for hub %s", device.id)
                except Exception as err:
                    _LOGGER.error("Failed to fetch HubObject for hub %s: %s", device.id, err)

            # Update room association
            if device.room_id and device.room_id in space.rooms:
                room = space.rooms[device.room_id]
                if device_id not in room.device_ids:
                    room.device_ids.append(device_id)

    async def _async_update_notifications(
        self, space_id: str, limit: int = 50
    ) -> None:
        """Update notifications for a specific space."""
        _LOGGER.debug("Updating notifications for space %s", space_id)

        space = self.account.spaces.get(space_id)
        if not space:
            return

        # TODO: Implement notification fetching when API method is ready
        # For now, notifications will be populated via streaming updates
        pass

    def _parse_security_state(self, state_value: Any) -> SecurityState:
        """Parse security state from API response."""
        if isinstance(state_value, str):
            state_str = state_value.upper()
            if "ARMED" in state_str and "PARTIALLY" not in state_str:
                return SecurityState.ARMED
            elif "DISARMED" in state_str:
                return SecurityState.DISARMED
            elif "NIGHT" in state_str:
                return SecurityState.NIGHT_MODE
            elif "PARTIALLY" in state_str:
                return SecurityState.PARTIALLY_ARMED

        return SecurityState.NONE

    def _parse_device_type(self, type_str: str) -> DeviceType:
        """Parse device type from API response."""
        type_map = {
            # Motion detectors
            "motion_protect": DeviceType.MOTION_DETECTOR,
            "motion": DeviceType.MOTION_DETECTOR,
            "pir": DeviceType.MOTION_DETECTOR,
            "motionprotect": DeviceType.MOTION_DETECTOR,

            # Door/Window contacts
            "door_protect": DeviceType.DOOR_CONTACT,
            "doorprotect": DeviceType.DOOR_CONTACT,
            "door": DeviceType.DOOR_CONTACT,
            "window": DeviceType.DOOR_CONTACT,
            "opening": DeviceType.DOOR_CONTACT,
            "magnet": DeviceType.DOOR_CONTACT,

            # Glass break
            "glass_protect": DeviceType.GLASS_BREAK,
            "glassprotect": DeviceType.GLASS_BREAK,
            "glass": DeviceType.GLASS_BREAK,

            # Smoke detectors
            "fire_protect": DeviceType.SMOKE_DETECTOR,
            "fireprotect": DeviceType.SMOKE_DETECTOR,
            "smoke": DeviceType.SMOKE_DETECTOR,
            "fire": DeviceType.SMOKE_DETECTOR,

            # Flood detectors
            "leak_protect": DeviceType.FLOOD_DETECTOR,
            "leakprotect": DeviceType.FLOOD_DETECTOR,
            "leak": DeviceType.FLOOD_DETECTOR,
            "water": DeviceType.FLOOD_DETECTOR,
            "flood": DeviceType.FLOOD_DETECTOR,

            # Temperature
            "temperature": DeviceType.TEMPERATURE_SENSOR,
            "temp": DeviceType.TEMPERATURE_SENSOR,

            # Controls
            "keypad": DeviceType.KEYPAD,
            "space_control": DeviceType.REMOTE_CONTROL,
            "spacecontrol": DeviceType.REMOTE_CONTROL,
            "remote": DeviceType.REMOTE_CONTROL,

            # Sirens
            "siren": DeviceType.SIREN,
            "alarm": DeviceType.SIREN,

            # Smart devices
            "socket": DeviceType.SOCKET,
            "relay": DeviceType.RELAY,
            "thermostat": DeviceType.THERMOSTAT,

            # Cameras
            "camera": DeviceType.CAMERA,
            "cam": DeviceType.CAMERA,

            # Hub
            "hub": DeviceType.HUB,
        }

        # Try exact match (case insensitive)
        type_lower = type_str.lower()
        if type_lower in type_map:
            return type_map[type_lower]

        # Try partial match
        for key, device_type in type_map.items():
            if key in type_lower or type_lower in key:
                _LOGGER.debug(
                    "Device type '%s' matched to %s via partial match with '%s'",
                    type_str,
                    device_type.value,
                    key,
                )
                return device_type

        # Log unknown types to help improve mapping
        _LOGGER.warning(
            "Unknown device type '%s' - please report this to help improve the integration. "
            "Device will be marked as UNKNOWN.",
            type_str,
        )
        return DeviceType.UNKNOWN

    # ============================================================================
    # Control methods
    # ============================================================================

    async def async_arm_space(self, space_id: str) -> None:
        """Arm a space."""
        _LOGGER.info("Arming space %s", space_id)

        try:
            await self.api.async_arm(space_id)

            # Update local state optimistically
            if space_id in self.account.spaces:
                self.account.spaces[space_id].security_state = SecurityState.ARMED

            # Request immediate data refresh
            await self.async_request_refresh()

        except AjaxApiError as err:
            _LOGGER.error("Failed to arm space %s: %s", space_id, err)
            raise

    async def async_disarm_space(self, space_id: str) -> None:
        """Disarm a space."""
        _LOGGER.info("Disarming space %s", space_id)

        try:
            await self.api.async_disarm(space_id)

            # Update local state optimistically
            if space_id in self.account.spaces:
                self.account.spaces[space_id].security_state = SecurityState.DISARMED

            # Request immediate data refresh
            await self.async_request_refresh()

        except AjaxApiError as err:
            _LOGGER.error("Failed to disarm space %s: %s", space_id, err)
            raise

    async def async_arm_night_mode(self, space_id: str) -> None:
        """Activate night mode for a space."""
        _LOGGER.info("Activating night mode for space %s", space_id)

        try:
            await self.api.async_arm_night_mode(space_id)

            # Update local state optimistically
            if space_id in self.account.spaces:
                self.account.spaces[space_id].security_state = SecurityState.NIGHT_MODE

            # Request immediate data refresh
            await self.async_request_refresh()

        except AjaxApiError as err:
            _LOGGER.error("Failed to activate night mode for space %s: %s", space_id, err)
            raise

    async def async_press_panic_button(self, space_id: str) -> None:
        """Press panic button (trigger panic alarm) for a space."""
        _LOGGER.warning("PANIC BUTTON pressed for space %s", space_id)

        try:
            await self.api.async_press_panic_button(space_id)

            # Request immediate data refresh to get updated state
            await self.async_request_refresh()

        except AjaxApiError as err:
            _LOGGER.error("Failed to trigger panic for space %s: %s", space_id, err)
            raise

    # ============================================================================
    # Helper methods
    # ============================================================================

    def get_space(self, space_id: str) -> AjaxSpace | None:
        """Get a space by ID."""
        return self.account.spaces.get(space_id) if self.account else None

    def get_device(self, space_id: str, device_id: str) -> AjaxDevice | None:
        """Get a device by space and device ID."""
        space = self.get_space(space_id)
        return space.devices.get(device_id) if space else None

    def get_room(self, space_id: str, room_id: str) -> AjaxRoom | None:
        """Get a room by space and room ID."""
        space = self.get_space(space_id)
        return space.rooms.get(room_id) if space else None

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator."""
        _LOGGER.info("Shutting down Ajax coordinator")

        # Stop all streaming tasks
        for space_id, task in self._streaming_tasks.items():
            if not task.done():
                _LOGGER.debug("Cancelling streaming task for space %s", space_id)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._streaming_tasks.clear()

        # Close API connection
        await self.api.close()
