"""Video Edge device handler for Ajax surveillance cameras.

Handles:
- TurretCam
- BulletCam
- MiniDome
- NVR (Network Video Recorder)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import AjaxVideoEdge


class VideoEdgeHandler:
    """Handler for Ajax Video Edge surveillance cameras."""

    def __init__(self, video_edge: AjaxVideoEdge) -> None:
        """Initialize the handler."""
        self.video_edge = video_edge

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for video edges."""
        sensors = []

        # Ensure channels is a list
        channels = self.video_edge.channels
        if not isinstance(channels, list):
            return sensors

        # For each channel, we can have AI detection sensors
        for i, channel in enumerate(channels):
            channel_id = (
                channel.get("id", str(i)) if isinstance(channel, dict) else str(i)
            )
            channel_name = f"Channel {i + 1}" if len(channels) > 1 else ""

            # Motion detection
            sensors.append(
                {
                    "key": f"motion_{channel_id}" if channel_name else "motion",
                    "translation_key": "video_motion",
                    "value_fn": lambda cid=channel_id: self._has_detection_by_id(
                        cid, "VIDEO_MOTION"
                    ),
                    "enabled_by_default": True,
                    "channel_id": channel_id,
                }
            )

            # Human detection
            sensors.append(
                {
                    "key": f"human_{channel_id}" if channel_name else "human",
                    "translation_key": "video_human",
                    "value_fn": lambda cid=channel_id: self._has_detection_by_id(
                        cid, "VIDEO_HUMAN"
                    ),
                    "enabled_by_default": True,
                    "channel_id": channel_id,
                }
            )

            # Vehicle detection
            sensors.append(
                {
                    "key": f"vehicle_{channel_id}" if channel_name else "vehicle",
                    "translation_key": "video_vehicle",
                    "value_fn": lambda cid=channel_id: self._has_detection_by_id(
                        cid, "VIDEO_VEHICLE"
                    ),
                    "enabled_by_default": True,
                    "channel_id": channel_id,
                }
            )

            # Pet detection
            sensors.append(
                {
                    "key": f"pet_{channel_id}" if channel_name else "pet",
                    "translation_key": "video_pet",
                    "value_fn": lambda cid=channel_id: self._has_detection_by_id(
                        cid, "VIDEO_PET"
                    ),
                    "enabled_by_default": True,
                    "channel_id": channel_id,
                }
            )

        return sensors

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for video edges."""
        sensors = []

        # IP Address
        if self.video_edge.ip_address:
            sensors.append(
                {
                    "key": "ip_address",
                    "translation_key": "ip_address",
                    "value_fn": lambda: self.video_edge.ip_address,
                    "enabled_by_default": True,
                    "entity_category": "diagnostic",
                }
            )

        # MAC Address
        if self.video_edge.mac_address:
            sensors.append(
                {
                    "key": "mac_address",
                    "translation_key": "mac_address",
                    "value_fn": lambda: self.video_edge.mac_address,
                    "enabled_by_default": True,
                    "entity_category": "diagnostic",
                }
            )

        # Firmware version
        if self.video_edge.firmware_version:
            sensors.append(
                {
                    "key": "firmware",
                    "translation_key": "firmware_version",
                    "value_fn": lambda: self.video_edge.firmware_version,
                    "enabled_by_default": True,
                    "entity_category": "diagnostic",
                }
            )

        return sensors

    def _get_channel_by_id(self, channel_id: str) -> dict | None:
        """Get channel dict by ID from current video_edge.channels."""
        channels = self.video_edge.channels
        if not isinstance(channels, list):
            return None
        for i, channel in enumerate(channels):
            if isinstance(channel, dict):
                if channel.get("id") == channel_id:
                    return channel
            elif str(i) == channel_id:
                # Fallback for index-based ID
                return channel if isinstance(channel, dict) else None
        return None

    def _has_detection_by_id(self, channel_id: str, detection_type: str) -> bool:
        """Check if channel has a specific detection active by channel ID."""
        channel = self._get_channel_by_id(channel_id)
        if not channel:
            return False
        return self._has_detection(channel, detection_type)

    def _has_detection(self, channel: dict, detection_type: str) -> bool:
        """Check if channel has a specific detection active."""
        if not isinstance(channel, dict):
            return False
        states = channel.get("state", [])
        if not isinstance(states, list):
            return False
        for state in states:
            if isinstance(state, dict) and state.get("type") == detection_type:
                return state.get("active", False)
        return False
