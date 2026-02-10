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
- The event_codes module provides multilingual translations (fr, en, es)
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import SIGNAL_NEW_SMART_LOCK
from .event_codes import (
    DEFAULT_LANGUAGE,
    get_event_message,
    get_event_type_description,
    parse_event_code,
)
from .models import AjaxSmartLock, SecurityState

if TYPE_CHECKING:
    from .coordinator import AjaxDataCoordinator
    from .sqs_client import AjaxSQSClient

_LOGGER = logging.getLogger(__name__)

# Map SQS event tags to SecurityState
EVENT_TAG_TO_STATE = {
    "arm": SecurityState.ARMED,
    "armwithmalfunctions": SecurityState.ARMED,
    "disarm": SecurityState.DISARMED,
    "nightmodeon": SecurityState.NIGHT_MODE,
    "nightmodeonwithmalfunctions": SecurityState.NIGHT_MODE,
    "nightmodeoff": SecurityState.DISARMED,
    "partialarm": SecurityState.PARTIALLY_ARMED,
    "grouparm": SecurityState.PARTIALLY_ARMED,
    "groupdisarm": SecurityState.PARTIALLY_ARMED,  # Triggers refresh to get actual state
}

# Event tags by category (eventTag -> (action_key, is_triggered))
DOOR_EVENTS = {
    "dooropened": ("door_opened", True),
    "doorclosed": ("door_closed", False),
    "doorrestored": ("door_closed", False),  # Alternative tag for door closed
    "doornormal": ("door_closed", False),  # Alternative tag for door closed
    "extcontactopened": ("ext_contact_opened", True),
    "extcontactclosed": ("ext_contact_closed", False),
}

MOTION_EVENTS = {
    "motiondetected": ("motion_detected", True),
    "nomotiondetected": ("motion_cleared", False),
}

SMOKE_EVENTS = {
    "smokedetected": ("smoke_detected", True),
    "nosmokedetected": ("smoke_cleared", False),
    "temperatureabovethreshold": ("temp_high", True),
    "temperaturebacktonormal": ("temp_normal", False),
    "rapidtemperaturerise": ("rapid_temp_rise", True),
    "codetected": ("co_detected", True),
    "colevelok": ("co_cleared", False),
}

FLOOD_EVENTS = {
    "leakagedetected": ("leak_detected", True),
    "noleakagedetected": ("leak_cleared", False),
}

GLASS_EVENTS = {
    "glassbreakdetected": ("glass_break", True),
}

# WireInput alarm events (when system is armed)
# These are sent by MultiTransmitterWireInput and MultiTransmitterWireInputRs devices
WIRE_INPUT_EVENTS = {
    "intrusionalarm": ("intrusion_alarm", True),
    "s1alarm": ("s1_alarm", True),
    "s2alarm": ("s2_alarm", True),
    "s3alarm": ("s3_alarm", True),
    "rollershutteralarm": ("roller_shutter_alarm", True),
    "rollershutteroffline": ("roller_shutter_offline", True),
}

TAMPER_EVENTS = {
    "lidopen": ("tamper_open", True),
    "lidclosed": ("tamper_closed", False),
    "tampered": ("tamper_open", True),
    "tamperopened": ("tamper_open", True),  # Uses transition for actual state
    "tamperclosed": ("tamper_closed", False),
}

DEVICE_STATUS_EVENTS = {
    "online": ("device_online", False),
    "offline": ("device_offline", True),
    "lowbattery": ("low_battery", True),
    "batterycharged": ("battery_ok", False),
    "externalpowerdisconnected": ("power_disconnected", True),
    "externalpowerrestored": ("power_restored", False),
}

RELAY_EVENTS = {
    "switchedon": ("switched_on", True),
    "switchedoff": ("switched_off", False),
    "turnedon": ("light_on", True),
    "turnedoff": ("light_off", False),
    "relayonbyuser": ("relay_on", True),
    "relayoffbyuser": ("relay_off", False),
}

