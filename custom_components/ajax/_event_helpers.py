"""Shared helpers for SSE and SQS event managers.

Both `sse_manager.AjaxSSEManager` and `sqs_manager.AjaxSQSManager` need
identical plumbing around video edges, doorbell rings and channel state
updates. The `EventHandlerMixin` provides a single, canonical
implementation so they stay consistent.

The mixin only relies on ``self.coordinator`` being available on the
subclass, which is true for both managers.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from .const import EVENT_AJAX_CAMERA_DETECTION, EVENT_AJAX_UNHANDLED_EVENT
from .event_codes import parse_event_code
from .event_maps import (
    ACCELEROMETER_EVENT_CODES,
    ACCELEROMETER_EVENTS,
    ACCELEROMETER_RESET_SECONDS,
    ALARM_EVENT_TYPE,
    ARMED_SECURITY_STATES,
    DEVICE_STATUS_EVENTS,
    TAMPER_EVENTS,
)
from .models import SecurityState

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import AjaxDataCoordinator  # noqa: F401
    from .models import AjaxDevice, AjaxSpace, AjaxVideoEdge

_LOGGER = logging.getLogger(__name__)


def resolve_camera_entity_id(hass: HomeAssistant, video_edge_id: str) -> str | None:
    """Resolve the main-stream camera entity_id for a video_edge.

    Returns the standalone camera first; falls back to the first NVR
    channel. Used to attach ``camera_entity_id`` / ``snapshot_url`` to
    detection bus events so automations can fire `camera.snapshot` or
    embed `/api/camera_proxy/...` directly.
    """
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    from .const import DOMAIN  # noqa: PLC0415

    registry = er.async_get(hass)
    for unique_id in (
        f"{video_edge_id}_camera_main",
        f"{video_edge_id}_camera_ch0_main",
    ):
        entity_id = registry.async_get_entity_id("camera", DOMAIN, unique_id)
        if entity_id:
            return entity_id
    return None


VIDEO_DETECTION_EVENT_TYPES: dict[str, str] = {
    "VIDEO_MOTION": "motion",
    "VIDEO_HUMAN": "human",
    "VIDEO_VEHICLE": "vehicle",
    "VIDEO_PET": "pet",
    "VIDEO_LINE_CROSSING": "line_crossing",
}

# Minimum delay between two discovery refreshes triggered by an unknown
# device id seen in an SSE/SQS event — avoids hammering the API when a
# burst of events references a device the integration doesn't know yet.
_DISCOVERY_REFRESH_THROTTLE = 60.0


class EventHandlerMixin:
    """Shared device/video-edge lookup and state-update helpers.

    Subclasses are expected to expose ``self.coordinator`` (an
    ``AjaxDataCoordinator``). All methods are intentionally synchronous
    since they only touch in-memory coordinator state.
    """

    coordinator: AjaxDataCoordinator
    _last_discovery_refresh: float = 0.0
    # Provided by each manager (tracked hass.loop.call_later).
    _schedule_later: Callable[[float, Callable[[], Any]], None]
    _find_device: Callable[[AjaxSpace, str, str], AjaxDevice | None]

    @staticmethod
    def _alarm_type(event: dict[str, Any]) -> str:
        """Ajax's event classification, ``ALARM`` if either field says so.

        Ajax sets ``eventTypeV2`` and/or ``eventType``; a non-alarm value in
        one must not hide ``ALARM`` in the other.
        """
        types = (event.get("eventTypeV2") or "", event.get("eventType") or "")
        if ALARM_EVENT_TYPE in types:
            return ALARM_EVENT_TYPE
        return types[0] or types[1]

    @staticmethod
    def _is_accelerometer_event(event_tag: str, event_code: str) -> bool:
        return event_tag in ACCELEROMETER_EVENTS or event_code in ACCELEROMETER_EVENT_CODES

    def _trigger_space_alarm(self, space: AjaxSpace, reason: str, source_name: str) -> None:
        """Put the space panel in TRIGGERED (idempotent)."""
        if space.security_state == SecurityState.TRIGGERED:
            return
        previous = space.security_state
        space.security_state = SecurityState.TRIGGERED
        _LOGGER.info("Alarm TRIGGERED by %s on %s (was %s)", reason, source_name, previous.value)

    def _handle_accelerometer_event(
        self,
        space: AjaxSpace,
        event_tag: str,
        event_code: str,
        event_type: str,
        source_name: str,
        source_id: str,
    ) -> tuple[str, bool]:
        """Tilt/shock on a DoorProtect Plus (#246); return (action key, is alarm).

        These used to fall through as "not handled": the tilt/shock binary
        sensors never turned on and a real alarm left the panel untouched.
        The sensor is set and cleared after ``ACCELEROMETER_RESET_SECONDS``
        (Ajax sends no "restored" event). The panel triggers when Ajax
        classes the event as an alarm, or when the space is armed.
        """
        action = ACCELEROMETER_EVENTS.get(event_tag) or ACCELEROMETER_EVENT_CODES[event_code]
        device = self._find_device(space, source_name, source_id)
        if device is not None:
            stamp = datetime.now(UTC).isoformat()
            device.attributes[action] = True
            device.attributes[f"{action}_at"] = stamp
            # A later event re-stamps the flag; this timer then no-ops, so the
            # sensor stays on for the full delay after the LAST event.
            self._schedule_later(
                ACCELEROMETER_RESET_SECONDS,
                partial(self._reset_device_flag, space.id, device.id, action, stamp),
            )
            source_name = device.name
        else:
            _LOGGER.debug("Accelerometer device not found: name=%s, id=%s", source_name, source_id)

        is_alarm = event_type == ALARM_EVENT_TYPE or space.security_state in ARMED_SECURITY_STATES
        if is_alarm:
            self._trigger_space_alarm(space, action, source_name)
        _LOGGER.info("Real-time: %s -> %s", source_name, action)
        return action, is_alarm

    def _reset_device_flag(self, space_id: str, device_id: str, attribute: str, stamp: str | None = None) -> None:
        """Clear a transient device flag set by an IMPULSE event.

        With ``stamp``, only if no newer event re-armed the flag since.
        """
        try:
            if not self.coordinator.account:
                return
            space = self.coordinator.account.spaces.get(space_id)
            device = space.devices.get(device_id) if space else None
            if stamp is not None and device is not None and device.attributes.get(f"{attribute}_at") != stamp:
                return
            if device is not None and device.attributes.get(attribute):
                device.attributes[attribute] = False
                _LOGGER.debug("Auto-reset %s on %s", attribute, device.name)
                self.coordinator.async_set_updated_data(self.coordinator.account)
        except Exception as err:  # noqa: BLE001 — best-effort reset
            _LOGGER.debug("Error resetting %s: %s", attribute, err)

    def _handle_unmapped_event(
        self,
        space: AjaxSpace,
        event: dict[str, Any],
        *,
        transport: str,
        event_tag: str,
        event_code: str,
        event_type: str,
        source_name: str,
        source_id: str,
        source_type: str,
    ) -> str | None:
        """Event with no handler: never drop an alarm silently (#246).

        Returns the action key when Ajax classed it as an alarm (the panel
        is then triggered), else ``None``. Every unmapped event is also
        fired on the bus as ``ajax_unhandled_event``.
        """
        is_alarm = event_type == ALARM_EVENT_TYPE
        (_LOGGER.error if is_alarm else _LOGGER.warning)(
            "%s event not handled%s: tag=%s, type=%s, code=%s, source=%s (%s, id=%s). Raw: %s",
            transport,
            " — treated as a generic alarm" if is_alarm else "",
            event_tag,
            event_type or "none",
            event_code or "none",
            source_name,
            source_type,
            source_id,
            event,
        )
        self.coordinator.hass.bus.async_fire(
            EVENT_AJAX_UNHANDLED_EVENT,
            {
                "space_id": space.id,
                "hub_id": space.hub_id,
                "event_tag": event_tag,
                "event_type": event_type,
                "event_code": event_code,
                "transition": event.get("transition", ""),
                "source_id": source_id,
                "source_name": source_name,
                "source_type": source_type,
                "room_name": event.get("sourceRoomName", ""),
            },
        )
        if not is_alarm:
            return None
        code_info = parse_event_code(event_code) if event_code else None
        action = str(code_info.get("action") or "") if code_info else ""
        if action in ("", "unknown"):
            action = "alarm"
        self._trigger_space_alarm(space, action, source_name)
        return action

    @staticmethod
    def _apply_tamper_state(device: AjaxDevice, event_tag: str, transition: str) -> str:
        """Mutate ``device`` for a tamper event; return the action key.

        Single source of truth for BOTH transports. The tag alone is
        ambiguous — Ajax reuses ``tamperopened`` for the lid opening AND
        closing — so the ``transition`` field (TRIGGERED/RECOVERED)
        overrides the static tuple, exactly like the door handlers.
        """
        action_key, is_triggered = TAMPER_EVENTS[event_tag]
        if transition == "RECOVERED":
            is_triggered = False
        elif transition == "TRIGGERED":
            is_triggered = True
        device.attributes["tampered"] = is_triggered
        return action_key

    @staticmethod
    def _apply_device_status(device: AjaxDevice, event_tag: str) -> str:
        """Mutate ``device`` for a status event; return the action key.

        Single source of truth for BOTH transports: online/offline,
        battery, and external power (the one key the REST poller also
        writes, hence the optimistic reservation).
        """
        action_key, is_problem = DEVICE_STATUS_EVENTS[event_tag]
        if event_tag in ("online", "offline"):
            device.online = not is_problem
        elif event_tag in ("lowbattery", "batterycharged"):
            device.attributes["low_battery"] = is_problem
        elif event_tag in ("externalpowerdisconnected", "externalpowerrestored"):
            # ``externally_powered`` True = mains present. Reserve it against
            # the next REST poll overwriting the fresher realtime value.
            device.attributes["externally_powered"] = not is_problem
            device.mark_optimistic("externally_powered", 15.0)
        return action_key

    def _request_discovery_refresh(self, source_id: str) -> None:
        """Ask the coordinator to refresh when an unknown device appears.

        SSE/SQS can reference a device added to the Ajax account since the
        last poll. A normal `async_request_refresh` lets `_async_update_devices`
        pick it up and emit `SIGNAL_NEW_DEVICE` on the next cycle, instead of
        waiting up to an hour for the next full metadata refresh. Throttled so
        a burst of events for a genuinely-absent id cannot spam the API.
        """
        if not source_id:
            return
        now = time.time()
        if now - self._last_discovery_refresh < _DISCOVERY_REFRESH_THROTTLE:
            return
        self._last_discovery_refresh = now
        _LOGGER.debug("Unknown device id=%s in event — requesting discovery refresh", source_id)
        # Bump the diagnostics counter only when we actually fire the
        # refresh (post-throttle); 'attempts' would mostly count duplicate
        # event bursts and drown the useful signal.
        stats = getattr(self.coordinator, "stats", None)
        if stats is not None:
            stats["discovery_refreshes"] = stats.get("discovery_refreshes", 0) + 1
        self.coordinator.hass.async_create_task(self.coordinator.async_request_refresh())

    def _find_video_edge(
        self, space: AjaxSpace, source_name: str, source_id: str
    ) -> tuple[AjaxVideoEdge | None, str | None]:
        """Locate a video edge (camera / NVR) from event metadata.

        Returns ``(video_edge, channel_id)`` — ``channel_id`` is non-None
        when the match is done through an NVR channel (either by ID or by
        the channel's ``name`` field).
        """
        if source_id:
            if source_id in space.video_edges:
                return space.video_edges[source_id], None

            # For NVR: the source_id might be a channel ID
            for video_edge in space.video_edges.values():
                for channel in video_edge.channels:
                    if isinstance(channel, dict) and channel.get("id") == source_id:
                        return video_edge, source_id

        if source_name:
            for video_edge in space.video_edges.values():
                if video_edge.name == source_name:
                    return video_edge, None
                for channel in video_edge.channels:
                    if isinstance(channel, dict) and channel.get("name") == source_name:
                        return video_edge, channel.get("id")

        return None, None

    def _update_video_detection(
        self,
        video_edge: AjaxVideoEdge,
        channel_id: str | None,
        detection_type: str,
        active: bool,
    ) -> None:
        """Mark ``detection_type`` as (in)active on ``video_edge``'s channel."""
        channels = video_edge.channels
        if not isinstance(channels, list):
            return

        target_channel: dict[str, Any] | None = None
        for channel in channels:
            if isinstance(channel, dict) and (channel_id is None or channel.get("id") == channel_id):
                target_channel = channel
                break

        if not target_channel:
            if channel_id is None and not channels:
                target_channel = {"id": "0", "state": []}
                channels.append(target_channel)
            else:
                return

        if not isinstance(target_channel.get("state"), list):
            target_channel["state"] = []

        state_list = target_channel["state"]
        for entry in state_list:
            if isinstance(entry, dict) and entry.get("type") == detection_type:
                entry["active"] = active
                return
        state_list.append({"type": detection_type, "active": active})

    def _fire_video_detection_event(self, video_edge: AjaxVideoEdge, detection_type: str) -> None:
        """Fire HA event entity for a video AI detection.

        Mirrors what doorbell ring does (`_handle_doorbell_event`) so that
        the `event.<camera>_detection` entity actually triggers when the
        cloud reports motion/human/vehicle/pet via SSE or SQS — without
        this the entity stays mute outside of ONVIF.

        Also fires the ``ajax_camera_detection`` bus event so the logbook
        can show a meaningful "<camera> a détecté <type>" line instead of
        HA's generic "a détecté un événement" fallback.
        """
        event_type = VIDEO_DETECTION_EVENT_TYPES.get(detection_type)
        if not event_type:
            return
        event_entity = self.coordinator._event_entities.get(f"{video_edge.id}_detection")
        if event_entity is not None:
            event_entity.fire(event_type)

        bus_data: dict[str, str] = {
            "device_id": video_edge.id,
            "device_name": video_edge.name,
            "event_type": event_type,
        }
        if event_entity is not None and event_entity.entity_id:
            bus_data["entity_id"] = event_entity.entity_id
        camera_entity_id = resolve_camera_entity_id(self.coordinator.hass, video_edge.id)
        if camera_entity_id:
            bus_data["camera_entity_id"] = camera_entity_id
            bus_data["snapshot_url"] = f"/api/camera_proxy/{camera_entity_id}"
        self.coordinator.hass.bus.async_fire(EVENT_AJAX_CAMERA_DETECTION, bus_data)

    def _reset_doorbell_ring(self, space_id: str, device_id: str) -> None:
        """Clear the transient ``doorbell_ring`` flag for a device."""
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
        except Exception as err:  # noqa: BLE001 — best-effort reset
            _LOGGER.debug("Error resetting doorbell ring: %s", err)
