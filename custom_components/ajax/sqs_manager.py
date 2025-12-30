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

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .event_codes import (
    DEFAULT_LANGUAGE,
    get_event_message,
    get_event_type_description,
    parse_event_code,
)
from .models import SecurityState

if TYPE_CHECKING:
    from .coordinator import AjaxDataCoordinator
    from .sqs_client import AjaxSQSClient

_LOGGER = logging.getLogger(__name__)

# Map SQS event tags to SecurityState
EVENT_TAG_TO_STATE = {
    "arm": SecurityState.ARMED,
    "disarm": SecurityState.DISARMED,
    "nightmodeon": SecurityState.NIGHT_MODE,
    "nightmodeoff": SecurityState.DISARMED,
    "partialarm": SecurityState.PARTIALLY_ARMED,
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

TAMPER_EVENTS = {
    "lidopen": ("tamper_open", True),
    "lidclosed": ("tamper_closed", False),
    "tampered": ("tamper_open", True),
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
}

# Map event tags to action keys for security events
SECURITY_EVENT_ACTIONS = {
    "arm": "armed",
    "disarm": "disarmed",
    "nightmodeon": "night_mode",
    "nightmodeoff": "night_mode_off",
    "partialarm": "armed",
}


class SQSManager:
    """Manager for AWS SQS real-time event integration."""

    # Don't let REST overwrite SQS state for this many seconds
    STATE_PROTECTION_SECONDS = 15.0
    # Maximum events to keep in history
    MAX_EVENTS_HISTORY = 10

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
        self._language: str = DEFAULT_LANGUAGE

    def set_language(self, language: str) -> None:
        """Set the language for event messages."""
        self._language = (
            language if language in ("fr", "en", "es") else DEFAULT_LANGUAGE
        )
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

            _LOGGER.info(
                "SQS event: type=%s, tag=%s, code=%s, source=%s (%s), transition=%s",
                event_type,
                event_tag,
                event_code,
                source_name,
                source_type,
                transition,
            )

            if not hub_id or not event_tag:
                _LOGGER.debug("SQS event missing hubId or eventTag")
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
                await self._handle_door_event(space, event_tag, source_name, source_id)
            elif event_tag in MOTION_EVENTS:
                await self._handle_motion_event(
                    space, event_tag, source_name, source_id
                )
            elif event_tag in SMOKE_EVENTS:
                await self._handle_alarm_event(
                    space, "smoke", event_tag, source_name, source_id
                )
            elif event_tag in FLOOD_EVENTS:
                await self._handle_alarm_event(
                    space, "flood", event_tag, source_name, source_id
                )
            elif event_tag in GLASS_EVENTS:
                await self._handle_alarm_event(
                    space, "glass", event_tag, source_name, source_id
                )
            elif event_tag in RELAY_EVENTS:
                await self._handle_relay_event(space, event_tag, source_name, source_id)
            elif event_tag in TAMPER_EVENTS or event_tag in DEVICE_STATUS_EVENTS:
                await self._handle_device_status_event(
                    space, event_tag, source_name, source_id
                )
            else:
                _LOGGER.debug("SQS event %s not handled specifically", event_tag)

            # Create notification if it's an alarm event
            if (
                event_type == "ALARM"
                or event_tag in SMOKE_EVENTS
                or event_tag in FLOOD_EVENTS
            ):
                await self._create_alarm_notification(space, event_record)

            # Always update UI to show new event in history
            self.coordinator.async_set_updated_data(self.coordinator.account)

        except Exception as err:
            _LOGGER.error("Error handling SQS event: %s", err)

    def _find_space(self, hub_id: str):
        """Find space by hub ID."""
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
            for events_dict in [
                DOOR_EVENTS,
                MOTION_EVENTS,
                SMOKE_EVENTS,
                FLOOD_EVENTS,
                GLASS_EVENTS,
                RELAY_EVENTS,
                TAMPER_EVENTS,
                DEVICE_STATUS_EVENTS,
                SECURITY_EVENT_ACTIONS,
            ]:
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
            "timestamp": datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
            if timestamp
            else datetime.now(timezone.utc),
            "transition": transition,
        }

    def _add_event_to_history(self, space, event_record: dict[str, Any]) -> None:
        """Add event to space's recent events history."""
        # Insert at beginning (most recent first)
        space.recent_events.insert(0, event_record)
        # Keep only last N events
        if len(space.recent_events) > self.MAX_EVENTS_HISTORY:
            space.recent_events = space.recent_events[: self.MAX_EVENTS_HISTORY]

    async def _handle_security_event(
        self, space, event_tag: str, source_name: str
    ) -> bool:
        """Handle arm/disarm/night mode events."""
        new_state = EVENT_TAG_TO_STATE.get(event_tag)
        if not new_state:
            _LOGGER.debug("SQS security: unknown event_tag=%s", event_tag)
            return False

        old_state = space.security_state
        state_changed = old_state != new_state

        _LOGGER.info(
            "SQS security: tag=%s, old=%s, new=%s, changed=%s",
            event_tag,
            old_state.value,
            new_state.value,
            state_changed,
        )

        # Check if this was triggered by Home Assistant
        if self.coordinator.get_pending_ha_action(space.hub_id):
            source_name = "Home Assistant"

        # Update state if changed
        if state_changed:
            space.security_state = new_state
            self._last_state_update[space.hub_id] = time.time()

        # Always create notification from SQS (even if state unchanged)
        # because SQS contains user info that REST doesn't have
        _LOGGER.info(
            "SQS instant: %s -> %s par %s (state_changed=%s)",
            old_state.value,
            new_state.value,
            source_name or "inconnu",
            state_changed,
        )

        await self.coordinator._create_sqs_notification(
            action=new_state.value,
            source_name=source_name,
            space_name=space.name,
        )

        return True

    async def _handle_door_event(
        self, space, event_tag: str, source_name: str, source_id: str
    ) -> bool:
        """Handle door open/close events."""
        event_data = DOOR_EVENTS.get(event_tag)
        if event_data is None:
            return False
        action, is_open = event_data

        # Find and update the device
        device = self._find_device(space, source_name, source_id)
        if device:
            device.attributes["door_opened"] = is_open
            device.last_trigger_time = datetime.now(timezone.utc) if is_open else None
            message = get_event_message(action, self._language)
            _LOGGER.info("SQS instant: %s -> %s", source_name, message)
            return True

        _LOGGER.warning("SQS: Door device %s not found", source_name)
        return False

    async def _handle_motion_event(
        self, space, event_tag: str, source_name: str, source_id: str
    ) -> bool:
        """Handle motion detection events."""
        event_data = MOTION_EVENTS.get(event_tag)
        if event_data is None:
            return False
        action, is_motion = event_data

        device = self._find_device(space, source_name, source_id)
        if device:
            device.attributes["motion"] = is_motion
            device.last_trigger_time = datetime.now(timezone.utc) if is_motion else None
            message = get_event_message(action, self._language)
            _LOGGER.info("SQS instant: %s -> %s", source_name, message)
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
            device.last_trigger_time = datetime.now(timezone.utc) if is_alarm else None

            message = get_event_message(action, self._language)
            _LOGGER.info("SQS instant: %s - %s", source_name, message)
            return True

        _LOGGER.warning("SQS: Alarm device %s not found", source_name)
        return False

    async def _handle_relay_event(
        self, space, event_tag: str, source_name: str, source_id: str
    ) -> bool:
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

    async def _handle_device_status_event(
        self, space, event_tag: str, source_name: str, source_id: str
    ) -> bool:
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
        """Find device by name or ID."""
        # Try by ID first
        if source_id:
            for device in space.devices.values():
                if device.id == source_id:
                    return device
        # Fall back to name
        if source_name:
            for device in space.devices.values():
                if device.name == source_name:
                    return device
        return None

    async def _create_alarm_notification(
        self, space, event_record: dict[str, Any]
    ) -> None:
        """Create a Home Assistant notification for alarm events."""
        from homeassistant.components.persistent_notification import async_create

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

    def is_state_protected(self, hub_id: str) -> bool:
        """Check if state was recently updated by SQS (protected from REST overwrite)."""
        last_update = self._last_state_update.get(hub_id, 0)
        elapsed = time.time() - last_update
        is_protected = elapsed < self.STATE_PROTECTION_SECONDS
        if is_protected:
            _LOGGER.debug(
                "Hub %s state protected (%.1fs since SQS update)", hub_id, elapsed
            )
        return is_protected

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def last_event_time(self) -> float:
        return self._last_event_time