BUTTON_EVENTS = {
    "buttonpressed": ("single_press", True),
    "buttonsinglepress": ("single_press", True),
    "buttondoublepress": ("double_press", True),
    "buttonlongpress": ("long_press", True),
    "buttonshortpress": ("single_press", True),
    "panicbuttonpressed": ("panic", True),
    "emergencybuttonpressed": ("emergency", True),
}

# Doorbell events
DOORBELL_EVENTS = {
    "doorbellpressed": True,
    "doorbellbuttonpressed": True,
    "doorbellring": True,
    "doorbell": True,
}

# Hub system/malfunction events (informational, logged but not actionable)
HUB_EVENTS: set[str] = {
    "firmwareupdateinprogress",
    "firmwareupdatecompleted",
    "ethernetconnectionloss",
    "ethernetconnectionrestored",
    "gsmconnectionloss",
    "gsmconnectionrestored",
    "serverconnectionloss",
    "serverconnectionrestored",
    "huboffline",
    "hubonline",
    "powersupplyloss",
    "powersupplyrestored",
}

# Scenario events that might be triggered by a Button
SCENARIO_EVENTS = {
    "relayonbyscenario": "scenario_triggered",
    "relayoffbyscenario": "scenario_triggered",
    "scenarioexecuted": "scenario_triggered",
}

# Video AI detection events (eventTag -> detection_type)
# These events are sent when cameras detect motion, humans, vehicles, or pets
VIDEO_EVENTS = {
    "videomotiondetected": "VIDEO_MOTION",
    "videohumandetected": "VIDEO_HUMAN",
    "videovehicledetected": "VIDEO_VEHICLE",
    "videopetdetected": "VIDEO_PET",
    # Alternative event tags (may vary by firmware)
    "videomotion": "VIDEO_MOTION",
    "videohuman": "VIDEO_HUMAN",
    "videovehicle": "VIDEO_VEHICLE",
    "videopet": "VIDEO_PET",
}

# eventTypeV2 values that indicate video AI detection
VIDEO_EVENT_TYPES = {
    "VIDEO_MOTION": "VIDEO_MOTION",
    "VIDEO_HUMAN": "VIDEO_HUMAN",
    "VIDEO_VEHICLE": "VIDEO_VEHICLE",
    "VIDEO_PET": "VIDEO_PET",
}

# Smart lock events (LockBridge Jeweller)
# Mapping event codes -> is_locked (more reliable than transition field)
LOCK_EVENT_CODE_STATES: dict[str, bool] = {
    "M_7E_20": False,  # Unlocked by knob (thumbturn)
    "M_7E_21": False,  # Unlocked by code
    "M_7E_23": False,  # Unlocked by user (app)
    "M_7E_27": True,  # Locked by knob (thumbturn)
    "M_7E_29": True,  # Locked automatically
    "M_7E_2A": True,  # Locked by user (app)
}

# Mapping event codes -> is_door_open
LOCK_DOOR_EVENT_CODE_STATES: dict[str, bool] = {
    "M_7E_2E": True,  # Door open
    "M_7E_2F": False,  # Door closed
}

# Event tags for routing (lowercased by _handle_event)
LOCK_EVENTS: set[str] = {
    "smartlockunlockedbyuser",
    "smartlockunlockedbycode",
    "smartlockunlockedbyknob",
    "smartlockmodulelockedautomatically",
}

LOCK_DOOR_EVENTS: set[str] = {
    "smartlockdooropen",
}

# Map event tags to action keys for security events
SECURITY_EVENT_ACTIONS = {
    "arm": "armed",
    "disarm": "disarmed",
    "nightmodeon": "night_mode",
    "nightmodeoff": "night_mode_off",
    "partialarm": "armed",
    "grouparm": "group_armed",
    "groupdisarm": "group_disarmed",
}


