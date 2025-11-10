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
        self._groups_loaded_events: dict[str, asyncio.Event] = {}  # space_id -> event triggered when groups are loaded

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

    async def _async_process_notification_event(self, space_id: str, event) -> None:
        """Process a notification event from the stream."""
        from datetime import datetime, timezone

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
                            if not armed_groups:
                                space.security_state = SecurityState.DISARMED
                            elif len(armed_groups) == len(space.groups):
                                space.security_state = SecurityState.ARMED
                            else:
                                space.security_state = SecurityState.PARTIALLY_ARMED

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
                    if not batch_mode:
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

            if not batch_mode:
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

        for device_data in devices_data:
            device_id = device_data.get("id")
            if not device_id:
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
                    _LOGGER.info("Added new device: %s (%s)", device.name, device.type.value)
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

                # Update device state based on notification event
                if notification.device_id:
                    self._update_device_from_notification(space, notification)

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
        from datetime import datetime, timezone

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
        from datetime import datetime

        device = space.devices.get(notification.device_id)
        if not device:
            return

        if not notification.title:
            return

        event_type = notification.title.lower()

        # Update device based on event type
        if "motion" in event_type:
            device.attributes["motion_detected"] = True
            device.attributes["motion_detected_at"] = notification.timestamp
            device.last_trigger_time = notification.timestamp
            device.last_notification = notification

        elif "door" in event_type and "opened" in event_type:
            device.attributes["door_opened"] = True
            device.last_trigger_time = notification.timestamp
            device.last_notification = notification

        elif "door" in event_type and "closed" in event_type:
            device.attributes["door_opened"] = False

        elif "smoke" in event_type:
            device.attributes["smoke_detected"] = True
            device.last_trigger_time = notification.timestamp
            device.last_notification = notification

        elif "leak" in event_type:
            device.attributes["leak_detected"] = True
            device.last_trigger_time = notification.timestamp
            device.last_notification = notification

        elif "tamper" in event_type:
            device.attributes["tampered"] = True
            device.last_trigger_time = notification.timestamp
            device.last_notification = notification

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

        # Close API connection
        await self.api.close()
