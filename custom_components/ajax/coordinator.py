"""Ajax data coordinator for Home Assistant.

This coordinator manages:
- Real-time streaming updates from Ajax API
- Space, Room, Device, and Notification data
- State synchronization between Ajax and Home Assistant
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AjaxApi, AjaxApiError, AjaxAuthError, AjaxConnectionError
from .const import (
    CONF_NOTIFICATION_FILTER,
    CONF_PERSISTENT_NOTIFICATION,
    DOMAIN,
    NOTIFICATION_FILTER_ALL,
    NOTIFICATION_FILTER_ALARMS_ONLY,
    NOTIFICATION_FILTER_NONE,
    NOTIFICATION_FILTER_SECURITY_EVENTS,
    UPDATE_INTERVAL,
    get_event_message,
)
from .models import (
    AjaxAccount,
    AjaxDevice,
    AjaxNotification,
    AjaxRoom,
    AjaxSpace,
    DeviceType,
    GroupState,
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
        self._streaming_tasks: dict[str, asyncio.Task] = {}  # space_id -> space updates streaming task
        self._notification_streaming_tasks: dict[str, asyncio.Task] = {}  # space_id -> notification streaming task
        self._device_streaming_tasks: dict[str, asyncio.Task] = {}  # space_id -> device status streaming task
        self._specific_device_streams: dict[str, asyncio.Task] = {}  # device_id -> device-specific streaming task
        self._fast_poll_tasks: dict[str, asyncio.Task] = {}  # device_id -> fast polling task for door sensors
        self._groups_loaded_events: dict[str, asyncio.Event] = {}  # space_id -> event triggered when groups are loaded
        self._wire_input_polling_tasks: dict[str, asyncio.Task] = {}  # space_id -> wire_input polling task

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

            # Reset expired motion detections (only for motion detectors)
            for space_id in self.account.spaces.keys():
                space = self.account.spaces.get(space_id)
                if space:
                    self._reset_expired_motion_detections(space)

            # Update notifications (last 50)
            for space_id in self.account.spaces.keys():
                await self._async_update_notifications(space_id, limit=50)

            # Create events for waiting on groups to be loaded from stream
            for space_id in self.account.spaces.keys():
                if space_id not in self._groups_loaded_events:
                    self._groups_loaded_events[space_id] = asyncio.Event()

            # Start real-time streaming tasks for each space (if not already started)
            await self._async_start_streaming_tasks()

            # Wait for groups to be loaded from stream (with timeout)
            # This ensures group/zone entities are created during first setup
            if self.account:
                await self._async_wait_for_groups()

            return self.account

        except AjaxAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except AjaxApiError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err

    async def _async_wait_for_groups(self) -> None:
        """Wait for groups to be loaded from stream during initial setup.

        This method waits for the first stream update containing group/security data
        to ensure that group entities are created during the initial setup.
        Uses a timeout to avoid blocking if the space has no groups.
        """
        timeout = 10  # seconds
        wait_tasks = []

        for space_id, event in self._groups_loaded_events.items():
            if not event.is_set():
                wait_tasks.append((space_id, event.wait()))

        if not wait_tasks:
            return

        _LOGGER.debug("Waiting for groups to be loaded from stream (timeout: %ds)...", timeout)

        try:
            # Wait for all events with timeout
            await asyncio.wait_for(
                asyncio.gather(*[task for _, task in wait_tasks], return_exceptions=True),
                timeout=timeout
            )
            _LOGGER.info("Groups loaded from stream successfully")
        except asyncio.TimeoutError:
            # Timeout is expected if space has no groups or stream is slow
            _LOGGER.debug("Timeout waiting for groups (this is normal if space has no groups)")
        except Exception as err:
            _LOGGER.warning("Error waiting for groups: %s", err)

    async def _async_start_streaming_tasks(self) -> None:
        """Start real-time streaming tasks for all spaces."""
        if not self.account:
            return

        for space_id in self.account.spaces.keys():
            # Start space updates stream if not already running
            if space_id not in self._streaming_tasks or self._streaming_tasks[space_id].done():
                task = asyncio.create_task(self._async_stream_space(space_id))
                self._streaming_tasks[space_id] = task
                _LOGGER.info("Started space updates streaming for space %s", space_id)

            # Start notification updates stream if not already running
            if space_id not in self._notification_streaming_tasks or self._notification_streaming_tasks[space_id].done():
                task = asyncio.create_task(self._async_stream_notifications(space_id))
                self._notification_streaming_tasks[space_id] = task
                _LOGGER.info("Started notification streaming for space %s", space_id)

            # Start device status updates stream if not already running
            if space_id not in self._device_streaming_tasks or self._device_streaming_tasks[space_id].done():
                task = asyncio.create_task(self._async_stream_devices(space_id))
                self._device_streaming_tasks[space_id] = task
                _LOGGER.info("Started device status streaming for space %s", space_id)

            # REMOVED: Device-specific stream for door sensor 30DD9A8B (buggy device, now ignored)

    async def _async_stream_space(self, space_id: str) -> None:
        """Stream updates for a specific space in the background."""
        retry_count = 0
        max_retries = 10
        was_disconnected = False

        while retry_count < max_retries:
            try:
                async for success in self.api.async_stream_space_updates(space_id):
                    # Log successful reconnection if we were previously disconnected
                    if was_disconnected:
                        _LOGGER.info("Successfully reconnected space stream for %s", space_id)
                        was_disconnected = False

                    # Reset retry count on successful message
                    retry_count = 0
                    # Process the update
                    await self._async_process_stream_update(space_id, success)

            except asyncio.CancelledError:
                _LOGGER.info("Streaming cancelled for space %s", space_id)
                raise

            except AjaxConnectionError as err:
                # Temporary network error - adjust log level based on retry count
                retry_count += 1
                was_disconnected = True

                # First 3 attempts: WARNING (it's probably temporary)
                if retry_count <= 3:
                    _LOGGER.warning(
                        "Connection lost for space %s (attempt %d/%d), retrying...",
                        space_id,
                        retry_count,
                        max_retries,
                    )
                # After 3 attempts: ERROR (it's becoming a problem)
                else:
                    _LOGGER.error(
                        "Connection still lost for space %s (attempt %d/%d)",
                        space_id,
                        retry_count,
                        max_retries,
                    )

                if retry_count < max_retries:
                    # Exponential backoff: 5s, 10s, 20s, 40s, then 60s max
                    wait_time = min(5 * (2 ** (retry_count - 1)), 60)
                    await asyncio.sleep(wait_time)
                else:
                    _LOGGER.error("Max retries reached for space streaming %s, giving up", space_id)
                    break

            except Exception as err:
                # Real error (not just network issue) - always log as ERROR with traceback
                retry_count += 1
                was_disconnected = True
                _LOGGER.error(
                    "Error in streaming task for space %s (attempt %d/%d): %s",
                    space_id,
                    retry_count,
                    max_retries,
                    err,
                    exc_info=True
                )
                if retry_count < max_retries:
                    wait_time = min(5 * (2 ** (retry_count - 1)), 60)
                    _LOGGER.info("Retrying space streaming for %s in %d seconds", space_id, wait_time)
                    await asyncio.sleep(wait_time)
                else:
                    _LOGGER.error("Max retries reached for space streaming %s, giving up", space_id)
                    break

    async def _async_stream_notifications(self, space_id: str) -> None:
        """Stream real-time notification updates for a specific space."""
        retry_count = 0
        max_retries = 10
        was_disconnected = False

        _LOGGER.info("Starting notification streaming for space %s", space_id)

        while retry_count < max_retries:
            try:
                async for event in self.api.async_stream_notification_updates(space_id):
                    # Log successful reconnection if we were previously disconnected
                    if was_disconnected:
                        _LOGGER.info("Successfully reconnected notification stream for %s", space_id)
                        was_disconnected = False
                        retry_count = 0

                    # Process notification event
                    await self._async_process_notification_event(space_id, event)

            except asyncio.CancelledError:
                _LOGGER.info("Notification streaming cancelled for space %s", space_id)
                raise

            except AjaxConnectionError as err:
                # Temporary network error - adjust log level based on retry count
                retry_count += 1
                was_disconnected = True

                # First 3 attempts: WARNING (it's probably temporary)
                if retry_count <= 3:
                    _LOGGER.warning(
                        "Connection lost for notification stream %s (attempt %d/%d), retrying...",
                        space_id,
                        retry_count,
                        max_retries,
                    )
                # After 3 attempts: ERROR (it's becoming a problem)
                else:
                    _LOGGER.error(
                        "Connection still lost for notification stream %s (attempt %d/%d)",
                        space_id,
                        retry_count,
                        max_retries,
                    )

                if retry_count < max_retries:
                    # Exponential backoff: 5s, 10s, 20s, 40s, then 60s max
                    wait_time = min(5 * (2 ** (retry_count - 1)), 60)
                    await asyncio.sleep(wait_time)
                else:
                    _LOGGER.error("Max retries reached for notification streaming %s, giving up", space_id)
                    break

            except Exception as err:
                # Real error (not just network issue) - always log as ERROR with traceback
                retry_count += 1
                was_disconnected = True
                _LOGGER.error(
                    "Error in notification streaming for space %s (attempt %d/%d): %s",
                    space_id,
                    retry_count,
                    max_retries,
                    err,
                    exc_info=True
                )
                if retry_count < max_retries:
                    wait_time = min(5 * (2 ** (retry_count - 1)), 60)
                    await asyncio.sleep(wait_time)
                else:
                    _LOGGER.error("Max retries reached for notification streaming %s, giving up", space_id)
                    break

    async def _async_stream_devices(self, space_id: str) -> None:
        """Stream real-time device status updates for a specific space."""
        retry_count = 0
        max_retries = 10
        was_disconnected = False

        _LOGGER.info("Starting device status streaming for space %s", space_id)

        while retry_count < max_retries:
            try:
                async for update in self.api.async_stream_light_devices(space_id):
                    # Log successful reconnection if we were previously disconnected
                    if was_disconnected:
                        _LOGGER.info("Successfully reconnected device stream for %s", space_id)
                        was_disconnected = False
                        retry_count = 0

                    # Process device status update
                    await self._async_process_device_update(space_id, update)

            except asyncio.CancelledError:
                _LOGGER.info("Device streaming cancelled for space %s", space_id)
                raise

            except AjaxConnectionError as err:
                # Temporary network error - adjust log level based on retry count
                retry_count += 1
                was_disconnected = True

                # First 3 attempts: WARNING (it's probably temporary)
                if retry_count <= 3:
                    _LOGGER.warning(
                        "Connection lost for device stream %s (attempt %d/%d), retrying...",
                        space_id,
                        retry_count,
                        max_retries,
                    )
                # After 3 attempts: ERROR (it's becoming a problem)
                else:
                    _LOGGER.error(
                        "Connection still lost for device stream %s (attempt %d/%d)",
                        space_id,
                        retry_count,
                        max_retries,
                    )

                if retry_count < max_retries:
                    # Exponential backoff: 5s, 10s, 20s, 40s, then 60s max
                    wait_time = min(5 * (2 ** (retry_count - 1)), 60)
                    await asyncio.sleep(wait_time)
                else:
                    _LOGGER.error("Max retries reached for device streaming %s, giving up", space_id)
                    break

            except Exception as err:
                # Real error (not just network issue) - always log as ERROR with traceback
                retry_count += 1
                was_disconnected = True
                _LOGGER.error(
                    "Error in device streaming for space %s (attempt %d/%d): %s",
                    space_id,
                    retry_count,
                    max_retries,
                    err,
                    exc_info=True
                )
                if retry_count < max_retries:
                    wait_time = min(5 * (2 ** (retry_count - 1)), 60)
                    await asyncio.sleep(wait_time)
                else:
                    _LOGGER.error("Max retries reached for device streaming %s, giving up", space_id)
                    break

    async def _async_stream_specific_device(self, space_id: str, device_id: str) -> None:
        """Stream real-time updates for a specific device (door sensor).

        This uses the device-specific stream API which sends complete snapshots
        of the device state, including door_opened=false when closed.
        """
        retry_count = 0
        max_retries = 10
        was_disconnected = False

        _LOGGER.info("Starting device-specific stream for device %s in space %s", device_id, space_id)

        while retry_count < max_retries:
            try:
                async for hub_device in self.api.async_stream_device_updates(space_id, device_id):
                    # Log successful reconnection if we were previously disconnected
                    if was_disconnected:
                        _LOGGER.info("Successfully reconnected device-specific stream for %s", device_id)
                        was_disconnected = False
                        retry_count = 0

                    # Parse the HubDevice protobuf to get door_opened status
                    try:
                        # Extract device state from the HubDevice protobuf
                        door_opened = False  # Default to closed

                        if hasattr(hub_device, "common_part") and hub_device.HasField("common_part"):
                            common = hub_device.common_part
                            if hasattr(common, "door_opened") and common.HasField("door_opened"):
                                door_opened = True

                        # Update the device in our account model
                        space = self.account.spaces.get(space_id)
                        if space and device_id in space.devices:
                            device = space.devices[device_id]
                            old_state = device.attributes.get("door_opened", False)

                            if old_state != door_opened:
                                device.attributes["door_opened"] = door_opened
                                state_str = "opened" if door_opened else "closed"
                                _LOGGER.info("Device '%s': Door %s (via device-specific stream)", device.name, state_str)

                                # Notify Home Assistant of the change
                                self.async_set_updated_data(self.account)

                    except Exception as err:
                        _LOGGER.error("Error processing device-specific stream update for %s: %s", device_id, err, exc_info=True)

            except asyncio.CancelledError:
                _LOGGER.info("Device-specific streaming cancelled for device %s", device_id)
                raise

            except Exception as err:
                retry_count += 1
                was_disconnected = True
                _LOGGER.error(
                    "Error in device-specific streaming for %s (attempt %d/%d): %s",
                    device_id,
                    retry_count,
                    max_retries,
                    err,
                    exc_info=True
                )
                if retry_count < max_retries:
                    wait_time = min(5 * (2 ** (retry_count - 1)), 60)
                    await asyncio.sleep(wait_time)
                else:
                    _LOGGER.error("Max retries reached for device-specific streaming %s, giving up", device_id)
                    break

    async def _async_fast_poll_door_sensor(self, space_id: str, device_id: str) -> None:
        """Fast poll a door sensor after opening to quickly detect closure.

        When a door opens (detected via stream), we start polling it every 3 seconds
        for up to 2 minutes to quickly detect when it closes.
        """
        poll_interval = 3  # Poll every 3 seconds
        max_duration = 120  # Stop after 2 minutes
        start_time = asyncio.get_event_loop().time()

        _LOGGER.info("Starting fast polling for door sensor %s", device_id)

        try:
            while True:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > max_duration:
                    _LOGGER.info("Fast polling timeout for door sensor %s after %d seconds", device_id, int(elapsed))
                    break

                try:
                    # Refresh device data from API
                    devices_data = await self.api.async_get_devices(space_id)

                    # Update account with fresh data (similar to _async_update_data)
                    space = self.account.spaces.get(space_id)
                    if not space:
                        break

                    # Find our device in the updated data
                    device_found = False
                    for device_data in devices_data:
                        if device_data.get("id") == device_id:
                            device_found = True
                            door_opened = device_data.get("attributes", {}).get("door_opened", False)

                            # Update the device in our account
                            if device_id in space.devices:
                                device = space.devices[device_id]
                                device.attributes.update(device_data.get("attributes", {}))

                            # If door is now closed, stop polling
                            if not door_opened:
                                _LOGGER.info("Door sensor %s closed, stopping fast polling", device_id)
                                # Notify HA of the change
                                self.async_set_updated_data(self.account)
                                break

                    if not device_found:
                        _LOGGER.warning("Device %s not found in API response", device_id)
                        break

                except Exception as err:
                    _LOGGER.error("Error in fast polling for door sensor %s: %s", device_id, err)

                # Wait before next poll
                await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            _LOGGER.info("Fast polling cancelled for door sensor %s", device_id)
            raise
        finally:
            # Clean up task from dictionary
            if device_id in self._fast_poll_tasks:
                del self._fast_poll_tasks[device_id]

    async def _async_poll_wire_inputs(self, space_id: str) -> None:
        """Poll wire_input devices every 10 seconds to get their states.

        Wire_input (EOL sensors) states are not transmitted via streaming API,
        so we need to poll them periodically.
        """
        poll_interval = 10  # seconds
        _LOGGER.info("Starting wire_input polling for space %s (interval: %ds)", space_id, poll_interval)

        try:
            while True:
                try:
                    # Get fresh device data from API
                    devices_data = await self.api.async_get_devices(space_id)

                    if not devices_data:
                        _LOGGER.debug("No devices returned from polling for space %s", space_id)
                        await asyncio.sleep(poll_interval)
                        continue

                    # Update only wire_input devices
                    space = self.account.spaces.get(space_id)
                    if space:
                        updated_count = 0
                        for device_data in devices_data:
                            device_id = device_data.get("id")
                            device_type = device_data.get("type")

                            # Only process wire_input devices
                            if device_type != "wire_input" or not device_id:
                                continue

                            # Check if device exists in our data
                            existing_device = space.devices.get(device_id)
                            if not existing_device:
                                continue

                            # Check if door_opened state changed
                            new_door_opened = device_data.get("attributes", {}).get("door_opened", False)
                            old_door_opened = existing_device.attributes.get("door_opened", False)

                            if new_door_opened != old_door_opened:
                                _LOGGER.info(
                                    "Wire_input '%s' state changed: %s -> %s",
                                    existing_device.name,
                                    "open" if old_door_opened else "closed",
                                    "open" if new_door_opened else "closed"
                                )
                                # Update the device attributes
                                existing_device.attributes["door_opened"] = new_door_opened
                                updated_count += 1

                        # Notify Home Assistant of changes if any updates occurred
                        if updated_count > 0:
                            _LOGGER.debug("Updated %d wire_input device(s), notifying listeners", updated_count)
                            self.async_update_listeners()

                except asyncio.CancelledError:
                    _LOGGER.info("Wire_input polling cancelled for space %s", space_id)
                    raise

                except Exception as err:
                    _LOGGER.error("Error polling wire_input devices for space %s: %s", space_id, err, exc_info=True)

                # Wait before next poll
                await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            _LOGGER.info("Wire_input polling stopped for space %s", space_id)
            raise

    async def _async_process_device_update(self, space_id: str, update: dict) -> None:
        """Process a device status update from the stream."""
        try:
            update_type = update.get("type")

            # Process snapshot (full device state)
            # Snapshots arrive périodiquement (~60s) avec l'état complet de tous les devices
            # C'est le seul moyen de détecter la fermeture de porte en temps réel
            if update_type == "snapshot":
                _LOGGER.debug("Processing device snapshot for space %s", space_id)
                devices = update.get("devices", [])

                space = self.account.spaces.get(space_id)
                if not space:
                    return

                # Update all devices from snapshot
                for device_data in devices:
                    device_id = device_data.get("id")
                    if not device_id or device_id not in space.devices:
                        continue

                    device = space.devices[device_id]
                    attributes = device_data.get("attributes", {})

                    # Update door_opened state from snapshot
                    # If door_opened is not in attributes, it means the door is closed
                    old_door_state = device.attributes.get("door_opened", False)
                    new_door_state = attributes.get("door_opened", False)

                    if old_door_state != new_door_state:
                        device.attributes["door_opened"] = new_door_state
                        state_str = "opened" if new_door_state else "closed"
                        _LOGGER.info("Device '%s': Door %s (via snapshot)", device.name, state_str)
                        # Notify Home Assistant of change
                        self.async_set_updated_data(self.account)

                return

            # Process real-time update
            if update_type == "update":
                update_data = update.get("data", {})
                device_id = update_data.get("device_id")
                status_data = update_data.get("status", {})

                if not device_id or not status_data:
                    return

                # Get the device
                space = self.account.spaces.get(space_id)
                if not space:
                    return

                device = space.devices.get(device_id)
                if not device:
                    return

                # Update device attributes based on status data
                updated = False

                # Motion detected
                if "motion_detected" in status_data:
                    device.attributes["motion_detected"] = status_data["motion_detected"]
                    if "motion_detected_at" in status_data:
                        device.attributes["motion_detected_at"] = status_data["motion_detected_at"].isoformat()
                    _LOGGER.info("Device '%s': Motion status updated via stream: %s", device.name, status_data["motion_detected"])
                    updated = True

                # Door opened/closed
                if "door_opened" in status_data:
                    old_state = device.attributes.get("door_opened", False)
                    new_state = status_data["door_opened"]
                    if old_state != new_state:
                        device.attributes["door_opened"] = new_state
                        state_str = "opened" if new_state else "closed"
                        _LOGGER.info("Device '%s': Door %s (via stream)", device.name, state_str)
                        updated = True

                        # Start fast polling when door opens to quickly detect closure
                        if new_state and device_id not in self._fast_poll_tasks:
                            _LOGGER.info("Door opened, starting fast polling for device %s", device_id)
                            task = asyncio.create_task(self._async_fast_poll_door_sensor(space_id, device_id))
                            self._fast_poll_tasks[device_id] = task
                        # Cancel fast polling if door closed (detected via stream or snapshot)
                        elif not new_state and device_id in self._fast_poll_tasks:
                            _LOGGER.info("Door closed, cancelling fast polling for device %s", device_id)
                            task = self._fast_poll_tasks[device_id]
                            task.cancel()
                            del self._fast_poll_tasks[device_id]

                # Notify Home Assistant of changes
                if updated:
                    self.async_set_updated_data(self.account)

        except Exception as err:
            _LOGGER.error("Error processing device update for space %s: %s", space_id, err, exc_info=True)

    async def _async_process_notification_event(self, space_id: str, event) -> None:
        """Process a notification event from the stream."""
        from datetime import datetime

        try:
            space = self.account.spaces.get(space_id)
            if not space:
                return

            # Check event type
            if event.HasField("notification_created"):
                notification_proto = event.notification_created

                # Parse notification
                notification_data = self.api._parse_notification(notification_proto)
                if not notification_data:
                    return

                # Extract event details
                event_type = notification_data.get("event_type", "") or ""
                device_id = notification_data.get("device_id")
                device_name = notification_data.get("device_name", "Device")
                timestamp = notification_data.get("timestamp") or datetime.now(timezone.utc)

                # Ensure timezone is set
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)

                # Determine notification type
                notif_type = self._parse_notification_type(event_type)

                # Get room name if available
                room_name = None
                if device_id and device_id in space.devices:
                    device = space.devices[device_id]
                    if device.room_id and device.room_id in space.rooms:
                        room_name = space.rooms[device.room_id].name

                # Get language from Home Assistant (default to English)
                language = self.hass.config.language if self.hass.config.language in ["en", "fr"] else "en"

                # Format message like the Ajax app (only if event_type is not empty)
                if event_type:
                    formatted_message = get_event_message(
                        event_type,
                        language=language,
                        device_name=device_name,
                        room_name=room_name,
                    )
                else:
                    # Fallback for notifications without event_type (e.g., arming/disarming)
                    formatted_message = notification_data.get("message", "") or device_name

                # Create notification object
                notification = AjaxNotification(
                    id=notification_data.get("id", ""),
                    space_id=space_id,
                    type=notif_type,
                    title=event_type,  # Keep raw event type for filtering/processing
                    message=formatted_message,  # Use formatted message
                    timestamp=timestamp,
                    device_id=device_id,
                    device_name=device_name,
                    read=notification_data.get("read", False),
                )

                # Add to space notifications list (keep only last 50)
                space.notifications.insert(0, notification)
                if len(space.notifications) > 50:
                    space.notifications = space.notifications[:50]

                # Update device state based on notification
                if device_id and event_type:
                    self._update_device_from_notification(space, notification)

                # Create persistent notification if enabled
                await self._create_persistent_notification(
                    notification, formatted_message, notif_type
                )

                # Fire Home Assistant event for automations
                await self._fire_ajax_event(
                    space, notification, event_type, device_id, device_name, room_name, timestamp
                )

                # Trigger update
                _LOGGER.info(
                    "Real-time notification: %s (%s)",
                    formatted_message,
                    timestamp.strftime("%H:%M:%S"),
                )
                self.async_set_updated_data(self.account)

            elif event.HasField("notification_updated"):
                # Notification was updated (e.g., marked as read)
                # We could update the notification in our list, but for now just log it
                pass

            elif event.HasField("counters_updated"):
                # Unread counter updated
                pass

        except Exception as err:
            _LOGGER.error("Error processing notification event for space %s: %s", space_id, err, exc_info=True)

    async def _async_parse_groups_from_snapshot(self, space_id: str, space_protobuf) -> None:
        """Parse groups from Space snapshot and update the data model.

        Args:
            space_id: The space ID
            space_protobuf: The Space protobuf message
        """
        try:
            # Use the API client's parsing method
            groups_data = self.api._parse_groups_from_space(space_protobuf)

            # Get the space from our account
            if not self.account or space_id not in self.account.spaces:
                _LOGGER.warning("Space %s not found in account", space_id)
                return

            space = self.account.spaces[space_id]

            # Update space with group mode settings
            space.group_mode_enabled = groups_data["group_mode_enabled"]
            space.night_mode_enabled = groups_data["night_mode_enabled"]

            # Convert parsed groups to AjaxGroup objects
            from .models import AjaxGroup, GroupState

            for group_id, group_data in groups_data["groups"].items():
                # Map string state to GroupState enum
                state_str = group_data.get("state", "none")
                if state_str == "armed":
                    state = GroupState.ARMED
                elif state_str == "disarmed":
                    state = GroupState.DISARMED
                else:
                    state = GroupState.NONE

                # Create or update the group
                ajax_group = AjaxGroup(
                    id=group_id,
                    name=group_data["name"],
                    space_id=space_id,
                    state=state,
                    night_mode_enabled=group_data.get("night_mode_enabled", False),
                    bulk_arm_involved=group_data.get("bulk_arm_involved", False),
                    bulk_disarm_involved=group_data.get("bulk_disarm_involved", False),
                    image_id=group_data.get("image_id"),
                )

                space.groups[group_id] = ajax_group

            _LOGGER.info(
                "Updated space %s with %d groups (group_mode=%s)",
                space_id,
                len(space.groups),
                space.group_mode_enabled,
            )

        except Exception as err:
            _LOGGER.exception("Error parsing groups from snapshot: %s", err)

    async def _async_parse_rooms_from_snapshot(self, space_id: str, space_protobuf) -> None:
        """Parse rooms from Space snapshot and update the data model.

        Args:
            space_id: The space ID
            space_protobuf: The Space protobuf message
        """
        try:
            # Use the API client's parsing method
            rooms_data = self.api._parse_rooms_from_space(space_protobuf)

            # Get the space from our account
            if not self.account or space_id not in self.account.spaces:
                _LOGGER.warning("Space %s not found in account", space_id)
                return

            space = self.account.spaces[space_id]

            # Convert parsed rooms to AjaxRoom objects
            for room_id, room_data in rooms_data.items():
                # Create or update the room
                ajax_room = AjaxRoom(
                    id=room_id,
                    name=room_data["name"],
                    space_id=space_id,
                    image_id=room_data.get("image_id"),
                    image_url=room_data.get("image_url"),
                )

                space.rooms[room_id] = ajax_room

            _LOGGER.info(
                "Updated space %s with %d rooms",
                space_id,
                len(space.rooms),
            )

        except Exception as err:
            _LOGGER.exception("Error parsing rooms from snapshot: %s", err)

    async def _async_process_stream_update(self, space_id: str, success) -> None:
        """Process a single stream update."""
        try:
            # Check if it's a snapshot (initial data) or an update
            if success.HasField("snapshot"):
                # The snapshot IS the Space object, parse it directly
                await self._async_parse_groups_from_snapshot(space_id, success.snapshot)
                await self._async_parse_rooms_from_snapshot(space_id, success.snapshot)
                # Trigger a refresh to ensure consistency
                self.async_set_updated_data(self.account)

            elif success.HasField("update"):
                # Single update
                await self._async_handle_single_update(space_id, success.update, batch_mode=False)

            elif success.HasField("updates"):
                # Multiple updates - use batch mode to avoid multiple refreshes
                for update in success.updates.updates:
                    await self._async_handle_single_update(space_id, update, batch_mode=True)
                # Trigger single refresh at the end
                self.async_set_updated_data(self.account)

        except Exception as err:
            _LOGGER.error("Error processing stream update for space %s: %s", space_id, err)

    async def _async_handle_single_update(self, space_id: str, update, batch_mode: bool = False) -> None:
        """Handle a single update from the stream.

        Args:
            space_id: The space ID
            update: The update protobuf message
            batch_mode: If True, don't trigger async_set_updated_data (for batch processing)
        """
        try:
            # Security mode update
            if update.HasField("security_mode"):
                security_mode = update.security_mode
                space = self.account.spaces.get(space_id)
                if space:
                    try:
                        # Check if it's group mode
                        if security_mode.HasField("group_mode"):
                            from .models import GroupState

                            group_mode = security_mode.group_mode
                            space.group_mode_enabled = True

                            # Update night mode
                            if hasattr(group_mode, "night_mode_enabled"):
                                space.night_mode_enabled = group_mode.night_mode_enabled

                            # Update group states
                            if hasattr(group_mode, "groups") and group_mode.groups:
                                for group_security in group_mode.groups:
                                    group_id = group_security.group_id if hasattr(group_security, "group_id") else None
                                    if not group_id or group_id not in space.groups:
                                        continue

                                    group = space.groups[group_id]

                                    # Parse state
                                    if hasattr(group_security, "state"):
                                        state_value = group_security.state
                                        if state_value == 1:  # GROUP_SECURITY_STATE_ARMED
                                            group.state = GroupState.ARMED
                                        elif state_value == 2:  # GROUP_SECURITY_STATE_DISARMED
                                            group.state = GroupState.DISARMED
                                        else:
                                            group.state = GroupState.NONE

                                    # Parse night mode for this group
                                    if hasattr(group_security, "transition") and group_security.transition:
                                        transition = group_security.transition
                                        if hasattr(transition, "desired_state") and transition.desired_state:
                                            desired = transition.desired_state
                                            if hasattr(desired, "night_mode_enabled"):
                                                group.night_mode_enabled = desired.night_mode_enabled

                            # Determine overall space security state based on armed groups
                            armed_groups = [g for g in space.groups.values() if g.state == GroupState.ARMED]
                            night_mode_groups = [g for g in space.groups.values() if g.night_mode_enabled]
                            _LOGGER.debug(
                                "Night mode detection for space %s: %d/%d groups armed, %d/%d with night_mode, groups: %s",
                                space.name,
                                len(armed_groups),
                                len(space.groups),
                                len(night_mode_groups),
                                len(space.groups),
                                {g.name: {"state": g.state, "night_mode": g.night_mode_enabled} for g in space.groups.values()}
                            )

                            # Check if night mode is active (any group has night_mode_enabled)
                            if night_mode_groups:
                                space.security_state = SecurityState.NIGHT_MODE
                                _LOGGER.info("Space %s: NIGHT_MODE (%d/%d groups with night_mode enabled)", space.name, len(night_mode_groups), len(space.groups))
                            elif not armed_groups:
                                space.security_state = SecurityState.DISARMED
                                _LOGGER.debug("Space %s: DISARMED (no armed groups, no night mode)", space.name)
                            elif len(armed_groups) == len(space.groups):
                                # Check if all armed groups have night mode enabled
                                all_night_mode = all(g.night_mode_enabled for g in armed_groups)
                                if all_night_mode:
                                    space.security_state = SecurityState.NIGHT_MODE
                                    _LOGGER.info("Space %s: NIGHT_MODE (all %d groups armed with night mode)", space.name, len(armed_groups))
                                else:
                                    space.security_state = SecurityState.ARMED
                                    _LOGGER.debug("Space %s: ARMED (all %d groups armed, night mode: %s)", space.name, len(armed_groups), [g.night_mode_enabled for g in armed_groups])
                            else:
                                # Partially armed - check if any armed groups have night mode
                                any_night_mode = any(g.night_mode_enabled for g in armed_groups if g.state == GroupState.ARMED)
                                if any_night_mode:
                                    space.security_state = SecurityState.NIGHT_MODE
                                    _LOGGER.info("Space %s: NIGHT_MODE (partial: %d/%d groups armed, at least one with night mode)", space.name, len(armed_groups), len(space.groups))
                                else:
                                    space.security_state = SecurityState.PARTIALLY_ARMED
                                    _LOGGER.debug("Space %s: PARTIALLY_ARMED (%d/%d groups armed, no night mode)", space.name, len(armed_groups), len(space.groups))

                            if not batch_mode:
                                self.async_set_updated_data(self.account)

                        # Regular mode (not group mode)
                        elif security_mode.HasField("regular_mode"):
                            space.group_mode_enabled = False
                            mode_value = security_mode.regular_mode

                            if mode_value and hasattr(mode_value, "space_state"):
                                # space_state is an enum: 1=ARMED, 2=DISARMED, 3=NIGHT_MODE
                                state_int = int(mode_value.space_state)

                                # Map to internal state
                                if state_int == 2:
                                    new_state = SecurityState.DISARMED
                                elif state_int == 3:
                                    new_state = SecurityState.NIGHT_MODE
                                elif state_int == 1:
                                    new_state = SecurityState.ARMED
                                else:
                                    _LOGGER.warning("Unknown security state value: %d", state_int)
                                    new_state = None

                                # Update if state changed
                                if new_state and space.security_state != new_state:
                                    old_state = space.security_state
                                    space.security_state = new_state
                                    _LOGGER.info(
                                        "Security state changed for %s: %s -> %s (from regular_mode)",
                                        space.name,
                                        old_state.value,
                                        new_state.value
                                    )
                                    # Fire event for state change
                                    self._fire_security_state_event(space, old_state, new_state)
                                    if not batch_mode:
                                        self.async_set_updated_data(self.account)

                        # Check displayed_security_state as fallback
                        elif hasattr(security_mode, "displayed_security_state"):
                            mode_value = security_mode.displayed_security_state

                            if mode_value and hasattr(mode_value, "space_state"):
                                state_int = int(mode_value.space_state)

                                if state_int == 2:
                                    new_state = SecurityState.DISARMED
                                elif state_int == 3:
                                    new_state = SecurityState.NIGHT_MODE
                                elif state_int == 1:
                                    new_state = SecurityState.ARMED
                                else:
                                    _LOGGER.warning("Unknown security state value: %d", state_int)
                                    new_state = None

                                if new_state and space.security_state != new_state:
                                    old_state = space.security_state
                                    space.security_state = new_state
                                    _LOGGER.info(
                                        "Security state changed for %s: %s -> %s (from displayed_security_state)",
                                        space.name,
                                        old_state.value,
                                        new_state.value
                                    )
                                    # Fire event for state change
                                    self._fire_security_state_event(space, old_state, new_state)
                                    if not batch_mode:
                                        self.async_set_updated_data(self.account)

                        # Signal that groups/security data has been loaded from stream
                        # This unblocks the initial setup waiting for group entities
                        if space_id in self._groups_loaded_events:
                            self._groups_loaded_events[space_id].set()

                    except Exception as mode_err:
                        _LOGGER.error("Error parsing security mode: %s", mode_err)

            # Group update
            elif update.HasField("group"):
                group_proto = update.group
                space = self.account.spaces.get(space_id)
                if space and hasattr(group_proto, "id"):
                    await self._async_handle_group_update(space, group_proto, update)

            # Device update (from stream_space_updates)
            elif update.HasField("device"):
                _LOGGER.debug("Received device update from stream_space_updates")
                device_proto = update.device

                # Log the entire device update for debugging wire_input states
                _LOGGER.debug("Device update type: %s", type(device_proto).__name__)
                _LOGGER.debug("Device update fields: %s", device_proto)

                # Check if it's a hub device (contains wire_input devices)
                if device_proto.HasField("hub"):
                    hub = device_proto.hub
                    _LOGGER.debug("Hub device update - ID: %s", hub.id if hasattr(hub, "id") else "unknown")

                    # Log all fields to see what's available
                    for field in hub.DESCRIPTOR.fields:
                        if hub.HasField(field.name):
                            field_value = getattr(hub, field.name)
                            _LOGGER.debug("  Hub field '%s': %s", field.name, field_value)

        except Exception as err:
            _LOGGER.error("Error handling stream update: %s", err)

    async def _async_handle_group_update(self, space, group_proto, update) -> None:
        """Handle a group update from the stream.

        Args:
            space: The AjaxSpace object
            group_proto: The Group protobuf message
            update: The Update message containing space_update_type
        """
        try:
            from .models import AjaxGroup, GroupState

            group_id = group_proto.id
            update_type = update.space_update_type if hasattr(update, "space_update_type") else 0

            # SPACE_UPDATE_TYPE_REMOVE = 3
            if update_type == 3:
                # Group was removed
                if group_id in space.groups:
                    del space.groups[group_id]
                    _LOGGER.info("Group %s removed from space %s", group_id, space.name)
                    self.async_set_updated_data(self.account)
                return

            # SPACE_UPDATE_TYPE_ADD = 1 or SPACE_UPDATE_TYPE_UPDATE = 2
            # Extract group metadata
            group_name = group_proto.name if hasattr(group_proto, "name") else f"Group {group_id}"
            bulk_arm = group_proto.bulk_arm_involved if hasattr(group_proto, "bulk_arm_involved") else False
            bulk_disarm = group_proto.bulk_disarm_involved if hasattr(group_proto, "bulk_disarm_involved") else False

            # Extract image ID
            image_id = None
            if hasattr(group_proto, "images") and group_proto.images:
                images = group_proto.images
                if hasattr(images, "light") and images.light:
                    image_id = images.light if isinstance(images.light, str) else None

            # Update or create the group
            if group_id in space.groups:
                # Update existing group
                group = space.groups[group_id]
                group.name = group_name
                group.bulk_arm_involved = bulk_arm
                group.bulk_disarm_involved = bulk_disarm
                group.image_id = image_id
            else:
                # Create new group (state will be updated later via security_mode updates)
                group = AjaxGroup(
                    id=group_id,
                    name=group_name,
                    space_id=space.id,
                    state=GroupState.NONE,
                    bulk_arm_involved=bulk_arm,
                    bulk_disarm_involved=bulk_disarm,
                    image_id=image_id,
                )
                space.groups[group_id] = group
                _LOGGER.info("Added new group %s to space %s", group_name, space.name)

            self.async_set_updated_data(self.account)

        except Exception as err:
            _LOGGER.exception("Error handling group update: %s", err)


    async def _async_init_account(self) -> None:
        """Initialize the account data."""
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
        spaces_data = await self.api.async_get_spaces()

        # Get selected spaces from config entry (default to all if not specified)
        selected_spaces = self.config_entry.data.get("selected_spaces")
        if selected_spaces:
            _LOGGER.info("Loading selected spaces: %s", selected_spaces)

        for space_data in spaces_data:
            space_id = space_data.get("id")
            if not space_id:
                continue

            # Skip spaces not in selection (if selection is defined)
            if selected_spaces and space_id not in selected_spaces:
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

        space = self.account.spaces.get(space_id)
        if not space:
            return

        devices_data = await self.api.async_get_devices(space_id)

        new_devices_count = 0

        for device_data in devices_data:
            device_id = device_data.get("id")
            if not device_id:
                continue

            # TEMPORARY: Ignore buggy door sensor (device ID: 30DD9A8B)
            if device_id == "30DD9A8B":
                _LOGGER.debug("Ignoring buggy device 30DD9A8B (Capteur Porte d'entrée)")
                continue

            # Parse device type
            raw_device_type = device_data.get("type", "unknown")
            device_type = self._parse_device_type(raw_device_type)

            # Create or update device
            if device_id not in space.devices:
                device = AjaxDevice(
                    id=device_id,
                    name=device_data.get("name", "Unknown Device"),
                    type=device_type,
                    space_id=space_id,
                    hub_id=device_data.get("hub_id", space.hub_id or ""),
                    raw_type=raw_device_type,
                    room_id=device_data.get("room_id"),
                    group_id=device_data.get("group_id"),
                )
                space.devices[device_id] = device
                new_devices_count += 1

                # Log warning for unknown device types
                if device_type == DeviceType.UNKNOWN:
                    _LOGGER.warning(
                        "Unknown device type detected: '%s' for device '%s' (ID: %s). "
                        "Please report this to the integration developer.",
                        raw_device_type,
                        device.name,
                        device_id,
                    )
                else:
                    _LOGGER.debug("Added new device: %s (%s)", device.name, device.type.value)
            else:
                device = space.devices[device_id]
                # Update raw_type in case it changed
                device.raw_type = raw_device_type

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
                    hub_obj_data = await self.api.async_stream_hub_object(device.id)
                    if hub_obj_data:
                        _LOGGER.debug("Received HubObject data for hub %s", device.id)

                        # Process SIM card data from HubObject
                        if "sim_card" in hub_obj_data:
                            sim_card = hub_obj_data["sim_card"]
                            sim_card_status = sim_card.get("sim_card_status", "UNKNOWN")
                            active_sim = sim_card.get("active_sim_card", 1)

                            # Create sim_cards list in the same format as _parse_light_device
                            sim_info = {
                                "status": sim_card_status,
                                "installed": sim_card_status not in ["MISSING", "NOT_INSTALLED", "2"],
                                "slot": active_sim,
                                "imei": sim_card.get("imei"),
                            }

                            # Determine total slots based on hub model
                            # Hub 2 Plus and Hub 2 (4G) have 2 SIM slots
                            device_type_lower = (device.raw_type or "").lower()
                            if "plus" in device_type_lower or "4g" in device_type_lower or "two" in device_type_lower:
                                total_slots = 2
                            else:
                                total_slots = 1

                            installed_count = 1 if sim_info["installed"] else 0

                            # Create sim_status attributes
                            device.attributes["sim_status"] = f"{installed_count}/{total_slots}"
                            device.attributes["sim_cards"] = [sim_info]
                            device.attributes["sim_slots_total"] = total_slots
                            device.attributes["sim_slots_used"] = installed_count

                            _LOGGER.debug(
                                "Processed SIM data for hub %s: %s",
                                device.id,
                                device.attributes["sim_status"]
                            )

                        # Merge other hub_obj_data (except sim_card which we processed)
                        for key, value in hub_obj_data.items():
                            if key != "sim_card":
                                device.attributes[key] = value
                    else:
                        _LOGGER.warning("No HubObject data received for hub %s", device.id)
                except Exception as err:
                    _LOGGER.error("Failed to fetch HubObject for hub %s: %s", device.id, err)

            # Update room association
            if device.room_id and device.room_id in space.rooms:
                room = space.rooms[device.room_id]
                if device_id not in room.device_ids:
                    room.device_ids.append(device_id)

        # Log summary of devices loaded
        if new_devices_count > 0:
            _LOGGER.info("Discovered %d new device(s) in space %s", new_devices_count, space_id)
        _LOGGER.debug("Total devices in space %s: %d", space_id, len(space.devices))

    def _reset_expired_motion_detections(self, space: AjaxSpace) -> None:
        """Reset motion_detected to False for motion detectors if no recent detection.

        Motion detection events are impulse-based (not persistent state),
        so we reset them after 30 seconds of no new detection.

        Args:
            space: The AjaxSpace to process
        """
        from datetime import timezone, timedelta

        now = datetime.now(timezone.utc)
        expiry_seconds = 30  # Reset motion detection after 30 seconds

        for device in space.devices.values():
            # Only process motion detectors
            if device.type != DeviceType.MOTION_DETECTOR:
                continue

            # Check if motion_detected is currently True
            if not device.attributes.get("motion_detected"):
                continue

            # Get last detection time
            last_detected_at = device.attributes.get("motion_detected_at")
            if not last_detected_at:
                # No timestamp, reset immediately
                device.attributes["motion_detected"] = False
                _LOGGER.debug("Device '%s': Motion reset (no timestamp)", device.name)
                continue

            # Parse timestamp
            try:
                last_detected = datetime.fromisoformat(last_detected_at)
                # Make timezone-aware if needed
                if last_detected.tzinfo is None:
                    last_detected = last_detected.replace(tzinfo=timezone.utc)

                # Check if expired
                if (now - last_detected).total_seconds() > expiry_seconds:
                    device.attributes["motion_detected"] = False
                    _LOGGER.debug(
                        "Device '%s': Motion reset (expired after %d seconds)",
                        device.name,
                        expiry_seconds
                    )
            except (ValueError, TypeError) as err:
                _LOGGER.warning(
                    "Failed to parse motion_detected_at timestamp for %s: %s",
                    device.name,
                    err
                )

    async def _async_update_notifications(
        self, space_id: str, limit: int = 50
    ) -> None:
        """Update notifications for a specific space."""
        space = self.account.spaces.get(space_id)
        if not space:
            return

        try:
            # Fetch notifications from API
            notifications_data = await self.api.async_find_notifications(space_id, limit)

            # Clear existing notifications and add new ones
            space.notifications.clear()

            for notif_data in notifications_data:
                # Parse notification data
                from datetime import datetime
                from .models import AjaxNotification, NotificationType

                # Determine notification type based on event_type
                event_type = notif_data.get("event_type", "")
                notif_type = self._parse_notification_type(event_type)

                # Create notification object
                notification = AjaxNotification(
                    id=notif_data.get("id", ""),
                    space_id=notif_data.get("space_id", space_id),
                    type=notif_type,
                    title=event_type.replace("_", " ").title() if event_type else "Event",
                    message=f"{notif_data.get('device_name', 'Device')}: {event_type.replace('_', ' ')}" if event_type else "",
                    timestamp=notif_data.get("timestamp") or datetime.now(),
                    device_id=notif_data.get("device_id"),
                    device_name=notif_data.get("device_name"),
                    read=notif_data.get("read", False),
                )

                space.notifications.append(notification)

                # Note: We do NOT update device state from historical notifications
                # Device state comes from the real-time device snapshot only
                # Real-time notification updates happen via the stream (async_stream_notification_updates)

            # Update unread count
            space.unread_notifications = sum(1 for n in space.notifications if not n.read)

            _LOGGER.info(
                "Updated notifications for space %s: %d total, %d unread",
                space_id,
                len(space.notifications),
                space.unread_notifications,
            )

        except Exception as err:
            _LOGGER.error("Error updating notifications for space %s: %s", space_id, err, exc_info=True)

    def _parse_notification_type(self, event_type: str | None) -> NotificationType:
        """Parse notification type from event type string."""
        from .models import NotificationType

        if not event_type:
            return NotificationType.INFO

        event_lower = event_type.lower()

        if any(keyword in event_lower for keyword in ["alarm", "intrusion", "panic", "fire", "smoke", "leak", "gas"]):
            return NotificationType.ALARM
        elif any(keyword in event_lower for keyword in ["malfunction", "low", "tamper", "loss", "error", "fault"]):
            return NotificationType.WARNING
        elif any(keyword in event_lower for keyword in ["arm", "disarm", "motion", "door", "opened"]):
            return NotificationType.SECURITY_EVENT
        elif any(keyword in event_lower for keyword in ["update", "added", "changed", "test"]):
            return NotificationType.SYSTEM_EVENT
        else:
            return NotificationType.INFO

    async def _create_persistent_notification(
        self,
        notification: AjaxNotification,
        formatted_message: str,
        notif_type: NotificationType,
    ) -> None:
        """Create a persistent notification in Home Assistant if enabled and passes filter.

        Args:
            notification: The Ajax notification object
            formatted_message: The formatted notification message
            notif_type: The notification type (ALARM, WARNING, etc.)
        """
        # Get config options
        config_entry = self.config_entry
        options = config_entry.options if config_entry else {}

        # Check if persistent notifications are enabled
        if not options.get(CONF_PERSISTENT_NOTIFICATION, False):
            return

        # Get notification filter
        notification_filter = options.get(CONF_NOTIFICATION_FILTER, NOTIFICATION_FILTER_NONE)

        # Check if notification passes filter
        if notification_filter == NOTIFICATION_FILTER_NONE:
            return
        elif notification_filter == NOTIFICATION_FILTER_ALARMS_ONLY:
            # Only show alarms
            if notif_type != NotificationType.ALARM:
                return
        elif notification_filter == NOTIFICATION_FILTER_SECURITY_EVENTS:
            # Show alarms and security events (arming/disarming)
            if notif_type not in [NotificationType.ALARM, NotificationType.SECURITY_EVENT]:
                return
        # NOTIFICATION_FILTER_ALL: show all notifications (no filter)

        # Ensure we have a valid message
        if not formatted_message:
            return

        # Create persistent notification in Home Assistant
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Ajax Security",
                "message": formatted_message,
                "notification_id": f"ajax_{notification.id}",
            },
        )

    async def _fire_ajax_event(
        self,
        space: AjaxSpace,
        notification: AjaxNotification,
        event_type: str,
        device_id: str | None,
        device_name: str,
        room_name: str | None,
        timestamp,
    ) -> None:
        """Fire a Home Assistant event for Ajax notifications.

        This allows users to create automations based on Ajax events.

        Args:
            space: The AjaxSpace object
            notification: The AjaxNotification object
            event_type: The raw event type from Ajax
            device_id: Device ID that triggered the event
            device_name: Device name
            room_name: Room name if available
            timestamp: Event timestamp
        """
        if not event_type:
            return

        event_type_lower = event_type.lower()

        # Map Ajax event types to HA event names
        event_mapping = {
            "motion_detected": "ajax_motion_detected",
            "door_opened": "ajax_door_opened",
            "door_closed": "ajax_door_closed",
            "window_opened": "ajax_window_opened",
            "window_closed": "ajax_window_closed",
            "alarm_triggered": "ajax_alarm_triggered",
            "tamper": "ajax_device_tampered",
            "tampered": "ajax_device_tampered",
            "armed": "ajax_armed",
            "disarmed": "ajax_disarmed",
            "smoke_detected": "ajax_smoke_detected",
            "leak_detected": "ajax_leak_detected",
            "gas_detected": "ajax_gas_detected",
            "glass_break_detected": "ajax_glass_break_detected",
        }

        # Find matching event
        ha_event_name = None
        for key, event_name in event_mapping.items():
            if key in event_type_lower:
                ha_event_name = event_name
                break

        # If no specific mapping, use generic event
        if not ha_event_name:
            ha_event_name = "ajax_event"

        # Get device type if available
        device_type = None
        if device_id and device_id in space.devices:
            device = space.devices[device_id]
            device_type = device.type.value if device.type else None

        # Prepare event data
        event_data = {
            "space_id": space.id,
            "space_name": space.name,
            "event_type": event_type,
            "notification_type": notification.type.value,
            "message": notification.message,
            "timestamp": timestamp.isoformat(),
            "device_name": device_name,
        }

        # Add optional fields if available
        if device_id:
            event_data["device_id"] = device_id
        if device_type:
            event_data["device_type"] = device_type
        if room_name:
            event_data["room_name"] = room_name

        # Fire the event
        self.hass.bus.async_fire(ha_event_name, event_data)

        _LOGGER.debug(
            "Fired event '%s' for device %s in room %s",
            ha_event_name,
            device_name,
            room_name or "unknown",
        )

    def _fire_security_state_event(
        self,
        space: AjaxSpace,
        old_state: SecurityState,
        new_state: SecurityState,
    ) -> None:
        """Fire a Home Assistant event when security state changes.

        Args:
            space: The AjaxSpace object
            old_state: Previous security state
            new_state: New security state
        """
        from datetime import timezone

        # Determine event type based on new state
        if new_state == SecurityState.ARMED:
            event_name = "ajax_armed"
        elif new_state == SecurityState.DISARMED:
            event_name = "ajax_disarmed"
        elif new_state == SecurityState.NIGHT_MODE:
            event_name = "ajax_armed_night"
        elif new_state == SecurityState.PARTIALLY_ARMED:
            event_name = "ajax_armed_home"
        else:
            event_name = "ajax_security_state_changed"

        # Prepare event data
        event_data = {
            "space_id": space.id,
            "space_name": space.name,
            "old_state": old_state.value,
            "new_state": new_state.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Add group information if in group mode
        if space.group_mode_enabled and space.groups:
            armed_groups = [g.name for g in space.groups.values() if g.state == GroupState.ARMED]
            disarmed_groups = [g.name for g in space.groups.values() if g.state == GroupState.DISARMED]
            event_data["armed_groups"] = armed_groups
            event_data["disarmed_groups"] = disarmed_groups
            event_data["group_mode"] = True
        else:
            event_data["group_mode"] = False

        # Fire the event
        self.hass.bus.async_fire(event_name, event_data)

        _LOGGER.debug(
            "Fired event '%s' for space %s: %s -> %s",
            event_name,
            space.name,
            old_state.value,
            new_state.value,
        )

    def _update_device_from_notification(self, space: AjaxSpace, notification: AjaxNotification) -> None:
        """Update device state based on notification event."""

        device = space.devices.get(notification.device_id)
        if not device:
            return

        if not notification.title:
            return

        event_type = notification.title.lower()
        _LOGGER.debug("Updating device %s from notification: %s", device.name, event_type)

        # Update device attributes based on event type
        if "motion" in event_type or "mouvement" in event_type:
            # Motion detected event
            if "detected" in event_type or "détecté" in event_type:
                device.attributes["motion_detected"] = True
                device.attributes["motion_detected_at"] = notification.timestamp.isoformat()
                device.last_trigger_time = notification.timestamp.isoformat()
                _LOGGER.info("Device '%s': Motion detected", device.name)
            elif "cleared" in event_type or "norm" in event_type:
                device.attributes["motion_detected"] = False
                _LOGGER.info("Device '%s': Motion cleared", device.name)

        elif "door" in event_type or "porte" in event_type:
            # Door sensor event
            if "opened" in event_type or "open" in event_type or "ouvert" in event_type:
                device.attributes["door_opened"] = True
                _LOGGER.info("Device '%s': Door opened", device.name)

                # Start fast polling when door opens to quickly detect closure
                device_id = notification.device_id
                if device_id not in self._fast_poll_tasks:
                    _LOGGER.info("Door opened, starting fast polling for device %s", device_id)
                    task = asyncio.create_task(self._async_fast_poll_door_sensor(space.id, device_id))
                    self._fast_poll_tasks[device_id] = task

            elif "closed" in event_type or "close" in event_type or "fermé" in event_type:
                device.attributes["door_opened"] = False
                _LOGGER.info("Device '%s': Door closed", device.name)

                # Cancel fast polling if door closed
                device_id = notification.device_id
                if device_id in self._fast_poll_tasks:
                    _LOGGER.info("Door closed, cancelling fast polling for device %s", device_id)
                    task = self._fast_poll_tasks[device_id]
                    task.cancel()
                    del self._fast_poll_tasks[device_id]

        elif "window" in event_type or "fenêtre" in event_type:
            # Window sensor event (use same attribute as door)
            if "opened" in event_type or "open" in event_type:
                device.attributes["door_opened"] = True
                _LOGGER.info("Device '%s': Window opened", device.name)
            elif "closed" in event_type or "close" in event_type:
                device.attributes["door_opened"] = False
                _LOGGER.info("Device '%s': Window closed", device.name)

        elif "glass" in event_type or "verre" in event_type or "vitre" in event_type:
            # Glass break event
            if "detected" in event_type or "bris" in event_type or "cassé" in event_type:
                device.attributes["glass_break_detected"] = True
                device.attributes["glass_break_detected_at"] = notification.timestamp.isoformat()
                device.last_trigger_time = notification.timestamp.isoformat()
                _LOGGER.info("Device '%s': Glass break detected", device.name)
            elif "cleared" in event_type or "norm" in event_type:
                device.attributes["glass_break_detected"] = False

        elif "smoke" in event_type or "fire" in event_type or "fumée" in event_type or "incendie" in event_type:
            # Smoke/fire event
            if "detected" in event_type or "détecté" in event_type:
                device.attributes["smoke_detected"] = True
                device.attributes["smoke_detected_at"] = notification.timestamp.isoformat()
                device.last_trigger_time = notification.timestamp.isoformat()
                _LOGGER.info("Device '%s': Smoke detected", device.name)
            elif "cleared" in event_type or "norm" in event_type:
                device.attributes["smoke_detected"] = False

        elif "leak" in event_type or "water" in event_type or "flood" in event_type or "fuite" in event_type or "inondation" in event_type:
            # Water leak event
            if "detected" in event_type or "détecté" in event_type:
                device.attributes["leak_detected"] = True
                device.attributes["leak_detected_at"] = notification.timestamp.isoformat()
                device.last_trigger_time = notification.timestamp.isoformat()
                _LOGGER.info("Device '%s': Leak detected", device.name)
            elif "cleared" in event_type or "norm" in event_type:
                device.attributes["leak_detected"] = False

        elif "tamper" in event_type or "sabotage" in event_type:
            # Tamper event
            # NOTE: Ajax sends "tamper_opened" when the cover is CLOSED (tamper OK)
            # This is counter-intuitive but confirmed by testing
            if "opened" in event_type:
                # Cover is closed/normal - tamper OK
                device.attributes["tampered"] = False
                _LOGGER.info("Device '%s': Tamper cleared (cover closed)", device.name)
            elif "detected" in event_type or "triggered" in event_type or "détecté" in event_type or "closed" in event_type:
                # Cover was opened - tamper detected
                device.attributes["tampered"] = True
                device.attributes["tampered_at"] = notification.timestamp.isoformat()
                _LOGGER.warning("Device '%s': Tamper detected (cover opened)!", device.name)

        elif "external_contact" in event_type or "external contact" in event_type:
            # External contact event (for EOL/wire_input sensors)
            if "lost" in event_type or "open" in event_type:
                # Contact lost = door/window opened or wire disconnected
                device.attributes["door_opened"] = True
                device.attributes["external_contact_state"] = "lost"
                _LOGGER.info("Device '%s': External contact lost (opened)", device.name)
            elif "ok" in event_type or "closed" in event_type:
                # Contact OK = door/window closed or wire connected
                device.attributes["door_opened"] = False
                device.attributes["external_contact_state"] = "ok"
                _LOGGER.info("Device '%s': External contact OK (closed)", device.name)
            elif "short" in event_type or "circuit" in event_type:
                # Short circuit fault
                device.attributes["external_contact_state"] = "short_circuit"
                device.attributes["problem"] = True
                _LOGGER.warning("Device '%s': External contact short circuit", device.name)
            elif "fault" in event_type or "resistance" in event_type:
                # Resistance fault or hard fault
                device.attributes["external_contact_state"] = "fault"
                device.attributes["problem"] = True
                _LOGGER.warning("Device '%s': External contact fault", device.name)

        # Hub-specific events (battery, power, signal, antenna, noise, system faults)
        elif device.type == DeviceType.HUB:
            # Battery disconnected event
            if "battery" in event_type and ("disconnect" in event_type or "déconnect" in event_type or "batterie" in event_type):
                device.attributes["battery_disconnected"] = True
                device.attributes["battery_disconnected_at"] = notification.timestamp.isoformat()
                _LOGGER.warning("Hub '%s': Battery disconnected", device.name)
            elif "battery" in event_type and ("connect" in event_type or "reconnect" in event_type or "ok" in event_type):
                device.attributes["battery_disconnected"] = False
                _LOGGER.info("Hub '%s': Battery reconnected", device.name)

            # External power loss event
            elif "power" in event_type or "alimentation" in event_type:
                if "loss" in event_type or "lost" in event_type or "perte" in event_type:
                    device.attributes["external_power_loss"] = True
                    device.attributes["external_power_loss_at"] = notification.timestamp.isoformat()
                    device.attributes["externally_powered"] = False
                    _LOGGER.warning("Hub '%s': External power loss", device.name)
                elif "restored" in event_type or "ok" in event_type or "rétabli" in event_type:
                    device.attributes["external_power_loss"] = False
                    device.attributes["externally_powered"] = True
                    _LOGGER.info("Hub '%s': External power restored", device.name)

            # Cellular/GSM signal events
            elif "cellular" in event_type or "gsm" in event_type or "signal" in event_type or "cellulaire" in event_type:
                if "low" in event_type or "weak" in event_type or "faible" in event_type:
                    device.attributes["cellular_signal_low"] = True
                    device.attributes["cellular_signal_low_at"] = notification.timestamp.isoformat()
                    _LOGGER.warning("Hub '%s': Cellular signal low", device.name)
                elif "ok" in event_type or "normal" in event_type or "restored" in event_type:
                    device.attributes["cellular_signal_low"] = False
                    _LOGGER.info("Hub '%s': Cellular signal restored", device.name)

            # GSM antenna events
            elif "antenna" in event_type or "antenne" in event_type:
                if "damaged" in event_type or "endommagé" in event_type or "fault" in event_type:
                    device.attributes["gsm_antenna_damaged"] = True
                    device.attributes["gsm_antenna_damaged_at"] = notification.timestamp.isoformat()
                    device.attributes["problem"] = True
                    _LOGGER.error("Hub '%s': GSM antenna damaged", device.name)
                elif "disconnect" in event_type or "déconnect" in event_type:
                    device.attributes["gsm_antenna_disconnected"] = True
                    device.attributes["gsm_antenna_disconnected_at"] = notification.timestamp.isoformat()
                    _LOGGER.warning("Hub '%s': GSM antenna disconnected", device.name)
                elif "connect" in event_type or "ok" in event_type:
                    device.attributes["gsm_antenna_damaged"] = False
                    device.attributes["gsm_antenna_disconnected"] = False
                    _LOGGER.info("Hub '%s': GSM antenna OK", device.name)

            # Jeweller/Wings noise events
            elif "noise" in event_type or "bruit" in event_type:
                if "high" in event_type or "élevé" in event_type:
                    if "jeweller" in event_type:
                        device.attributes["jeweller_noise_high"] = True
                        device.attributes["jeweller_noise_high_at"] = notification.timestamp.isoformat()
                        _LOGGER.warning("Hub '%s': Jeweller noise high", device.name)
                    elif "wings" in event_type:
                        device.attributes["wings_noise_high"] = True
                        device.attributes["wings_noise_high_at"] = notification.timestamp.isoformat()
                        _LOGGER.warning("Hub '%s': Wings noise high", device.name)
                elif "ok" in event_type or "normal" in event_type:
                    if "jeweller" in event_type:
                        device.attributes["jeweller_noise_high"] = False
                        _LOGGER.info("Hub '%s': Jeweller noise normal", device.name)
                    elif "wings" in event_type:
                        device.attributes["wings_noise_high"] = False
                        _LOGGER.info("Hub '%s': Wings noise normal", device.name)

            # Software/System fault events
            elif "software" in event_type or "system" in event_type or "logiciel" in event_type or "système" in event_type:
                if "fault" in event_type or "error" in event_type or "défaut" in event_type or "erreur" in event_type:
                    device.attributes["system_fault"] = True
                    device.attributes["system_fault_at"] = notification.timestamp.isoformat()
                    device.attributes["problem"] = True
                    _LOGGER.error("Hub '%s': System fault detected", device.name)
                elif "ok" in event_type or "cleared" in event_type or "résolu" in event_type:
                    device.attributes["system_fault"] = False
                    _LOGGER.info("Hub '%s': System fault cleared", device.name)

            # CMS connection loss events
            elif "cms" in event_type or "monitoring" in event_type or "télésurveillance" in event_type:
                if "loss" in event_type or "lost" in event_type or "perte" in event_type:
                    device.attributes["cms_connection_loss"] = True
                    device.attributes["cms_connection_loss_at"] = notification.timestamp.isoformat()
                    _LOGGER.warning("Hub '%s': CMS connection loss", device.name)
                elif "restored" in event_type or "ok" in event_type or "rétabli" in event_type:
                    device.attributes["cms_connection_loss"] = False
                    _LOGGER.info("Hub '%s': CMS connection restored", device.name)

    def _parse_security_state(self, state_value: Any) -> SecurityState:
        """Parse security state from API response."""
        if isinstance(state_value, str):
            state_str = state_value.upper()
            # Check DISARMED first, before ARMED, since "DISARMED" contains "ARMED"
            if "DISARMED" in state_str:
                return SecurityState.DISARMED
            elif "PARTIALLY" in state_str:
                return SecurityState.PARTIALLY_ARMED
            elif "NIGHT" in state_str:
                return SecurityState.NIGHT_MODE
            elif "ARMED" in state_str:
                return SecurityState.ARMED

        return SecurityState.NONE

    def _parse_device_type(self, type_str: str) -> DeviceType:
        """Parse device type from API response."""
        type_map = {
            # Motion detectors
            "motion_protect": DeviceType.MOTION_DETECTOR,
            "motion": DeviceType.MOTION_DETECTOR,
            "pir": DeviceType.MOTION_DETECTOR,
            "motionprotect": DeviceType.MOTION_DETECTOR,

            # Combi detectors (motion + glass break)
            "combi_protect": DeviceType.COMBI_PROTECT,
            "combiprotect": DeviceType.COMBI_PROTECT,
            "combi": DeviceType.COMBI_PROTECT,

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

            # Controls - Keypads and Keyboards
            "keypad": DeviceType.KEYPAD,
            "keyboard": DeviceType.KEYPAD,
            "keypadplus": DeviceType.KEYPAD,
            "keypad_plus": DeviceType.KEYPAD,
            "keypadsplus": DeviceType.KEYPAD,
            "keypad_s_plus": DeviceType.KEYPAD,
            "keypadplusg3": DeviceType.KEYPAD,
            "keypad_plus_g3": DeviceType.KEYPAD,
            "keypadcombi": DeviceType.KEYPAD,
            "keypad_combi": DeviceType.KEYPAD,
            "keyboardfibra": DeviceType.KEYPAD,
            "keyboard_fibra": DeviceType.KEYPAD,
            "keypadtouchscreen": DeviceType.KEYPAD,
            "keypad_touchscreen": DeviceType.KEYPAD,
            "keypadtouchscreeng3": DeviceType.KEYPAD,
            "keypad_touchscreen_g3": DeviceType.KEYPAD,
            "keypadbeep": DeviceType.KEYPAD,
            "keypad_beep": DeviceType.KEYPAD,
            "keypadbase": DeviceType.KEYPAD,
            "keypad_base": DeviceType.KEYPAD,
            "keypadtouchscreenfibra": DeviceType.KEYPAD,
            "keypad_touchscreen_fibra": DeviceType.KEYPAD,
            "keypadoutdoorbase": DeviceType.KEYPAD,
            "keypad_outdoor_base": DeviceType.KEYPAD,
            "keypadoutdoor": DeviceType.KEYPAD,
            "keypad_outdoor": DeviceType.KEYPAD,
            "keypadoutdoorfibra": DeviceType.KEYPAD,
            "keypad_outdoor_fibra": DeviceType.KEYPAD,
            # Remote controls
            "space_control": DeviceType.REMOTE_CONTROL,
            "spacecontrol": DeviceType.REMOTE_CONTROL,
            "remote": DeviceType.REMOTE_CONTROL,

            # Buttons
            "button": DeviceType.BUTTON,
            "double_button": DeviceType.BUTTON,
            "doublebutton": DeviceType.BUTTON,

            # Sirens
            "siren": DeviceType.SIREN,
            "alarm": DeviceType.SIREN,

            # Transmitter
            "transmitter": DeviceType.TRANSMITTER,
            "integration": DeviceType.TRANSMITTER,

            # Repeater / Range Extender
            "repeater": DeviceType.REPEATER,
            "rex": DeviceType.REPEATER,
            "range_extender": DeviceType.REPEATER,
            "extender": DeviceType.REPEATER,

            # Wired Input Modules
            "wire_input_mt": DeviceType.WIRE_INPUT,
            "wireinputmt": DeviceType.WIRE_INPUT,
            "wire_input_rs": DeviceType.WIRE_INPUT,
            "wireinputrs": DeviceType.WIRE_INPUT,

            # Line Splitter
            "line_split_fibra": DeviceType.LINE_SPLITTER,
            "linesplitfibra": DeviceType.LINE_SPLITTER,
            "line_splitter": DeviceType.LINE_SPLITTER,
            "linesplitter": DeviceType.LINE_SPLITTER,

            # Smart devices
            "socket": DeviceType.SOCKET,
            "relay": DeviceType.RELAY,
            "thermostat": DeviceType.THERMOSTAT,
            "life_quality": DeviceType.LIFE_QUALITY,
            "lifequality": DeviceType.LIFE_QUALITY,

            # Cameras
            "camera": DeviceType.CAMERA,
            "cam": DeviceType.CAMERA,

            # Hub
            "hub": DeviceType.HUB,
        }

        # Clean up the type string (remove protobuf formatting artifacts)
        # Example: "wire_input_mt {\n}\n" -> "wire_input_mt"
        type_cleaned = type_str.strip().split()[0].lower() if type_str else ""

        # Try exact match (case insensitive)
        type_lower = type_str.lower()
        if type_cleaned in type_map:
            return type_map[type_cleaned]
        if type_lower in type_map:
            return type_map[type_lower]

        # Try partial match
        for key, device_type in type_map.items():
            if key in type_lower or type_lower in key:
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

    async def async_arm_space(self, space_id: str, force: bool = False) -> None:
        """Arm a space.

        Args:
            space_id: The space ID to arm
            force: If True, ignore alarms and force arm even with open sensors or problems
        """
        _LOGGER.info("Arming space %s (force=%s)", space_id, force)

        try:
            await self.api.async_arm(space_id, force=force)
            # State will be updated via real-time stream to avoid race conditions

        except AjaxApiError as err:
            _LOGGER.error("Failed to arm space %s: %s", space_id, err)
            raise

    async def async_disarm_space(self, space_id: str) -> None:
        """Disarm a space."""
        _LOGGER.info("Disarming space %s", space_id)

        try:
            await self.api.async_disarm(space_id)
            # State will be updated via real-time stream to avoid race conditions

        except AjaxApiError as err:
            _LOGGER.error("Failed to disarm space %s: %s", space_id, err)
            raise

    async def async_arm_night_mode(self, space_id: str, force: bool = False) -> None:
        """Activate night mode for a space.

        Args:
            space_id: The space ID to arm in night mode
            force: If True, ignore alarms and force arm even with open sensors or problems
        """
        _LOGGER.info("Activating night mode for space %s (force=%s)", space_id, force)

        try:
            await self.api.async_arm_night_mode(space_id, force=force)
            # State will be updated via real-time stream to avoid race conditions

        except AjaxApiError as err:
            _LOGGER.error("Failed to activate night mode for space %s: %s", space_id, err)
            raise

    async def async_arm_group(self, space_id: str, group_id: str, force: bool = False) -> None:
        """Arm a specific group/zone.

        Args:
            space_id: The space ID
            group_id: The group ID to arm
            force: If True, ignore alarms and force arm even with open sensors or problems
        """
        _LOGGER.info("Arming group %s in space %s (force=%s)", group_id, space_id, force)

        try:
            await self.api.async_arm_group(space_id, group_id, force=force)
            # State will be updated via real-time stream to avoid race conditions

        except AjaxApiError as err:
            _LOGGER.error("Failed to arm group %s in space %s: %s", group_id, space_id, err)
            raise

    async def async_disarm_group(self, space_id: str, group_id: str) -> None:
        """Disarm a specific group/zone.

        Args:
            space_id: The space ID
            group_id: The group ID to disarm
        """
        _LOGGER.info("Disarming group %s in space %s", group_id, space_id)

        try:
            await self.api.async_disarm_group(space_id, group_id)
            # State will be updated via real-time stream to avoid race conditions

        except AjaxApiError as err:
            _LOGGER.error("Failed to disarm group %s in space %s: %s", group_id, space_id, err)
            raise

    async def async_press_panic_button(self, space_id: str) -> None:
        """Press panic button (trigger panic alarm) for a space."""
        _LOGGER.warning("PANIC BUTTON pressed for space %s", space_id)

        try:
            await self.api.async_press_panic_button(space_id)
            # No state update needed, panic is instantaneous

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
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._streaming_tasks.clear()

        # Stop all notification streaming tasks
        for space_id, task in self._notification_streaming_tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._notification_streaming_tasks.clear()

        # Stop all device streaming tasks
        for space_id, task in self._device_streaming_tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._device_streaming_tasks.clear()

        # Stop all device-specific streaming tasks
        for device_id, task in self._specific_device_streams.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._specific_device_streams.clear()

        # Stop all wire_input polling tasks
        for space_id, task in self._wire_input_polling_tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._wire_input_polling_tasks.clear()

        # Close API connection
        await self.api.close()
