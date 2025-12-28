"""Ajax data coordinator for Home Assistant.

This coordinator manages:
- Periodic polling updates from Ajax REST API
- Real-time event updates via AWS SQS (optional)
- Space, Room, Device, and Notification data
- State synchronization between Ajax and Home Assistant

Architecture:
- Hybrid Mode: SQS real-time events + REST polling fallback
- SQS events trigger immediate REST refresh for instant state updates
- REST polling continues every 30s as baseline
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AjaxRestApi, AjaxRestApiError, AjaxRestAuthError
from .const import (
    CONF_NOTIFICATION_FILTER,
    CONF_PERSISTENT_NOTIFICATION,
    DOMAIN,
    NOTIFICATION_FILTER_ALARMS_ONLY,
    NOTIFICATION_FILTER_ALL,
    NOTIFICATION_FILTER_NONE,
    UPDATE_INTERVAL,
    UPDATE_INTERVAL_ARMED,
)
from .models import (
    AjaxAccount,
    AjaxDevice,
    AjaxRoom,
    AjaxSpace,
    DeviceType,
    GroupState,
    NotificationType,
    SecurityState,
)

# Optional SQS support
try:
    from .sqs_client import AjaxSQSClient
    from .sqs_manager import SQSManager

    SQS_AVAILABLE = True
except ImportError:
    SQS_AVAILABLE = False
    SQSManager = None
    AjaxSQSClient = None

# Optional SSE support (for proxy mode)
try:
    from .sse_client import AjaxSSEClient
    from .sse_manager import SSEManager

    SSE_AVAILABLE = True
except ImportError:
    SSE_AVAILABLE = False
    SSEManager = None
    AjaxSSEClient = None

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

    def __init__(
        self,
        hass: HomeAssistant,
        api: AjaxRestApi,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        queue_name: str | None = None,
        sse_url: str | None = None,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: Home Assistant instance
            api: Ajax REST API instance
            aws_access_key_id: AWS access key ID (optional, for SQS in direct mode)
            aws_secret_access_key: AWS secret access key (optional, for SQS in direct mode)
            queue_name: SQS queue name (optional, for SQS in direct mode)
            sse_url: SSE endpoint URL (optional, for proxy mode)
        """
        self.api = api
        self.account: AjaxAccount | None = None
        self._fast_poll_tasks: dict[
            str, asyncio.Task
        ] = {}  # device_id -> fast polling task for door sensors
        self._initial_load_done: bool = False  # Track if initial data load is complete
        self._pending_ha_actions: dict[
            str, float
        ] = {}  # hub_id -> timestamp of HA action

        # SQS real-time events (optional, for direct mode)
        self.sqs_manager: SQSManager | None = None
        self._aws_access_key_id = aws_access_key_id
        self._aws_secret_access_key = aws_secret_access_key
        self._queue_name = queue_name
        self._sqs_initialized = False

        # SSE real-time events (optional, for proxy mode)
        self.sse_manager: SSEManager | None = None
        self._sse_url = sse_url or (api.sse_url if hasattr(api, "sse_url") else None)
        self._sse_initialized = False

        # Device details refresh optimization
        # Battery/signal don't change often, so refresh every 5 minutes instead of every poll
        self._last_device_details_refresh: float = 0
        self._device_details_refresh_interval: int = 300  # 5 minutes in seconds

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    def _update_polling_interval(self, security_state: SecurityState) -> None:
        """Update polling interval based on security state.

        When armed or in night mode, poll faster (10s) to detect door sensor
        changes quickly. When disarmed, poll slower (30s) to reduce API calls.

        Args:
            security_state: Current security state of the space
        """
        if security_state in (
            SecurityState.ARMED,
            SecurityState.NIGHT_MODE,
            SecurityState.PARTIALLY_ARMED,
        ):
            new_interval = UPDATE_INTERVAL_ARMED
        else:
            new_interval = UPDATE_INTERVAL

        current_interval = (
            self.update_interval.total_seconds()
            if self.update_interval
            else UPDATE_INTERVAL
        )

        if new_interval != current_interval:
            self.update_interval = timedelta(seconds=new_interval)
            _LOGGER.info(
                "Polling interval changed to %ds (security state: %s)",
                new_interval,
                security_state.value,
            )

    async def _async_update_data(self) -> AjaxAccount:
        """Fetch data from Ajax REST API.

        This is called periodically for updates.
        """
        try:
            # Initialize account if needed
            if self.account is None:
                await self._async_init_account()

            # Only do full data load on first run or manual reload
            if not self._initial_load_done:
                # Update spaces - use hubs endpoint directly to get hubId
                await self._async_update_spaces_from_hubs()

                # Load devices and notifications in parallel for all spaces
                tasks = []
                for space_id in self.account.spaces:
                    tasks.append(self._async_update_devices(space_id))
                    tasks.append(self._async_update_notifications(space_id, limit=20))

                # Execute all API calls in parallel for faster startup
                await asyncio.gather(*tasks, return_exceptions=True)

                # Mark initial load as complete
                self._initial_load_done = True
                _LOGGER.info("Initial data load complete")

                # Initialize real-time events in background
                # Priority: SSE (proxy mode) > SQS (direct mode)
                if not self._sse_initialized and self._sse_url:
                    # Proxy mode: use SSE for real-time events
                    asyncio.create_task(self._async_init_sse())
                elif not self._sqs_initialized and self._aws_access_key_id:
                    # Direct mode: use SQS for real-time events
                    asyncio.create_task(self._async_init_sqs())
            else:
                # Periodic update - refresh hub state and devices
                await self._async_update_spaces_from_hubs()

                for space_id in self.account.spaces:
                    space = self.account.spaces.get(space_id)
                    if space:
                        self._reset_expired_motion_detections(space)
                        await self._async_update_devices(space_id)

            return self.account

        except AjaxRestAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except AjaxRestApiError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err

    async def _async_fast_poll_door_sensor(self, space_id: str, device_id: str) -> None:
        """Fast poll a door sensor after opening to quickly detect closure."""
        poll_interval = 3  # Poll every 3 seconds
        max_duration = 120  # Stop after 2 minutes
        start_time = asyncio.get_event_loop().time()

        try:
            while True:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > max_duration:
                    break

                try:
                    space = self.account.spaces.get(space_id)
                    if not space:
                        break
                    devices_data = await self.api.async_get_devices(space.hub_id)

                    device_found = False
                    for device_data in devices_data:
                        if device_data.get("id") == device_id:
                            device_found = True

                            if device_id in space.devices:
                                device = space.devices[device_id]
                                # Normalize attributes from API
                                api_attrs = device_data.get("attributes", {})
                                normalized_attrs = self._normalize_device_attributes(
                                    api_attrs, device.type
                                )
                                device.attributes.update(normalized_attrs)

                                # Check door state (after normalization)
                                door_opened = device.attributes.get(
                                    "door_opened", False
                                )

                                if not door_opened:
                                    self.async_set_updated_data(self.account)
                                    if device_id in self._fast_poll_tasks:
                                        del self._fast_poll_tasks[device_id]
                                    break

                    if not device_found:
                        break

                except Exception as err:
                    _LOGGER.error(
                        "Error in fast polling for door sensor %s: %s", device_id, err
                    )

                await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            raise
        finally:
            if device_id in self._fast_poll_tasks:
                del self._fast_poll_tasks[device_id]

    async def _async_init_account(self) -> None:
        """Initialize the account data."""
        # Use login info directly (no /user endpoint available)
        self.account = AjaxAccount(
            user_id=self.api.user_id or "",
            name=self.api.email.split("@")[0] if self.api.email else "Unknown",
            email=self.api.email or "",
        )

        _LOGGER.info(
            "Initialized account for %s (user_id: %s)",
            self.account.name,
            self.account.user_id,
        )

    async def _async_init_sqs(self) -> None:
        """Initialize AWS SQS for real-time events (optional).

        SQS provides real-time event notifications (<1s latency) that trigger
        immediate REST API updates. This creates a hybrid mode:
        - SQS events: Real-time triggers for instant state updates
        - REST polling: Baseline updates every 30s as fallback

        Note:
            Requires aiobotocore package and AWS credentials.
            If SQS fails to initialize, integration falls back to REST-only mode.
        """
        # Check if SQS is available
        if not SQS_AVAILABLE:
            _LOGGER.info(
                "AWS SQS not available (aiobotocore not installed). "
                "Using REST API polling only."
            )
            self._sqs_initialized = True  # Mark as "initialized" to prevent retries
            return

        # Check if credentials are provided
        if (
            not self._aws_access_key_id
            or not self._aws_secret_access_key
            or not self._queue_name
        ):
            _LOGGER.debug(
                "AWS credentials not configured. Using REST API polling only."
            )
            self._sqs_initialized = True
            return

        try:
            _LOGGER.info("Initializing AWS SQS for real-time events...")

            # Create SQS client with HA's event loop for thread-safe callbacks
            sqs_client = AjaxSQSClient(
                aws_access_key_id=self._aws_access_key_id,
                aws_secret_access_key=self._aws_secret_access_key,
                queue_name=self._queue_name,
                hass_loop=self.hass.loop,
            )

            # Create SQS manager
            self.sqs_manager = SQSManager(
                coordinator=self,
                sqs_client=sqs_client,
            )

            # Set language from Home Assistant settings
            ha_language = self.hass.config.language or "en"
            # Map HA language codes to our supported languages
            lang_map = {"fr": "fr", "es": "es", "en": "en"}
            sqs_language = lang_map.get(ha_language[:2], "en")
            self.sqs_manager.set_language(sqs_language)

            # Start receiving events
            success = await self.sqs_manager.start()

            if success:
                _LOGGER.info(
                    "✓ AWS SQS initialized successfully - Real-time events enabled!"
                )
            else:
                _LOGGER.warning(
                    "Failed to start SQS - Falling back to REST API polling only"
                )
                self.sqs_manager = None

        except Exception as err:
            _LOGGER.warning(
                "Failed to initialize AWS SQS: %s - Using REST API polling only",
                err,
            )
            self.sqs_manager = None

        finally:
            self._sqs_initialized = True

    async def _async_init_sse(self) -> None:
        """Initialize SSE for real-time events (proxy mode).

        SSE provides real-time event notifications via the proxy server.
        This is used when connecting through a proxy instead of direct SQS.

        Note:
            Requires proxy mode and SSE URL from login response.
            If SSE fails to initialize, integration falls back to REST-only mode.
        """
        # Check if SSE is available
        if not SSE_AVAILABLE:
            _LOGGER.info(
                "SSE not available (module not loaded). " "Using REST API polling only."
            )
            self._sse_initialized = True
            return

        # Check if SSE URL is provided
        if not self._sse_url:
            _LOGGER.debug("SSE URL not configured. Using REST API polling only.")
            self._sse_initialized = True
            return

        # Check if we have a session token
        if not self.api.session_token:
            _LOGGER.warning(
                "No session token available for SSE. Using REST API polling only."
            )
            self._sse_initialized = True
            return

        try:
            _LOGGER.info("Initializing SSE for real-time events...")

            # Create SSE client
            sse_client = AjaxSSEClient(
                sse_url=self._sse_url,
                session_token=self.api.session_token,
                callback=lambda event: None,  # Will be set by manager
                hass_loop=self.hass.loop,
            )

            # Create SSE manager
            self.sse_manager = SSEManager(
                coordinator=self,
                sse_client=sse_client,
            )

            # Set language from Home Assistant settings
            ha_language = self.hass.config.language or "en"
            lang_map = {"fr": "fr", "es": "es", "en": "en"}
            sse_language = lang_map.get(ha_language[:2], "en")
            self.sse_manager.set_language(sse_language)

            # Start receiving events
            success = await self.sse_manager.start()

            if success:
                _LOGGER.info(
                    "✓ SSE initialized successfully - Real-time events enabled!"
                )
            else:
                _LOGGER.warning(
                    "Failed to start SSE - Falling back to REST API polling only"
                )
                self.sse_manager = None

        except Exception as err:
            _LOGGER.warning(
                "Failed to initialize SSE: %s - Using REST API polling only",
                err,
            )
            self.sse_manager = None

        finally:
            self._sse_initialized = True

    async def _async_update_spaces_from_hubs(self) -> None:
        """Update spaces by fetching hubs directly (use hub_id as space_id)."""
        hubs_data = await self.api.async_get_hubs()

        for hub_data in hubs_data:
            hub_id = hub_data.get("hubId")
            if not hub_id:
                continue

            # Get hub details to get the name and state
            try:
                hub_details = await self.api.async_get_hub(hub_id)
                # Get rooms for this hub
                rooms_data: list[dict] = []
                try:
                    rooms_data = await self.api.async_get_rooms(hub_id)
                    # Build room_id -> room_name mapping
                    rooms_map = {
                        room.get("id"): room.get("roomName")
                        for room in rooms_data
                        if room.get("id")
                    }
                    _LOGGER.info(
                        "Loaded %d rooms for hub %s: %s",
                        len(rooms_map),
                        hub_id,
                        rooms_map,
                    )
                except Exception as room_err:
                    _LOGGER.warning(
                        "Could not get rooms for hub %s: %s", hub_id, room_err
                    )
                    rooms_map = {}
                # Try to get hub name from multiple possible fields
                hub_name = (
                    hub_details.get("name")
                    or hub_details.get("hubName")
                    or hub_details.get("deviceName")
                    or f"Hub {hub_id[:6]}"  # Use first 6 chars of hub_id as fallback
                )
                # Parse security state from hub details - use 'state' field, not 'securityMode'
                hub_state = hub_details.get("state", "DISARMED")
                security_state = self._parse_security_state(hub_state)
            except Exception as err:
                _LOGGER.warning("Failed to get hub details for %s: %s", hub_id, err)
                hub_name = f"Hub {hub_id}"
                security_state = SecurityState.NONE
                hub_details = {}
                rooms_data = []
                rooms_map = {}

            # Use hub_id as space_id since we're mapping 1:1
            space_id = hub_id

            # Create or update space
            if space_id not in self.account.spaces:
                # New space - always use API state for initial value
                space = AjaxSpace(
                    id=space_id,
                    name=hub_name,
                    hub_id=hub_id,
                    security_state=security_state,  # Use API state at creation
                    hub_details=hub_details,  # Store all hub information
                )
                self.account.spaces[space_id] = space
                _LOGGER.info(
                    "Added new space from hub: %s (hub_id: %s, initial state: %s)",
                    space.name,
                    space.hub_id,
                    security_state,
                )

                # Set initial polling interval based on security state
                self._update_polling_interval(security_state)
            else:
                # Existing space - update name, hub_id, hub_details, and potentially state
                space = self.account.spaces[space_id]
                space.name = hub_name
                space.hub_id = hub_id
                space.hub_details = hub_details  # Update hub information

            # Store rooms mapping in space for device room name lookup
            space._rooms_map = rooms_map  # type: ignore

            # Populate space.rooms with AjaxRoom objects
            for room_data in rooms_data:
                room_id = room_data.get("id")
                if room_id:
                    space.rooms[room_id] = AjaxRoom(
                        id=room_id,
                        name=room_data.get("roomName", f"Room {room_id}"),
                        space_id=space_id,
                        image_id=room_data.get("imageId"),
                        image_url=room_data.get("imageUrl"),
                    )

            # Fetch users for this hub
            try:
                users_data = await self.api.async_get_users(hub_id)
                space._users = users_data  # type: ignore
            except Exception:
                space._users = []  # type: ignore

            # Check if SQS/SSE recently updated this hub's state
            # If so, don't overwrite with potentially stale REST data
            old_state = space.security_state
            if old_state != security_state:
                # Check real-time event protection (don't overwrite recent updates)
                sqs_protected = (
                    self.sqs_manager and self.sqs_manager.is_state_protected(hub_id)
                )
                sse_protected = (
                    self.sse_manager and self.sse_manager.is_state_protected(hub_id)
                )
                if sqs_protected or sse_protected:
                    _LOGGER.debug(
                        "Hub %s: REST has %s but real-time event recently set %s (protected)",
                        hub_id,
                        security_state.value,
                        old_state.value,
                    )
                else:
                    space.security_state = security_state
                    _LOGGER.info(
                        "Hub %s: %s -> %s",
                        hub_id,
                        old_state.value,
                        security_state.value,
                    )

                    # Update polling interval based on new state
                    self._update_polling_interval(security_state)

                    # Create event from state change
                    self._create_event_from_state_change(
                        space, old_state, security_state
                    )

    async def _async_update_devices(self, space_id: str) -> None:
        """Update devices for a specific space."""
        space = self.account.spaces.get(space_id)
        if not space:
            return

        # Get all devices with full details in one API call (enrich=True)
        # The detailed data is nested in a "model" object that we merge into the device
        devices_list = await self.api.async_get_devices(space.hub_id, enrich=True)

        # Check if we need to refresh battery/signal (every 5 minutes)
        current_time = time.time()
        need_details_refresh = (
            current_time - self._last_device_details_refresh
            >= self._device_details_refresh_interval
        )
        if need_details_refresh:
            _LOGGER.debug("Refreshing battery/signal - 5 min interval")
            self._last_device_details_refresh = current_time

        new_devices_count = 0
        processed_ids: set[str] = set()  # Track processed IDs to skip duplicates

        for device_summary in devices_list:
            device_id = device_summary.get("id")
            if not device_id:
                continue

            # Skip duplicate device IDs in the same API response
            # (MultiTransmitter can appear twice with different names)
            if device_id in processed_ids:
                _LOGGER.warning(
                    "Skipping duplicate device ID %s (%s) - already processed",
                    device_id,
                    device_summary.get("deviceName", "unknown"),
                )
                continue
            processed_ids.add(device_id)

            # With enrich=True, detailed data is in the "model" sub-object
            # Merge model into device_data so the rest of the code works
            device_data = dict(device_summary)
            if "model" in device_summary:
                device_data.update(device_summary["model"])

            is_new_device = device_id not in space.devices

            # Parse device type - API uses camelCase (deviceType, deviceName)
            raw_device_type = device_data.get(
                "deviceType", device_data.get("type", "unknown")
            )
            device_type = self._parse_device_type(raw_device_type)

            # Get room_id and room_name
            room_id = device_data.get("roomId", device_data.get("room_id"))
            rooms_map = getattr(space, "_rooms_map", {})
            room_name = rooms_map.get(room_id) if room_id else None
            _LOGGER.debug(
                "Device %s: roomId=%s, room_name=%s",
                device_data.get("deviceName", device_id),
                room_id,
                room_name,
            )

            # Create or update device
            if device_id not in space.devices:
                device = AjaxDevice(
                    id=device_id,
                    name=device_data.get(
                        "deviceName", device_data.get("name", "Unknown Device")
                    ),
                    type=device_type,
                    space_id=space_id,
                    hub_id=device_data.get("hub_id", space.hub_id or ""),
                    raw_type=raw_device_type,
                    room_id=room_id,
                    room_name=room_name,
                    group_id=device_data.get("groupId", device_data.get("group_id")),
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
                device = space.devices[device_id]
                # Update raw_type in case it changed
                device.raw_type = raw_device_type
                # Update room info
                device.room_id = room_id
                device.room_name = room_name

            # Update basic device attributes from list endpoint
            device.online = device_data.get("online", True)
            device.bypassed = device_data.get("bypassed", False)

            # malfunctions can be a list or an int - normalize to int (count)
            malfunctions_data = device_data.get("malfunctions", 0)
            if isinstance(malfunctions_data, list):
                device.malfunctions = len(malfunctions_data)
            else:
                device.malfunctions = malfunctions_data

            # Battery and signal: only update every 5 minutes (or for new devices)
            # These don't change often and SQS sends MALFUNCTION events for low battery
            if need_details_refresh or is_new_device:
                device.battery_level = device_data.get(
                    "batteryChargeLevelPercentage",
                    device_data.get("battery_level"),
                )
                device.battery_state = device_data.get(
                    "batteryState", device_data.get("battery_state")
                )
                # Convert signal level string to percentage
                signal_level = device_data.get(
                    "signalLevel", device_data.get("signal_strength")
                )
                if isinstance(signal_level, str):
                    signal_map = {
                        "EXCELLENT": 100,
                        "STRONG": 85,
                        "GOOD": 70,
                        "MEDIUM": 50,
                        "WEAK": 30,
                        "POOR": 15,
                    }
                    device.signal_strength = signal_map.get(signal_level.upper(), None)
                else:
                    device.signal_strength = signal_level

            device.firmware_version = device_data.get(
                "firmwareVersion", device_data.get("firmware_version")
            )
            device.hardware_version = device_data.get(
                "hardwareVersion", device_data.get("hardware_version")
            )
            device.states = device_data.get("states", [])

            # Store tampered status in attributes
            if "tampered" in device_data:
                device.attributes["tampered"] = device_data.get("tampered", False)

            # Store temperature if available (DoorProtect Plus)
            if "temperature" in device_data:
                device.attributes["temperature"] = device_data.get("temperature")

            # Store other useful attributes
            if "alwaysActive" in device_data:
                device.attributes["always_active"] = device_data.get(
                    "alwaysActive", False
                )
            if "nightModeArm" in device_data or "armedInNightMode" in device_data:
                night_mode_value = device_data.get(
                    "nightModeArm",
                    device_data.get("armedInNightMode", False),
                )
                device.attributes["armed_in_night_mode"] = night_mode_value
                device.attributes["night_mode_arm"] = (
                    night_mode_value  # Alias for handlers
                )

            # DoorProtect Plus specific attributes
            if "extraContactAware" in device_data:
                device.attributes["extra_contact_aware"] = device_data.get(
                    "extraContactAware", False
                )
            if "shockSensorAware" in device_data:
                device.attributes["shock_sensor_aware"] = device_data.get(
                    "shockSensorAware", False
                )
            if "accelerometerAware" in device_data:
                device.attributes["accelerometer_aware"] = device_data.get(
                    "accelerometerAware", False
                )
            if "shockSensorSensitivity" in device_data:
                device.attributes["shock_sensor_sensitivity"] = device_data.get(
                    "shockSensorSensitivity", 0
                )
            if "accelerometerTiltDegrees" in device_data:
                device.attributes["accelerometer_tilt_degrees"] = device_data.get(
                    "accelerometerTiltDegrees", 5
                )
            if "ignoreSimpleImpact" in device_data:
                device.attributes["ignore_simple_impact"] = device_data.get(
                    "ignoreSimpleImpact", False
                )
            if "sirenTriggers" in device_data:
                device.attributes["siren_triggers"] = device_data.get(
                    "sirenTriggers", []
                )

            # Door contact state (reedClosed -> door_opened)
            if "reedClosed" in device_data:
                # reedClosed=True means door is closed, so door_opened=False
                device.attributes["door_opened"] = not device_data.get(
                    "reedClosed", True
                )
            # External contact state (extraContactClosed -> external_contact_opened)
            if "extraContactClosed" in device_data:
                device.attributes["external_contact_opened"] = not device_data.get(
                    "extraContactClosed", True
                )
            # Multitransmitter state (externalContactState -> door_opened)
            if "externalContactState" in device_data:
                # externalContactState=!OK means door is opened, so door_opened=True
                device.attributes["door_opened"] = (
                    device_data.get("externalContactState") != "OK"
                )

            # Sensitivity (GlassProtect, MotionProtect, etc.)
            if "sensitivity" in device_data:
                device.attributes["sensitivity"] = device_data.get("sensitivity")

            # Device color
            if "color" in device_data:
                device.attributes["color"] = device_data.get("color")

            # Siren specific attributes
            if "sirenVolumeLevel" in device_data:
                device.attributes["siren_volume_level"] = device_data.get(
                    "sirenVolumeLevel"
                )
            if "beepVolumeLevel" in device_data:
                device.attributes["beep_volume_level"] = device_data.get(
                    "beepVolumeLevel"
                )
            if "alarmDuration" in device_data:
                device.attributes["alarm_duration"] = device_data.get("alarmDuration")
            if "v2sirenIndicatorLightMode" in device_data:
                device.attributes["led_indication"] = device_data.get(
                    "v2sirenIndicatorLightMode"
                )
            elif "blinkWhileArmed" in device_data:
                device.attributes["led_indication"] = device_data.get("blinkWhileArmed")
            # Siren beep/chime settings
            if "beepOnArmDisarm" in device_data:
                device.attributes["beep_on_arm_disarm"] = device_data.get(
                    "beepOnArmDisarm"
                )
            if "beepOnDelay" in device_data:
                device.attributes["beep_on_delay"] = device_data.get("beepOnDelay")
            if "chimesEnabled" in device_data:
                device.attributes["chimes_enabled"] = device_data.get("chimesEnabled")
            if "buzzerState" in device_data:
                device.attributes["buzzer_state"] = device_data.get("buzzerState")

            # LED indicator mode (all devices)
            if "indicatorLightMode" in device_data:
                device.attributes["indicatorLightMode"] = device_data.get(
                    "indicatorLightMode"
                )

            # Alerts by sirens setting
            if "alertsBySirens" in device_data:
                device.attributes["alertsBySirens"] = device_data.get(
                    "alertsBySirens", False
                )

            # MotionCam specific attributes
            if "imageResolution" in device_data:
                device.attributes["imageResolution"] = device_data.get(
                    "imageResolution"
                )
            if "photosPerAlarm" in device_data:
                device.attributes["photosPerAlarm"] = device_data.get("photosPerAlarm")

            # Socket/Relay/WallSwitch: Parse switchState to is_on (direct from enriched data)
            if "switchState" in device_data:
                switch_state = device_data["switchState"]
                # switchState is a list: [] = on, ["SWITCHED_OFF"] = off
                # Device is OFF only if SWITCHED_OFF is explicitly in the list
                if isinstance(switch_state, list):
                    device.attributes["is_on"] = "SWITCHED_OFF" not in switch_state
                else:
                    device.attributes["is_on"] = True

            # Update device metadata (API uses "color" not "device_color")
            device.device_color = device_data.get("color")
            device.device_label = device_data.get("device_label")
            device.device_marketing_id = device_data.get("device_marketing_id")

            # Update device attributes dict
            if "attributes" in device_data:
                # Normalize API attributes to internal format
                normalized_attrs = self._normalize_device_attributes(
                    device_data["attributes"], device.type
                )
                device.attributes.update(normalized_attrs)
            # Update room association
            if device.room_id and device.room_id in space.rooms:
                room = space.rooms[device.room_id]
                if device_id not in room.device_ids:
                    room.device_ids.append(device_id)

        # Log summary of devices loaded
        if new_devices_count > 0:
            _LOGGER.info(
                "Discovered %d new device(s) in space %s", new_devices_count, space_id
            )

    def _normalize_device_attributes(
        self, api_attributes: dict[str, Any], device_type: DeviceType
    ) -> dict[str, Any]:
        """Normalize Ajax API attributes to internal format.

        The Ajax API uses specific attribute names (e.g., reedClosed) that we
        normalize to more intuitive names (e.g., door_opened) for internal use.

        Args:
            api_attributes: Raw attributes from Ajax API
            device_type: Type of device

        Returns:
            Normalized attributes dict
        """
        normalized = dict(api_attributes)  # Start with original attributes

        # Door contacts: Support both API formats
        if device_type in [DeviceType.DOOR_CONTACT, DeviceType.WIRE_INPUT]:
            # If API already provides door_opened, use it directly
            if "door_opened" not in api_attributes and "reedClosed" in api_attributes:
                # Convert reedClosed (False=open) to door_opened (True=open)
                # Invert the logic: reedClosed=False means door is open
                normalized["door_opened"] = not api_attributes["reedClosed"]

            # MultiTransmitterWireInput uses externalContactState
            if (
                "door_opened" not in normalized
                and "externalContactState" in api_attributes
            ):
                # externalContactState: "OK" = closed, anything else = open
                normalized["door_opened"] = (
                    api_attributes["externalContactState"] != "OK"
                )

            # External contact: Support both formats
            if (
                "external_contact_opened" not in api_attributes
                and "extraContactClosed" in api_attributes
            ):
                # Convert extraContactClosed to external_contact_opened
                # extraContactClosed=False means contact is open (alarm state)
                normalized["external_contact_opened"] = not api_attributes[
                    "extraContactClosed"
                ]

        # Motion detectors: Support both camelCase and snake_case
        if device_type == DeviceType.MOTION_DETECTOR:
            if (
                "motion_detected" not in api_attributes
                and "motionDetected" in api_attributes
            ):
                normalized["motion_detected"] = api_attributes["motionDetected"]
            if (
                "motion_detected_at" not in api_attributes
                and "motionDetectedAt" in api_attributes
            ):
                normalized["motion_detected_at"] = api_attributes["motionDetectedAt"]

        # Smoke detectors: Support both formats
        if (
            device_type == DeviceType.SMOKE_DETECTOR
            and "smoke_detected" not in api_attributes
            and "smokeDetected" in api_attributes
        ):
            normalized["smoke_detected"] = api_attributes["smokeDetected"]

        # Flood detectors: Support both formats
        if (
            device_type == DeviceType.FLOOD_DETECTOR
            and "leak_detected" not in api_attributes
            and "leakDetected" in api_attributes
        ):
            normalized["leak_detected"] = api_attributes["leakDetected"]

        # Glass break detectors: Support both formats
        if (
            device_type == DeviceType.GLASS_BREAK
            and "glass_break_detected" not in api_attributes
            and "glassBreakDetected" in api_attributes
        ):
            normalized["glass_break_detected"] = api_attributes["glassBreakDetected"]

        # Socket/Relay/WallSwitch: Parse switchState to is_on
        if (
            device_type in (DeviceType.SOCKET, DeviceType.RELAY, DeviceType.WALLSWITCH)
            and "switchState" in api_attributes
        ):
            switch_state = api_attributes["switchState"]
            # switchState is a list: [] = on, ["SWITCHED_OFF"] = off
            # Device is OFF only if SWITCHED_OFF is explicitly in the list
            if isinstance(switch_state, list):
                normalized["is_on"] = "SWITCHED_OFF" not in switch_state
            else:
                normalized["is_on"] = True

        return normalized

    def _reset_expired_motion_detections(self, space: AjaxSpace) -> None:
        """Reset motion_detected to False for motion detectors if no recent detection.

        Motion detection events are impulse-based (not persistent state),
        so we reset them after 30 seconds of no new detection.

        Args:
            space: The AjaxSpace to process
        """
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
                device.attributes["motion_detected"] = False
                continue

            # Parse timestamp
            try:
                last_detected = datetime.fromisoformat(last_detected_at)
                if last_detected.tzinfo is None:
                    last_detected = last_detected.replace(tzinfo=timezone.utc)

                if (now - last_detected).total_seconds() > expiry_seconds:
                    device.attributes["motion_detected"] = False
            except (ValueError, TypeError) as err:
                _LOGGER.warning(
                    "Failed to parse motion_detected_at timestamp for %s: %s",
                    device.name,
                    err,
                )

    async def _async_update_notifications(self, space_id: str, limit: int = 50) -> None:
        """Update notifications for a specific space."""
        space = self.account.spaces.get(space_id)
        if not space:
            return

        try:
            # TODO: Fetch notifications from API when endpoint is available
            # For now, notifications come from SQS real-time events
            # notifications_data = await self.api.async_find_notifications(space_id, limit)
            notifications_data = []

            # Clear existing notifications and add new ones
            space.notifications.clear()

            for notif_data in notifications_data:
                # Parse notification data
                from .models import AjaxNotification

                # Determine notification type based on event_type
                event_type = notif_data.get("event_type", "")
                notif_type = self._parse_notification_type(event_type)

                # Create notification object
                notification = AjaxNotification(
                    id=notif_data.get("id", ""),
                    space_id=notif_data.get("space_id", space_id),
                    type=notif_type,
                    title=event_type.replace("_", " ").title()
                    if event_type
                    else "Event",
                    message=f"{notif_data.get('device_name', 'Device')}: {event_type.replace('_', ' ')}"
                    if event_type
                    else "",
                    timestamp=notif_data.get("timestamp") or datetime.now(),
                    device_id=notif_data.get("device_id"),
                    device_name=notif_data.get("device_name"),
                    read=notif_data.get("read", False),
                    user_name=notif_data.get(
                        "user_name"
                    ),  # User who triggered the event
                )

                space.notifications.append(notification)

            # Update unread count
            space.unread_notifications = sum(
                1 for n in space.notifications if not n.read
            )

        except Exception as err:
            _LOGGER.error(
                "Error updating notifications for space %s: %s", space_id, err
            )

    def _parse_notification_type(self, event_type: str | None) -> NotificationType:
        """Parse notification type from event type string."""
        from .models import NotificationType

        if not event_type:
            return NotificationType.INFO

        event_lower = event_type.lower()

        if any(
            keyword in event_lower
            for keyword in [
                "alarm",
                "intrusion",
                "panic",
                "fire",
                "smoke",
                "leak",
                "gas",
            ]
        ):
            return NotificationType.ALARM
        elif any(
            keyword in event_lower
            for keyword in ["malfunction", "low", "tamper", "loss", "error", "fault"]
        ):
            return NotificationType.WARNING
        elif any(
            keyword in event_lower
            for keyword in ["arm", "disarm", "motion", "door", "opened"]
        ):
            return NotificationType.SECURITY_EVENT
        elif any(
            keyword in event_lower for keyword in ["update", "added", "changed", "test"]
        ):
            return NotificationType.SYSTEM_EVENT
        else:
            return NotificationType.INFO

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
            armed_groups = [
                g.name for g in space.groups.values() if g.state == GroupState.ARMED
            ]
            disarmed_groups = [
                g.name for g in space.groups.values() if g.state == GroupState.DISARMED
            ]
            event_data["armed_groups"] = armed_groups
            event_data["disarmed_groups"] = disarmed_groups
            event_data["group_mode"] = True
        else:
            event_data["group_mode"] = False

        # Fire the event
        self.hass.bus.async_fire(event_name, event_data)

    def _create_event_from_state_change(
        self,
        space: AjaxSpace,
        old_state: SecurityState,
        new_state: SecurityState,
    ) -> None:
        """Create an event when security state changes.

        REST API is the single source of truth. When state changes,
        we always create an event here. SQS is only used to trigger
        faster polling, not to create events directly.

        Args:
            space: The AjaxSpace object
            old_state: Previous security state
            new_state: New security state
        """
        # Map state to action key (same keys as SQS events)
        # format_event_text() will translate to French
        action_map = {
            SecurityState.ARMED: "armed",
            SecurityState.DISARMED: "disarmed",
            SecurityState.NIGHT_MODE: "night_mode",
            SecurityState.PARTIALLY_ARMED: "partially_armed",
        }

        action = action_map.get(new_state, new_state.value.lower())

        # Create event
        # Note: source_name/user_name not included because REST API
        # doesn't tell us WHO triggered the action
        event = {
            "action": action,
            "hub_id": space.hub_id or space.id,
            "space_id": space.id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_time": datetime.now(timezone.utc),
        }

        # Store in recent_events (keep last 10)
        space.recent_events.insert(0, event)
        space.recent_events = space.recent_events[:10]

        _LOGGER.debug("Event stored: %s (total: %d)", action, len(space.recent_events))

        # Fire Home Assistant event for automations
        self._fire_security_state_event(space, old_state, new_state)

    def _create_event_from_sqs(
        self,
        space: AjaxSpace,
        old_state: SecurityState,
        new_state: SecurityState,
        source_name: str = "",
    ) -> None:
        """Create an event from SQS data (includes user who triggered).

        Args:
            space: The AjaxSpace object
            old_state: Previous security state
            new_state: New security state
            source_name: Name of user/device who triggered the action
        """
        action_map = {
            SecurityState.ARMED: "armed",
            SecurityState.DISARMED: "disarmed",
            SecurityState.NIGHT_MODE: "night_mode",
            SecurityState.PARTIALLY_ARMED: "partially_armed",
        }

        action = action_map.get(new_state, new_state.value.lower())

        # Create event with source info from SQS
        event = {
            "action": action,
            "hub_id": space.hub_id or space.id,
            "space_id": space.id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_time": datetime.now(timezone.utc),
        }

        # Add source/user info if available
        if source_name:
            event["source_name"] = source_name

        # Store in recent_events (keep last 10)
        space.recent_events.insert(0, event)
        space.recent_events = space.recent_events[:10]

        _LOGGER.debug(
            "Event stored: %s by %s (total: %d)",
            action,
            source_name or "?",
            len(space.recent_events),
        )

        # Update polling interval based on new state
        self._update_polling_interval(new_state)

        # Fire Home Assistant event for automations
        self._fire_security_state_event(space, old_state, new_state)

        # Create persistent notification in HA
        asyncio.create_task(
            self._create_sqs_notification(action, source_name, space.name)
        )

    async def _create_sqs_notification(
        self, action: str, source_name: str, space_name: str
    ) -> None:
        """Create a persistent notification in HA for SQS events."""
        from homeassistant.components.persistent_notification import async_create

        # Check notification filter settings
        options = self.config_entry.options if self.config_entry else {}

        # Check if persistent notifications are enabled
        if not options.get(CONF_PERSISTENT_NOTIFICATION, True):
            return

        notification_filter = options.get(
            CONF_NOTIFICATION_FILTER, NOTIFICATION_FILTER_ALL
        )

        # Apply filter
        if notification_filter == NOTIFICATION_FILTER_NONE:
            return

        # Arm/disarm are security events, not alarms
        is_arming_event = action in (
            "armed",
            "disarmed",
            "night_mode",
            "partially_armed",
            # Legacy keys
            "nightmodeon",
            "partiallyarmed",
        )

        if notification_filter == NOTIFICATION_FILTER_ALARMS_ONLY and is_arming_event:
            # "Alarms only" should NOT show arm/disarm events
            return

        # NOTIFICATION_FILTER_SECURITY_EVENTS and NOTIFICATION_FILTER_ALL show everything

        # Map action to translated message using event_codes
        from .event_codes import get_event_message

        # Get language from Home Assistant
        ha_language = self.hass.config.language or "en"
        lang_map = {"fr": "fr", "es": "es", "en": "en"}
        language = lang_map.get(ha_language[:2], "en")

        # Map SecurityState values to action keys in event_codes
        action_to_key = {
            "armed": "armed",
            "disarmed": "disarmed",
            "night_mode": "night_mode",
            "partially_armed": "partially_armed",
            # Legacy keys for backwards compatibility
            "nightmodeon": "night_mode",
            "partiallyarmed": "partially_armed",
        }

        action_key = action_to_key.get(action, action)
        message = get_event_message(action_key, language)

        _LOGGER.info(
            "SQS notification: action=%s, action_key=%s, lang=%s, message=%s",
            action,
            action_key,
            language,
            message,
        )

        if source_name:
            by_word = {"fr": "par", "en": "by", "es": "por"}.get(language, "by")
            message = f"{message} {by_word} {source_name}"

        title = f"Ajax - {space_name}"

        async_create(
            self.hass,
            message,
            title=title,
            notification_id=f"ajax_{action}_{int(time.time())}",
        )

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
            "motioncam": DeviceType.MOTION_DETECTOR,
            "motion_cam": DeviceType.MOTION_DETECTOR,
            "motioncamoutdoor": DeviceType.MOTION_DETECTOR,
            "motion_cam_outdoor": DeviceType.MOTION_DETECTOR,
            "motioncamoutdoorphod": DeviceType.MOTION_DETECTOR,
            "motion_cam_outdoor_phod": DeviceType.MOTION_DETECTOR,
            "motionprotectcurtain": DeviceType.MOTION_DETECTOR,
            "motion_protect_curtain": DeviceType.MOTION_DETECTOR,
            "motionprotectoutdoor": DeviceType.MOTION_DETECTOR,
            "motion_protect_outdoor": DeviceType.MOTION_DETECTOR,
            # Combi detectors (motion + glass break)
            "combi_protect": DeviceType.COMBI_PROTECT,
            "combiprotect": DeviceType.COMBI_PROTECT,
            "combi": DeviceType.COMBI_PROTECT,
            # Door/Window contacts
            "door_protect": DeviceType.DOOR_CONTACT,
            "doorprotect": DeviceType.DOOR_CONTACT,
            "doorprotectplus": DeviceType.DOOR_CONTACT,
            "door_protect_plus": DeviceType.DOOR_CONTACT,
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
            "fireprotectplus": DeviceType.SMOKE_DETECTOR,
            "fire_protect_plus": DeviceType.SMOKE_DETECTOR,
            "fireprotect2plus": DeviceType.SMOKE_DETECTOR,
            "fire_protect_2_plus": DeviceType.SMOKE_DETECTOR,
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
            "homesiren": DeviceType.SIREN,
            "home_siren": DeviceType.SIREN,
            "streetsiren": DeviceType.SIREN,
            "street_siren": DeviceType.SIREN,
            "streetsirendoubledeck": DeviceType.SIREN,
            "street_siren_double_deck": DeviceType.SIREN,
            # SpeakerPhone
            "speakerphone": DeviceType.SPEAKERPHONE,
            # Transmitter
            "transmitter": DeviceType.TRANSMITTER,
            "integration": DeviceType.TRANSMITTER,
            # MultiTransmitter (wired sensors hub)
            "multitransmitter": DeviceType.MULTI_TRANSMITTER,
            "multi_transmitter": DeviceType.MULTI_TRANSMITTER,
            # MultiTransmitter wired inputs (treat as door contacts)
            "multitransmitterwireinput": DeviceType.WIRE_INPUT,
            "multitransmitter_wire_input": DeviceType.WIRE_INPUT,
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
            "wallswitch": DeviceType.WALLSWITCH,
            "thermostat": DeviceType.THERMOSTAT,
            "life_quality": DeviceType.LIFE_QUALITY,
            "lifequality": DeviceType.LIFE_QUALITY,
            # Cameras
            "camera": DeviceType.CAMERA,
            "cam": DeviceType.CAMERA,
            # Hub
            "hub": DeviceType.HUB,
        }

        # Clean up the type string (remove formatting artifacts)
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

    def _register_ha_action(self, hub_id: str) -> None:
        """Register that Home Assistant triggered an action on this hub."""
        import time

        self._pending_ha_actions[hub_id] = time.time()

    def get_pending_ha_action(self, hub_id: str) -> bool:
        """Check if Home Assistant triggered an action on this hub recently.

        Returns True if HA action was within the last 10 seconds.
        """
        import time

        timestamp = self._pending_ha_actions.get(hub_id, 0)
        if time.time() - timestamp < 10:
            # Clear the pending action
            del self._pending_ha_actions[hub_id]
            return True
        return False

    async def async_arm_space(self, space_id: str, force: bool = True) -> None:
        """Arm a space.

        Args:
            space_id: The space ID to arm
            force: If True, ignore problems and force arm even with open sensors
        """
        _LOGGER.info("Arming space %s (force=%s)", space_id, force)

        try:
            self._register_ha_action(space_id)
            await self.api.async_arm(space_id, ignore_problems=force)
            # State will be updated via SQS with "Home Assistant" as source

        except AjaxRestApiError as err:
            _LOGGER.error("Failed to arm space %s: %s", space_id, err)
            raise

    async def async_disarm_space(self, space_id: str) -> None:
        """Disarm a space."""
        _LOGGER.info("Disarming space %s", space_id)

        try:
            self._register_ha_action(space_id)
            await self.api.async_disarm(space_id)
            # State will be updated via SQS with "Home Assistant" as source

        except AjaxRestApiError as err:
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
            self._register_ha_action(space_id)
            await self.api.async_night_mode(space_id, enabled=True)
            # State will be updated via SQS with "Home Assistant" as source

        except AjaxRestApiError as err:
            _LOGGER.error(
                "Failed to activate night mode for space %s: %s", space_id, err
            )
            raise

    async def async_press_panic_button(self, space_id: str) -> None:
        """Press panic button (trigger panic alarm) for a space."""
        _LOGGER.warning("PANIC BUTTON pressed for space %s", space_id)

        try:
            await self.api.async_press_panic_button(space_id)
            # No state update needed, panic is instantaneous

        except AjaxRestApiError as err:
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
        """Shutdown the coordinator and cleanup resources."""
        _LOGGER.info("Shutting down Ajax coordinator")

        # Stop SQS Manager (real-time events - direct mode)
        if self.sqs_manager:
            try:
                _LOGGER.debug("Stopping AWS SQS Manager...")
                await self.sqs_manager.stop()
            except Exception as err:
                _LOGGER.error("Error stopping SQS Manager: %s", err)

        # Stop SSE Manager (real-time events - proxy mode)
        if self.sse_manager:
            try:
                _LOGGER.debug("Stopping SSE Manager...")
                await self.sse_manager.stop()
            except Exception as err:
                _LOGGER.error("Error stopping SSE Manager: %s", err)

        # Stop all fast poll tasks
        for task in self._fast_poll_tasks.values():
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        self._fast_poll_tasks.clear()

        # Close API connection
        await self.api.close()
