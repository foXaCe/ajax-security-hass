"""SSE Manager for Ajax real-time events via proxy.

This manager receives events from the SSE client (proxy mode) and processes them
in the same way as the SQS manager. The event format from the proxy should be
compatible with the SQS event format.

Architecture:
- SSE events are used for INSTANT state updates (< 1 second)
- REST API polling confirms state periodically (fallback)
- SSE events directly update coordinator state for fastest response
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    EVENT_AJAX_BUTTON_PRESSED,
    EVENT_AJAX_DOORBELL_RING,
    EVENT_AJAX_SCENARIO_TRIGGERED,
    EVENT_AJAX_SMART_LOCK_DOORBELL,
    SIGNAL_NEW_SMART_LOCK,
)
from .event_codes import DEFAULT_LANGUAGE, get_event_message, parse_event_code, resolve_event_language
from .event_maps import (  # Single source of truth for event mappings
    BUTTON_EVENTS,
    DEVICE_STATUS_EVENTS,
    DOOR_EVENTS,
    DOORBELL_EVENTS,
    EVENT_TAG_TO_STATE,
    FLOOD_EVENTS,
    FULL_ARM_EVENT_TAGS,
    GLASS_EVENTS,
    GROUP_ARM_EVENT_TAGS,
    HUB_EVENTS,
    LOCK_DOOR_EVENT_CODE_STATES,
    LOCK_DOOR_EVENTS,
    LOCK_EVENT_CODE_STATES,
    LOCK_EVENTS,
    MOTION_EVENTS,
    RELAY_EVENTS,
    SCENARIO_EVENTS,
    SECURITY_EVENT_ACTIONS,
    SMOKE_EVENTS,
    TAMPER_EVENTS,
    VIDEO_EVENT_TYPES,
    VIDEO_EVENTS,
    WIRE_INPUT_EVENTS,
)
from .models import AjaxDevice, AjaxSmartLock, AjaxSpace, SecurityState

if TYPE_CHECKING:
    from .coordinator import AjaxDataCoordinator
    from .sse_client import AjaxSSEClient

from ._event_helpers import EventHandlerMixin

_LOGGER = logging.getLogger(__name__)


class SSEManager(EventHandlerMixin):
    """Manages SSE events from Ajax proxy."""

    # Don't let REST overwrite SSE state for this many seconds
    STATE_PROTECTION_SECONDS = 5.0
    # Deduplication window in seconds (ignore duplicate events within this window)
    DEDUP_WINDOW_SECONDS = 5.0

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        sse_client: AjaxSSEClient,
    ):
        """Initialize SSE Manager.

        Args:
            coordinator: The Ajax data coordinator
            sse_client: SSE client instance
        """
        self.coordinator = coordinator
        self.sse_client = sse_client
        self._language = DEFAULT_LANGUAGE
        self._last_state_update: dict[str, float] = {}  # hub_id -> timestamp
        self._recent_events: dict[str, float] = {}  # event_key -> timestamp
        self._dedup_window = self.DEDUP_WINDOW_SECONDS
        # Track scheduled call_later handles so we can cancel them on stop()
        # and strong-ref background tasks so they are not GC'd mid-flight.
        self._pending_timers: set[asyncio.TimerHandle] = set()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        # Serialise security_event handlers so overlapping events cannot
        # flip the coordinator skip-flag in a racy way.
        self._security_event_lock = asyncio.Lock()

    def set_language(self, language: str) -> None:
        """Set language for event messages."""
        self._language = language

    async def start(self) -> bool:
        """Start receiving SSE events.

        Returns:
            True if started successfully
        """
        _LOGGER.info("Starting SSE Manager...")

        # Set up callback for received events
        self.sse_client._callback = self._handle_event

        # Start SSE client
        success = await self.sse_client.start()

        if success:
            _LOGGER.info("SSE Manager started successfully")
        else:
            _LOGGER.error("Failed to start SSE Manager")

        return success

    def _schedule_later(self, delay: float, callback: Callable[[], Any]) -> None:
        """Wrap hass.loop.call_later to track the handle for cancellation.

        The handle removes itself from ``_pending_timers`` once it fires, so the
        set only ever holds genuinely pending timers (otherwise every doorbell
        ring / video detection would leak a spent TimerHandle for the lifetime
        of the integration).
        """

        def _wrapped() -> None:
            self._pending_timers.discard(handle)
            callback()

        handle = self.coordinator.hass.loop.call_later(delay, _wrapped)
        self._pending_timers.add(handle)

    def _spawn_background(self, coro: Any) -> None:
        """Create a tracked background task so it cannot be GC'd mid-flight."""
        task = self.coordinator.hass.async_create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def stop(self) -> None:
        """Stop receiving SSE events."""
        _LOGGER.info("Stopping SSE Manager...")
        # Cancel all scheduled timers
        for handle in list(self._pending_timers):
            handle.cancel()
        self._pending_timers.clear()
        await self.sse_client.stop()
        # Let any spawned background tasks finish gracefully
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        _LOGGER.info("SSE Manager stopped")

    def is_state_protected(self, hub_id: str) -> bool:
        """Check if a hub's state was recently updated via SSE.

        This prevents REST polling from overwriting recent SSE updates.

        Args:
            hub_id: Hub ID to check

        Returns:
            True if state was updated via SSE in the last 5 seconds
        """
        last_update = self._last_state_update.get(hub_id, 0)
        return (time.time() - last_update) < self.STATE_PROTECTION_SECONDS

    async def _handle_event(self, event_data: dict[str, Any]) -> None:
        """Handle an SSE event.

        The proxy should send events in a format similar to SQS:
        {
            "event": {
                "eventTag": "Disarm",
                "eventCode": "M_22_00",
                "hubId": "002BB321",
                "timestamp": 1234567890,
                "source": {"name": "User Name", "type": "USER"},
                "device": {"id": "xxx", "name": "Device Name", "type": "DoorProtect"}
            }
        }

        Or a simplified format from the proxy:
        {
            "eventTag": "Disarm",
            "hubId": "002BB321",
            "sourceName": "User Name",
            ...
        }
        """
        try:
            self.coordinator.stats["events_sse_received"] += 1
            # Handle both nested and flat event formats
            event = event_data.get("event", event_data)

            event_tag = event.get("eventTag", "").lower()
            hub_id = event.get("hubId")

            if not event_tag or not hub_id:
                _LOGGER.debug("SSE event missing eventTag or hubId: %s", event_data)
                return

            # Extract event details
            event_code = event.get("eventCode", "")

            # Try multiple ways to get source info (different proxy formats)
            source = event.get("source", {})
            device = event.get("device", {})

            # Source name: try device.name, source.name, sourceObjectName, sourceName
            source_name = (
                device.get("name")
                if isinstance(device, dict) and device.get("name")
                else (source.get("name") if isinstance(source, dict) else None)
            )
            if not source_name:
                source_name = event.get("sourceObjectName") or event.get("sourceName", "")

            # Source ID: try device.id, sourceObjectId, deviceId
            source_id: str = str(
                device.get("id")
                if isinstance(device, dict) and device.get("id")
                else event.get("sourceObjectId") or event.get("deviceId") or ""
            )

            # Source type: try device.type, source.type, sourceObjectType, sourceType
            source_type = (
                device.get("type")
                if isinstance(device, dict) and device.get("type")
                else (source.get("type") if isinstance(source, dict) else None)
            )
            if not source_type:
                source_type = event.get("sourceObjectType") or event.get("sourceType", "")

            # Parse event code for type info
            code_info = parse_event_code(event_code)
            event_type = code_info.get("category", "unknown") if code_info else "unknown"
            transition = code_info.get("transition", "TRIGGERED") if code_info else "TRIGGERED"

            # Also check eventTypeV2 for video AI events
            event_type_v2 = event.get("eventTypeV2", "")

            # DEBUG, not INFO: source_name can be an Ajax user's display
            # name (PII) and this fires on every event.
            _LOGGER.debug(
                "SSE event: type=%s, tag=%s, code=%s, source=%s (%s), id=%s, transition=%s, typeV2=%s",
                event_type,
                event_tag,
                event_code,
                source_name,
                source_type,
                source_id,
                transition,
                event_type_v2 or "none",
            )

            # Log raw event data at DEBUG level for troubleshooting
            _LOGGER.debug("SSE raw event data: %s", event)

            # Deduplication: ignore duplicate events within window
            # For group events, include group ID to allow multiple zones from same user
            group_id = None
            if event_tag in GROUP_ARM_EVENT_TAGS:
                # Extract group ID from event data
                related_groups = event.get("additionalData", {}).get("relatedGroupsInfo", [])
                if related_groups:
                    group_id = related_groups[0].get("id")

            # Build deduplication key - include group_id for group events.
            # ``event_code`` is the precise per-event Ajax identifier; including
            # it gives parity with SQS dedup (which uses timestamp) so that two
            # back-to-back events of the same tag with different codes are
            # both processed instead of the second being silently dropped.
            if group_id:
                event_key = f"{source_id}:{event_tag}:{group_id}:{transition}:{event_code}"
            else:
                event_key = f"{source_id}:{event_tag}:{transition}:{event_code}"

            now = time.time()
            last_time = self._recent_events.get(event_key, 0)
            if now - last_time < self._dedup_window:
                _LOGGER.debug(
                    "SSE event ignored (duplicate): %s, last seen %.1fs ago",
                    event_key,
                    now - last_time,
                )
                return
            self._recent_events[event_key] = now

            # Cleanup old entries in-place (avoid dict reassignment)
            expired = [k for k, v in self._recent_events.items() if now - v >= 60]
            for k in expired:
                del self._recent_events[k]

            # Get space by hub_id
            space = None
            if self.coordinator.account is None:
                _LOGGER.warning("SSE: No account data available")
                return
            for s in self.coordinator.account.spaces.values():
                if s.hub_id == hub_id:
                    space = s
                    break

            if not space:
                _LOGGER.warning("SSE: Unknown hub %s", hub_id)
                return

            # Process event by type
            if event_tag in EVENT_TAG_TO_STATE:
                await self._handle_security_event(space, event_tag, source_name, source_type)
            elif event_tag in DOOR_EVENTS:
                self._handle_door_event(space, event_tag, source_name, source_id, transition)
            elif event_tag in MOTION_EVENTS:
                self._handle_motion_event(space, event_tag, source_name, source_id)
            elif event_tag in SMOKE_EVENTS:
                self._handle_smoke_event(space, event_tag, source_name, source_id)
            elif event_tag in FLOOD_EVENTS:
                self._handle_flood_event(space, event_tag, source_name, source_id)
            elif event_tag in GLASS_EVENTS:
                self._handle_glass_event(space, event_tag, source_name, source_id)
            elif event_tag in TAMPER_EVENTS:
                self._handle_tamper_event(space, event_tag, source_name, source_id, transition)
            elif event_tag in DEVICE_STATUS_EVENTS:
                self._handle_device_status_event(space, event_tag, source_name, source_id)
            elif event_tag in RELAY_EVENTS:
                self._handle_relay_event(space, event_tag, source_name, source_id)
            elif event_tag in BUTTON_EVENTS:
                self._handle_button_event(space, event_tag, source_name, source_id)
            elif event_tag in WIRE_INPUT_EVENTS:
                self._handle_wire_input_event(space, event_tag, source_name, source_id, transition)
            elif event_tag in SCENARIO_EVENTS:
                self._handle_scenario_event(space, event, event_tag)
            elif event_tag in VIDEO_EVENTS or event_type_v2 in VIDEO_EVENT_TYPES:
                self._handle_video_event(space, event_tag, event_type_v2, source_name, source_id)
            elif event_tag in DOORBELL_EVENTS:
                self._handle_doorbell_event(space, source_name, source_id)
            elif event_tag in LOCK_EVENTS or event_tag in LOCK_DOOR_EVENTS:
                self._handle_lock_event(space, event_tag, source_name, source_id, event_code, event)
            elif event_tag in HUB_EVENTS:
                _LOGGER.info("SSE: Hub event: %s (%s)", event_tag, source_name)
            elif event_type_v2 == "LIFECYCLE":
                # Device add/remove/(de)activate notices (e.g. ObjectAdded) carry
                # no per-device state; the periodic poll reconciles the device
                # list, so log at debug instead of an "unhandled" warning (#88).
                _LOGGER.debug(
                    "SSE: lifecycle event %s (%s, id=%s) - ignored",
                    event_tag,
                    source_name,
                    source_id,
                )
            else:
                _LOGGER.warning(
                    "SSE event not handled: tag=%s, type=%s, typeV2=%s, source=%s (id=%s). Raw: %s",
                    event_tag,
                    source_type,
                    event_type_v2 or "none",
                    source_name,
                    source_id,
                    event,
                )

            # Notify HA of update
            if self.coordinator.account is not None:
                self.coordinator.async_set_updated_data(self.coordinator.account)

        except Exception as err:
            _LOGGER.error("SSE event processing error: %s", err, exc_info=True)

    async def _handle_security_event(
        self,
        space: AjaxSpace,
        event_tag: str,
        source_name: str,
        source_type: str | None = None,
    ) -> None:
        """Handle arm/disarm/night mode events."""
        new_state = EVENT_TAG_TO_STATE.get(event_tag)
        if not new_state:
            return

        old_state = space.security_state
        state_changed = old_state != new_state

        _LOGGER.info(
            "SSE security: tag=%s, old=%s, new=%s, changed=%s",
            event_tag,
            old_state.value,
            new_state.value,
            state_changed,
        )

        # Check if this was triggered by Home Assistant.
        # Use the non-consuming peek (has_pending_ha_action) — NOT a
        # consume-on-read variant — so that when Ajax emits two
        # state-mapped events for a single HA-initiated arm/disarm (e.g.
        # 'arm' then 'armwithmalfunctions', or a per-group event followed
        # by the system event) BOTH stay attributed to Home Assistant.
        # Consuming the flag on the first event would attribute the second
        # to the raw Ajax user/keypad name and fire a misleading
        # 'armed/disarmed by <user>' notification/bus event (#parity SQS).
        if self.coordinator.has_pending_ha_action(space.hub_id):  # type: ignore[arg-type]
            source_name = "Home Assistant"
            source_type = "HA"

        # Group arm/disarm events need a FULL refresh to update group states
        # because the final state depends on how many groups are armed
        is_group_event = event_tag in GROUP_ARM_EVENT_TAGS

        # Full arm/disarm also affects all groups - need refresh to update them
        is_full_arm_disarm = event_tag in FULL_ARM_EVENT_TAGS

        if is_group_event or is_full_arm_disarm:
            start_time = time.time()
            _LOGGER.info(
                "SSE: Security event '%s' detected for hub %s at t=0ms, refreshing groups",
                event_tag,
                space.hub_id,
            )
            # The lock spans the whole sequence (sleep + refresh): the
            # skip-flag must be set BEFORE the sleep, otherwise a REST poll
            # tick landing in the 0.3s window sees the flag still False and
            # fires a duplicate ajax_armed/ajax_disarmed bus event (#133).
            async with self._security_event_lock:
                # A forced refresh that STARTED after this event was received
                # has already fetched backend state covering it - re-running
                # the whole sleep+refresh sequence would just repeat the same
                # REST calls (multi-group "arm all" bursts emit one event per
                # group). Skip the skip-set/sleep/bypass-flag/refresh/fallback
                # entirely for a coalesced event; per-event notification /
                # history / bus-event handling below still runs unconditionally.
                already_covered = self.coordinator._last_forced_state_refresh_started >= start_time
                if already_covered:
                    _LOGGER.debug(
                        "SSE: Security event '%s' coalesced into the refresh started at %.3f",
                        event_tag,
                        self.coordinator._last_forced_state_refresh_started,
                    )
                else:
                    try:
                        # Skip REST-side event creation for THIS hub until the refresh is done
                        if space.hub_id:
                            self.coordinator._skipped_state_change_hubs.add(space.hub_id)
                        # Small delay to let Ajax backend process the change
                        # Reduced from 1.0s to 0.3s for faster real-time updates
                        await asyncio.sleep(0.3)
                        _LOGGER.debug("SSE: Sleep completed at t=%dms", int((time.time() - start_time) * 1000))
                        # CRITICAL: Bypass proxy cache to get fresh group states from Ajax API
                        self.coordinator._bypass_cache_next_refresh = True
                        # Group states are already fetched on every tick (#150), so an
                        # arm/disarm event only needs an immediate light refresh - not a
                        # full account-wide metadata pass (rooms/users/video re-fetches
                        # across all hubs).
                        await self.coordinator.async_force_state_refresh()
                        elapsed = int((time.time() - start_time) * 1000)
                        _LOGGER.info("SSE: Group refresh completed at t=%dms", elapsed)
                    except Exception as err:
                        _LOGGER.error("SSE: State refresh failed after security event: %s", err)
                        # Fallback: apply SSE state directly since refresh failed
                        if state_changed:
                            space.security_state = new_state
                            self._last_state_update[space.hub_id] = time.time()  # type: ignore[index]
                            _LOGGER.info("SSE: Fallback state update applied after refresh failure")
                    finally:
                        # Always clear the per-hub skip flag
                        if space.hub_id:
                            self.coordinator._skipped_state_change_hubs.discard(space.hub_id)

        # Update state immediately only for events that don't trigger a refresh
        # For group and full arm/disarm events, the metadata refresh will update the state
        # This prevents race conditions where SSE updates state before refresh completes
        if state_changed and not is_group_event and not is_full_arm_disarm:
            space.security_state = new_state
            self._last_state_update[space.hub_id] = time.time()  # type: ignore[index]
            # Realign the polling interval (and door fast-poll) like the SQS
            # handler does — otherwise transitions such as nightmodeoff leave
            # the coordinator stuck on the previous (armed) interval.
            self.coordinator._update_polling_interval(new_state)

        # For group / night-mode-off events use the specific action
        # (group_armed/group_disarmed/night_mode_off) instead of the system
        # state value, so the notification label and the ALARMS_ONLY filter
        # behave identically to the SQS transport.
        notification_action = SECURITY_EVENT_ACTIONS.get(event_tag, new_state.value)

        # Always create notification (even if state unchanged).
        # DEBUG, not INFO: source_name is the Ajax user who armed/disarmed (PII).
        _LOGGER.debug(
            "SSE instant: %s -> %s par %s (action=%s, state_changed=%s)",
            old_state.value,
            new_state.value,
            source_name or "inconnu",
            notification_action,
            state_changed,
        )

        # Create notification
        await self.coordinator._create_sqs_notification(
            action=notification_action,
            source_name=source_name,
            space_name=space.name,
            space_id=space.id,
        )

        # Fire HA bus event so automations can react to who triggered the
        # arm/disarm (user name, keypad, space control, HA...). Always
        # fire — when REST polling has already consumed the state change
        # (state_changed=False) the bus event would otherwise never reach
        # automations. The coordinator's _skip_state_change_event flag
        # prevents the REST poller from emitting a duplicate.
        self.coordinator._fire_security_state_event(
            space,
            old_state,
            new_state,
            source_name=source_name,
            source_type=source_type,
        )

    def _record_alarm_event(
        self,
        space: AjaxSpace,
        action_key: str,
        source_name: str,
        room_name: str = "",
    ) -> None:
        """Append an alarm-class event to history and raise a notification.

        Restores parity with the SQS transport: SQS appends every event to
        ``space.recent_events`` and raises a persistent dashboard
        notification for alarm-class events. In proxy (SSE) mode the
        per-device alarm handlers previously only flipped device attributes
        and set ``security_state=TRIGGERED``, so an intrusion/smoke/flood
        produced no recent_events entry and no persistent notification.

        Self-contained: mirrors SQS's history append + alarm notification
        using only existing coordinator helpers (``_create_sqs_notification``)
        and the translated message catalogue, so no cross-file refactor is
        required.

        Args:
            space: The space the alarm belongs to.
            action_key: Translation key for the alarm (e.g. ``smoke_detected``).
            source_name: Device/user name reported by the event (may be PII).
            room_name: Room name, when known.
        """
        language = resolve_event_language(self.coordinator.hass.config.language)
        message = get_event_message(action_key, language)

        # Mirror SQS _add_event_to_history: insert most-recent-first, cap at 10.
        event: dict[str, Any] = {
            "action": action_key,
            "message": message,
            "is_alarm": True,
            "source_name": source_name,
            "room_name": room_name,
            "hub_id": space.hub_id or space.id,
            "space_id": space.id,
            "timestamp": datetime.now(UTC).isoformat(),
            "event_time": datetime.now(UTC),
        }
        space.recent_events.insert(0, event)
        space.recent_events = space.recent_events[:10]

        # Raise the persistent alarm notification via the existing coordinator
        # helper. The action key is non-arming, so the ALARMS_ONLY filter still
        # shows it; only the NONE filter (and disabled persistent notifications)
        # suppress it — matching SQS _create_alarm_notification semantics.
        self._spawn_background(
            self.coordinator._create_sqs_notification(
                action=action_key,
                source_name=source_name,
                space_name=space.name,
                space_id=space.id,
            )
        )

    def _find_device(self, space: AjaxSpace, source_name: str, source_id: str) -> AjaxDevice | None:
        """Find device by name or ID.

        Tries multiple matching strategies similar to SQS manager.
        """
        # Try by exact ID match first
        if source_id:
            if source_id in space.devices:
                return space.devices[source_id]

            # For WireInput devices: try matching by suffix (wire input index)
            if len(source_id) == 8:
                for device in space.devices.values():
                    if len(device.id) == 16 and device.id.endswith(source_id):
                        _LOGGER.debug(
                            "SSE: Matched device %s by suffix %s",
                            device.name,
                            source_id,
                        )
                        return device

            # Try matching by prefix (parent ID) — parity with the SQS
            # manager: a MultiTransmitter child identified by its parent's
            # 8-char prefix must resolve in proxy mode too.
            if len(source_id) == 8:
                for device in space.devices.values():
                    if len(device.id) == 16 and device.id.startswith(source_id):
                        _LOGGER.debug(
                            "SSE: Matched device %s by prefix %s",
                            device.name,
                            source_id,
                        )
                        return device

        # Fall back to name match
        if source_name:
            for device in space.devices.values():
                if device.name == source_name:
                    return device

        # The device may have been added since the last poll — let the
        # coordinator discover it instead of staying blind until the next
        # full metadata refresh.
        if source_id:
            self._request_discovery_refresh(source_id)
        return None

    def _handle_door_event(
        self, space: AjaxSpace, event_tag: str, source_name: str, source_id: str, transition: str
    ) -> None:
        """Handle door opened/closed events."""
        action_key, is_triggered = DOOR_EVENTS[event_tag]

        # Use transition to determine actual state
        if transition == "RECOVERED":
            is_triggered = False
        elif transition == "TRIGGERED":
            is_triggered = True

        dev = self._find_device(space, source_name, source_id)
        if dev:
            # extcontact* events drive the External Contact entity, not the reed
            # (Door) entity — write the matching attribute so SSE updates it in
            # real time instead of lagging on the next REST poll (issue #151).
            attr_key = "external_contact_opened" if event_tag.startswith("extcontact") else "door_opened"
            dev.attributes[attr_key] = is_triggered
            dev.attributes[f"{attr_key}_at"] = datetime.now(UTC).isoformat()
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)

            # Parity with SQS: a door opening while the space is armed is an
            # alarm-class event — record it in history and raise a persistent
            # notification (does NOT change security_state, mirroring SQS's
            # door handler which never sets TRIGGERED).
            if is_triggered and space.security_state in (
                SecurityState.ARMED,
                SecurityState.NIGHT_MODE,
                SecurityState.PARTIALLY_ARMED,
            ):
                self._record_alarm_event(space, action_key, dev.name)
        else:
            _LOGGER.debug("SSE: Door device not found: name=%s, id=%s", source_name, source_id)

    def _handle_motion_event(self, space: AjaxSpace, event_tag: str, source_name: str, source_id: str) -> None:
        """Handle motion detected events."""
        action_key, is_triggered = MOTION_EVENTS[event_tag]

        dev = self._find_device(space, source_name, source_id)
        if dev:
            dev.attributes["motion_detected"] = is_triggered
            dev.attributes["motion_detected_at"] = datetime.now(UTC).isoformat()
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)

            # If system is armed and motion detected, trigger alarm
            if is_triggered and space.security_state in (
                SecurityState.ARMED,
                SecurityState.NIGHT_MODE,
                SecurityState.PARTIALLY_ARMED,
            ):
                previous_state = space.security_state
                space.security_state = SecurityState.TRIGGERED
                _LOGGER.info(
                    "SSE: Alarm TRIGGERED by motion on %s (was %s)",
                    dev.name,
                    previous_state.value,
                )
                # Parity with SQS: history entry + persistent alarm notification
                self._record_alarm_event(space, action_key, dev.name)
        else:
            _LOGGER.debug("SSE: Motion device not found: name=%s, id=%s", source_name, source_id)

    def _handle_smoke_event(self, space: AjaxSpace, event_tag: str, source_name: str, source_id: str) -> None:
        """Handle smoke/fire detector events."""
        action_key, is_triggered = SMOKE_EVENTS[event_tag]

        dev = self._find_device(space, source_name, source_id)
        if dev:
            if "smoke" in action_key:
                dev.attributes["smoke_detected"] = is_triggered
            elif "temp" in action_key:
                dev.attributes["temperature_alert"] = is_triggered
            elif "co" in action_key:
                dev.attributes["co_detected"] = is_triggered
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)

            # Smoke/fire alarms trigger regardless of arm state (life safety)
            if is_triggered:
                if space.security_state != SecurityState.TRIGGERED:
                    space.security_state = SecurityState.TRIGGERED
                    _LOGGER.info("SSE: Alarm TRIGGERED by smoke/fire on %s", dev.name)
                # Parity with SQS: history entry + persistent alarm notification
                self._record_alarm_event(space, action_key, dev.name)
        else:
            _LOGGER.debug("SSE: Smoke device not found: name=%s, id=%s", source_name, source_id)

    def _handle_flood_event(self, space: AjaxSpace, event_tag: str, source_name: str, source_id: str) -> None:
        """Handle flood/leak detector events."""
        action_key, is_triggered = FLOOD_EVENTS[event_tag]

        dev = self._find_device(space, source_name, source_id)
        if dev:
            dev.attributes["leak_detected"] = is_triggered
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)

            # Flood alarms trigger regardless of arm state (life safety)
            if is_triggered:
                if space.security_state != SecurityState.TRIGGERED:
                    space.security_state = SecurityState.TRIGGERED
                    _LOGGER.info("SSE: Alarm TRIGGERED by flood on %s", dev.name)
                # Parity with SQS: history entry + persistent alarm notification
                self._record_alarm_event(space, action_key, dev.name)
        else:
            _LOGGER.debug("SSE: Flood device not found: name=%s, id=%s", source_name, source_id)

    def _handle_glass_event(self, space: AjaxSpace, event_tag: str, source_name: str, source_id: str) -> None:
        """Handle glass break events."""
        action_key, is_triggered = GLASS_EVENTS[event_tag]

        dev = self._find_device(space, source_name, source_id)
        if dev:
            dev.attributes["glass_break_detected"] = is_triggered
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)

            # Glass break when armed triggers alarm
            if is_triggered and space.security_state in (
                SecurityState.ARMED,
                SecurityState.NIGHT_MODE,
                SecurityState.PARTIALLY_ARMED,
            ):
                space.security_state = SecurityState.TRIGGERED
                _LOGGER.info("SSE: Alarm TRIGGERED by glass break on %s", dev.name)
                # Parity with SQS: history entry + persistent alarm notification
                self._record_alarm_event(space, action_key, dev.name)
        else:
            _LOGGER.debug("SSE: Glass device not found: name=%s, id=%s", source_name, source_id)

    def _handle_tamper_event(
        self, space: AjaxSpace, event_tag: str, source_name: str, source_id: str, transition: str
    ) -> None:
        """Handle tamper events."""
        dev = self._find_device(space, source_name, source_id)
        if dev:
            action_key = self._apply_tamper_state(dev, event_tag, transition)
            _LOGGER.info(
                "SSE instant: %s -> %s (transition=%s)",
                dev.name,
                action_key,
                transition,
            )
        else:
            _LOGGER.warning(
                "SSE: Device not found for tamper: name=%s, id=%s",
                source_name,
                source_id,
            )

    def _handle_device_status_event(self, space: AjaxSpace, event_tag: str, source_name: str, source_id: str) -> None:
        """Handle device status events (online/offline, battery)."""
        dev = self._find_device(space, source_name, source_id)
        if dev:
            action_key = self._apply_device_status(dev, event_tag)
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)
        else:
            _LOGGER.warning(
                "SSE: Device not found for status: name=%s, id=%s",
                source_name,
                source_id,
            )

    def _handle_relay_event(self, space: AjaxSpace, event_tag: str, source_name: str, source_id: str) -> None:
        """Handle relay/socket on/off events."""
        action_key, is_on = RELAY_EVENTS[event_tag]

        dev = self._find_device(space, source_name, source_id)
        if dev:
            dev.attributes["is_on"] = is_on
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)
        else:
            _LOGGER.debug("SSE: Relay device not found: name=%s, id=%s", source_name, source_id)

    def _handle_doorbell_event(self, space: AjaxSpace, source_name: str, source_id: str) -> None:
        """Handle doorbell ring events.

        The Ajax Doorbell can be either a regular device or a Video Edge.
        """
        dev = self._find_device(space, source_name, source_id)
        device_id: str | None = None
        device_name: str | None = None

        if dev:
            device_id = dev.id
            device_name = dev.name
            dev.attributes["last_ring"] = datetime.now(UTC).isoformat()
            dev.attributes["doorbell_ring"] = True

            self._schedule_later(
                10.0,
                lambda: self._reset_doorbell_ring(space.id, dev.id),
            )
        else:
            for ve in space.video_edges.values():
                if (source_id and ve.id == source_id) or (source_name and ve.name == source_name):
                    device_id = ve.id
                    device_name = ve.name
                    ve.detections["doorbell_ring"] = True
                    break

        if device_id:
            self.coordinator.hass.bus.async_fire(
                EVENT_AJAX_DOORBELL_RING,
                {
                    "device_id": device_id,
                    "device_name": device_name,
                    "space_name": space.name,
                },
            )

            event_entity = self.coordinator._event_entities.get(f"{device_id}_doorbell_press")
            if event_entity is not None:
                event_entity.fire("ring")

            _LOGGER.info("SSE instant: %s -> doorbell ring", source_name)
        else:
            _LOGGER.debug("SSE: Doorbell device not found: name=%s, id=%s", source_name, source_id)

    def _handle_button_event(self, space: AjaxSpace, event_tag: str, source_name: str, source_id: str) -> None:
        """Handle button press events (single/double/long/panic/emergency)."""
        event_data = BUTTON_EVENTS.get(event_tag)
        if event_data is None:
            return
        action, _ = event_data

        dev = self._find_device(space, source_name, source_id)
        if not dev:
            _LOGGER.debug("SSE: Button device not found: %s (id=%s)", source_name, source_id)
            return

        dev.attributes["last_action"] = action

        self.coordinator.hass.bus.async_fire(
            EVENT_AJAX_BUTTON_PRESSED,
            {
                "device_id": dev.id,
                "device_name": dev.name,
                "action": action,
                "space_name": space.name,
            },
        )

        event_entity = self.coordinator._event_entities.get(f"{dev.id}_button_press")
        if event_entity is not None:
            event_entity.fire(action)

        _LOGGER.info("SSE instant: %s -> %s (button)", source_name, action)

    def _handle_wire_input_event(
        self,
        space: AjaxSpace,
        event_tag: str,
        source_name: str,
        source_id: str,
        transition: str,
    ) -> None:
        """Handle WireInput alarm events (intrusion, S1/S2/S3, roller shutter)."""
        event_data = WIRE_INPUT_EVENTS.get(event_tag)
        if event_data is None:
            return
        action, is_triggered = event_data

        if transition == "RECOVERED":
            is_triggered = False
        elif transition == "TRIGGERED":
            is_triggered = True

        dev = self._find_device(space, source_name, source_id)
        if not dev:
            _LOGGER.debug("SSE: WireInput device not found: %s (id=%s)", source_name, source_id)
            return

        dev.attributes["door_opened"] = is_triggered

        _LOGGER.info("SSE instant: %s -> %s (wire input)", source_name, action)

    def _handle_lock_event(
        self,
        space: AjaxSpace,
        event_tag: str,
        source_name: str,
        source_id: str,
        event_code: str,
        event: dict[str, Any],
    ) -> None:
        """Handle smart lock events (lock/unlock, door open/close).

        Uses event_code mapping for reliable state determination
        (transition derived from event code parity is unreliable for smart locks).
        """
        # Find the smart lock by source_id or source_name
        smart_lock = None
        if source_id:
            smart_lock = space.smart_locks.get(source_id)
        if not smart_lock and source_name:
            for sl in space.smart_locks.values():
                if sl.name == source_name:
                    smart_lock = sl
                    break

        if not smart_lock:
            # Auto-create from event data (API may not list the device)
            if source_id:
                smart_lock = AjaxSmartLock(
                    id=source_id,
                    name=source_name or f"Smart Lock {source_id[:6]}",
                    space_id=space.id,
                )
                space.smart_locks[source_id] = smart_lock
                _LOGGER.info(
                    "SSE: Auto-discovered smart lock from event: %s (%s)",
                    smart_lock.name,
                    source_id,
                )
                # Signal platforms to create entities for the new smart lock
                async_dispatcher_send(self.coordinator.hass, SIGNAL_NEW_SMART_LOCK, space.id, source_id)
                # Persist to storage so it survives reboots
                self._spawn_background(self.coordinator._async_save_smart_locks())
            else:
                _LOGGER.warning(
                    "SSE: Smart lock event without source_id: tag=%s, name=%s",
                    event_tag,
                    source_name,
                )
                return

        # Extract user who triggered the event
        additional_data = event.get("additionalData", {})
        if isinstance(additional_data, dict):
            user_name = additional_data.get("sourceUserName")
            if user_name:
                smart_lock.last_changed_by = user_name

        event_code_upper = event_code.upper() if event_code else ""

        event_entity = self.coordinator._event_entities.get(f"{smart_lock.id}_smart_lock_event")

        if event_tag == "smartlockdoorbellbuttonpressed":
            self.coordinator.hass.bus.async_fire(
                EVENT_AJAX_SMART_LOCK_DOORBELL,
                {
                    "device_id": smart_lock.id,
                    "device_name": smart_lock.name,
                    "space_name": space.name,
                },
            )
            if event_entity:
                # "ring" is the HA-standard doorbell event type (the entity is
                # device_class DOORBELL); see event.py.
                event_entity.fire("ring")
            _LOGGER.info("SSE instant: Smart lock %s -> doorbell pressed", smart_lock.name)
        elif event_tag in LOCK_DOOR_EVENTS:
            if event_code_upper in LOCK_DOOR_EVENT_CODE_STATES:
                smart_lock.is_door_open = LOCK_DOOR_EVENT_CODE_STATES[event_code_upper]
            if event_tag == "smartlockdoorleftopen":
                if event_entity:
                    event_entity.fire("door_left_open")
                _LOGGER.warning("SSE: Smart lock %s door left open", smart_lock.name)
            else:
                _LOGGER.info(
                    "SSE instant: Smart lock %s door -> %s",
                    smart_lock.name,
                    "open" if smart_lock.is_door_open else "closed",
                )
        elif event_tag in LOCK_EVENTS:
            if event_code_upper in LOCK_EVENT_CODE_STATES:
                smart_lock.is_locked = LOCK_EVENT_CODE_STATES[event_code_upper]
            _LOGGER.info(
                "SSE instant: Smart lock %s -> %s (by %s)",
                smart_lock.name,
                "locked" if smart_lock.is_locked else "unlocked",
                smart_lock.last_changed_by or "unknown",
            )

        smart_lock.last_event_tag = event_tag
        smart_lock.last_event_time = datetime.now(UTC)
        smart_lock.last_sse_event_time = datetime.now(UTC)

    def _handle_scenario_event(self, space: AjaxSpace, event: dict[str, Any], event_tag: str) -> None:
        """Handle scenario events that might be triggered by a Button.

        When a Button is configured in 'Control' mode, Ajax doesn't send a direct
        button press event. Instead, it sends a scenario event (e.g., RelayOnByScenario).
        We extract the initiator info to identify the button and fire an HA event.
        """
        # Extract initiator info from additionalDataV2
        additional_data_v2 = event.get("additionalDataV2", [])
        source_name = event.get("sourceObjectName", "")

        initiator_name = None
        initiator_type = None
        for data in additional_data_v2:
            if data.get("additionalDataV2Type") == "INITIATOR_INFO":
                initiator_name = data.get("objectName")
                initiator_type = data.get("objectType")
                break

        if not initiator_name:
            _LOGGER.debug("SSE scenario: no initiator info found")
            return

        _LOGGER.info(
            "SSE scenario: %s triggered by %s (type=%s)",
            event_tag,
            initiator_name,
            initiator_type,
        )

        # Fire a Home Assistant event for automations
        self.coordinator.hass.bus.async_fire(
            EVENT_AJAX_SCENARIO_TRIGGERED,
            {
                "scenario_name": initiator_name,
                "initiator_type": initiator_type,
                "target_name": source_name,
                "event_tag": event_tag,
                "space_name": space.name,
            },
        )

    def _handle_video_event(
        self,
        space: AjaxSpace,
        event_tag: str,
        event_type_v2: str,
        source_name: str,
        source_id: str,
    ) -> None:
        """Handle video AI detection events (motion, human, vehicle, pet).

        These events are sent by surveillance cameras (Video Edge devices).
        Updates the channel state to reflect the active detection.
        """
        # Determine the detection type from eventTag or eventTypeV2
        detection_type = None
        if event_tag in VIDEO_EVENTS:
            detection_type = VIDEO_EVENTS[event_tag]
        elif event_type_v2 in VIDEO_EVENT_TYPES:
            detection_type = VIDEO_EVENT_TYPES[event_type_v2]

        if not detection_type:
            _LOGGER.debug(
                "SSE video: unknown detection type for tag=%s, type=%s",
                event_tag,
                event_type_v2,
            )
            return

        # Find the video edge device
        video_edge, channel_id = self._find_video_edge(space, source_name, source_id)
        if not video_edge:
            _LOGGER.warning(
                "SSE: Video edge device not found: name=%s, id=%s",
                source_name,
                source_id,
            )
            return

        # Update the channel state with the detection
        self._update_video_detection(video_edge, channel_id, detection_type, True)

        # Fire event entity (modern HA event platform) — parity with doorbell
        self._fire_video_detection_event(video_edge, detection_type)

        _LOGGER.info(
            "SSE instant: %s -> %s detected (channel=%s)",
            video_edge.name,
            detection_type,
            channel_id or "default",
        )

        # Schedule auto-reset after detection timeout (30 seconds)
        self._schedule_later(
            30.0,
            lambda: self._reset_video_detection(space.id, video_edge.id, channel_id, detection_type),
        )

    def _reset_video_detection(
        self,
        space_id: str,
        video_edge_id: str,
        channel_id: str | None,
        detection_type: str,
    ) -> None:
        """Reset video detection after timeout (called from timer)."""
        try:
            if not self.coordinator.account:
                return

            space = self.coordinator.account.spaces.get(space_id)
            if not space:
                return

            video_edge = space.video_edges.get(video_edge_id)
            if not video_edge:
                return

            self._update_video_detection(video_edge, channel_id, detection_type, False)

            _LOGGER.debug(
                "Video detection auto-reset: %s -> %s cleared",
                video_edge.name,
                detection_type,
            )

            # Notify HA of update
            if self.coordinator.account is not None:
                self.coordinator.async_set_updated_data(self.coordinator.account)

        except Exception as err:
            _LOGGER.debug("Error resetting video detection: %s", err)
