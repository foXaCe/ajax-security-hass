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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AjaxRestApi, AjaxRestApiError, AjaxRestAuthError
from .const import (
    CONF_NOTIFICATION_FILTER,
    CONF_PERSISTENT_NOTIFICATION,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_USERNAME,
    DOMAIN,
    METADATA_REFRESH_INTERVAL,
    NOTIFICATION_FILTER_ALARMS_ONLY,
    NOTIFICATION_FILTER_ALL,
    NOTIFICATION_FILTER_NONE,
    UPDATE_INTERVAL,
    UPDATE_INTERVAL_ARMED,
    UPDATE_INTERVAL_DOOR_SENSORS,
)
from .models import (
    AjaxAccount,
    AjaxDevice,
    AjaxGroup,
    AjaxNotification,
    AjaxRoom,
    AjaxSmartLock,
    AjaxSpace,
    AjaxVideoEdge,
    DeviceType,
    GroupState,
    NotificationType,
    SecurityState,
    VideoEdgeType,
)

# Type imports for optional modules (for type checking only)
if TYPE_CHECKING:
    from .onvif_client import OnvifDetectionEvent
    from .onvif_manager import AjaxOnvifManager
    from .sqs_manager import SQSManager
    from .sse_manager import SSEManager

# Optional SQS support
SQS_AVAILABLE = False
_SQSManager: type | None = None
_AjaxSQSClient: type | None = None
try:
    from .sqs_client import AjaxSQSClient as _AjaxSQSClient
    from .sqs_manager import SQSManager as _SQSManager

    SQS_AVAILABLE = True
except ImportError:
    pass

# Optional SSE support (for proxy mode)
SSE_AVAILABLE = False
_SSEManager: type | None = None
_AjaxSSEClient: type | None = None
try:
    from .sse_client import AjaxSSEClient as _AjaxSSEClient
    from .sse_manager import SSEManager as _SSEManager

    SSE_AVAILABLE = True
except ImportError:
    pass

# Optional ONVIF support (for local AI detections)
ONVIF_AVAILABLE = False
_AjaxOnvifManager: type | None = None
try:
    from .onvif_manager import AjaxOnvifManager as _AjaxOnvifManager

    ONVIF_AVAILABLE = True
