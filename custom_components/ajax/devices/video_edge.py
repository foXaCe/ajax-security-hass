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

        # For each channel, we can have AI detection sensors
        for i, channel in enumerate(self.video_edge.channels):
            channel_id = channel.get("id", str(i))
            channel_name = (
                f"Channel {i + 1}" if len(self.video_edge.channels) > 1 else ""
            )

            # Motion detection
            sensors.append(
                {
                    "key": f"motion_{channel_id}" if channel_name else "motion",
                    "translation_key": "video_motion",
                    "icon": "mdi:motion-sensor",
                    "value_fn": lambda ch=channel: self._has_detection(
                        ch, "VIDEO_MOTION"
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
                    "icon": "mdi:human",
                    "value_fn": lambda ch=channel: self._has_detection(
                        ch, "VIDEO_HUMAN"
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
                    "icon": "mdi:car",
                    "value_fn": lambda ch=channel: self._has_detection(
                        ch, "VIDEO_VEHICLE"
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
                    "icon": "mdi:dog",
                    "value_fn": lambda ch=channel: self._has_detection(ch, "VIDEO_PET"),
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
                    "icon": "mdi:ip-network",
                    "value_fn": lambda: self.video_edge.ip_address,
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
                    "icon": "mdi:chip",
                    "value_fn": lambda: self.video_edge.firmware_version,
                    "enabled_by_default": True,
                    "entity_category": "diagnostic",
                }
            )

        return sensors

    def _has_detection(self, channel: dict, detection_type: str) -> bool:
        """Check if channel has a specific detection active."""
        states = channel.get("state", [])
        for state in states:
            if state.get("type") == detection_type:
                return state.get("active", False)
        return False
