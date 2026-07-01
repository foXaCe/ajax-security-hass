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
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ._coordinator_arm import AjaxArmServiceMixin
from ._coordinator_devices import AjaxDevicesMixin
from ._coordinator_door_poll import AjaxDoorPollingMixin
from ._coordinator_events import AjaxEventDispatchMixin
from ._coordinator_init import SMART_LOCK_STORE_VERSION, AjaxBootstrapMixin
from ._coordinator_onvif import AjaxOnvifMixin
from ._coordinator_spaces import AjaxSpacesMixin
from ._coordinator_state import AjaxStateUpdaterMixin
from .api import AjaxRestApi, AjaxRestApiError, AjaxRestAuthError
from .const import (
    DOMAIN,
    METADATA_REFRESH_INTERVAL,
    UPDATE_INTERVAL,
    UPDATE_INTERVAL_ARMED,
    AjaxConfigEntry,
)
from .models import (
    AjaxAccount,
    AjaxDevice,
    AjaxGroup,
    AjaxRoom,
    AjaxSpace,
    SecurityState,
)

# Type imports for optional modules (for type checking only)
if TYPE_CHECKING:
    from .onvif_manager import AjaxOnvifManager
    from .sqs_manager import SQSManager
    from .sse_manager import SSEManager

# The optional-import guards (SQS_AVAILABLE / SSE_AVAILABLE / ONVIF_AVAILABLE)
# live in their respective coordinator mixin modules now — the coordinator
# itself only needs the typing-only imports above.

_LOGGER = logging.getLogger(__name__)


