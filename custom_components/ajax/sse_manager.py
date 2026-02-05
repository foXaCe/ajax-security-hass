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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import SIGNAL_NEW_SMART_LOCK
from .event_codes import DEFAULT_LANGUAGE, parse_event_code
from .models import AjaxSmartLock
from .sqs_manager import (  # Import event mappings from SQS manager to avoid duplication
    DEVICE_STATUS_EVENTS,
    DOOR_EVENTS,
    DOORBELL_EVENTS,
    EVENT_TAG_TO_STATE,
    FLOOD_EVENTS,
    GLASS_EVENTS,
    LOCK_DOOR_EVENT_CODE_STATES,
    LOCK_DOOR_EVENTS,
    LOCK_EVENT_CODE_STATES,
    LOCK_EVENTS,
    MOTION_EVENTS,
    RELAY_EVENTS,
    SCENARIO_EVENTS,
    SMOKE_EVENTS,
    TAMPER_EVENTS,
    VIDEO_EVENT_TYPES,
    VIDEO_EVENTS,
)

if TYPE_CHECKING:
    from .coordinator import AjaxDataCoordinator
    from .sse_client import AjaxSSEClient

_LOGGER = logging.getLogger(__name__)


class SSEManager:
    """Manages SSE events from Ajax proxy."""

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
        self._dedup_window = 5  # seconds to ignore duplicate events

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

    async def stop(self) -> None:
        """Stop receiving SSE events."""
        _LOGGER.info("Stopping SSE Manager...")
        await self.sse_client.stop()
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
        return (time.time() - last_update) < 5

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

            _LOGGER.info(
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
            if event_tag in ("grouparm", "groupdisarm"):
                # Extract group ID from event data
                related_groups = event.get("additionalData", {}).get("relatedGroupsInfo", [])
                if related_groups:
                    group_id = related_groups[0].get("id")

            # Build deduplication key - include group_id for group events
            if group_id:
                event_key = f"{source_id}:{event_tag}:{group_id}:{transition}"
            else:
                event_key = f"{source_id}:{event_tag}:{transition}"

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
                await self._handle_security_event(space, event_tag, source_name)
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
            elif event_tag in SCENARIO_EVENTS:
                self._handle_scenario_event(space, event, event_tag)
            elif event_tag in VIDEO_EVENTS or event_type_v2 in VIDEO_EVENT_TYPES:
                self._handle_video_event(space, event_tag, event_type_v2, source_name, source_id)
            elif event_tag in DOORBELL_EVENTS:
                self._handle_doorbell_event(space, source_name, source_id)
            elif event_tag in LOCK_EVENTS or event_tag in LOCK_DOOR_EVENTS:
                self._handle_lock_event(space, event_tag, source_name, source_id, event_code, event)
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

    async def _handle_security_event(self, space, event_tag: str, source_name: str) -> None:
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

        # Check if this was triggered by Home Assistant
        if self.coordinator.get_pending_ha_action(space.hub_id):
            source_name = "Home Assistant"

        # Group arm/disarm events need a FULL refresh to update group states
        # because the final state depends on how many groups are armed
        is_group_event = event_tag in ("grouparm", "groupdisarm")

        # Full arm/disarm also affects all groups - need refresh to update them
        is_full_arm_disarm = event_tag in ("arm", "disarm")

        if is_group_event or is_full_arm_disarm:
            start_time = time.time()
            _LOGGER.info(
                "SSE: Security event '%s' detected for hub %s at t=0ms, refreshing groups",
                event_tag,
                space.hub_id,
            )
            # Small delay to let Ajax backend process the change
            # Reduced from 1.0s to 0.3s for faster real-time updates
            await asyncio.sleep(0.3)
            _LOGGER.debug("SSE: Sleep completed at t=%dms", int((time.time() - start_time) * 1000))
            try:
                # Set flag to skip event creation during refresh (SSE already created it)
                self.coordinator._skip_state_change_event = True
                # CRITICAL: Bypass proxy cache to get fresh group states from Ajax API
                self.coordinator._bypass_cache_next_refresh = True
                # Use async_force_metadata_refresh to ensure full_refresh=True
                # This updates groups, not just hub state
                await self.coordinator.async_force_metadata_refresh()
                elapsed = int((time.time() - start_time) * 1000)
                _LOGGER.info("SSE: Group refresh completed at t=%dms", elapsed)
            except Exception as err:
                _LOGGER.error("SSE: Metadata refresh failed after security event: %s", err)
            finally:
                # Always reset the flag
                self.coordinator._skip_state_change_event = False

        if state_changed and not is_group_event:
            space.security_state = new_state
            self._last_state_update[space.hub_id] = time.time()

        # Always create notification (even if state unchanged)
        _LOGGER.info(
            "SSE instant: %s -> %s par %s (state_changed=%s)",
            old_state.value,
            new_state.value,
            source_name or "inconnu",
            state_changed,
        )

        # Create notification
        await self.coordinator._create_sqs_notification(
            action=new_state.value,
            source_name=source_name,
            space_name=space.name,
        )

    def _find_device(self, space, source_name: str, source_id: str):
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

        # Fall back to name match
        if source_name:
            for device in space.devices.values():
                if device.name == source_name:
                    return device

        return None

    def _handle_door_event(self, space, event_tag: str, source_name: str, source_id: str, transition: str) -> None:
        """Handle door opened/closed events."""
        action_key, is_triggered = DOOR_EVENTS[event_tag]

        # Use transition to determine actual state
        if transition == "RECOVERED":
            is_triggered = False
        elif transition == "TRIGGERED":
            is_triggered = True

        dev = self._find_device(space, source_name, source_id)
        if dev:
            dev.attributes["door_opened"] = is_triggered
            dev.attributes["door_opened_at"] = datetime.now(UTC).isoformat()
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)
        else:
            _LOGGER.warning("SSE: Door device not found: name=%s, id=%s", source_name, source_id)

    def _handle_motion_event(self, space, event_tag: str, source_name: str, source_id: str) -> None:
        """Handle motion detected events."""
        from .models import SecurityState

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
                space.security_state = SecurityState.TRIGGERED
                _LOGGER.info(
                    "SSE: Alarm TRIGGERED by motion on %s (was %s)",
                    dev.name,
                    space.security_state.value,
                )
        else:
            _LOGGER.warning("SSE: Motion device not found: name=%s, id=%s", source_name, source_id)

    def _handle_smoke_event(self, space, event_tag: str, source_name: str, source_id: str) -> None:
        """Handle smoke/fire detector events."""
        from .models import SecurityState

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
            if is_triggered and space.security_state != SecurityState.TRIGGERED:
                space.security_state = SecurityState.TRIGGERED
                _LOGGER.info("SSE: Alarm TRIGGERED by smoke/fire on %s", dev.name)
        else:
            _LOGGER.warning("SSE: Smoke device not found: name=%s, id=%s", source_name, source_id)

    def _handle_flood_event(self, space, event_tag: str, source_name: str, source_id: str) -> None:
        """Handle flood/leak detector events."""
        from .models import SecurityState

        action_key, is_triggered = FLOOD_EVENTS[event_tag]

        dev = self._find_device(space, source_name, source_id)
        if dev:
            dev.attributes["leak_detected"] = is_triggered
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)

            # Flood alarms trigger regardless of arm state (life safety)
            if is_triggered and space.security_state != SecurityState.TRIGGERED:
                space.security_state = SecurityState.TRIGGERED
                _LOGGER.info("SSE: Alarm TRIGGERED by flood on %s", dev.name)
        else:
            _LOGGER.warning("SSE: Flood device not found: name=%s, id=%s", source_name, source_id)

    def _handle_glass_event(self, space, event_tag: str, source_name: str, source_id: str) -> None:
        """Handle glass break events."""
        from .models import SecurityState

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
        else:
            _LOGGER.warning("SSE: Glass device not found: name=%s, id=%s", source_name, source_id)

    def _handle_tamper_event(self, space, event_tag: str, source_name: str, source_id: str, transition: str) -> None:
        """Handle tamper events."""
        action_key, is_triggered = TAMPER_EVENTS[event_tag]

        # Use transition to determine actual state (like door events)
        if transition == "RECOVERED":
            is_triggered = False
        elif transition == "TRIGGERED":
            is_triggered = True

        dev = self._find_device(space, source_name, source_id)
        if dev:
            dev.attributes["tampered"] = is_triggered
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

    def _handle_device_status_event(self, space, event_tag: str, source_name: str, source_id: str) -> None:
        """Handle device status events (online/offline, battery)."""
        action_key, is_problem = DEVICE_STATUS_EVENTS[event_tag]

        dev = self._find_device(space, source_name, source_id)
        if dev:
            if "online" in action_key or "offline" in action_key:
                dev.online = not is_problem
            elif "battery" in action_key:
                dev.attributes["low_battery"] = is_problem
            elif "power" in action_key:
                dev.attributes["external_power_lost"] = is_problem
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)
        else:
            _LOGGER.warning(
                "SSE: Device not found for status: name=%s, id=%s",
                source_name,
                source_id,
            )

    def _handle_relay_event(self, space, event_tag: str, source_name: str, source_id: str) -> None:
        """Handle relay/socket on/off events."""
        action_key, is_on = RELAY_EVENTS[event_tag]

        dev = self._find_device(space, source_name, source_id)
        if dev:
            dev.attributes["is_on"] = is_on
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)
        else:
            _LOGGER.warning("SSE: Relay device not found: name=%s, id=%s", source_name, source_id)

    def _handle_doorbell_event(self, space, source_name: str, source_id: str) -> None:
        """Handle doorbell ring events."""
        dev = self._find_device(space, source_name, source_id)
        if dev:
            # Store the last ring time in device attributes
            dev.attributes["last_ring"] = datetime.now(UTC).isoformat()
            dev.last_trigger_time = datetime.now(UTC)

            # Set the doorbell_ring state to True (will auto-reset)
            dev.attributes["doorbell_ring"] = True

            # Fire a Home Assistant event for automations
            self.coordinator.hass.bus.async_fire(
                "ajax_doorbell_ring",
                {
                    "device_id": dev.id,
                    "device_name": dev.name,
                    "space_name": space.name,
                },
            )

            _LOGGER.info("SSE instant: %s -> doorbell ring", dev.name)

            # Schedule auto-reset of doorbell_ring state after 10 seconds
            self.coordinator.hass.loop.call_later(
                10.0,
                lambda: self._reset_doorbell_ring(space.id, dev.id),
            )
        else:
            _LOGGER.warning("SSE: Doorbell device not found: name=%s, id=%s", source_name, source_id)

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

    def _handle_lock_event(
        self,
        space,
        event_tag: str,
        source_name: str,
        source_id: str,
        event_code: str,
        event: dict,
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

        if event_tag in LOCK_DOOR_EVENTS:
            if event_code_upper in LOCK_DOOR_EVENT_CODE_STATES:
                smart_lock.is_door_open = LOCK_DOOR_EVENT_CODE_STATES[event_code_upper]
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

    def _handle_scenario_event(self, space, event: dict, event_tag: str) -> None:
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
            "ajax_scenario_triggered",
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
        space,
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

        _LOGGER.info(
            "SSE instant: %s -> %s detected (channel=%s)",
            video_edge.name,
            detection_type,
            channel_id or "default",
        )

        # Schedule auto-reset after detection timeout (30 seconds)
        self.coordinator.hass.loop.call_later(
            30.0,
            lambda: self._reset_video_detection(space.id, video_edge.id, channel_id, detection_type),
        )

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
            if self.coordinator.account is not None:
                self.coordinator.async_set_updated_data(self.coordinator.account)

        except Exception as err:
            _LOGGER.debug("Error resetting video detection: %s", err)
