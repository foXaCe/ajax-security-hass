"""SQS Manager for Ajax real-time events.

Architecture:
- SQS events are used for INSTANT state updates (< 1 second)
- REST API polling confirms state periodically (fallback)
- SQS events directly update coordinator state for fastest response

Supported event types:
- SECURITY: arm/disarm/night mode changes
- ALARM: intrusion, smoke, flood, glass break, motion (when armed)
- MALFUNCTION: device offline, low battery, tamper
- SMART_HOME: relay/socket on/off

Event codes:
- Events contain both eventTag (e.g., "DoorOpened") and eventCode (e.g., "M_01_20")
- The event_codes module provides multilingual translations (en, fr, es, de, nl, sv, uk)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.persistent_notification import async_create
from homeassistant.helpers.dispatcher import async_dispatcher_send

from ._event_helpers import EventHandlerMixin
from .const import (
    CONF_MONITORED_SPACES,
    CONF_NOTIFICATION_FILTER,
    CONF_PERSISTENT_NOTIFICATION,
    EVENT_AJAX_BUTTON_PRESSED,
    EVENT_AJAX_DOORBELL_RING,
    EVENT_AJAX_SCENARIO_TRIGGERED,
    EVENT_AJAX_SMART_LOCK_DOORBELL,
    NOTIFICATION_FILTER_ALL,
    NOTIFICATION_FILTER_NONE,
    SIGNAL_NEW_SMART_LOCK,
)
from .event_codes import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    get_event_message,
    get_event_type_description,
    parse_event_code,
)
from .event_maps import (
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
    from .sqs_client import AjaxSQSClient

_LOGGER = logging.getLogger(__name__)

# Real-time event-mapping tables now live in ``event_maps`` (single source of
# truth shared with ``sse_manager``) — imported at the top of this module.


class SQSManager(EventHandlerMixin):
    """Manager for AWS SQS real-time event integration."""

    # Don't let REST overwrite SQS state for this many seconds
    STATE_PROTECTION_SECONDS = 15.0
    # Maximum events to keep in history
    MAX_EVENTS_HISTORY = 10
    # Deduplication window in seconds (ignore duplicate events within this window)
    DEDUP_WINDOW_SECONDS = 5.0

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        sqs_client: AjaxSQSClient,
    ) -> None:
        self.coordinator = coordinator
        self.sqs_client = sqs_client
        self._enabled = False
        self._last_event_time: float = 0.0
        self._last_state_update: dict[str, float] = {}  # hub_id -> timestamp
        self._recent_event_ids: dict[str, float] = {}  # event_key -> timestamp
        self._language: str = DEFAULT_LANGUAGE
        # Track scheduled call_later handles so we can cancel them on stop()
        # and strong-ref background tasks so they are not GC'd mid-flight.
        self._pending_timers: set[asyncio.TimerHandle] = set()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        # Serialise security_event handlers so overlapping events cannot
        # flip the coordinator skip-flag in a racy way.
        self._security_event_lock = asyncio.Lock()

    def set_language(self, language: str) -> None:
        """Set the language for event messages."""
        self._language = language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
        _LOGGER.debug("SQS Manager language set to: %s", self._language)

    async def start(self) -> bool:
        try:
            if not await self.sqs_client.connect():
                _LOGGER.error("Failed to connect to SQS")
                return False

            self.sqs_client.event_callback = self._handle_event
            await self.sqs_client.start_receiving()

            self._enabled = True
            _LOGGER.info("SQS Manager started")
            return True

        except Exception as err:
            _LOGGER.error("Failed to start SQS Manager: %s", err)
            return False

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
        self._enabled = False
        # Cancel all scheduled timers
        for handle in list(self._pending_timers):
            handle.cancel()
        self._pending_timers.clear()
        try:
            # close() stops the receive loop itself before releasing the client.
            await self.sqs_client.close()
            _LOGGER.info("SQS Manager stopped")
        except Exception as err:
            _LOGGER.error("Error stopping SQS Manager: %s", err)
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

    def _is_duplicate_event(self, event_key: str) -> bool:
        """Check if event was already processed within dedup window."""
        now = time.time()
        # Cleanup old entries
        self._recent_event_ids = {
            k: v for k, v in self._recent_event_ids.items() if now - v < self.DEDUP_WINDOW_SECONDS
        }
        if event_key in self._recent_event_ids:
            return True
        self._recent_event_ids[event_key] = now
        return False

    async def _handle_event(self, event_data: dict[str, Any]) -> bool:
        """Handle SQS event by directly updating state (instant response)."""
        if not self._enabled:
            return False

        try:
            self._last_event_time = time.time()
            self.coordinator.stats["events_sqs_received"] += 1

            # Extract event info
            event = event_data.get("event", {})
            event_tag = event.get("eventTag", "").lower()
            event_type = event.get("eventTypeV2", "")
            event_code = event.get("eventCode", "")  # M_XX_YY format
            hub_id = event.get("hubId", "")
            hub_name = event.get("hubName", "")
            source_name = event.get("sourceObjectName", "")
            source_type = event.get("sourceObjectType", "")
            source_id = event.get("sourceObjectId", "")
            room_name = event.get("sourceRoomName", "")
            timestamp = event.get("timestamp", 0)
            transition = event.get("transition", "")  # TRIGGERED/RECOVERED/IMPULSE
            additional_data_v2 = event.get("additionalDataV2", [])

            # DEBUG, not INFO: source_name can be an Ajax user's display
            # name (PII) and this fires on every event.
            _LOGGER.debug(
                "SQS event: type=%s, tag=%s, code=%s, source=%s (%s), id=%s, transition=%s",
                event_type,
                event_tag,
                event_code,
                source_name,
                source_type,
                source_id,
                transition,
            )

            # Log raw event data at DEBUG level for troubleshooting
            _LOGGER.debug("SQS raw event data: %s", event)

            if not hub_id or not event_tag:
                _LOGGER.debug("SQS event missing hubId or eventTag")
                return True

            # Deduplicate: SQS may redeliver messages
            event_key = f"{event_tag}_{source_id}_{timestamp}"
            if self._is_duplicate_event(event_key):
                _LOGGER.debug("Duplicate SQS event ignored: %s", event_key)
                return True

            # Find the space for this hub
            space = self._find_space(hub_id)
            if not space:
                _LOGGER.debug("SQS: hub %s not managed by this instance, leaving message in queue", hub_id)
                return False

            # Create event record for history
            event_record = self._create_event_record(
                event_tag=event_tag,
                event_type=event_type,
                event_code=event_code,
                source_name=source_name,
                source_type=source_type,
                source_id=source_id,
                room_name=room_name,
                hub_name=hub_name,
                timestamp=timestamp,
                transition=transition,
            )

            # Add to space's event history
            self._add_event_to_history(space, event_record)

            # Process based on event type
            if event_tag in EVENT_TAG_TO_STATE:
                await self._handle_security_event(space, event_tag, source_name, source_type)
            elif event_tag in DOOR_EVENTS:
                await self._handle_door_event(space, event_tag, source_name, source_id, transition)
            elif event_tag in MOTION_EVENTS:
                await self._handle_motion_event(space, event_tag, source_name, source_id)
            elif event_tag in SMOKE_EVENTS:
                await self._handle_alarm_event(space, "smoke", event_tag, source_name, source_id)
            elif event_tag in FLOOD_EVENTS:
                await self._handle_alarm_event(space, "flood", event_tag, source_name, source_id)
            elif event_tag in GLASS_EVENTS:
                await self._handle_alarm_event(space, "glass", event_tag, source_name, source_id)
            elif event_tag in RELAY_EVENTS:
                await self._handle_relay_event(space, event_tag, source_name, source_id)
            elif event_tag in BUTTON_EVENTS:
                await self._handle_button_event(space, event_tag, source_name, source_id)
            elif event_tag in DOORBELL_EVENTS:
                await self._handle_doorbell_event(space, event_tag, source_name, source_id)
            elif event_tag in SCENARIO_EVENTS:
                await self._handle_scenario_event(space, event_tag, source_name, additional_data_v2)
            elif event_tag in WIRE_INPUT_EVENTS:
                await self._handle_wire_input_event(space, event_tag, source_name, source_id, transition)
            elif event_tag in TAMPER_EVENTS or event_tag in DEVICE_STATUS_EVENTS:
                await self._handle_device_status_event(space, event_tag, source_name, source_id, transition)
            elif event_tag in VIDEO_EVENTS or event_type in VIDEO_EVENT_TYPES:
                await self._handle_video_event(space, event_tag, event_type, source_name, source_id)
            elif event_tag in LOCK_EVENTS or event_tag in LOCK_DOOR_EVENTS:
                await self._handle_lock_event(space, event_tag, source_name, source_id, event_code, event)
            elif event_tag in HUB_EVENTS:
                _LOGGER.info("SQS: Hub event: %s (%s)", event_tag, source_name)
            elif event_type == "LIFECYCLE":
                # Device add/remove/(de)activate notices carry no per-device
                # state; the poll reconciles devices, so log at debug instead of
                # an "unhandled" warning (parity with sse_manager, #88).
                _LOGGER.debug(
                    "SQS: lifecycle event %s (%s, id=%s) - ignored",
                    event_tag,
                    source_name,
                    source_id,
                )
            else:
                _LOGGER.warning(
                    "SQS event not handled: tag=%s, type=%s, source=%s (id=%s). Raw: %s",
                    event_tag,
                    event_type,
                    source_name,
                    source_id,
                    event,
                )

            # Create notification if it's an alarm event
            if event_type == "ALARM" or event_tag in SMOKE_EVENTS or event_tag in FLOOD_EVENTS:
                await self._create_alarm_notification(space, event_record)

            # Always update UI to show new event in history
            if self.coordinator.account is not None:
                self.coordinator.async_set_updated_data(self.coordinator.account)

            return True

        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.error("Error handling SQS event: %s", err, exc_info=True)
            return True

    def _find_space(self, hub_id: str) -> AjaxSpace | None:
        """Find space by hub ID."""
        if self.coordinator.account is None:
            return None
        for space in self.coordinator.account.spaces.values():
            if space.hub_id == hub_id or space.id == hub_id:
                return space
        return None

    def _create_event_record(
        self,
        event_tag: str,
        event_type: str,
        event_code: str,
        source_name: str,
        source_type: str,
        source_id: str,
        room_name: str,
        hub_name: str,
        timestamp: int,
        transition: str,
    ) -> dict[str, Any]:
        """Create a standardized event record with multilingual support."""
        # First try to get action and message from event code (M_XX_YY format)
        action = event_tag
        message = event_tag
        is_alarm = False
        category = "unknown"

        parsed = parse_event_code(event_code, self._language) if event_code else None
        if parsed:
            action = parsed["action"]
            message = parsed["message"]
            is_alarm = parsed["is_alarm"]
            category = parsed["category"]
        else:
            # Fall back to event tag mapping
            # Using Any for mixed dict types (some have str, others have tuple[str, bool])
            event_dicts: list[dict[str, Any]] = [
                DOOR_EVENTS,
                MOTION_EVENTS,
                SMOKE_EVENTS,
                FLOOD_EVENTS,
                GLASS_EVENTS,
                RELAY_EVENTS,
                TAMPER_EVENTS,
                DEVICE_STATUS_EVENTS,
                SECURITY_EVENT_ACTIONS,
            ]
            for events_dict in event_dicts:
                if event_tag in events_dict:
                    value = events_dict[event_tag]
                    if isinstance(value, tuple):
                        action = value[0]
                        is_alarm = value[1]
                    else:
                        action = value
                    break

            # Get translated message
            message = get_event_message(action, self._language)

        # Get translated event type description
        event_type_desc = get_event_type_description(event_type, self._language)

        return {
            "event_tag": event_tag,
            "event_type": event_type,
            "event_type_description": event_type_desc,
            "event_code": event_code,
            "action": action,
            "message": message,
            "is_alarm": is_alarm,
            "category": category,
            "source_name": source_name,
            "source_type": source_type,
            "source_id": source_id,
            "room_name": room_name,
            "hub_name": hub_name,
            # Ajax timestamps are in milliseconds, convert to seconds
            "timestamp": datetime.fromtimestamp(timestamp / 1000, tz=UTC) if timestamp else datetime.now(UTC),
            "transition": transition,
        }

    def _add_event_to_history(self, space: AjaxSpace, event_record: dict[str, Any]) -> None:
        """Add event to space's recent events history."""
        # Insert at beginning (most recent first)
        space.recent_events.insert(0, event_record)
        # Keep only last N events
        if len(space.recent_events) > self.MAX_EVENTS_HISTORY:
            space.recent_events = space.recent_events[: self.MAX_EVENTS_HISTORY]

    async def _handle_security_event(
        self,
        space: AjaxSpace,
        event_tag: str,
        source_name: str,
        source_type: str | None = None,
    ) -> bool:
        """Handle arm/disarm/night mode events."""
        new_state = EVENT_TAG_TO_STATE.get(event_tag)
        if not new_state:
            _LOGGER.debug("SQS security: unknown event_tag=%s", event_tag)
            return False

        old_state = space.security_state
        state_changed = old_state != new_state

        # Check if this was triggered by Home Assistant
        ha_action_pending = self.coordinator.has_pending_ha_action(space.hub_id)  # type: ignore[arg-type]
        if ha_action_pending:
            source_name = "Home Assistant"
            source_type = "HA"

        _LOGGER.info(
            "SQS security: tag=%s, old=%s, new=%s, changed=%s, ha_pending=%s",
            event_tag,
            old_state.value,
            new_state.value,
            state_changed,
            ha_action_pending,
        )

        # Group arm/disarm events need an immediate refresh to update group
        # states (group states are already fetched on every tick, #150)
        is_group_event = event_tag in GROUP_ARM_EVENT_TAGS

        # Full arm/disarm also affects all groups - need refresh to update them
        is_full_arm_disarm = event_tag in FULL_ARM_EVENT_TAGS

        if is_group_event or is_full_arm_disarm:
            event_received = time.time()
            _LOGGER.info(
                "SQS: Security event '%s' detected for hub %s, refreshing groups",
                event_tag,
                space.hub_id,
            )
            # The lock spans the whole sequence (sleep + refresh): the
            # skip-flag must be set BEFORE the sleep, otherwise a REST poll
            # tick landing in the 1s window sees the flag still False and
            # fires a duplicate ajax_armed/ajax_disarmed bus event (#133).
            async with self._security_event_lock:
                # A forced refresh that STARTED after this event was received
                # has already fetched backend state covering it - re-running
                # the whole sleep+refresh sequence would just repeat the same
                # REST calls (multi-group "arm all" bursts emit one event per
                # group). Skip the skip-set/sleep/bypass-flag/refresh
                # entirely for a coalesced event; per-event notification /
                # history / bus-event handling below still runs unconditionally.
                already_covered = self.coordinator._last_forced_state_refresh_started >= event_received
                if already_covered:
                    _LOGGER.debug(
                        "SQS: Security event '%s' coalesced into the refresh started at %.3f",
                        event_tag,
                        self.coordinator._last_forced_state_refresh_started,
                    )
                else:
                    try:
                        # Skip REST-side event creation for THIS hub until the refresh is done
                        if space.hub_id:
                            self.coordinator._skipped_state_change_hubs.add(space.hub_id)
                        # Wait for Ajax backend to process the change before refreshing
                        # Without this delay, the API may return stale state
                        await asyncio.sleep(1.0)
                        # Bypass proxy cache to get fresh group states from Ajax API
                        # (parity with the SSE path)
                        self.coordinator._bypass_cache_next_refresh = True
                        # Group states are already fetched on every tick (#150), so an
                        # arm/disarm event only needs an immediate light refresh - not a
                        # full account-wide metadata pass (rooms/users/video re-fetches
                        # across all hubs).
                        await self.coordinator.async_force_state_refresh()
                        _LOGGER.info("SQS: State refresh completed after security event")
                    except Exception as err:
                        _LOGGER.error("SQS: State refresh failed after security event: %s", err)
                    finally:
                        # Always clear the per-hub skip flag
                        if space.hub_id:
                            self.coordinator._skipped_state_change_hubs.discard(space.hub_id)

        # Skip state update if HA action is pending (protect optimistic update)
        # But still record the event in history and create notification
        if state_changed and not ha_action_pending and not is_group_event:
            space.security_state = new_state
            self._last_state_update[space.hub_id] = time.time()  # type: ignore[index]
            # Update polling interval based on new state
            self.coordinator._update_polling_interval(new_state)

        # Always create notification from SQS (even if state unchanged)
        # because SQS contains user info that REST doesn't have
        # For group events, use specific action (group_armed/group_disarmed)
        # instead of system state (partially_armed)
        notification_action = SECURITY_EVENT_ACTIONS.get(event_tag, new_state.value)

        # DEBUG, not INFO: source_name is the Ajax user who armed/disarmed (PII).
        _LOGGER.debug(
            "SQS instant: %s -> %s par %s (action=%s, state_changed=%s)",
            old_state.value,
            new_state.value,
            source_name or "inconnu",
            notification_action,
            state_changed,
        )

        await self.coordinator._create_sqs_notification(
            action=notification_action,
            source_name=source_name,
            space_name=space.name,
            space_id=space.id,
        )

        # Fire HA bus event so automations can branch on who triggered
        # the arm/disarm (user name, keypad, space control, HA...). Always
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
        return True

    async def _handle_door_event(
        self, space: AjaxSpace, event_tag: str, source_name: str, source_id: str, transition: str
    ) -> bool:
        """Handle door open/close events."""
        event_data = DOOR_EVENTS.get(event_tag)
        if event_data is None:
            return False
        action, is_open = event_data
        is_external = event_tag.startswith("extcontact")

        # Use transition to determine actual state (fixes issue #9)
        # RECOVERED means the alarm condition ended (closed)
        # TRIGGERED means the alarm condition started (opened)
        if transition == "RECOVERED":
            is_open = False
            action = "ext_contact_closed" if is_external else "door_closed"
        elif transition == "TRIGGERED":
            is_open = True
            action = "ext_contact_opened" if is_external else "door_opened"

        # Find and update the device. extcontact* events drive the External
        # Contact entity, not the reed (Door) entity (issue #151).
        device = self._find_device(space, source_name, source_id)
        if device:
            attr_key = "external_contact_opened" if is_external else "door_opened"
            device.attributes[attr_key] = is_open
            message = get_event_message(action, self._language)
            _LOGGER.info("SQS instant: %s -> %s", source_name, message)
            return True

        _LOGGER.debug("SQS: Door device %s not found", source_name)
        return False

    async def _handle_motion_event(self, space: AjaxSpace, event_tag: str, source_name: str, source_id: str) -> bool:
        """Handle motion detection events."""
        event_data = MOTION_EVENTS.get(event_tag)
        if event_data is None:
            return False
        action, is_motion = event_data

        device = self._find_device(space, source_name, source_id)
        if device:
            # Align with the SSE handler and the motion binary sensor, which read
            # ``motion_detected`` (+ ``motion_detected_at`` for impulse expiry).
            device.attributes["motion_detected"] = is_motion
            if is_motion:
                device.attributes["motion_detected_at"] = datetime.now(UTC).isoformat()
            message = get_event_message(action, self._language)
            _LOGGER.info("SQS instant: %s -> %s", source_name, message)

            # If system is armed and motion detected, trigger alarm
            if is_motion and space.security_state in (
                SecurityState.ARMED,
                SecurityState.NIGHT_MODE,
                SecurityState.PARTIALLY_ARMED,
            ):
                space.security_state = SecurityState.TRIGGERED
                _LOGGER.info("SQS: Alarm TRIGGERED by motion on %s", device.name)
            return True

        _LOGGER.debug("SQS: Motion device %s not found", source_name)
        return False

    async def _handle_alarm_event(
        self, space: AjaxSpace, alarm_type: str, event_tag: str, source_name: str, source_id: str
    ) -> bool:
        """Handle smoke/flood/glass alarm events."""
        device = self._find_device(space, source_name, source_id)
        if device:
            # Determine action and alarm state from event dictionaries
            action = event_tag
            is_alarm = False
            for events_dict in [SMOKE_EVENTS, FLOOD_EVENTS, GLASS_EVENTS]:
                if event_tag in events_dict:
                    action, is_alarm = events_dict[event_tag]
                    break

            # SMOKE_EVENTS bundles smoke, CO and high-/rapid-temperature alarms.
            # Route each to the attribute its sensor actually reads (mirrors
            # sse_manager._handle_smoke_event); a blanket ``smoke_alarm`` would
            # light the smoke sensor on a CO/temperature event and leave the CO /
            # high-temperature sensors dark over SQS.
            if alarm_type == "smoke":
                if "smoke" in action:
                    device.attributes["smoke_alarm"] = is_alarm
                elif "temp" in action:
                    device.attributes["temperature_alert"] = is_alarm
                elif "co" in action:
                    device.attributes["co_detected"] = is_alarm
            else:
                device.attributes[f"{alarm_type}_alarm"] = is_alarm

            message = get_event_message(action, self._language)
            _LOGGER.info("SQS instant: %s - %s", source_name, message)

            # Smoke/flood trigger regardless of arm state (life safety)
            # Glass break triggers when armed (intrusion)
            if is_alarm and space.security_state != SecurityState.TRIGGERED:
                if alarm_type in ("smoke", "flood"):
                    # Life safety alarms always trigger
                    space.security_state = SecurityState.TRIGGERED
                    _LOGGER.info("SQS: Alarm TRIGGERED by %s on %s", alarm_type, device.name)
                elif alarm_type == "glass" and space.security_state in (
                    SecurityState.ARMED,
                    SecurityState.NIGHT_MODE,
                    SecurityState.PARTIALLY_ARMED,
                ):
                    space.security_state = SecurityState.TRIGGERED
                    _LOGGER.info("SQS: Alarm TRIGGERED by glass break on %s", device.name)

            return True

        _LOGGER.debug("SQS: Alarm device %s not found", source_name)
        return False

    async def _handle_relay_event(self, space: AjaxSpace, event_tag: str, source_name: str, source_id: str) -> bool:
        """Handle relay/socket/light on/off events."""
        event_data = RELAY_EVENTS.get(event_tag)
        if event_data is None:
            return False
        action, is_on = event_data

        device = self._find_device(space, source_name, source_id)
        if device:
            device.attributes["is_on"] = is_on
            message = get_event_message(action, self._language)
            _LOGGER.info("SQS instant: %s -> %s", source_name, message)
            return True

        _LOGGER.debug("SQS: Relay device %s not found", source_name)
        return False

    async def _handle_wire_input_event(
        self, space: AjaxSpace, event_tag: str, source_name: str, source_id: str, transition: str
    ) -> bool:
        """Handle WireInput alarm events (intrusion, S1/S2/S3, roller shutter).

        These events are sent when the system is armed and a WireInput device
        is triggered. We treat them as door open/close events.
        """
        event_data = WIRE_INPUT_EVENTS.get(event_tag)
        if event_data is None:
            return False
        action, is_triggered = event_data

        # Use transition to determine actual state
        if transition == "RECOVERED":
            is_triggered = False
        elif transition == "TRIGGERED":
            is_triggered = True

        # Find the device
        device = self._find_device(space, source_name, source_id)
        if device:
            # Update door_opened state (same as door events)
            device.attributes["door_opened"] = is_triggered

            message = get_event_message(action, self._language)
            _LOGGER.info("SQS instant: %s -> %s (wire input)", source_name, message)
            return True

        _LOGGER.warning("SQS: WireInput device %s (id=%s) not found", source_name, source_id)
        return False

    async def _handle_button_event(self, space: AjaxSpace, event_tag: str, source_name: str, source_id: str) -> bool:
        """Handle button press events."""
        event_data = BUTTON_EVENTS.get(event_tag)
        if event_data is None:
            return False
        action, _ = event_data

        device = self._find_device(space, source_name, source_id)
        if device:
            # Store[Any] the last action in device attributes
            device.attributes["last_action"] = action

            # Fire a Home Assistant event for automations (legacy bus event)
            self.coordinator.hass.bus.async_fire(
                EVENT_AJAX_BUTTON_PRESSED,
                {
                    "device_id": device.id,
                    "device_name": device.name,
                    "action": action,
                    "space_name": space.name,
                },
            )

            # Fire event entity (modern HA event platform)
            event_entity = self.coordinator._event_entities.get(f"{device.id}_button_press")
            if event_entity:
                event_entity.fire(action)

            message = get_event_message(action, self._language)
            _LOGGER.info("SQS instant: %s -> %s (button)", source_name, message)
            return True

        _LOGGER.debug("SQS: Button device %s not found", source_name)
        return False

    async def _handle_doorbell_event(self, space: AjaxSpace, event_tag: str, source_name: str, source_id: str) -> bool:
        """Handle doorbell ring events.

        The Ajax Doorbell is a Video Edge device, so we search both
        devices and video_edges to find it.
        """
        # Try regular devices first
        device = self._find_device(space, source_name, source_id)
        device_id = None
        device_name = None

        if device:
            device_id = device.id
            device_name = device.name
            device.attributes["last_ring"] = datetime.now(UTC).isoformat()
            device.attributes["doorbell_ring"] = True

            # Schedule auto-reset of doorbell_ring state after 10 seconds
            self._schedule_later(
                10.0,
                lambda: self._reset_doorbell_ring(space.id, device.id),
            )
        else:
            # Search in video_edges (Ajax Doorbell is a Video Edge)
            for ve in space.video_edges.values():
                if (source_id and ve.id == source_id) or (source_name and ve.name == source_name):
                    device_id = ve.id
                    device_name = ve.name
                    ve.detections["doorbell_ring"] = True
                    break

        if device_id:
            # Fire a Home Assistant event for automations (legacy bus event)
            self.coordinator.hass.bus.async_fire(
                EVENT_AJAX_DOORBELL_RING,
                {
                    "device_id": device_id,
                    "device_name": device_name,
                    "space_name": space.name,
                },
            )

            # Fire event entity (modern HA event platform)
            event_entity = self.coordinator._event_entities.get(f"{device_id}_doorbell_press")
            if event_entity:
                event_entity.fire("ring")

            _LOGGER.info("SQS instant: %s -> doorbell ring", source_name)
            return True

        _LOGGER.debug("SQS: Doorbell device %s not found", source_name)
        return False

    async def _handle_scenario_event(
        self, space: AjaxSpace, event_tag: str, source_name: str, additional_data_v2: list[Any]
    ) -> bool:
        """Handle scenario events that might be triggered by a Button.

        When a Button is configured in 'Control' mode, Ajax doesn't send a direct
        button press event. Instead, it sends a scenario event (e.g., RelayOnByScenario).
        We extract the initiator info to identify the button and fire an HA event.
        """
        # Extract initiator info from additionalDataV2
        initiator_name = None
        initiator_type = None
        for data in additional_data_v2:
            if data.get("additionalDataV2Type") == "INITIATOR_INFO":
                initiator_name = data.get("objectName")
                initiator_type = data.get("objectType")
                break

        if not initiator_name:
            _LOGGER.debug("SQS scenario: no initiator info found")
            return False

        _LOGGER.info(
            "SQS scenario: %s triggered by %s (type=%s)",
            event_tag,
            initiator_name,
            initiator_type,
        )

        # Fire a Home Assistant event for automations
        # This allows users to trigger automations based on scenario execution
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

        return True

    async def _handle_device_status_event(
        self, space: AjaxSpace, event_tag: str, source_name: str, source_id: str, transition: str
    ) -> bool:
        """Handle device status events (online/offline, battery, tamper)."""
        device = self._find_device(space, source_name, source_id)
        if not device:
            _LOGGER.debug("SQS: Device %s not found for status event", source_name)
            return False

        action = event_tag
        if event_tag in TAMPER_EVENTS:
            # Shared helper: honours ``transition`` (Ajax reuses ``tamperopened``
            # for open AND close), exactly like the SSE path.
            action = self._apply_tamper_state(device, event_tag, transition)
        elif event_tag in DEVICE_STATUS_EVENTS:
            action = self._apply_device_status(device, event_tag)

        message = get_event_message(action, self._language)
        _LOGGER.info("SQS instant: %s - %s", source_name, message)
        return True

    def _find_device(self, space: AjaxSpace, source_name: str, source_id: str) -> AjaxDevice | None:
        """Find device by name or ID.

        WireInput devices have composite IDs (parent ID + wire input index).
        SQS events might use the full ID, just the index, or just the parent ID.
        We try multiple matching strategies to find the device.
        """
        # Try by exact ID match first
        if source_id:
            for device in space.devices.values():
                if device.id == source_id:
                    return device

            # For WireInput devices: try matching by suffix (wire input index)
            # Device ID format: 16 chars = 8 char parent ID + 8 char wire input index
            # SQS might send just the 8-char index
            if len(source_id) == 8:
                for device in space.devices.values():
                    if len(device.id) == 16 and device.id.endswith(source_id):
                        _LOGGER.debug(
                            "SQS: Matched device %s by suffix %s",
                            device.name,
                            source_id,
                        )
                        return device

            # Try matching by prefix (parent ID)
            if len(source_id) == 8:
                for device in space.devices.values():
                    if len(device.id) == 16 and device.id.startswith(source_id):
                        _LOGGER.debug(
                            "SQS: Matched device %s by prefix %s",
                            device.name,
                            source_id,
                        )
                        return device

        # Fall back to name match
        if source_name:
            for device in space.devices.values():
                if device.name == source_name:
                    return device

        # Log all device IDs for debugging if no match found
        if source_id or source_name:
            device_ids = [f"{d.name}:{d.id}" for d in list(space.devices.values())[:10]]
            _LOGGER.debug(
                "SQS: No device match for id=%s, name=%s. Devices: %s",
                source_id,
                source_name,
                device_ids,
            )
            # The device may have been added since the last poll — let the
            # coordinator discover it instead of staying blind until the
            # next full metadata refresh.
            self._request_discovery_refresh(source_id)
        return None

    async def _create_alarm_notification(self, space: AjaxSpace, event_record: dict[str, Any]) -> None:
        """Create a Home Assistant notification for alarm events.

        Respects user notification preferences:
        - NONE: no notifications
        - ALARMS_ONLY: show alarm notifications (this method)
        - SECURITY_EVENTS: show alarm + arm/disarm notifications
        - ALL: show all notifications
        """
        # Check notification filter settings
        options = self.coordinator.config_entry.options if self.coordinator.config_entry else {}

        # Check if persistent notifications are enabled
        if not options.get(CONF_PERSISTENT_NOTIFICATION, True):
            return

        # Check notification filter - alarm notifications are shown for all filters except NONE
        notification_filter = options.get(CONF_NOTIFICATION_FILTER, NOTIFICATION_FILTER_ALL)
        if notification_filter == NOTIFICATION_FILTER_NONE:
            return

        # Per-space filter from the "notifications" options step. An empty
        # selection means "all spaces" (backwards compatible).
        monitored_spaces = options.get(CONF_MONITORED_SPACES, [])
        if monitored_spaces and space.id not in monitored_spaces:
            return

        source = event_record.get("source_name", "")
        room = event_record.get("room_name", "")
        message = event_record.get("message", "")

        # Build notification message. Persistent notifications render markdown,
        # so source/room (user-editable Ajax device/room labels) must be escaped
        # to neutralise [text](javascript:...) injection and stray formatting.
        parts = [f"**{message}**"]
        if source:
            parts.append(f"Source: {self.coordinator._escape_markdown(source)}")
        if room:
            parts.append(f"Room: {self.coordinator._escape_markdown(room)}")

        notification_message = "\n".join(parts)

        # Stable notification_id keyed on (space, event_code or action) so a
        # burst of identical alarm events updates the same persistent
        # notification instead of spamming the dashboard. Time-based ids
        # (time.time()) created a new notification per ms.
        notif_key = event_record.get("event_code") or event_record.get("action") or "alarm"
        async_create(
            self.coordinator.hass,
            notification_message,
            title=f"🚨 Ajax - {space.name}",
            notification_id=f"ajax_alarm_{space.id}_{notif_key}",
        )

    async def _handle_video_event(
        self,
        space: AjaxSpace,
        event_tag: str,
        event_type: str,
        source_name: str,
        source_id: str,
    ) -> bool:
        """Handle video AI detection events (motion, human, vehicle, pet).

        These events are sent by surveillance cameras (Video Edge devices).
        Updates the channel state to reflect the active detection.
        """
        # Determine the detection type from eventTag or eventTypeV2
        detection_type = None
        if event_tag in VIDEO_EVENTS:
            detection_type = VIDEO_EVENTS[event_tag]
        elif event_type in VIDEO_EVENT_TYPES:
            detection_type = VIDEO_EVENT_TYPES[event_type]

        if not detection_type:
            _LOGGER.debug(
                "SQS video: unknown detection type for tag=%s, type=%s",
                event_tag,
                event_type,
            )
            return False

        # Find the video edge device
        video_edge, channel_id = self._find_video_edge(space, source_name, source_id)
        if not video_edge:
            _LOGGER.warning(
                "SQS: Video edge device not found: name=%s, id=%s",
                source_name,
                source_id,
            )
            return False

        # Update the channel state with the detection
        self._update_video_detection(video_edge, channel_id, detection_type, True)

        # Fire event entity (modern HA event platform) — parity with doorbell
        self._fire_video_detection_event(video_edge, detection_type)

        _LOGGER.info(
            "SQS instant: %s -> %s detected (channel=%s)",
            video_edge.name,
            detection_type,
            channel_id or "default",
        )

        # Schedule auto-reset after detection timeout (30 seconds)
        self._schedule_later(
            30.0,
            lambda: self._reset_video_detection(space.id, video_edge.id, channel_id, detection_type),
        )

        return True

    async def _handle_lock_event(
        self,
        space: AjaxSpace,
        event_tag: str,
        source_name: str,
        source_id: str,
        event_code: str,
        event: dict[str, Any],
    ) -> bool:
        """Handle smart lock events (lock/unlock, door open/close).

        Smart locks are space devices stored in space.smart_locks.
        Uses event_code mapping for reliable state (transition field is
        unreliable for smart lock codes in SSE mode).
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
                    "SQS: Auto-discovered smart lock from event: %s (%s)",
                    smart_lock.name,
                    source_id,
                )
                # Signal platforms to create entities for the new smart lock
                async_dispatcher_send(self.coordinator.hass, SIGNAL_NEW_SMART_LOCK, space.id, source_id)
                # Persist to storage so it survives reboots
                self._spawn_background(self.coordinator._async_save_smart_locks())
            else:
                _LOGGER.warning(
                    "SQS: Smart lock event without source_id: tag=%s, name=%s",
                    event_tag,
                    source_name,
                )
                return False

        # Extract user who triggered the event
        additional_data = event.get("additionalData", {})
        if isinstance(additional_data, dict):
            user_name = additional_data.get("sourceUserName")
            if user_name:
                smart_lock.last_changed_by = user_name

        event_code_upper = event_code.upper() if event_code else ""

        event_entity = self.coordinator._event_entities.get(f"{smart_lock.id}_smart_lock_event")

        if event_tag == "smartlockdoorbellbuttonpressed":
            # Doorbell button on smart lock — fire HA event
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
            _LOGGER.info("SQS instant: Smart lock %s -> doorbell pressed", smart_lock.name)
        elif event_tag in LOCK_DOOR_EVENTS:
            # Door open/close/left open — use event code mapping
            if event_code_upper in LOCK_DOOR_EVENT_CODE_STATES:
                smart_lock.is_door_open = LOCK_DOOR_EVENT_CODE_STATES[event_code_upper]
            if event_tag == "smartlockdoorleftopen":
                if event_entity:
                    event_entity.fire("door_left_open")
                _LOGGER.warning(
                    "SQS: Smart lock %s door left open",
                    smart_lock.name,
                )
            else:
                _LOGGER.info(
                    "SQS instant: Smart lock %s door -> %s",
                    smart_lock.name,
                    "open" if smart_lock.is_door_open else "closed",
                )
        elif event_tag in LOCK_EVENTS:
            # Lock/unlock — use event code mapping
            if event_code_upper in LOCK_EVENT_CODE_STATES:
                smart_lock.is_locked = LOCK_EVENT_CODE_STATES[event_code_upper]
            _LOGGER.info(
                "SQS instant: Smart lock %s -> %s (by %s)",
                smart_lock.name,
                "locked" if smart_lock.is_locked else "unlocked",
                smart_lock.last_changed_by or "unknown",
            )

        smart_lock.last_event_tag = event_tag
        smart_lock.last_event_time = datetime.now(UTC)
        smart_lock.last_sse_event_time = datetime.now(UTC)

        return True

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
            self.coordinator.async_set_updated_data(self.coordinator.account)

        except Exception as err:
            _LOGGER.debug("Error resetting video detection: %s", err)

    def is_state_protected(self, hub_id: str) -> bool:
        """Check if state was recently updated by SQS (protected from REST overwrite)."""
        last_update = self._last_state_update.get(hub_id, 0)
        elapsed = time.time() - last_update
        is_protected = elapsed < self.STATE_PROTECTION_SECONDS
        if is_protected:
            _LOGGER.debug("Hub %s state protected (%.1fs since SQS update)", hub_id, elapsed)
        return is_protected
