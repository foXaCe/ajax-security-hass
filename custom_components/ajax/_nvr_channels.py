"""Helpers for the NVR channels that have no Ajax camera behind them (#282).

Ajax cameras deliver their detections over their own ONVIF endpoint. A
third-party camera recorded by the NVR has none: its primary source is the NVR
itself, so only the NVR's ONVIF events can carry its detections.

Channels are matched by token, never by index: the index changes when cameras
are enrolled again, and index routing used to send events to the wrong camera.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .models import VideoEdgeType

if TYPE_CHECKING:
    from .models import AjaxVideoEdge


def is_third_party_channel(nvr: AjaxVideoEdge, channel: Any) -> bool:
    """Return True when the channel's primary source is the NVR itself."""
    if nvr.video_edge_type != VideoEdgeType.NVR or not isinstance(channel, dict):
        return False
    aliases = channel.get("sourceAliases")
    sources = aliases.get("sources") if isinstance(aliases, dict) else None
    if not isinstance(sources, list):
        return False
    return any(
        isinstance(source, dict) and source.get("sourceType") == "PRIMARY" and source.get("videoEdgeId") == nvr.id
        for source in sources
    )


def has_third_party_channels(nvr: AjaxVideoEdge) -> bool:
    """Return True when at least one channel only exists through the NVR."""
    channels = nvr.channels
    return isinstance(channels, list) and any(is_third_party_channel(nvr, channel) for channel in channels)


def find_channel_by_token(nvr: AjaxVideoEdge, token: str) -> dict[str, Any] | None:
    """Return the channel an ONVIF source token designates, or None if unsure.

    The token is either the channel id, or a text holding the first part of it
    (``Y8nWCzHpYI`` in ``Y8nWCzHpYI-0``) as a whole word. A token that could
    designate several channels designates none.
    """
    if not token or not isinstance(nvr.channels, list):
        return None
    channels = [channel for channel in nvr.channels if isinstance(channel, dict) and channel.get("id")]

    for channel in channels:
        if str(channel["id"]) == token:
            return channel

    words = {word for word in re.split(r"[^0-9A-Za-z]+", token) if word}
    matches = [
        channel
        for channel in channels
        # A short or numeric part would match an index.
        if len(prefix := str(channel["id"]).split("-")[0]) >= 6 and not prefix.isdigit() and prefix in words
    ]
    return matches[0] if len(matches) == 1 else None