except ImportError:
    pass

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
        enabled_spaces: list[str] | None = None,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: Home Assistant instance
            api: Ajax REST API instance
            aws_access_key_id: AWS access key ID (optional, for SQS in direct mode)
            aws_secret_access_key: AWS secret access key (optional, for SQS in direct mode)
            queue_name: SQS queue name (optional, for SQS in direct mode)
            sse_url: SSE endpoint URL (optional, for proxy mode)
            enabled_spaces: List of space IDs to enable (None = all spaces)
        """
        self.api = api
        self.account: AjaxAccount | None = None
        self._enabled_spaces: list[str] | None = enabled_spaces
        self.all_discovered_spaces: dict[str, str] = {}  # space_id -> name (for options flow)
        self._fast_poll_tasks: dict[str, asyncio.Task] = {}  # device_id -> fast polling task for door sensors
        self._door_sensor_poll_task: asyncio.Task | None = (
            None  # Continuous door sensor polling when disarmed or in night mode
        )
        self._door_sensor_poll_security_state: SecurityState = SecurityState.DISARMED
        self._initial_load_done: bool = False  # Track if initial data load is complete
        self._force_metadata_refresh: bool = False  # Flag to force full metadata refresh
        self._pending_ha_actions: dict[str, float] = {}  # hub_id -> timestamp of HA action

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

        # ONVIF local AI detections (optional, for video edge cameras)
        self.onvif_manager: AjaxOnvifManager | None = None
        self._onvif_initialized = False
        self.config_entry = None  # Set by __init__.py after coordinator creation

        # Device details refresh optimization
        # Battery/signal don't change often, so refresh every 5 minutes instead of every poll
        self._last_device_details_refresh: float = 0
        self._device_details_refresh_interval: int = 300  # 5 minutes in seconds

        # Metadata refresh optimization (rooms, users, groups)
        # These don't change often, so refresh every hour instead of every poll
        self._last_metadata_refresh: float = 0

        # Door sensor fast polling option (disabled by default to reduce API calls)
        self._door_sensor_fast_poll_enabled: bool = False

        # Flag to skip state change event creation when SQS already created the event
        self._skip_state_change_event: bool = False

        # Flag to bypass proxy cache on next refresh (after SSE event or user action)
        self._bypass_cache_next_refresh: bool = False

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
            # Debouncer: Wait 1.5s of silence before triggering refresh
            # Prevents flooding when multiple SQS/SSE events arrive rapidly
            request_refresh_debouncer=Debouncer(
                hass,
                _LOGGER,
                cooldown=1.5,
                immediate=False,
            ),
        )

    def _update_polling_interval(self, security_state: SecurityState) -> None:
        """Update polling interval based on security state and proxy suggestion.

        - Armed/Night/Partial: 60s (SSE/SQS handles real-time events)
        - Disarmed: 30s (no SSE/SQS, need faster polling)
        - Proxy suggestion: respected if higher (load balancing)

        Also manages door sensor fast polling (5s) when disarmed.

        Args:
            security_state: Current security state of the space
        """
        is_disarmed = security_state == SecurityState.DISARMED

        if security_state in (
            SecurityState.ARMED,
            SecurityState.NIGHT_MODE,
            SecurityState.PARTIALLY_ARMED,
        ):
            base_interval = UPDATE_INTERVAL_ARMED
        else:
            base_interval = UPDATE_INTERVAL

        # Respect proxy suggested interval if higher (load balancing)
        # Proxy sends X-Suggested-Interval: 30|60|120 based on rate limit remaining
        new_interval = base_interval
        if self.api.suggested_interval and self.api.suggested_interval > base_interval:
            new_interval = self.api.suggested_interval
            _LOGGER.debug(
                "Using proxy suggested interval: %ds (base: %ds)",
                new_interval,
                base_interval,
            )

        current_interval = self.update_interval.total_seconds() if self.update_interval else UPDATE_INTERVAL

        if new_interval != current_interval:
            self.update_interval = timedelta(seconds=new_interval)
            _LOGGER.info(
                "Polling interval changed to %ds (security state: %s, proxy suggested: %s)",
                new_interval,
                security_state.value,
                self.api.suggested_interval,
            )

        # Manage door sensor fast polling based on security state
        # Poll when disarmed OR in night mode (for sensors excluded from night mode)
        should_poll = is_disarmed or security_state == SecurityState.NIGHT_MODE
        self._manage_door_sensor_polling(should_poll, security_state)

    def _manage_door_sensor_polling(self, should_poll: bool, security_state: SecurityState) -> None:
        """Start or stop door sensor fast polling.

        Args:
            should_poll: True to start polling, False to stop
            security_state: Current security state (used to filter sensors in night mode)
        """
        # Check if fast polling is enabled (can be disabled to reduce API calls)
        # Also disable fast polling in proxy mode to reduce load on shared proxy
        if not self._door_sensor_fast_poll_enabled or self._sse_url:
            should_poll = False

        # Store security state for the polling loop
        self._door_sensor_poll_security_state = security_state

        if should_poll and self._door_sensor_poll_task is None:
            # Start door sensor polling when disarmed or in night mode
            self._door_sensor_poll_task = self.config_entry.async_create_background_task(
                self.hass, self._async_poll_door_sensors_loop(), "ajax_door_sensor_poll"
            )
            _LOGGER.info(
                "Started door sensor fast polling (every %ds, state: %s)",
                UPDATE_INTERVAL_DOOR_SENSORS,
                security_state.value,
            )
        elif not should_poll and self._door_sensor_poll_task is not None:
            # Stop door sensor polling when armed
            self._door_sensor_poll_task.cancel()
            self._door_sensor_poll_task = None
            _LOGGER.info("Stopped door sensor fast polling (system armed)")

    async def _async_poll_door_sensors_loop(self) -> None:
        """Continuous polling loop for door sensors.

        Polls door sensors in two scenarios:
        - When disarmed: poll all door sensors
        - When in night mode: poll only sensors excluded from night mode
        """
        try:
            while True:
                await asyncio.sleep(UPDATE_INTERVAL_DOOR_SENSORS)

                if not self.account:
                    continue

                # Poll door sensors and transmitters for each space
                for space_id, space in self.account.spaces.items():
                    current_state = space.security_state

                    # Determine which sensors to poll based on security state
                    if current_state == SecurityState.DISARMED:
                        # When disarmed: poll all door sensors and transmitters
                        contact_sensors = [
                            device
                            for device in space.devices.values()
                            if device.type in (DeviceType.DOOR_CONTACT, DeviceType.TRANSMITTER)
                        ]
                    elif current_state == SecurityState.NIGHT_MODE:
                        # When in night mode: only poll sensors excluded from night mode
                        contact_sensors = [
                            device
                            for device in space.devices.values()
                            if device.type in (DeviceType.DOOR_CONTACT, DeviceType.TRANSMITTER)
                            and not device.attributes.get("night_mode_arm", True)
                        ]
                    else:
                        # Skip for other armed states
                        continue

                    if not contact_sensors:
                        continue

                    if not space.hub_id:
                        continue

                    try:
                        # Get all devices with enriched data (includes reedClosed)
                        devices_data = await self.api.async_get_devices(space.hub_id, enrich=True)
                        updated = False

                        for device_summary in devices_data:
                            device_id = device_summary.get("id")
                            if device_id and device_id in space.devices:
                                device = space.devices[device_id]

                                # With enrich=True, detailed data is in "model" sub-object
                                device_data = dict(device_summary)
                                if "model" in device_summary:
                                    device_data.update(device_summary["model"])

                                if device.type == DeviceType.DOOR_CONTACT:
                                    # Get current state
                                    old_door_state = device.attributes.get("door_opened")

                                    # Check reedClosed directly from device_data
                                    reed_closed = device_data.get("reedClosed")
                                    external_state = device_data.get("externalContactState")

                                    # Calculate new door state
                                    if reed_closed is not None:
                                        new_door_state = not reed_closed
                                    elif external_state is not None:
                                        # Default to externalContactState
                                        new_door_state = external_state != "OK"
                                        # Check wiringSchemeSpecificDetails for more accurate state
                                        wiring_details = device_data.get("wiringSchemeSpecificDetails", {})
                                        wiring_type = wiring_details.get("wiringSchemeType")
                                        if wiring_type == "TWO_EOL":
                                            contact_two = wiring_details.get("contactTwoDetails", {})
                                            contact_state = contact_two.get("contactState")
                                            if contact_state:
                                                new_door_state = contact_state != "OK"
                                        elif wiring_type in ("NO_EOL", "ONE_EOL"):
                                            contact_state = wiring_details.get("contactState")
                                            if contact_state:
                                                new_door_state = contact_state != "OK"
                                    else:
                                        # Try attributes as fallback
                                        api_attrs = device_data.get("attributes", {})
                                        normalized_attrs = self._normalize_device_attributes(api_attrs, device.type)
                                        new_door_state = bool(normalized_attrs.get("door_opened", old_door_state))

                                    if old_door_state != new_door_state:
                                        device.attributes["door_opened"] = new_door_state
                                        _LOGGER.debug(
                                            "Door sensor %s state changed: %s -> %s",
                                            device.name,
                                            old_door_state,
                                            new_door_state,
                                        )
                                        updated = True

                                elif device.type == DeviceType.TRANSMITTER:
                                    # Handle Transmitter external contact state
                                    old_triggered = device.attributes.get("externalContactTriggered")
                                    new_triggered = device_data.get("externalContactTriggered")

                                    if new_triggered is not None and old_triggered != new_triggered:
                                        device.attributes["externalContactTriggered"] = new_triggered
                                        _LOGGER.debug(
                                            "Transmitter %s contact changed: %s -> %s",
                                            device.name,
                                            old_triggered,
                                            new_triggered,
                                        )
                                        updated = True

                        if updated:
                            self.async_set_updated_data(self.account)

                    except (AjaxRestApiError, AjaxRestAuthError) as err:
                        _LOGGER.debug(
                            "Error polling door sensors for space %s: %s",
                            space_id,
                            err,
                        )

        except asyncio.CancelledError:
            _LOGGER.debug("Door sensor polling loop cancelled")
            raise

    def _should_refresh_metadata(self) -> bool:
        """Check if metadata (rooms, users, groups) should be refreshed.

        Returns True if more than METADATA_REFRESH_INTERVAL seconds have passed.
        """
        current_time = time.time()
        return current_time - self._last_metadata_refresh >= METADATA_REFRESH_INTERVAL

    async def async_force_metadata_refresh(self) -> None:
        """Force a full metadata refresh (rooms, users, groups).

        Can be called from a service or button to manually refresh.
        Uses async_refresh() instead of async_request_refresh() to bypass
        the DataUpdateCoordinator debouncer and execute immediately.
        async_request_refresh() defers to the debounce timer (30-60s),
        which causes group state updates to be delayed.
        """
        _LOGGER.info("Forcing full metadata refresh (immediate)")
        self._force_metadata_refresh = True  # Set flag to force refresh
        await self.async_refresh()

    async def async_request_refresh_bypass_cache(self) -> None:
        """Request a refresh that bypasses the proxy cache.

        Use this after SSE events or user actions to get fresh data.
        The proxy will fetch new data from Ajax API instead of cached data.
        """
        self._bypass_cache_next_refresh = True
        await self.async_request_refresh()

    async def _async_update_data(self) -> AjaxAccount:
        """Fetch data from Ajax REST API.

        Uses optimized polling strategy:
        - Light polling (every cycle): Hub state + devices only
        - Full metadata refresh (hourly): Rooms, users, groups
        """
        try:
            # Log when connection is restored after a failure
            if not self.last_update_success:
                _LOGGER.info("Connection to Ajax API restored")

            # Check if we need to bypass proxy cache (after SSE event or user action)
            if self._bypass_cache_next_refresh:
                self.api._bypass_cache_once = True
                self._bypass_cache_next_refresh = False
                _LOGGER.debug("Bypassing proxy cache for this refresh")

            # Initialize account if needed
            if self.account is None:
                await self._async_init_account()

            # After init, account must exist
            assert self.account is not None, "Account should be initialized"

            # Only do full data load on first run or manual reload
            if not self._initial_load_done:
                # Full update - use hubs endpoint directly to get hubId
                await self._async_update_spaces_from_hubs(full_refresh=True)
                self._last_metadata_refresh = time.time()

                # Load devices, video edges, and notifications
                # In proxy mode, execute sequentially to avoid rate limiting burst
                # In direct mode, execute in parallel for faster startup
                if self.api.is_proxy_mode:
                    _LOGGER.debug("Proxy mode: loading data sequentially to avoid rate limits")
                    for space_id in self.account.spaces:
                        await self._async_update_devices(space_id)
                        await self._async_update_video_edges(space_id)
                        await self._async_update_smart_locks(space_id)
                        await self._async_update_notifications(space_id, limit=20)
                else:
                    tasks = []
                    for space_id in self.account.spaces:
                        tasks.append(self._async_update_devices(space_id))
                        tasks.append(self._async_update_video_edges(space_id))
                        tasks.append(self._async_update_smart_locks(space_id))
                        tasks.append(self._async_update_notifications(space_id, limit=20))
                    await asyncio.gather(*tasks, return_exceptions=True)

                # Mark initial load as complete
                self._initial_load_done = True
                _LOGGER.info("Initial data load complete")

                # Clean up HA device registry: remove devices deleted from Ajax
                self._async_cleanup_stale_devices()

                # Start door sensor polling if any space is disarmed or in night mode
                for space in self.account.spaces.values():
                    if space.security_state in (
                        SecurityState.DISARMED,
                        SecurityState.NIGHT_MODE,
                    ):
                        self._manage_door_sensor_polling(True, space.security_state)
                        break

                # Initialize real-time events in background
                # Priority: SSE (proxy mode) > SQS (direct mode)
                if not self._sse_initialized and self._sse_url:
                    # Proxy mode: use SSE for real-time events
                    self.config_entry.async_create_background_task(self.hass, self._async_init_sse(), "ajax_init_sse")
                elif not self._sqs_initialized and self._aws_access_key_id:
                    # Direct mode: use SQS for real-time events
                    self.config_entry.async_create_background_task(self.hass, self._async_init_sqs(), "ajax_init_sqs")

                # Initialize ONVIF for local AI detections (works even when disarmed)
                if not self._onvif_initialized:
                    self.config_entry.async_create_background_task(
                        self.hass, self._async_init_onvif(), "ajax_init_onvif"
                    )
            else:
                # Periodic update - optimized polling
                # Check if we need full metadata refresh (hourly or forced)
                need_metadata_refresh = self._force_metadata_refresh or self._should_refresh_metadata()
                if need_metadata_refresh:
                    if self._force_metadata_refresh:
                        _LOGGER.info("Forced metadata refresh (groups will be updated)")
                        self._force_metadata_refresh = False  # Clear the flag
                    else:
                        _LOGGER.info("Hourly metadata refresh (rooms, users, groups)")
                    self._last_metadata_refresh = time.time()

                # Light or full update based on metadata refresh need
                await self._async_update_spaces_from_hubs(full_refresh=need_metadata_refresh)

                for space_id in self.account.spaces:
                    space_obj: AjaxSpace | None = self.account.spaces.get(space_id)
                    if space_obj:
                        self._reset_expired_motion_detections(space_obj)
                        await self._async_update_devices(space_id)
                        # Refresh video edges to get AI detection states
                        if space_obj.video_edges:
                            await self._async_update_video_edges(space_id)
                        # Refresh smart locks (API data is minimal, state is event-driven)
                        if space_obj.smart_locks:
                            await self._async_update_smart_locks(space_id)

            return self.account

        except AjaxRestAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except AjaxRestApiError as err:
            if self.last_update_success:
                _LOGGER.warning("Connection to Ajax API lost: %s", err)
            raise UpdateFailed(f"Error communicating with API: {err}") from err

    async def _async_fast_poll_door_sensor(self, space_id: str, device_id: str) -> None:
        """Fast poll a door sensor after opening to quickly detect closure."""
        poll_interval = 3  # Poll every 3 seconds
        max_duration = 120  # Stop after 2 minutes
        start_time = asyncio.get_running_loop().time()

        try:
            while True:
                elapsed = asyncio.get_running_loop().time() - start_time
                if elapsed > max_duration:
                    break

                try:
                    if self.account is None:
                        break
                    space = self.account.spaces.get(space_id)
                    if not space or not space.hub_id:
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
                                normalized_attrs = self._normalize_device_attributes(api_attrs, device.type)
                                device.attributes.update(normalized_attrs)

                                # Check door state (after normalization)
                                door_opened = device.attributes.get("door_opened", False)

                                if not door_opened:
                                    assert self.account is not None
                                    self.async_set_updated_data(self.account)
                                    if device_id in self._fast_poll_tasks:
                                        del self._fast_poll_tasks[device_id]
                                    break

                    if not device_found:
                        break

                except (AjaxRestApiError, AjaxRestAuthError) as err:
                    _LOGGER.error("Error in fast polling for door sensor %s: %s", device_id, err)

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
            _LOGGER.info("AWS SQS not available (aiobotocore not installed). Using REST API polling only.")
            self._sqs_initialized = True  # Mark as "initialized" to prevent retries
            return

        # Check if credentials are provided
        if not self._aws_access_key_id or not self._aws_secret_access_key or not self._queue_name:
            _LOGGER.debug("AWS credentials not configured. Using REST API polling only.")
            self._sqs_initialized = True
            return

        try:
            _LOGGER.info("Initializing AWS SQS for real-time events...")

            # Create SQS client with HA's event loop for thread-safe callbacks
            assert _AjaxSQSClient is not None  # Validated by SQS_AVAILABLE check above
            assert _SQSManager is not None
            sqs_client = _AjaxSQSClient(
                aws_access_key_id=self._aws_access_key_id,
                aws_secret_access_key=self._aws_secret_access_key,
                queue_name=self._queue_name,
                hass_loop=self.hass.loop,
            )

            # Create SQS manager
            self.sqs_manager = _SQSManager(
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
                _LOGGER.info("✓ AWS SQS initialized successfully - Real-time events enabled!")
            else:
                _LOGGER.warning("Failed to start SQS - Falling back to REST API polling only")
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
            _LOGGER.info("SSE not available (module not loaded). Using REST API polling only.")
            self._sse_initialized = True
            return

        # Check if SSE URL is provided
        if not self._sse_url:
            _LOGGER.debug("SSE URL not configured. Using REST API polling only.")
            self._sse_initialized = True
            return

        # Check if we have a session token
        if not self.api.session_token:
            _LOGGER.warning("No session token available for SSE. Using REST API polling only.")
            self._sse_initialized = True
            return

        try:
            _LOGGER.info("Initializing SSE for real-time events...")

            # Create SSE client
            assert _AjaxSSEClient is not None  # Validated by SSE_AVAILABLE check above
            assert _SSEManager is not None
            sse_client = _AjaxSSEClient(
                sse_url=self._sse_url,
                session_token=self.api.session_token or "",
                callback=lambda event: None,  # Will be set by manager
                hass_loop=self.hass.loop,
                user_id=self.api.user_id,
                verify_ssl=self.api.verify_ssl,
            )

            # Create SSE manager
            self.sse_manager = _SSEManager(
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
                _LOGGER.info("✓ SSE initialized successfully - Real-time events enabled!")
            else:
                _LOGGER.warning("Failed to start SSE - Falling back to REST API polling only")
                self.sse_manager = None

        except Exception as err:
            _LOGGER.warning(
                "Failed to initialize SSE: %s - Using REST API polling only",
                err,
            )
            self.sse_manager = None

        finally:
            self._sse_initialized = True

    async def _async_init_onvif(self) -> None:
        """Initialize ONVIF for local AI detection events.

        ONVIF provides local AI detection events (human, vehicle, pet, motion)
        directly from Ajax cameras without relying on the cloud API.

        Benefits:
        - Works even when alarm is DISARMED (unlike SSE/SQS)
        - 100% local, no cloud dependency for detections
        - Real-time events via ONVIF PullPoint subscription

        Note:
            Requires onvif-zeep-async package and RTSP/ONVIF credentials.
            If ONVIF fails to initialize, detection events fall back to REST API.
        """
        # Check if ONVIF is available
        if not ONVIF_AVAILABLE:
            _LOGGER.debug("ONVIF not available (onvif-zeep-async not installed)")
            self._onvif_initialized = True
            return

        # Check if config entry is available
        if not self.config_entry:
            _LOGGER.debug("Config entry not set, cannot get ONVIF credentials")
            self._onvif_initialized = True
            return

        # Get RTSP/ONVIF credentials from config entry options
        username = self.config_entry.options.get(CONF_RTSP_USERNAME, "")
        password = self.config_entry.options.get(CONF_RTSP_PASSWORD, "")

        if not username or not password:
            _LOGGER.debug("ONVIF credentials not configured, skipping local AI detections")
            self._onvif_initialized = True
            return

        # Collect all video edges with IP addresses
        if self.account is None:
            self._onvif_initialized = True
            return

        video_edges = []
        for space in self.account.spaces.values():
            for video_edge in space.video_edges.values():
                if video_edge.ip_address:
                    video_edges.append(video_edge)

        if not video_edges:
            _LOGGER.debug("No video edges with IP addresses found")
            self._onvif_initialized = True
            return

        try:
            _LOGGER.info("Initializing ONVIF for local AI detections...")

            # Create ONVIF manager with event callback
            assert _AjaxOnvifManager is not None  # Validated by ONVIF_AVAILABLE check above
            self.onvif_manager = _AjaxOnvifManager(
                username=username,
                password=password,
                event_callback=self._handle_onvif_event,
            )

            # Start connections to all cameras
            await self.onvif_manager.async_start(video_edges)

            if self.onvif_manager.connected_count > 0:
                _LOGGER.info(
                    "✓ ONVIF initialized - %d/%d cameras connected for local AI detections",
                    self.onvif_manager.connected_count,
                    len(video_edges),
                )
            else:
                _LOGGER.warning("ONVIF: No cameras connected - Check ONVIF credentials and camera network")

        except Exception as err:
            _LOGGER.warning("Failed to initialize ONVIF: %s", err)
            self.onvif_manager = None

        finally:
            self._onvif_initialized = True

    def _handle_onvif_event(self, event: OnvifDetectionEvent) -> None:
        """Handle ONVIF detection event from a camera or NVR.

        For NVR events, routes the detection to the correct camera based on channel_id.
        Updates the video edge detection state and triggers coordinator update.

        Args:
            event: The ONVIF detection event
        """
        if not self.account:
            return

        _LOGGER.debug(
            "ONVIF event received: %s (channel %s, active=%s)",
            event.detection_type,
            event.channel_id,
            event.active,
        )

        # Find the source video edge (NVR or camera)
        for space in self.account.spaces.values():
            source_ve = space.video_edges.get(event.video_edge_id)
            if not source_ve:
                continue

            # Determine target video edge for detection update
            target_ve = source_ve

            # If source is NVR, find linked camera for this channel
            if source_ve.video_edge_type == VideoEdgeType.NVR:
                target_ve = self._find_camera_for_nvr_channel(space, source_ve, event.channel_id)
                if target_ve and target_ve != source_ve:
                    _LOGGER.debug(
                        "Routing NVR event (channel %s) to camera: %s",
                        event.channel_id,
                        target_ve.name,
                    )

            # Update detection state on target video edge
            detection_key = event.detection_type.lower()  # e.g., "video_human"
            target_ve.detections[detection_key] = event.active

            # Trigger coordinator update to refresh entities
            self.async_set_updated_data(self.account)
            return

    def _find_camera_for_nvr_channel(self, space: AjaxSpace, nvr: AjaxVideoEdge, channel_id: str) -> AjaxVideoEdge:
        """Find the camera linked to an NVR channel.

        Args:
            space: The Ajax space
            nvr: The NVR video edge
            channel_id: The ONVIF channel ID

        Returns:
            The linked camera, or the NVR itself if no camera found
        """
        channels = nvr.channels
        if not isinstance(channels, list):
            return nvr

        # Ajax camera types that can be linked to NVR
        ajax_camera_types = {"TURRET", "TURRET_HL", "BULLET", "BULLET_HL", "MINIDOME", "MINIDOME_HL"}

        def get_linked_camera_from_channel(channel: dict) -> AjaxVideoEdge | None:
            """Extract linked camera from channel's sourceAliases."""
            if not isinstance(channel, dict):
                return None
            source_aliases = channel.get("sourceAliases", {})
            if not isinstance(source_aliases, dict):
                return None
            sources = source_aliases.get("sources", [])
            if not isinstance(sources, list):
                return None
            for source in sources:
                if not isinstance(source, dict):
                    continue
                if source.get("sourceType") == "PRIMARY":
                    source_type = source.get("type", "")
                    source_ve_id = source.get("videoEdgeId", "")
                    if source_type in ajax_camera_types and source_ve_id:
                        linked_camera = space.video_edges.get(source_ve_id)
                        if linked_camera:
                            return linked_camera
            return None

        # Try to find channel by index (channel_id is usually "0", "1", etc.)
        try:
            channel_idx = int(channel_id)
            if 0 <= channel_idx < len(channels):
                linked = get_linked_camera_from_channel(channels[channel_idx])
                if linked:
                    return linked
        except (ValueError, IndexError):
            pass

        # Fallback: search all channels for matching ID
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            ch_id = channel.get("id", "")
            if str(ch_id) == str(channel_id):
                linked = get_linked_camera_from_channel(channel)
                if linked:
                    return linked

        # No linked camera found, return NVR
        return nvr

    async def _async_update_spaces_from_hubs(self, full_refresh: bool = True) -> None:
        """Update spaces by fetching hubs directly (use hub_id as space_id).

        Args:
            full_refresh: If True, fetch all metadata (rooms, users, groups).
                         If False, only fetch hub state (light polling).
        """
        if self.account is None:
            return

        hubs_data = await self.api.async_get_hubs()

        for hub_data in hubs_data:
            hub_id = hub_data.get("hubId")
            if not hub_id:
                continue

            # Store all discovered spaces for options flow
            # First use hubName as fallback
            hub_name = hub_data.get("hubName", f"Hub {hub_id[:6]}")
            self.all_discovered_spaces[hub_id] = hub_name

            # Try to get the proper space name for all spaces (including disabled)
            # This ensures the options flow shows correct names
            try:
                space_binding = await self.api.async_get_space_by_hub(hub_id)
                if space_binding and space_binding.get("name"):
                    hub_name = str(space_binding.get("name"))
                    self.all_discovered_spaces[hub_id] = hub_name
            except Exception:  # noqa: S110
                pass  # Keep the fallback name

            # Skip spaces that are not enabled
            if self._enabled_spaces is not None and hub_id not in self._enabled_spaces:
                _LOGGER.debug("Skipping disabled space: %s (%s)", hub_name, hub_id)
                continue

            # Get hub details to get the name and state
            try:
                hub_details = await self.api.async_get_hub(hub_id)

                # Get space details only on full refresh or for new spaces
                space_id = hub_id
                is_new_space = space_id not in self.account.spaces

                space_name = None
                real_space_id = None
                rooms_data: list[dict] = []
                rooms_map: dict = {}

                if full_refresh or is_new_space:
                    # Full refresh: fetch space binding, rooms
                    try:
                        space_binding = await self.api.async_get_space_by_hub(hub_id)
                        if space_binding:
                            space_name = space_binding.get("name")
                            real_space_id = space_binding.get("id")
                            _LOGGER.debug(
                                "Found space '%s' (id: %s) for hub %s",
                                space_name,
                                real_space_id,
                                hub_id,
                            )
                    except Exception as space_err:
                        _LOGGER.debug("Could not get space for %s: %s", hub_id, space_err)

                    # Get rooms for this hub
                    try:
                        rooms_data = await self.api.async_get_rooms(hub_id)
                        # Build room_id -> room_name mapping
                        rooms_map = {room.get("id"): room.get("roomName") for room in rooms_data if room.get("id")}
                        _LOGGER.debug(
                            "Loaded %d rooms for hub %s",
                            len(rooms_map),
                            hub_id,
                        )
                    except Exception as room_err:
                        _LOGGER.warning("Could not get rooms for hub %s: %s", hub_id, room_err)
                else:
                    # Light refresh: reuse existing metadata
                    existing_space = self.account.spaces[space_id]
                    space_name = existing_space.name
                    real_space_id = existing_space.real_space_id
                    rooms_map = existing_space.rooms_map

                # Try to get name: prefer space name, fallback to hub details
                hub_name = (
                    space_name  # Space name from /spaces endpoint (e.g., "Maison")
                    or hub_details.get("name")
                    or hub_details.get("hubName")
                    or hub_details.get("deviceName")
                    or f"Hub {hub_id[:6]}"  # Use first 6 chars of hub_id as fallback
                )
                # Update all_discovered_spaces with the proper name
                self.all_discovered_spaces[hub_id] = hub_name

                # Parse security state from hub details
                # Check night mode first - it can be active even when groups are disarmed
                hub_state = hub_details.get("state", "DISARMED")
                # Night mode can be in dedicated fields OR in the state string itself
                # e.g., state="DISARMED_NIGHT_MODE_ON" means night mode is active
                night_mode_active = (
                    hub_details.get("nightMode")
                    or hub_details.get("nightModeEnabled")
                    or hub_details.get("nightModeActive")
                    or hub_details.get("isNightMode")
                    or "NIGHT_MODE_ON" in hub_state.upper()
                )
                _LOGGER.debug(
                    "Hub %s state parsing: state=%s, night_mode_active=%s",
                    hub_id,
                    hub_state,
                    night_mode_active,
                )
                if night_mode_active:
                    security_state = SecurityState.NIGHT_MODE
                else:
                    security_state = self._parse_security_state(hub_state)
            except (AjaxRestApiError, AjaxRestAuthError) as err:
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
                    real_space_id=real_space_id,  # Actual space ID for video edges
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
                space.real_space_id = real_space_id  # Update real space ID
                space.hub_details = hub_details  # Update hub information

            # Only update rooms, users on full refresh (they rarely change)
            if full_refresh or is_new_space:
                # Store rooms mapping in space for device room name lookup
                space.rooms_map = rooms_map

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
                    space.users = users_data
                except (AjaxRestApiError, AjaxRestAuthError):
                    space.users = []

            # Always fetch groups on every poll (group state changes frequently)
            groups_enabled = hub_details.get("groupsEnabled", False)
            space.group_mode_enabled = groups_enabled
            if groups_enabled:
                # Check if HA recently triggered an action (protect optimistic updates)
                ha_action_pending = self.has_pending_ha_action(hub_id)
                try:
                    groups_data = await self.api.async_get_groups(hub_id)
                    _LOGGER.debug(
                        "Hub %s: API returned %d groups, raw states: %s",
                        hub_id,
                        len(groups_data),
                        [(g.get("groupName"), g.get("state")) for g in groups_data],
                    )
                    for group_data in groups_data:
                        group_id = group_data.get("id")
                        if group_id:
                            # Parse group state
                            group_state_str = group_data.get("state", "DISARMED")
                            if group_state_str == "ARMED":
                                group_state = GroupState.ARMED
                            elif group_state_str == "DISARMED":
                                group_state = GroupState.DISARMED
                            else:
                                group_state = GroupState.NONE

                            # Check if group already exists
                            existing_group = space.groups.get(group_id)
                            if existing_group and ha_action_pending:
                                # Protect optimistic update - keep existing state
                                _LOGGER.debug(
                                    "Group %s: REST has %s but HA action pending, keeping %s",
                                    group_id,
                                    group_state.value,
                                    existing_group.state.value,
                                )
                                group_state = existing_group.state

                            space.groups[group_id] = AjaxGroup(
                                id=group_id,
                                name=group_data.get("groupName", f"Group {group_id}"),
                                space_id=space_id,
                                state=group_state,
                                bulk_arm_involved=group_data.get("bulkArmInvolved", False),
                                bulk_disarm_involved=group_data.get("bulkDisarmInvolved", False),
                            )
                    # Log group states for debugging
                    group_states = [f"{g.name}={g.state.value}" for g in space.groups.values()]
                    _LOGGER.debug(
                        "Hub %s: Updated %d groups: %s",
                        hub_id,
                        len(space.groups),
                        ", ".join(group_states) if group_states else "none",
                    )
                except (AjaxRestApiError, AjaxRestAuthError) as err:
                    _LOGGER.warning("Failed to get groups for hub %s: %s", hub_id, err)

            # Check if SQS/SSE recently updated this hub's state
            # If so, don't overwrite with potentially stale REST data
            old_state = space.security_state
            if old_state != security_state:
                # Check real-time event protection (don't overwrite recent updates)
                sqs_protected = self.sqs_manager and self.sqs_manager.is_state_protected(hub_id)
                sse_protected = self.sse_manager and self.sse_manager.is_state_protected(hub_id)
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

                    # Create event from state change (skip if SQS already created it)
                    if self._skip_state_change_event:
                        _LOGGER.debug("Skipping state change event (SQS already created it)")
                    else:
                        self._create_event_from_state_change(space, old_state, security_state)

    @callback
    def _async_cleanup_stale_devices(self) -> None:
        """Remove HA device registry entries for devices no longer in Ajax.

        After initial load, compare HA device registry with known Ajax devices.
        This catches devices deleted from Ajax while HA was offline.
        """
        if self.account is None:
            return

        # Collect all known Ajax device IDs across all spaces
        known_ids: set[str] = set()
        for space in self.account.spaces.values():
            # Hub/space itself is registered as a device
            known_ids.add(space.id)
            if space.hub_id:
                known_ids.add(space.hub_id)
            # Regular devices
            known_ids.update(space.devices.keys())
            # Video edge cameras
            known_ids.update(space.video_edges.keys())

        # Scan HA device registry for Ajax devices
        device_registry = dr.async_get(self.hass)
        stale_devices: list[tuple[str, str]] = []  # (ha_device_id, ajax_id)

        for ha_device in device_registry.devices.values():
            for id_tuple in ha_device.identifiers:
                if len(id_tuple) == 2 and id_tuple[0] == DOMAIN and id_tuple[1] not in known_ids:
                    stale_devices.append((ha_device.id, id_tuple[1]))

        # Remove stale devices
        for ha_device_id, ajax_id in stale_devices:
            device_registry.async_remove_device(ha_device_id)
            _LOGGER.info(
                "Auto-removed stale device (Ajax ID: %s) from HA registry - no longer in Ajax account",
                ajax_id,
            )

        if stale_devices:
            _LOGGER.info(
                "Cleaned up %d stale device(s) from HA registry",
                len(stale_devices),
            )

    async def _async_update_devices(self, space_id: str) -> None:
        """Update devices for a specific space."""
        if self.account is None:
            return
        space = self.account.spaces.get(space_id)
        if not space or not space.hub_id:
            return

        # Get all devices with full details in one API call (enrich=True)
        # The detailed data is nested in a "model" object that we merge into the device
        devices_list = await self.api.async_get_devices(space.hub_id, enrich=True)

        # Check if we need to refresh battery/signal (every 5 minutes)
        current_time = time.time()
        need_details_refresh = current_time - self._last_device_details_refresh >= self._device_details_refresh_interval
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
            raw_device_type = device_data.get("deviceType", device_data.get("type", "unknown"))
            device_type = self._parse_device_type(raw_device_type)

            # Get room_id and room_name
            room_id = device_data.get("roomId", device_data.get("room_id"))
            rooms_map = space.rooms_map
            room_name = rooms_map.get(room_id) if room_id else None

            # Create or update device
            if device_id not in space.devices:
                device = AjaxDevice(
                    id=device_id,
                    name=device_data.get("deviceName", device_data.get("name", "Unknown Device")),
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

                # Log new device with details for debugging
                multi_tx_id = device_data.get("multiTransmitterId", "")
                _LOGGER.debug(
                    "New device: %s (id=%s, type=%s, multiTxId=%s)",
                    device.name,
                    device_id,
                    raw_device_type,
                    multi_tx_id,
                )

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
                # Battery level: try multiple field names
                # Round to int to avoid jitter on last decimal
                battery = device_data.get("batteryChargeLevelPercentage")
                if battery is None:
                    battery = device_data.get("batteryPercents")
                if battery is None:
                    battery = device_data.get("battery_level")
                if battery is not None:
                    battery = round(battery)
                device.battery_level = battery
                device.battery_state = device_data.get("batteryState", device_data.get("battery_state"))
                # Convert signal level string to percentage
                signal_level = device_data.get("signalLevel", device_data.get("signal_strength"))
                if isinstance(signal_level, str):
                    signal_map = {
                        "EXCELLENT": 100,
                        "STRONG": 85,
                        "GOOD": 70,
                        "NORMAL": 60,
                        "MEDIUM": 50,
                        "WEAK": 30,
                        "POOR": 15,
                    }
                    device.signal_strength = signal_map.get(signal_level.upper())
                elif signal_level is not None:
                    # Round to int to avoid jitter on last decimal
                    device.signal_strength = round(signal_level)
                else:
                    device.signal_strength = signal_level

            device.firmware_version = device_data.get("firmwareVersion", device_data.get("firmware_version"))
            device.hardware_version = device_data.get("hardwareVersion", device_data.get("hardware_version"))
            device.states = device_data.get("states", [])

            # Store tampered status in attributes
            if "tampered" in device_data:
                device.attributes["tampered"] = device_data.get("tampered", False)

            # Store temperature if available (DoorProtect Plus)
            # Round to 1 decimal to avoid jitter on last decimal
            if "temperature" in device_data:
                temp = device_data.get("temperature")
                if temp is not None:
                    temp = round(temp, 1)
                device.attributes["temperature"] = temp

            # Store other useful attributes
            # For MultiTransmitterWireInput, settings are in wiredDeviceSettings
            wired_settings = device_data.get("wiredDeviceSettings") or {}

            if "alwaysActive" in device_data:
                device.attributes["always_active"] = device_data.get("alwaysActive", False)
            elif "alwaysActive" in wired_settings:
                device.attributes["always_active"] = wired_settings.get("alwaysActive", False)

            if "nightModeArm" in device_data or "armedInNightMode" in device_data:
                night_mode_value = device_data.get(
                    "nightModeArm",
                    device_data.get("armedInNightMode", False),
                )
                device.attributes["armed_in_night_mode"] = night_mode_value
                device.attributes["night_mode_arm"] = night_mode_value  # Alias for handlers
            elif "nightModeArm" in wired_settings:
                # MultiTransmitterWireInput: nightModeArm is in wiredDeviceSettings
                night_mode_value = wired_settings.get("nightModeArm", False)
                device.attributes["armed_in_night_mode"] = night_mode_value
                device.attributes["night_mode_arm"] = night_mode_value

            # DoorProtect Plus specific attributes
            if "extraContactAware" in device_data:
                device.attributes["extra_contact_aware"] = device_data.get("extraContactAware", False)
            if "shockSensorAware" in device_data:
                device.attributes["shock_sensor_aware"] = device_data.get("shockSensorAware", False)
            if "accelerometerAware" in device_data:
                device.attributes["accelerometer_aware"] = device_data.get("accelerometerAware", False)
            if "shockSensorSensitivity" in device_data:
                device.attributes["shock_sensor_sensitivity"] = device_data.get("shockSensorSensitivity", 0)
            if "accelerometerTiltDegrees" in device_data:
                device.attributes["accelerometer_tilt_degrees"] = device_data.get("accelerometerTiltDegrees", 5)
            if "ignoreSimpleImpact" in device_data:
                device.attributes["ignore_simple_impact"] = device_data.get("ignoreSimpleImpact", False)
            if "sirenTriggers" in device_data:
                device.attributes["siren_triggers"] = device_data.get("sirenTriggers", [])

            # Door contact state (reedClosed -> door_opened)
            if "reedClosed" in device_data:
                # reedClosed=True means door is closed, so door_opened=False
                device.attributes["door_opened"] = not device_data.get("reedClosed", True)
            # External contact state (extraContactClosed -> external_contact_opened)
            if "extraContactClosed" in device_data:
                device.attributes["external_contact_opened"] = not device_data.get("extraContactClosed", True)
            # Multitransmitter state (externalContactState -> door_opened)
            if "externalContactState" in device_data:
                ext_state = device_data.get("externalContactState")
                # externalContactState: OK=closed, TRIGGERED=open
                door_opened = ext_state != "OK"

                # Check wiringSchemeSpecificDetails for more accurate state
                wiring_details = device_data.get("wiringSchemeSpecificDetails", {})
                wiring_type = wiring_details.get("wiringSchemeType")

                # Store wiring type for handler to know if tamper sensor is available
                if wiring_type:
                    device.attributes["wiring_type"] = wiring_type

                if wiring_type == "TWO_EOL":
                    # TWO_EOL: contactOneDetails is tamper, contactTwoDetails is door
                    contact_one = wiring_details.get("contactOneDetails", {})
                    contact_two = wiring_details.get("contactTwoDetails", {})

                    # Parse tamper state from contactOneDetails
                    tamper_state = contact_one.get("contactState")
                    if tamper_state:
                        device.attributes["tampered"] = tamper_state != "OK"

                    # Parse door state from contactTwoDetails
                    contact_state = contact_two.get("contactState")
                    if contact_state:
                        door_opened = contact_state != "OK"
                elif wiring_type in ("NO_EOL", "ONE_EOL"):
                    # NO_EOL/ONE_EOL: contactState is directly in wiringSchemeSpecificDetails
                    contact_state = wiring_details.get("contactState")
                    if contact_state:
                        door_opened = contact_state != "OK"

                device.attributes["door_opened"] = door_opened

            # Sensitivity (GlassProtect, MotionProtect, etc.)
            if "sensitivity" in device_data:
                device.attributes["sensitivity"] = device_data.get("sensitivity")

            # Device color
            if "color" in device_data:
                device.attributes["color"] = device_data.get("color")

            # Siren specific attributes
            # Prefer v2sirenVolumeLevel (supports DISABLED), fallback to deprecated sirenVolumeLevel
            if "v2sirenVolumeLevel" in device_data:
                device.attributes["siren_volume_level"] = device_data.get("v2sirenVolumeLevel")
            elif "sirenVolumeLevel" in device_data:
                device.attributes["siren_volume_level"] = device_data.get("sirenVolumeLevel")
            if "beepVolumeLevel" in device_data:
                device.attributes["beep_volume_level"] = device_data.get("beepVolumeLevel")
            if "alarmDuration" in device_data:
                device.attributes["alarm_duration"] = device_data.get("alarmDuration")
            if "v2sirenIndicatorLightMode" in device_data:
                device.attributes["led_indication"] = device_data.get("v2sirenIndicatorLightMode")
            elif "blinkWhileArmed" in device_data:
                device.attributes["led_indication"] = device_data.get("blinkWhileArmed")
            # Siren beep/chime settings
            if "beepOnArmDisarm" in device_data:
                device.attributes["beep_on_arm_disarm"] = device_data.get("beepOnArmDisarm")
            if "beepOnDelay" in device_data:
                device.attributes["beep_on_delay"] = device_data.get("beepOnDelay")
            if "chimesEnabled" in device_data:
                device.attributes["chimes_enabled"] = device_data.get("chimesEnabled")
            if "buzzerState" in device_data:
                device.attributes["buzzer_state"] = device_data.get("buzzerState")
            # StreetSiren specific
            if "alertIfMoved" in device_data:
                device.attributes["alert_if_moved"] = device_data.get("alertIfMoved")
            if "externallyPowered" in device_data:
                device.attributes["externally_powered"] = device_data.get("externallyPowered")
            # Siren advanced settings
            if "postAlarmIndicationMode" in device_data:
                device.attributes["post_alarm_indication_mode"] = device_data.get("postAlarmIndicationMode")
            if "alarmRestrictionMode" in device_data:
                device.attributes["alarm_restriction_mode"] = device_data.get("alarmRestrictionMode")
            if "blinkWhileArmed" in device_data:
                device.attributes["blink_while_armed"] = device_data.get("blinkWhileArmed")

            # LED indicator mode (all devices)
            if "indicatorLightMode" in device_data:
                device.attributes["indicatorLightMode"] = device_data.get("indicatorLightMode")

            # FireProtect 2 specific attributes (smoke, CO, temperature, steam alarms)
            if "coAlarmEnable" in device_data:
                device.attributes["coAlarmEnable"] = device_data.get("coAlarmEnable")
            if "tempAlarmEnable" in device_data:
                device.attributes["tempAlarmEnable"] = device_data.get("tempAlarmEnable")
            if "tempDiffAlarmEnable" in device_data:
                device.attributes["tempDiffAlarmEnable"] = device_data.get("tempDiffAlarmEnable")
            if "smokeAlarm" in device_data:
                device.attributes["smokeAlarm"] = device_data.get("smokeAlarm")
            if "coAlarm" in device_data:
                device.attributes["coAlarm"] = device_data.get("coAlarm")
            if "steamAlarm" in device_data:
                device.attributes["steamAlarm"] = device_data.get("steamAlarm")
            if "tempAlarm" in device_data:
                device.attributes["tempAlarm"] = device_data.get("tempAlarm")
                # Create boolean for binary sensor (TEMP_ALARM_DETECTED vs TEMP_ALARM_NOT_DETECTED)
                device.attributes["temperatureAlarmDetected"] = device_data.get("tempAlarm") == "TEMP_ALARM_DETECTED"
            if "tempHighDiffAlarm" in device_data:
                device.attributes["tempHighDiffAlarm"] = device_data.get("tempHighDiffAlarm")
                # Create boolean for binary sensor
                device.attributes["highTemperatureDiffDetected"] = (
                    device_data.get("tempHighDiffAlarm") == "TEMP_HIGH_DIFF_ALARM_DETECTED"
                )

            # Alerts by sirens setting
            if "alertsBySirens" in device_data:
                device.attributes["alertsBySirens"] = device_data.get("alertsBySirens", False)

            # MotionCam specific attributes
            if "imageResolution" in device_data:
                device.attributes["imageResolution"] = device_data.get("imageResolution")
            if "photosPerAlarm" in device_data:
                device.attributes["photosPerAlarm"] = device_data.get("photosPerAlarm")

            # LifeQuality specific attributes (CO2, temperature, humidity sensors)
            if "actualCO2" in device_data:
                device.attributes["actualCO2"] = device_data.get("actualCO2")
            if "actualTemperature" in device_data:
                device.attributes["actualTemperature"] = device_data.get("actualTemperature")
            if "actualHumidity" in device_data:
                device.attributes["actualHumidity"] = device_data.get("actualHumidity")
            if "minComfortCO2" in device_data:
                device.attributes["minComfortCO2"] = device_data.get("minComfortCO2")
            if "maxComfortCO2" in device_data:
                device.attributes["maxComfortCO2"] = device_data.get("maxComfortCO2")
            if "minComfortTemperature" in device_data:
                device.attributes["minComfortTemperature"] = device_data.get("minComfortTemperature")
            if "maxComfortTemperature" in device_data:
                device.attributes["maxComfortTemperature"] = device_data.get("maxComfortTemperature")
            if "minComfortHumidity" in device_data:
                device.attributes["minComfortHumidity"] = device_data.get("minComfortHumidity")
            if "maxComfortHumidity" in device_data:
                device.attributes["maxComfortHumidity"] = device_data.get("maxComfortHumidity")
            if "calibrationState" in device_data:
                device.attributes["calibrationState"] = device_data.get("calibrationState")
            if "indication" in device_data:
                device.attributes["indication"] = device_data.get("indication")

            # WaterStop specific attributes (smart water valve)
            waterstop_attrs = [
                "valveState",
                "motorState",
                "tempProtectState",
                "extPower",
                "preventionEnable",
                "preventionDaysPeriod",
                "preventionExecuteHours",
                "preventionExecuteMinutes",
                "buttonCfg",
                "indicationMode",
            ]
            for attr in waterstop_attrs:
                if attr in device_data:
                    device.attributes[attr] = device_data.get(attr)

            # Socket/Relay/WallSwitch: Parse switchState to is_on (direct from enriched data)
            if "switchState" in device_data:
                switch_state = device_data["switchState"]
                # switchState is a list: [] = on, ["SWITCHED_OFF"] = off
                # Device is OFF only if SWITCHED_OFF is explicitly in the list
                if isinstance(switch_state, list):
                    device.attributes["is_on"] = "SWITCHED_OFF" not in switch_state
                else:
                    device.attributes["is_on"] = True

            # SocketOutlet (Type E/F): Parse socketState to is_on
            # socketState is a list: ["FIRST_CHANNEL_ON"] = on, [] or ["FIRST_CHANNEL_OFF"] = off
            if "socketState" in device_data:
                socket_state = device_data["socketState"]
                if isinstance(socket_state, list):
                    device.attributes["is_on"] = "FIRST_CHANNEL_ON" in socket_state
                else:
                    device.attributes["is_on"] = False

            # Socket specific attributes (power monitoring, protection settings)
            if "indicationEnabled" in device_data:
                device.attributes["indicationEnabled"] = device_data.get("indicationEnabled")
            if "indicationBrightness" in device_data:
                device.attributes["indicationBrightness"] = device_data.get("indicationBrightness")
            if "currentProtectionEnabled" in device_data:
                device.attributes["currentProtectionEnabled"] = device_data.get("currentProtectionEnabled")
            if "voltageProtectionEnabled" in device_data:
                device.attributes["voltageProtectionEnabled"] = device_data.get("voltageProtectionEnabled")
            if "contactNormalState" in device_data:
                device.attributes["contactNormalState"] = device_data.get("contactNormalState")
            if "lockupRelayMode" in device_data:
                device.attributes["lockupRelayMode"] = device_data.get("lockupRelayMode")
            if "lockupRelayTimeSeconds" in device_data:
                device.attributes["lockupRelayTimeSeconds"] = device_data.get("lockupRelayTimeSeconds")
            # Power monitoring values
            # powerConsumedWattsPerHour = energy consumed (for Socket without Outlet suffix)
            if "powerConsumedWattsPerHour" in device_data:
                device.attributes["energy"] = device_data.get("powerConsumedWattsPerHour", 0) / 1000.0  # Wh -> kWh
            # powerConsumptionWatts = instantaneous power (for SocketOutlet)
            if "powerConsumptionWatts" in device_data:
                device.attributes["power"] = device_data.get("powerConsumptionWatts")
            # currentMilliAmpers (with 's') for Socket
            if "currentMilliAmpers" in device_data:
                ma = device_data.get("currentMilliAmpers", 0)
                device.attributes["current"] = ma / 1000.0 if ma else 0
            # currentMilliAmpere (without 's') for SocketOutlet
            if "currentMilliAmpere" in device_data:
                ma = device_data.get("currentMilliAmpere", 0)
                device.attributes["current"] = ma / 1000.0 if ma else 0
            if "voltageVolts" in device_data:
                device.attributes["voltage"] = device_data.get("voltageVolts")
            # Current threshold (SocketOutlet)
            if "currentThresholdAmpere" in device_data:
                device.attributes["current_threshold"] = device_data.get("currentThresholdAmpere")
            # Indication settings (SocketOutlet)
            if "indicationMode" in device_data:
                device.attributes["indicationMode"] = device_data.get("indicationMode")
                # indicationEnabled derived from indicationMode
                device.attributes["indicationEnabled"] = device_data.get("indicationMode") == "ENABLED"
            if "indicationBrightnessV2" in device_data:
                device.attributes["indicationBrightness"] = device_data.get("indicationBrightnessV2")

            # Button specific attributes (panic button, keyfob)
            if "buttonMode" in device_data:
                device.attributes["button_mode"] = device_data.get("buttonMode")
            if "brightness" in device_data:
                device.attributes["brightness"] = device_data.get("brightness")
            if "falsePressFilter" in device_data:
                device.attributes["false_press_filter"] = device_data.get("falsePressFilter")
            if "customAlarmType" in device_data:
                device.attributes["custom_alarm_type"] = device_data.get("customAlarmType")
            if "associatedUserId" in device_data:
                device.attributes["associated_user_id"] = device_data.get("associatedUserId")

            # LightSwitch multi-gang: Parse channelStatuses and button names
            # These are at root level of device_data, not inside "attributes"
            if "channelStatuses" in device_data:
                channel_statuses = device_data.get("channelStatuses", [])
                _LOGGER.debug(
                    "LightSwitch %s channelStatuses from API: %s",
                    device.name,
                    channel_statuses,
                )
                # Check if device has pending optimistic update (don't overwrite for 15 seconds)
                optimistic_until = device.attributes.get("_optimistic_until", 0)
                if time.time() < optimistic_until:
                    _LOGGER.debug(
                        "Skipping channelStatuses update for %s (optimistic update pending)",
                        device.name,
                    )
                else:
                    device.attributes["channelStatuses"] = channel_statuses  # Raw list
                    device.attributes["channel_1_on"] = "CHANNEL_1_ON" in channel_statuses
                    device.attributes["channel_2_on"] = "CHANNEL_2_ON" in channel_statuses

            # LightSwitchDimmer: Parse brightness attributes (at root level)
            if "actualBrightnessCh1" in device_data:
                device.attributes["actualBrightnessCh1"] = device_data.get("actualBrightnessCh1")
            if "minBrightnessLimitCh1" in device_data:
                device.attributes["minBrightnessLimitCh1"] = device_data.get("minBrightnessLimitCh1")
            if "maxBrightnessLimitCh1" in device_data:
                device.attributes["maxBrightnessLimitCh1"] = device_data.get("maxBrightnessLimitCh1")
            if "armActionBrightnessCh1" in device_data:
                device.attributes["armActionBrightnessCh1"] = device_data.get("armActionBrightnessCh1")
            if "disarmActionBrightnessCh1" in device_data:
                device.attributes["disarmActionBrightnessCh1"] = device_data.get("disarmActionBrightnessCh1")
            if "brightnessChangeSpeed" in device_data:
                device.attributes["brightnessChangeSpeed"] = device_data.get("brightnessChangeSpeed")

            # LightSwitch settings and protection statuses
            if "settingsSwitch" in device_data:
                device.attributes["settingsSwitch"] = device_data.get("settingsSwitch", [])
            if "protectStatuses" in device_data:
                device.attributes["protectStatuses"] = device_data.get("protectStatuses", [])
            if "touchSensitivity" in device_data:
                device.attributes["touchSensitivity"] = device_data.get("touchSensitivity")
            if "touchMode" in device_data:
                device.attributes["touchMode"] = device_data.get("touchMode")
            if "dimmerSettings" in device_data:
                device.attributes["dimmerSettings"] = device_data.get("dimmerSettings", {})
            if "dataChannelSignalQuality" in device_data:
                device.attributes["dataChannelSignalQuality"] = device_data.get("dataChannelSignalQuality")
            if "dataChannelOk" in device_data:
                device.attributes["dataChannelOk"] = device_data.get("dataChannelOk")
            if "panelColor" in device_data:
                device.attributes["panelColor"] = device_data.get("panelColor")

            if "buttonOne" in device_data or "buttonTwo" in device_data:
                button_one = device_data.get("buttonOne")
                button_two = device_data.get("buttonTwo")
                _LOGGER.debug(
                    "LightSwitch %s (%s): buttonOne=%s (type=%s), buttonTwo=%s (type=%s)",
                    device.name,
                    device.raw_type,
                    button_one,
                    type(button_one).__name__,
                    button_two,
                    type(button_two).__name__,
                )
                # Handle both formats: string directly ("Switch 1") or object {"buttonName": "..."}
                if button_one:
                    if isinstance(button_one, str):
                        device.attributes["channel_1_name"] = button_one
                    elif isinstance(button_one, dict):
                        device.attributes["channel_1_name"] = button_one.get("buttonName", "Channel 1")
                    else:
                        device.attributes["channel_1_name"] = "Channel 1"
                if button_two:
                    if isinstance(button_two, str):
                        device.attributes["channel_2_name"] = button_two
                    elif isinstance(button_two, dict):
                        device.attributes["channel_2_name"] = button_two.get("buttonName", "Channel 2")
                    else:
                        device.attributes["channel_2_name"] = "Channel 2"
                # Multi-gang only if BOTH buttons exist (LightSwitchTwoGang, LightSwitchTwoChannelTwoWay)
                # LightSwitchTwoWay has only buttonOne (single channel with two-way control)
                device.attributes["is_multi_gang"] = bool(button_one and button_two)
                # Track which channels exist for proper switch creation
                device.attributes["has_channel_1"] = bool(button_one)
                device.attributes["has_channel_2"] = bool(button_two)

            # Update device metadata (API uses "color" not "device_color")
            device.device_color = device_data.get("color")
            device.device_label = device_data.get("device_label")
            device.device_marketing_id = device_data.get("device_marketing_id")

            # Update device attributes dict
            if "attributes" in device_data:
                # Normalize API attributes to internal format
                normalized_attrs = self._normalize_device_attributes(device_data["attributes"], device.type)
                device.attributes.update(normalized_attrs)
            # Update room association
            if device.room_id and device.room_id in space.rooms:
                room = space.rooms[device.room_id]
                if device_id not in room.device_ids:
                    room.device_ids.append(device_id)

        # Log summary of devices loaded
        if new_devices_count > 0:
            _LOGGER.info("Discovered %d new device(s) in space %s", new_devices_count, space_id)

        # Clean up devices that no longer exist in Ajax
        # Safety: only remove if API returned at least some devices (avoid wiping on empty response)
        if processed_ids and space.devices:
            existing_device_ids = set(space.devices.keys())
            removed_device_ids = existing_device_ids - processed_ids

            if removed_device_ids:
                device_registry = dr.async_get(self.hass)
                for device_id in removed_device_ids:
                    device = space.devices.get(device_id)
                    device_name = device.name if device else device_id

                    # Remove from HA device registry
                    ha_device = device_registry.async_get_device(identifiers={(DOMAIN, device_id)})
                    if ha_device:
                        device_registry.async_remove_device(ha_device.id)
                        _LOGGER.info(
                            "Auto-removed device '%s' (ID: %s) from Home Assistant - deleted from Ajax",
                            device_name,
                            device_id,
                        )
                    else:
                        _LOGGER.debug(
                            "Device '%s' (ID: %s) not found in HA registry, removing from internal tracking only",
                            device_name,
                            device_id,
                        )

                    # Remove from internal tracking
                    del space.devices[device_id]

                _LOGGER.info(
                    "Cleaned up %d removed device(s) from space %s",
                    len(removed_device_ids),
                    space_id,
                )

    def _normalize_device_attributes(self, api_attributes: dict[str, Any], device_type: DeviceType) -> dict[str, Any]:
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
            if "door_opened" not in normalized and "externalContactState" in api_attributes:
                ext_state = api_attributes["externalContactState"]
                # externalContactState: "OK" = closed, "TRIGGERED" = open
                door_opened = ext_state != "OK"

                # Check wiringSchemeSpecificDetails for more accurate state
                wiring_details = api_attributes.get("wiringSchemeSpecificDetails", {})
                wiring_type = wiring_details.get("wiringSchemeType")

                # Store wiring type for handler to know if tamper sensor is available
                if wiring_type:
                    normalized["wiring_type"] = wiring_type

                if wiring_type == "TWO_EOL":
                    # TWO_EOL: contactTwoDetails is the door contact, contactOneDetails is tamper
                    contact_one = wiring_details.get("contactOneDetails", {})
                    contact_two = wiring_details.get("contactTwoDetails", {})
                    contact_state = contact_two.get("contactState")
                    if contact_state:
                        door_opened = contact_state != "OK"
                    # Parse tamper from contactOneDetails
                    tamper_state = contact_one.get("contactState")
                    if tamper_state:
                        normalized["tampered"] = tamper_state != "OK"
                elif wiring_type in ("NO_EOL", "ONE_EOL"):
                    # NO_EOL/ONE_EOL: contactState is directly in wiringSchemeSpecificDetails
                    contact_state = wiring_details.get("contactState")
                    if contact_state:
                        door_opened = contact_state != "OK"

                normalized["door_opened"] = door_opened

            # External contact: Support both formats
            if "external_contact_opened" not in api_attributes and "extraContactClosed" in api_attributes:
                # Convert extraContactClosed to external_contact_opened
                # extraContactClosed=False means contact is open (alarm state)
                normalized["external_contact_opened"] = not api_attributes["extraContactClosed"]

        # Motion detectors: Support both camelCase and snake_case
        if device_type == DeviceType.MOTION_DETECTOR:
            if "motion_detected" not in api_attributes and "motionDetected" in api_attributes:
                normalized["motion_detected"] = api_attributes["motionDetected"]
            if "motion_detected_at" not in api_attributes and "motionDetectedAt" in api_attributes:
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

        # Manual Call Point (MCP): Parse button state and attributes
        if device_type == DeviceType.MANUAL_CALL_POINT:
            # switchState for MCP is a string: "BUTTON_UNPRESSED" or "BUTTON_PRESSED"
            if "switchState" in api_attributes:
                normalized["switchState"] = api_attributes["switchState"]
            # customEvent indicates the alarm type (e.g., "FIRE_ALARM")
            if "customEvent" in api_attributes:
                normalized["customEvent"] = api_attributes["customEvent"]
            # Device color (RED, BLUE, etc.)
            if "color" in api_attributes:
                normalized["color"] = api_attributes["color"]
            # Self-monitoring config
            if "selfMonitoringConfig" in api_attributes:
                normalized["selfMonitoringConfig"] = api_attributes["selfMonitoringConfig"]

        # Note: LightSwitch multi-gang (channelStatuses, buttonOne, buttonTwo)
        # is parsed directly from device_data in _update_devices since these
        # fields are at root level, not inside "attributes"

        return normalized

    def _reset_expired_motion_detections(self, space: AjaxSpace) -> None:
        """Reset motion_detected to False for motion detectors if no recent detection.

        Motion detection events are impulse-based (not persistent state),
        so we reset them after 30 seconds of no new detection.

        Args:
            space: The AjaxSpace to process
        """
        now = datetime.now(UTC)
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
                    last_detected = last_detected.replace(tzinfo=UTC)

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
        if self.account is None:
            return
        space = self.account.spaces.get(space_id)
        if not space:
            return

        try:
            # TODO: Fetch notifications from API when endpoint is available
            # For now, notifications come from SQS real-time events
            # notifications_data = await self.api.async_find_notifications(space_id, limit)
            notifications_data: list[dict[str, Any]] = []

            # Clear existing notifications and add new ones
            space.notifications.clear()

            for notif_data in notifications_data:
                # Parse notification data - AjaxNotification already imported at module level

                # Determine notification type based on event_type
                event_type = notif_data.get("event_type", "")
                notif_type = self._parse_notification_type(event_type)

                # Create notification object
                notification = AjaxNotification(
                    id=notif_data.get("id", ""),
                    space_id=notif_data.get("space_id", space_id),
                    type=notif_type,
                    title=event_type.replace("_", " ").title() if event_type else "Event",
                    message=f"{notif_data.get('device_name', 'Device')}: {event_type.replace('_', ' ')}"
                    if event_type
                    else "",
                    timestamp=notif_data.get("timestamp") or datetime.now(UTC),
                    device_id=notif_data.get("device_id"),
                    device_name=notif_data.get("device_name"),
                    read=notif_data.get("read", False),
                    user_name=notif_data.get("user_name"),  # User who triggered the event
                )

                space.notifications.append(notification)

            # Update unread count
            space.unread_notifications = sum(1 for n in space.notifications if not n.read)

        except (AjaxRestApiError, AjaxRestAuthError, KeyError, ValueError) as err:
            _LOGGER.error("Error updating notifications for space %s: %s", space_id, err)

    async def _async_update_video_edges(self, space_id: str) -> None:
        """Update video edge devices (surveillance cameras) for a specific space."""
        if self.account is None:
            return
        space = self.account.spaces.get(space_id)
        if not space:
            return

        # Need real_space_id to fetch video edges
        if not space.real_space_id:
            _LOGGER.debug("No real_space_id for space %s, skipping video edges", space_id)
            return
        _LOGGER.debug("Fetching video edges for space %s with real_space_id %s", space_id, space.real_space_id)

        try:
            video_edges_data = await self.api.async_get_video_edges(space.real_space_id)
            _LOGGER.debug(
                "Found %d video edge(s) for space %s",
                len(video_edges_data),
                space_id,
            )

            processed_ve_ids: set[str] = set()

            for ve_data in video_edges_data:
                ve_id = ve_data.get("id")
                if not ve_id:
                    continue

                processed_ve_ids.add(ve_id)

                # Parse video edge type
                ve_type_str = ve_data.get("type", "UNKNOWN")
                try:
                    ve_type = VideoEdgeType(ve_type_str)
                except ValueError:
                    ve_type = VideoEdgeType.UNKNOWN

                # Get network info (networkInterface in API response)
                network = ve_data.get("networkInterface", {})
                ethernet = network.get("ethernet", {})
                wifi = network.get("wifi", {})

                # Get IP address from ethernet or wifi (v4.address in API)
                ip_address = None
                eth_config = ethernet.get("configuration", {}) if ethernet else {}
                wifi_config = wifi.get("configuration", {}) if wifi else {}
                if eth_config.get("v4", {}).get("address"):
                    ip_address = eth_config["v4"]["address"]
                elif wifi_config.get("v4", {}).get("address"):
                    ip_address = wifi_config["v4"]["address"]

                # Get MAC address
                mac_address = (ethernet.get("macAddress") if ethernet else None) or (
                    wifi.get("macAddress") if wifi else None
                )

                # Get firmware version (currentVersion in API)
                firmware = ve_data.get("firmware", {})
                firmware_version = firmware.get("currentVersion")

                # Get connection state (ONLINE/OFFLINE)
                connection_state = ve_data.get("connectionState", "UNKNOWN")

                # Get room info
                room_id = None
                room_name = None
                # Video edges might have channels with room assignments
                channels_raw = ve_data.get("channels", [])
                # Normalize channels to always be a list of dicts
                if isinstance(channels_raw, dict):
                    # Single channel returned as dict instead of list
                    channels = [channels_raw]
                elif isinstance(channels_raw, list):
                    # Filter to only include dict elements
                    channels = [c for c in channels_raw if isinstance(c, dict)]
                else:
                    channels = []

                if channels:
                    first_channel = channels[0]
                    space_settings = first_channel.get("spaceSettings", {})
                    room_id = space_settings.get("roomId")
                    if room_id and room_id in space.rooms:
                        room_name = space.rooms[room_id].name

                # Create or update video edge
                if ve_id not in space.video_edges:
                    video_edge = AjaxVideoEdge(
                        id=ve_id,
                        name=ve_data.get("name", f"Camera {ve_id[:6]}"),
                        space_id=space_id,
                        video_edge_type=ve_type,
                        color=ve_data.get("color"),
                        ip_address=ip_address,
                        mac_address=mac_address,
                        firmware_version=firmware_version,
                        connection_state=connection_state,
                        channels=channels,
                        room_id=room_id,
                        room_name=room_name,
                        raw_data=ve_data,
                    )
                    space.video_edges[ve_id] = video_edge
                    _LOGGER.info(
                        "Added video edge: %s (%s) - %s",
                        video_edge.name,
                        video_edge.video_edge_type.value,
                        connection_state,
                    )
                else:
                    # Update existing video edge
                    video_edge = space.video_edges[ve_id]
                    video_edge.name = ve_data.get("name", video_edge.name)
                    video_edge.video_edge_type = ve_type
                    video_edge.color = ve_data.get("color")
                    video_edge.ip_address = ip_address
                    video_edge.mac_address = mac_address
                    video_edge.firmware_version = firmware_version
                    video_edge.connection_state = connection_state
                    video_edge.channels = channels
                    video_edge.room_id = room_id
                    video_edge.room_name = room_name
                    video_edge.raw_data = ve_data

            # Clean up video edges that no longer exist in Ajax
            # Safety: only remove if API returned at least some devices (avoid wiping on empty response)
            if processed_ve_ids and space.video_edges:
                existing_ve_ids = set(space.video_edges.keys())
                removed_ve_ids = existing_ve_ids - processed_ve_ids

                if removed_ve_ids:
                    device_registry = dr.async_get(self.hass)
                    for ve_id in removed_ve_ids:
                        video_edge = space.video_edges.get(ve_id)
                        ve_name = video_edge.name if video_edge else ve_id

                        # Remove from HA device registry
                        ha_device = device_registry.async_get_device(identifiers={(DOMAIN, ve_id)})
                        if ha_device:
                            device_registry.async_remove_device(ha_device.id)
                            _LOGGER.info(
                                "Auto-removed video edge '%s' (ID: %s) from Home Assistant - deleted from Ajax",
                                ve_name,
                                ve_id,
                            )
                        else:
                            _LOGGER.debug(
                                "Video edge '%s' (ID: %s) not found in HA registry, removing from internal tracking only",
                                ve_name,
                                ve_id,
                            )

                        # Remove from internal tracking
                        del space.video_edges[ve_id]

                    _LOGGER.info(
                        "Cleaned up %d removed video edge(s) from space %s",
                        len(removed_ve_ids),
                        space_id,
                    )

        except Exception as err:
            _LOGGER.warning("Error updating video edges for space %s: %s", space_id, err)

    async def _async_update_smart_locks(self, space_id: str) -> None:
        """Update smart lock devices for a specific space."""
        if self.account is None:
            return
        space = self.account.spaces.get(space_id)
        if not space:
            return

        # Need real_space_id to fetch smart locks
        if not space.real_space_id:
            _LOGGER.debug("No real_space_id for space %s, skipping smart locks", space_id)
            return

        try:
            smart_locks_data = await self.api.async_get_smart_locks(space.real_space_id)
            _LOGGER.debug(
                "Found %d smart lock(s) for space %s",
                len(smart_locks_data),
                space_id,
            )

            processed_ids: set[str] = set()

            for sl_data in smart_locks_data:
                sl_id = sl_data.get("id")
                if not sl_id:
                    continue

                processed_ids.add(sl_id)

                if sl_id not in space.smart_locks:
                    smart_lock = AjaxSmartLock(
                        id=sl_id,
                        name=sl_data.get("name", f"Smart Lock {sl_id[:6]}"),
                        space_id=space_id,
                        raw_data=sl_data,
                    )
                    space.smart_locks[sl_id] = smart_lock
                    _LOGGER.info("Discovered smart lock: %s (%s)", smart_lock.name, sl_id)
                else:
                    # Update existing — preserve event-driven state
                    existing = space.smart_locks[sl_id]
                    existing.raw_data = sl_data
                    if sl_data.get("name"):
                        existing.name = sl_data["name"]

            # Clean up removed smart locks (only API-discovered ones)
            # SSE/SQS-discovered locks have no raw_data and must be preserved
            if processed_ids and space.smart_locks:
                existing_ids = set(space.smart_locks.keys())
                removed_ids = existing_ids - processed_ids

                if removed_ids:
                    device_registry = dr.async_get(self.hass)
                    for sl_id in removed_ids:
                        smart_lock = space.smart_locks.get(sl_id)

                        # Preserve SSE/SQS-discovered locks (no raw_data from API)
                        if smart_lock and not smart_lock.raw_data:
                            _LOGGER.debug(
                                "Preserving SSE/SQS-discovered smart lock '%s' (ID: %s)",
                                smart_lock.name,
                                sl_id,
                            )
                            continue

                        sl_name = smart_lock.name if smart_lock else sl_id

                        ha_device = device_registry.async_get_device(identifiers={(DOMAIN, sl_id)})
                        if ha_device:
                            device_registry.async_remove_device(ha_device.id)
                            _LOGGER.info(
                                "Auto-removed smart lock '%s' (ID: %s) from Home Assistant",
                                sl_name,
                                sl_id,
                            )

                        del space.smart_locks[sl_id]

        except Exception as err:
            _LOGGER.warning("Error updating smart locks for space %s: %s", space_id, err)

    def get_smart_lock(self, space_id: str, smart_lock_id: str) -> AjaxSmartLock | None:
        """Get a smart lock by space and smart lock ID."""
        space = self.get_space(space_id)
        return space.smart_locks.get(smart_lock_id) if space else None

    def _parse_notification_type(self, event_type: str | None) -> NotificationType:
        """Parse notification type from event type string."""
        # NotificationType already imported at module level
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
        elif any(keyword in event_lower for keyword in ["malfunction", "low", "tamper", "loss", "error", "fault"]):
            return NotificationType.WARNING
        elif any(keyword in event_lower for keyword in ["arm", "disarm", "motion", "door", "opened"]):
            return NotificationType.SECURITY_EVENT
        elif any(keyword in event_lower for keyword in ["update", "added", "changed", "test"]):
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
        event_data: dict[str, Any] = {
            "space_id": space.id,
            "space_name": space.name,
            "old_state": old_state.value,
            "new_state": new_state.value,
            "timestamp": datetime.now(UTC).isoformat(),
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

        # Get translated message
        from .event_codes import get_event_message

        ha_language = self.hass.config.language or "en"
        lang_map = {"fr": "fr", "es": "es", "en": "en"}
        language = lang_map.get(ha_language[:2], "en")
        message = get_event_message(action, language)

        # Create event
        # Note: source_name/user_name not included because REST API
        # doesn't tell us WHO triggered the action
        event = {
            "action": action,
            "message": message,
            "hub_id": space.hub_id or space.id,
            "space_id": space.id,
            "timestamp": datetime.now(UTC).isoformat(),
            "event_time": datetime.now(UTC),
        }

        # Store in recent_events (keep last 10)
        space.recent_events.insert(0, event)
        space.recent_events = space.recent_events[:10]

        _LOGGER.debug("Event stored: %s (total: %d)", action, len(space.recent_events))

        # Fire Home Assistant event for automations
        self._fire_security_state_event(space, old_state, new_state)

    async def _create_sqs_notification(self, action: str, source_name: str, space_name: str) -> None:
        """Create a persistent notification in HA for SQS events."""
        from homeassistant.components.persistent_notification import async_create

        # Check notification filter settings
        options = self.config_entry.options if self.config_entry else {}

        # Check if persistent notifications are enabled
        if not options.get(CONF_PERSISTENT_NOTIFICATION, True):
            return

        notification_filter = options.get(CONF_NOTIFICATION_FILTER, NOTIFICATION_FILTER_ALL)

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
            # Check for NIGHT_MODE_ON specifically, not just "NIGHT"
            # because ARMED_NIGHT_MODE_OFF contains "NIGHT" but is actually ARMED
            # Also handle exact "NIGHT_MODE" state (without "_ON" suffix)
            elif "NIGHT_MODE_ON" in state_str or state_str == "NIGHT_MODE":
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
            "motioncamfibra": DeviceType.MOTION_DETECTOR,
            "motion_cam_fibra": DeviceType.MOTION_DETECTOR,
            "motioncamsphod": DeviceType.MOTION_DETECTOR,
            "motion_cam_s_phod": DeviceType.MOTION_DETECTOR,
            "curtainoutdoorjeweller": DeviceType.MOTION_DETECTOR,
            "curtain_outdoor_jeweller": DeviceType.MOTION_DETECTOR,
            # Combi detectors (motion + glass break)
            "combi_protect": DeviceType.COMBI_PROTECT,
            "combiprotect": DeviceType.COMBI_PROTECT,
            "combi": DeviceType.COMBI_PROTECT,
            # Door/Window contacts
            "door_protect": DeviceType.DOOR_CONTACT,
            "doorprotect": DeviceType.DOOR_CONTACT,
            "doorprotectplus": DeviceType.DOOR_CONTACT,
            "door_protect_plus": DeviceType.DOOR_CONTACT,
            "doorprotectplusfibra": DeviceType.DOOR_CONTACT,
            "door_protect_plus_fibra": DeviceType.DOOR_CONTACT,
            "doorprotectsplus": DeviceType.DOOR_CONTACT,
            "door_protect_s_plus": DeviceType.DOOR_CONTACT,
            "door": DeviceType.DOOR_CONTACT,
            "window": DeviceType.DOOR_CONTACT,
            "opening": DeviceType.DOOR_CONTACT,
            "magnet": DeviceType.DOOR_CONTACT,
            # Glass break
            "glass_protect": DeviceType.GLASS_BREAK,
            "glassprotect": DeviceType.GLASS_BREAK,
            "glassprotects": DeviceType.GLASS_BREAK,
            "glass_protect_s": DeviceType.GLASS_BREAK,
            "glass": DeviceType.GLASS_BREAK,
            # Smoke detectors
            "fire_protect": DeviceType.SMOKE_DETECTOR,
            "fireprotect": DeviceType.SMOKE_DETECTOR,
            "fireprotectplus": DeviceType.SMOKE_DETECTOR,
            "fire_protect_plus": DeviceType.SMOKE_DETECTOR,
            "fireprotect2plus": DeviceType.SMOKE_DETECTOR,
            "fire_protect_2_plus": DeviceType.SMOKE_DETECTOR,
            "fireprotect2": DeviceType.SMOKE_DETECTOR,
            "fire_protect_2": DeviceType.SMOKE_DETECTOR,
            "fire_protect_2_base": DeviceType.SMOKE_DETECTOR,
            "fireprotect2base": DeviceType.SMOKE_DETECTOR,
            "smoke": DeviceType.SMOKE_DETECTOR,
            "fire": DeviceType.SMOKE_DETECTOR,
            # Manual Call Point (fire alarm button)
            "switchbasemcpfire": DeviceType.MANUAL_CALL_POINT,
            "switch_base_mcp_fire": DeviceType.MANUAL_CALL_POINT,
            "manualcallpoint": DeviceType.MANUAL_CALL_POINT,
            "manual_call_point": DeviceType.MANUAL_CALL_POINT,
            "mcp": DeviceType.MANUAL_CALL_POINT,
            # Flood detectors
            "leak_protect": DeviceType.FLOOD_DETECTOR,
            "leakprotect": DeviceType.FLOOD_DETECTOR,
            "leaksprotect": DeviceType.FLOOD_DETECTOR,
            "leaks_protect": DeviceType.FLOOD_DETECTOR,
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
            "keypadfibra": DeviceType.KEYPAD,
            "keypad_fibra": DeviceType.KEYPAD,
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
            "streetsirensdoubledeck": DeviceType.SIREN,
            "street_siren_s_double_deck": DeviceType.SIREN,
            "homesirens": DeviceType.SIREN,
            "home_siren_s": DeviceType.SIREN,
            "streetsirefibra": DeviceType.SIREN,
            "street_siren_fibra": DeviceType.SIREN,
            "homesirefibra": DeviceType.SIREN,
            "home_siren_fibra": DeviceType.SIREN,
            # SpeakerPhone
            "speakerphone": DeviceType.SPEAKERPHONE,
            # Doorbell
            "doorbell": DeviceType.DOORBELL,
            "doorbellbutton": DeviceType.DOORBELL,
            "doorbell_button": DeviceType.DOORBELL,
            "motioncamvideodoorbell": DeviceType.DOORBELL,
            "motion_cam_video_doorbell": DeviceType.DOORBELL,
            # Transmitter
            "transmitter": DeviceType.TRANSMITTER,
            "transmitterfibra": DeviceType.TRANSMITTER,
            "transmitter_fibra": DeviceType.TRANSMITTER,
            "transmitterfibratwochannels": DeviceType.TRANSMITTER,
            "transmitter_fibra_two_channels": DeviceType.TRANSMITTER,
            "integration": DeviceType.TRANSMITTER,
            # MultiTransmitter (wired sensors hub)
            "multitransmitter": DeviceType.MULTI_TRANSMITTER,
            "multi_transmitter": DeviceType.MULTI_TRANSMITTER,
            "multitransmitterfibra": DeviceType.MULTI_TRANSMITTER,
            "multitransmitter_fibra": DeviceType.MULTI_TRANSMITTER,
            # MultiTransmitter wired inputs (treat as door contacts)
            "multitransmitterwireinput": DeviceType.WIRE_INPUT,
            "multitransmitter_wire_input": DeviceType.WIRE_INPUT,
            "multitransmitterfibrawireinput": DeviceType.WIRE_INPUT,
            "multitransmitter_fibra_wire_input": DeviceType.WIRE_INPUT,
            "multitransmitterwireinputrs": DeviceType.WIRE_INPUT,
            "multitransmitter_wire_input_rs": DeviceType.WIRE_INPUT,
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
            "socketoutlet": DeviceType.SOCKET,
            "socket_outlet": DeviceType.SOCKET,
            "socketoutlettypee": DeviceType.SOCKET,
            "socket_outlet_type_e": DeviceType.SOCKET,
            "socketoutlettypef": DeviceType.SOCKET,
            "socket_outlet_type_f": DeviceType.SOCKET,
            "relay": DeviceType.RELAY,
            "wallswitch": DeviceType.WALLSWITCH,
            "lightswitch": DeviceType.WALLSWITCH,
            "lightswitchonegang": DeviceType.WALLSWITCH,
            "lightswitchtwogang": DeviceType.WALLSWITCH,
            "lightswitchtwochanneltwoway": DeviceType.WALLSWITCH,
            "light_switch_two_channel_two_way": DeviceType.WALLSWITCH,
            "lightswitchdimmer": DeviceType.WALLSWITCH,
            "light_switch_dimmer": DeviceType.WALLSWITCH,
            "thermostat": DeviceType.THERMOSTAT,
            "life_quality": DeviceType.LIFE_QUALITY,
            "lifequality": DeviceType.LIFE_QUALITY,
            "waterstop": DeviceType.WATERSTOP,
            "water_stop": DeviceType.WATERSTOP,
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
        self._pending_ha_actions[hub_id] = time.time()

    def has_pending_ha_action(self, hub_id: str) -> bool:
        """Check if Home Assistant triggered an action on this hub recently.

        Returns True if HA action was within the last 10 seconds.
        Does NOT consume the pending action (can be called multiple times).
        """
        timestamp = self._pending_ha_actions.get(hub_id, 0)
        return time.time() - timestamp < 10

    def get_pending_ha_action(self, hub_id: str) -> bool:
        """Check and consume pending HA action.

        Returns True if HA action was within the last 10 seconds.
        Clears the pending action after returning True.
        """
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
            _LOGGER.error("Failed to activate night mode for space %s: %s", space_id, err)
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

    async def async_arm_group(self, space_id: str, group_id: str, force: bool = True) -> None:
        """Arm a specific group.

        Args:
            space_id: The space ID (hub_id)
            group_id: The group ID to arm
            force: If True, ignore problems and force arm
        """
        _LOGGER.info("Arming group %s in space %s (force=%s)", group_id, space_id, force)

        try:
            self._register_ha_action(space_id)
            await self.api.async_arm_group(space_id, group_id, ignore_problems=force)
            # Update local state
            space = self.get_space(space_id)
            if space and group_id in space.groups:
                space.groups[group_id].state = GroupState.ARMED
            # Trigger refresh to sync state
            await self.async_request_refresh()

        except AjaxRestApiError as err:
            _LOGGER.error("Failed to arm group %s in space %s: %s", group_id, space_id, err)
            raise

    async def async_disarm_group(self, space_id: str, group_id: str) -> None:
        """Disarm a specific group.

        Args:
            space_id: The space ID (hub_id)
            group_id: The group ID to disarm
        """
        _LOGGER.info("Disarming group %s in space %s", group_id, space_id)

        try:
            self._register_ha_action(space_id)
            await self.api.async_disarm_group(space_id, group_id)
            # Update local state
            space = self.get_space(space_id)
            if space and group_id in space.groups:
                space.groups[group_id].state = GroupState.DISARMED
            # Trigger refresh to sync state
            await self.async_request_refresh()

        except AjaxRestApiError as err:
            _LOGGER.error("Failed to disarm group %s in space %s: %s", group_id, space_id, err)
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

    def get_group(self, space_id: str, group_id: str) -> AjaxGroup | None:
        """Get a group by space and group ID."""
        space = self.get_space(space_id)
        return space.groups.get(group_id) if space else None

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

        # Stop ONVIF Manager (local AI detections)
        if self.onvif_manager:
            try:
                _LOGGER.debug("Stopping ONVIF Manager...")
                await self.onvif_manager.async_stop()
            except Exception as err:
                _LOGGER.error("Error stopping ONVIF Manager: %s", err)

        # Stop all fast poll tasks
        for task in self._fast_poll_tasks.values():
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        self._fast_poll_tasks.clear()

        # Stop door sensor polling task
        if self._door_sensor_poll_task is not None:
            self._door_sensor_poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._door_sensor_poll_task
            self._door_sensor_poll_task = None

        # Close API connection
        await self.api.close()