class SQSManager:
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

    def set_language(self, language: str) -> None:
        """Set the language for event messages."""
        self._language = language if language in ("fr", "en", "es") else DEFAULT_LANGUAGE
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

    async def stop(self) -> None:
        self._enabled = False
        try:
            await self.sqs_client.stop_receiving()
            await self.sqs_client.close()
            _LOGGER.info("SQS Manager stopped")
        except Exception as err:
            _LOGGER.error("Error stopping SQS Manager: %s", err)

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

    async def _handle_event(self, event_data: dict[str, Any]) -> None:
        """Handle SQS event by directly updating state (instant response)."""
        if not self._enabled:
            return

        try:
            self._last_event_time = time.time()

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

            _LOGGER.info(
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
                return

            # Deduplicate: SQS may redeliver messages
            event_key = f"{event_tag}_{source_id}_{timestamp}"
            if self._is_duplicate_event(event_key):
                _LOGGER.debug("Duplicate SQS event ignored: %s", event_key)
                return

            # Find the space for this hub
            space = self._find_space(hub_id)
            if not space:
                _LOGGER.warning("SQS: No space found for hub %s", hub_id)
                return

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
                await self._handle_security_event(space, event_tag, source_name)
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
                await self._handle_device_status_event(space, event_tag, source_name, source_id)
            elif event_tag in VIDEO_EVENTS or event_type in VIDEO_EVENT_TYPES:
                await self._handle_video_event(space, event_tag, event_type, source_name, source_id)
            elif event_tag in LOCK_EVENTS or event_tag in LOCK_DOOR_EVENTS:
                await self._handle_lock_event(space, event_tag, source_name, source_id, event_code, event)
            elif event_tag in HUB_EVENTS:
                _LOGGER.info("SQS: Hub event: %s (%s)", event_tag, source_name)
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

        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.error("Error handling SQS event: %s", err, exc_info=True)

    def _find_space(self, hub_id: str):
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

    def _add_event_to_history(self, space, event_record: dict[str, Any]) -> None:
        """Add event to space's recent events history."""
        # Insert at beginning (most recent first)
        space.recent_events.insert(0, event_record)
        # Keep only last N events
        if len(space.recent_events) > self.MAX_EVENTS_HISTORY:
            space.recent_events = space.recent_events[: self.MAX_EVENTS_HISTORY]

    async def _handle_security_event(self, space, event_tag: str, source_name: str) -> bool:
        """Handle arm/disarm/night mode events."""
        new_state = EVENT_TAG_TO_STATE.get(event_tag)
        if not new_state:
            _LOGGER.debug("SQS security: unknown event_tag=%s", event_tag)
            return False

        old_state = space.security_state
        state_changed = old_state != new_state

        # Check if this was triggered by Home Assistant
        ha_action_pending = self.coordinator.has_pending_ha_action(space.hub_id)
        if ha_action_pending:
            source_name = "Home Assistant"

        _LOGGER.info(
            "SQS security: tag=%s, old=%s, new=%s, changed=%s, ha_pending=%s",
            event_tag,
            old_state.value,
            new_state.value,
            state_changed,
            ha_action_pending,
        )

        # Group arm/disarm events need a FULL refresh to update group states
        # because the final state depends on how many groups are armed
        is_group_event = event_tag in ("grouparm", "groupdisarm")

        # Full arm/disarm also affects all groups - need refresh to update them
        is_full_arm_disarm = event_tag in ("arm", "disarm")

        if is_group_event or is_full_arm_disarm:
            _LOGGER.info(
                "SQS: Security event '%s' detected for hub %s, refreshing groups",
                event_tag,
                space.hub_id,
            )
            # Wait for Ajax backend to process the change before refreshing
            # Without this delay, the API may return stale state
            await asyncio.sleep(1.0)
            try:
                # Set flag to skip event creation during refresh (SQS already created it)
                self.coordinator._skip_state_change_event = True
                await self.coordinator.async_force_metadata_refresh()
                _LOGGER.info("SQS: Metadata refresh completed after security event")
            except Exception as err:
                _LOGGER.error("SQS: Metadata refresh failed after security event: %s", err)
            finally:
                # Always reset the flag
                self.coordinator._skip_state_change_event = False

        # Skip state update if HA action is pending (protect optimistic update)
        # But still record the event in history and create notification
        if state_changed and not ha_action_pending and not is_group_event:
            space.security_state = new_state
            self._last_state_update[space.hub_id] = time.time()
            # Update polling interval based on new state
            self.coordinator._update_polling_interval(new_state)

        # Always create notification from SQS (even if state unchanged)
        # because SQS contains user info that REST doesn't have
        # For group events, use specific action (group_armed/group_disarmed)
        # instead of system state (partially_armed)
        notification_action = SECURITY_EVENT_ACTIONS.get(event_tag, new_state.value)

        _LOGGER.info(
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
        )

        return True

    async def _handle_door_event(
        self, space, event_tag: str, source_name: str, source_id: str, transition: str
    ) -> bool:
        """Handle door open/close events."""
        event_data = DOOR_EVENTS.get(event_tag)
        if event_data is None:
            return False
        action, is_open = event_data

        # Use transition to determine actual state (fixes issue #9)
        # RECOVERED means the alarm condition ended (door closed)
        # TRIGGERED means the alarm condition started (door opened)
        if transition == "RECOVERED":
            is_open = False
            action = "door_closed"
        elif transition == "TRIGGERED":
            is_open = True
            action = "door_opened"

        # Find and update the device
        device = self._find_device(space, source_name, source_id)
        if device:
            device.attributes["door_opened"] = is_open
            device.last_trigger_time = datetime.now(UTC) if is_open else None
            message = get_event_message(action, self._language)
            _LOGGER.info("SQS instant: %s -> %s", source_name, message)
            return True

        _LOGGER.warning("SQS: Door device %s not found", source_name)
        return False

    async def _handle_motion_event(self, space, event_tag: str, source_name: str, source_id: str) -> bool:
        """Handle motion detection events."""
        event_data = MOTION_EVENTS.get(event_tag)
        if event_data is None:
            return False
        action, is_motion = event_data

        device = self._find_device(space, source_name, source_id)
        if device:
            device.attributes["motion"] = is_motion
            device.last_trigger_time = datetime.now(UTC) if is_motion else None
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

        _LOGGER.warning("SQS: Motion device %s not found", source_name)
        return False

    async def _handle_alarm_event(
        self, space, alarm_type: str, event_tag: str, source_name: str, source_id: str
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

            device.attributes[f"{alarm_type}_alarm"] = is_alarm
            device.last_trigger_time = datetime.now(UTC) if is_alarm else None

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

        _LOGGER.warning("SQS: Alarm device %s not found", source_name)
        return False

    async def _handle_relay_event(self, space, event_tag: str, source_name: str, source_id: str) -> bool:
        """Handle relay/socket/light on/off events."""
        event_data = RELAY_EVENTS.get(event_tag)
        if event_data is None:
            return False
        action, is_on = event_data

        device = self._find_device(space, source_name, source_id)
        if device:
            device.attributes["state"] = is_on
            message = get_event_message(action, self._language)
            _LOGGER.info("SQS instant: %s -> %s", source_name, message)
            return True

        _LOGGER.warning("SQS: Relay device %s not found", source_name)
        return False

    async def _handle_wire_input_event(
        self, space, event_tag: str, source_name: str, source_id: str, transition: str
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
            device.last_trigger_time = datetime.now(UTC) if is_triggered else None

            message = get_event_message(action, self._language)
            _LOGGER.info("SQS instant: %s -> %s (wire input)", source_name, message)
            return True

        _LOGGER.warning("SQS: WireInput device %s (id=%s) not found", source_name, source_id)
        return False

    async def _handle_button_event(self, space, event_tag: str, source_name: str, source_id: str) -> bool:
        """Handle button press events."""
        event_data = BUTTON_EVENTS.get(event_tag)
        if event_data is None:
            return False
        action, _ = event_data

        device = self._find_device(space, source_name, source_id)
        if device:
            # Store the last action in device attributes
            device.attributes["last_action"] = action
            device.last_trigger_time = datetime.now(UTC)

            # Fire a Home Assistant event for automations
            self.coordinator.hass.bus.async_fire(
                "ajax_button_pressed",
                {
                    "device_id": device.id,
                    "device_name": device.name,
                    "action": action,
                    "space_name": space.name,
                },
            )

            message = get_event_message(action, self._language)
            _LOGGER.info("SQS instant: %s -> %s (button)", source_name, message)
            return True

        _LOGGER.warning("SQS: Button device %s not found", source_name)
        return False

    async def _handle_doorbell_event(self, space, event_tag: str, source_name: str, source_id: str) -> bool:
        """Handle doorbell ring events."""
        device = self._find_device(space, source_name, source_id)
        if device:
            # Store the last ring time in device attributes
            device.attributes["last_ring"] = datetime.now(UTC).isoformat()
            device.last_trigger_time = datetime.now(UTC)

            # Set the doorbell_ring state to True (will auto-reset)
            device.attributes["doorbell_ring"] = True

            # Fire a Home Assistant event for automations
            self.coordinator.hass.bus.async_fire(
                "ajax_doorbell_ring",
                {
                    "device_id": device.id,
                    "device_name": device.name,
                    "space_name": space.name,
                },
            )

            _LOGGER.info("SQS instant: %s -> doorbell ring", source_name)

            # Schedule auto-reset of doorbell_ring state after 10 seconds
            self.coordinator.hass.loop.call_later(
                10.0,
                lambda: self._reset_doorbell_ring(space.id, device.id),
            )

            return True

        _LOGGER.warning("SQS: Doorbell device %s not found", source_name)
        return False

    def _reset_doorbell_ring(self, space_id: str, device_id: str) -> None:
        """Reset doorbell ring state after timeout."""
        try:
            if not self.coordinator.account:
                return

            space = self.coordinator.account.spaces.get(space_id)
            if not space:
                return

            device = space.devices.get(device_id)
            if device:
                device.attributes["doorbell_ring"] = False
                _LOGGER.debug("Doorbell ring auto-reset: %s", device.name)
                self.coordinator.async_set_updated_data(self.coordinator.account)
        except Exception as err:
            _LOGGER.debug("Error resetting doorbell ring: %s", err)

    async def _handle_scenario_event(self, space, event_tag: str, source_name: str, additional_data_v2: list) -> bool:
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
            "ajax_scenario_triggered",
            {
                "scenario_name": initiator_name,
                "initiator_type": initiator_type,
                "target_name": source_name,
                "event_tag": event_tag,
                "space_name": space.name,
            },
        )

        return True

    async def _handle_device_status_event(self, space, event_tag: str, source_name: str, source_id: str) -> bool:
        """Handle device status events (online/offline, battery, tamper)."""
        device = self._find_device(space, source_name, source_id)
        if not device:
            _LOGGER.warning("SQS: Device %s not found for status event", source_name)
            return False

        action = event_tag
        if event_tag in TAMPER_EVENTS:
            action, is_tampered = TAMPER_EVENTS[event_tag]
            device.attributes["tampered"] = is_tampered
        elif event_tag in DEVICE_STATUS_EVENTS:
            action, _ = DEVICE_STATUS_EVENTS[event_tag]
            if event_tag == "online":
                device.online = True
            elif event_tag == "offline":
                device.online = False
            elif event_tag == "lowbattery":
                device.attributes["low_battery"] = True
            elif event_tag == "batterycharged":
                device.attributes["low_battery"] = False

        message = get_event_message(action, self._language)
        _LOGGER.info("SQS instant: %s - %s", source_name, message)
        return True

    def _find_device(self, space, source_name: str, source_id: str):
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
        return None

    async def _create_alarm_notification(self, space, event_record: dict[str, Any]) -> None:
        """Create a Home Assistant notification for alarm events.

        Respects user notification preferences:
        - NONE: no notifications
        - ALARMS_ONLY: show alarm notifications (this method)
        - SECURITY_EVENTS: show alarm + arm/disarm notifications
        - ALL: show all notifications
        """
        from homeassistant.components.persistent_notification import async_create

        from .const import (
            CONF_NOTIFICATION_FILTER,
            CONF_PERSISTENT_NOTIFICATION,
            NOTIFICATION_FILTER_NONE,
        )

        # Check notification filter settings
        options = self.coordinator.config_entry.options if self.coordinator.config_entry else {}

        # Check if persistent notifications are enabled
        if not options.get(CONF_PERSISTENT_NOTIFICATION, True):
            return

        # Check notification filter - alarm notifications are shown for all filters except NONE
        notification_filter = options.get(CONF_NOTIFICATION_FILTER, NOTIFICATION_FILTER_NONE)
        if notification_filter == NOTIFICATION_FILTER_NONE:
            return

        source = event_record.get("source_name", "")
        room = event_record.get("room_name", "")
        message = event_record.get("message", "")

        # Build notification message
        parts = [f"**{message}**"]
        if source:
            parts.append(f"Source: {source}")
        if room:
            parts.append(f"Room: {room}")

        notification_message = "\n".join(parts)

        async_create(
            self.coordinator.hass,
            notification_message,
            title=f"🚨 Ajax - {space.name}",
            notification_id=f"ajax_alarm_{space.id}_{time.time()}",
        )

    async def _handle_video_event(
        self,
        space,
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

        _LOGGER.info(
            "SQS instant: %s -> %s detected (channel=%s)",
            video_edge.name,
            detection_type,
            channel_id or "default",
        )

        # Schedule auto-reset after detection timeout (30 seconds)
        self.coordinator.hass.loop.call_later(
            30.0,
            lambda: self._reset_video_detection(space.id, video_edge.id, channel_id, detection_type),
        )

        return True

    async def _handle_lock_event(
        self,
        space,
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
                self.coordinator.hass.async_create_task(self.coordinator._async_save_smart_locks())
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

        if event_tag in LOCK_DOOR_EVENTS:
            # Door open/close — use event code mapping
            if event_code_upper in LOCK_DOOR_EVENT_CODE_STATES:
                smart_lock.is_door_open = LOCK_DOOR_EVENT_CODE_STATES[event_code_upper]
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

    def _find_video_edge(self, space, source_name: str, source_id: str):
        """Find video edge device by name or ID.

        Returns a tuple of (video_edge, channel_id).
        For NVR devices, source_id might be the channel ID.
        """
        # Try by exact ID match first
        if source_id:
            if source_id in space.video_edges:
                return space.video_edges[source_id], None

            # For NVR: the source_id might be a channel ID
            # Try to find the video edge that has this channel
            for video_edge in space.video_edges.values():
                for channel in video_edge.channels:
                    if isinstance(channel, dict) and channel.get("id") == source_id:
                        return video_edge, source_id

        # Fall back to name match
        if source_name:
            for video_edge in space.video_edges.values():
                if video_edge.name == source_name:
                    return video_edge, None

                # For NVR: check channel names
                for channel in video_edge.channels:
                    if isinstance(channel, dict) and channel.get("name") == source_name:
                        return video_edge, channel.get("id")

        return None, None

    def _update_video_detection(
        self,
        video_edge,
        channel_id: str | None,
        detection_type: str,
        active: bool,
    ) -> None:
        """Update video edge channel state with detection.

        The detection is stored in the channel's state array as:
        {"type": "VIDEO_MOTION", "active": True}
        """
        channels = video_edge.channels
        if not isinstance(channels, list):
            return

        # Find target channel (first one if no channel_id specified)
        target_channel = None
        for channel in channels:
            if isinstance(channel, dict) and (channel_id is None or channel.get("id") == channel_id):
                target_channel = channel
                break

        if not target_channel:
            # Create a default channel if none exists
            if channel_id is None and not channels:
                target_channel = {"id": "0", "state": []}
                channels.append(target_channel)
            else:
                return

        # Ensure state is a list
        if not isinstance(target_channel.get("state"), list):
            target_channel["state"] = []

        # Find existing detection entry or create new one
        state_list = target_channel["state"]
        detection_entry = None
        for entry in state_list:
            if isinstance(entry, dict) and entry.get("type") == detection_type:
                detection_entry = entry
                break

        if detection_entry:
            detection_entry["active"] = active
        else:
            state_list.append({"type": detection_type, "active": active})

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

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def last_event_time(self) -> float:
        return self._last_event_time
