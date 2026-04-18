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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .coordinator import AjaxDataCoordinator  # noqa: F401
    from .models import AjaxSpace, AjaxVideoEdge

_LOGGER = logging.getLogger(__name__)

VIDEO_DETECTION_EVENT_TYPES: dict[str, str] = {
    "VIDEO_MOTION": "motion",
    "VIDEO_HUMAN": "human",
    "VIDEO_VEHICLE": "vehicle",
    "VIDEO_PET": "pet",
    "VIDEO_LINE_CROSSING": "line_crossing",
}


class EventHandlerMixin:
    """Shared device/video-edge lookup and state-update helpers.

    Subclasses are expected to expose ``self.coordinator`` (an
    ``AjaxDataCoordinator``). All methods are intentionally synchronous
    since they only touch in-memory coordinator state.
    """

    coordinator: AjaxDataCoordinator  # type: ignore[assignment]

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

        target_channel: dict | None = None
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
        self.coordinator.hass.bus.async_fire("ajax_camera_detection", bus_data)

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