class AjaxDataCoordinator(
    AjaxArmServiceMixin,
    AjaxBootstrapMixin,
    AjaxDevicesMixin,
    AjaxDoorPollingMixin,
    AjaxEventDispatchMixin,
    AjaxOnvifMixin,
    AjaxSpacesMixin,
    AjaxStateUpdaterMixin,
    DataUpdateCoordinator[AjaxAccount],
):
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
        entry: AjaxConfigEntry,
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
            entry: ConfigEntry this coordinator is bound to
            api: Ajax REST API instance
            aws_access_key_id: AWS access key ID (optional, for SQS in direct mode)
            aws_secret_access_key: AWS secret access key (optional, for SQS in direct mode)
            queue_name: SQS queue name (optional, for SQS in direct mode)
            sse_url: SSE endpoint URL (optional, for proxy mode)
            enabled_spaces: List of space IDs to enable (None = all spaces)
        """
        self.api = api
        self.config_entry = entry
        self.account: AjaxAccount | None = None
        self._enabled_spaces: list[str] | None = enabled_spaces
        self.all_discovered_spaces: dict[str, str] = {}  # space_id -> name (for options flow)
        # Cached space_binding responses to avoid repeated per-tick API calls.
        self._space_binding_cache: dict[str, dict[str, Any]] = {}  # hub_id -> space_binding
        self._door_sensor_poll_task: asyncio.Task[Any] | None = (
            None  # Continuous door sensor polling when disarmed or in night mode
        )
        self._door_sensor_poll_security_state: SecurityState = SecurityState.DISARMED
        self._initial_load_done: bool = False  # Track if initial data load is complete
        self._force_metadata_refresh: bool = False  # Flag to force full metadata refresh
        self._pending_ha_actions: dict[str, float] = {}  # hub_id -> timestamp of HA action
        # Per-space lock so concurrent arm/disarm calls cannot reach the API
        # out-of-order. Without this, two automations firing arm() then
        # disarm() within ms can land in the wrong order on Ajax's side.
        self._arm_locks: dict[str, asyncio.Lock] = {}
        # Cycle counter for adaptive polling — when SSE/SQS is delivering
        # real-time events, video_edges and smart_locks state is already
        # event-driven, so we only need a periodic REST sync (every Nth tick)
        # rather than every cycle. Keeps the proxy load low for many users.
        self._cycle_counter: int = 0
        self._realtime_skip_factor: int = 3

        # Lightweight diagnostics counters: each handler increments the
        # matching key, diagnostics.py reads the whole dict. Plain ints so
        # no lock is needed (single-threaded asyncio).
        self.stats: dict[str, int] = {
            "events_sse_received": 0,
            "events_sqs_received": 0,
            "events_onvif_received": 0,
            "auth_errors": 0,
            "discovery_refreshes": 0,
        }

        # SQS real-time events (optional, for direct mode)
        self.sqs_manager: SQSManager | None = None
        self._aws_access_key_id = aws_access_key_id
        self._aws_secret_access_key = aws_secret_access_key
        self._queue_name = queue_name
        self._sqs_initialized = False
        self._sqs_init_in_progress = False

        # SSE real-time events (optional, for proxy mode)
        self.sse_manager: SSEManager | None = None
        self._sse_url = sse_url or (api.sse_url if hasattr(api, "sse_url") else None)
        self._sse_initialized = False

        # ONVIF local AI detections (optional, for video edge cameras)
        self.onvif_manager: AjaxOnvifManager | None = None
        self._onvif_initialized = False

        # Device details refresh optimization
        # Battery/signal don't change often, so refresh every 5 minutes instead of every poll
        self._last_device_details_refresh: float = 0
        self._device_details_refresh_interval: int = 300  # 5 minutes in seconds

        # Metadata refresh optimization (rooms, users, groups)
        # These don't change often, so refresh every hour instead of every poll
        self._last_metadata_refresh: float = 0

        # Door sensor fast polling option (disabled by default to reduce API calls)
        self._door_sensor_fast_poll_enabled: bool = False

        # Event entity registry: device_id -> AjaxEventEntity
        self._event_entities: dict[str, Any] = {}

        # Hub IDs for which a SSE/SQS handler is mid-refresh and has already
        # created the state-change event — keyed per hub so the REST poller
        # only suppresses its duplicate for the affected hub, not every hub
        # processed in the same tick.
        self._skipped_state_change_hubs: set[str] = set()

        # Flag to bypass proxy cache on next refresh (after SSE event or user action)
        self._bypass_cache_next_refresh: bool = False

        # Auth error resilience: tolerate transient auth failures before triggering reauth
        self._consecutive_auth_errors: int = 0
        self._max_auth_errors: int = 3  # Trigger reauth after 3 consecutive auth failures

        # Persistent storage for SSE/SQS-discovered smart locks (survives reboots).
        # Schema migration is handled manually in _async_load_smart_locks so we
        # stay compatible with Store[Any] implementations that predate migrate_func.
        self._smart_lock_store: Store[Any] = Store[Any](hass, SMART_LOCK_STORE_VERSION, f"{DOMAIN}_smart_locks")

        # Exposed as a plain str so entities can namespace their unique_id and
        # device identifiers per config entry (multi-account collision safety,
        # schema v1.3) without the Optional dance on self.config_entry.
        self.entry_id: str = entry.entry_id

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
            # Debouncer: Wait 0.5s of silence before triggering refresh
            # Groups rapid multi-zone events into fewer API calls (proxy load)
            # while keeping real-time updates fast enough
            request_refresh_debouncer=Debouncer(
                hass,
                _LOGGER,
                cooldown=0.5,
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
                self.api.bypass_cache_next()
                self._bypass_cache_next_refresh = False
                _LOGGER.debug("Bypassing proxy cache for this refresh")

            # Initialize account if needed
            if self.account is None:
                await self._async_init_account()

            # After init, account must exist
            if self.account is None:
                raise UpdateFailed("Account data not available after initialization")

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
                    await asyncio.gather(*tasks)

                # Restore SSE/SQS-discovered smart locks from storage
                await self._async_restore_smart_locks()

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
                if self.config_entry is not None:
                    entry = self.config_entry
                    if not self._sse_initialized and self._sse_url:
                        # Proxy mode: use SSE for real-time events
                        entry.async_create_background_task(self.hass, self._async_init_sse(), "ajax_init_sse")
                    elif not self._sqs_initialized and not self._sqs_init_in_progress and self._aws_access_key_id:
                        # Direct mode: use SQS for real-time events
                        entry.async_create_background_task(self.hass, self._async_init_sqs(), "ajax_init_sqs")

                    # Initialize ONVIF for local AI detections (works even when disarmed)
                    if not self._onvif_initialized:
                        entry.async_create_background_task(self.hass, self._async_init_onvif(), "ajax_init_onvif")
            else:
                # Periodic update - optimized polling
                # Retry real-time SQS if it never came up in direct mode — e.g.
                # aiobotocore was still being installed during initial load. This
                # self-heals without a manual restart once the dependency lands.
                if (
                    self.config_entry is not None
                    and self._aws_access_key_id
                    and not self._sse_url
                    and not self._sqs_initialized
                    and not self._sqs_init_in_progress
                ):
                    self.config_entry.async_create_background_task(self.hass, self._async_init_sqs(), "ajax_retry_sqs")

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

                self._cycle_counter += 1
                # When real-time events are flowing in, video edges and smart
                # locks are kept fresh by SSE/SQS. Throttle the REST sync so
                # we only poll their full state every Nth cycle (or always on
                # forced metadata refresh). Direct mode without realtime keeps
                # the previous behaviour.
                realtime_active = (self.sse_manager is not None) or (self.sqs_manager is not None)
                refresh_video_smart = (
                    need_metadata_refresh
                    or not realtime_active
                    or (self._cycle_counter % self._realtime_skip_factor == 0)
                )

                for space_id in self.account.spaces:
                    space_obj: AjaxSpace | None = self.account.spaces.get(space_id)
                    if space_obj:
                        self._reset_expired_motion_detections(space_obj)
                        await self._async_update_devices(space_id)
                        if refresh_video_smart:
                            # Refresh video edges to get AI detection states
                            if space_obj.video_edges:
                                await self._async_update_video_edges(space_id)
                            # Refresh smart locks (API data is minimal, state is event-driven)
                            if space_obj.smart_locks:
                                await self._async_update_smart_locks(space_id)

            # Reset auth error counter on success
            self._consecutive_auth_errors = 0
            return self.account

        except AjaxRestAuthError as err:
            self._consecutive_auth_errors += 1
            self.stats["auth_errors"] += 1
            if self._consecutive_auth_errors >= self._max_auth_errors:
                _LOGGER.error(
                    "Authentication failed %d times consecutively, triggering reauth: %s",
                    self._consecutive_auth_errors,
                    err,
                )
                raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
            _LOGGER.warning(
                "Authentication error (%d/%d), will retry next poll: %s",
                self._consecutive_auth_errors,
                self._max_auth_errors,
                err,
            )
            raise UpdateFailed(f"Transient auth error: {err}") from err
        except AjaxRestApiError as err:
            if self.last_update_success:
                _LOGGER.warning("Connection to Ajax API lost: %s", err)
            raise UpdateFailed(f"Error communicating with API: {err}") from err

    # Account / smart-lock store / SSE / SQS bootstrap live in
    # ``_coordinator_init.AjaxBootstrapMixin``.

    # ONVIF init + event handler + NVR routing live in
    # ``_coordinator_onvif.AjaxOnvifMixin``.

    # Per-tick spaces / rooms / users / groups reconciliation lives in
    # ``_coordinator_spaces.AjaxSpacesMixin``.

    # Device polling, attribute normalisation, stale-device cleanup and
    # motion-detection auto-reset live in
    # ``_coordinator_devices.AjaxDevicesMixin``.

    # Notification refresh, video edges, smart locks, and the parsers for
    # security state / device type / notification type live in
    # ``_coordinator_state.AjaxStateUpdaterMixin``.

    # ============================================================================
    # Control methods
    # ============================================================================

    # Arm / disarm / night-mode / panic / group actions live in
    # ``_coordinator_arm.AjaxArmServiceMixin`` to keep this file focused
    # on the polling-and-state-update pipeline.

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

        # Stop door sensor polling task
        if self._door_sensor_poll_task is not None:
            self._door_sensor_poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._door_sensor_poll_task
            self._door_sensor_poll_task = None

        # Close API connection
        await self.api.close()
