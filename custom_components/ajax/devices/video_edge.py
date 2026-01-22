"""Video Edge device handler for Ajax surveillance cameras.

Handles:
- TurretCam
- BulletCam
- MiniDome
- NVR (Network Video Recorder)
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from ..models import VideoEdgeType

if TYPE_CHECKING:
    from ..models import AjaxVideoEdge

_LOGGER = logging.getLogger(__name__)


def _parse_iso_duration(duration: str | None) -> str | None:
    """Parse ISO 8601 duration (e.g., PT19M38.277S) to human-readable format."""
    if not duration:
        return None

    # Match ISO 8601 duration format: PT[nH][nM][nS]
    match = re.match(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?",
        duration,
    )
    if not match:
        return duration  # Return as-is if can't parse

    hours = int(match.group(1)) if match.group(1) else 0
    minutes = int(match.group(2)) if match.group(2) else 0
    seconds = float(match.group(3)) if match.group(3) else 0

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if seconds > 0 and hours == 0:  # Only show seconds if less than 1 hour
        parts.append(f"{int(seconds)}s")

    return " ".join(parts) if parts else "0s"


# Record mode translations (API value -> translation key)
RECORD_MODE_TRANSLATIONS = {
    "ON_DETECTION": "on_detection",
    "PERMANENT": "permanent",
    "DISABLED": "disabled",
    "UNKNOWN": "unknown",
}

# Storage status translations (API value -> translation key)
STORAGE_STATUS_TRANSLATIONS = {
    "READY": "ready",
    "IDLE": "idle",
    "NEED_FORMAT": "need_format",
    "FORMATTING": "formatting",
    "NONE": "none",
}

# Record policy translations (API value -> translation key)
RECORD_POLICY_TRANSLATIONS = {
    "ALWAYS": "always",
    "WHEN_REQUESTED": "when_requested",
    "UNKNOWN": "unknown",
}


class VideoEdgeHandler:
    """Handler for Ajax Video Edge surveillance cameras."""

    def __init__(self, video_edge: AjaxVideoEdge, all_video_edges: dict | None = None) -> None:
        """Initialize the handler.

        Args:
            video_edge: The video edge device to handle.
            all_video_edges: Optional dict of all video edges in the space.
                Used to find NVR links for cameras.
        """
        self.video_edge = video_edge
        self._all_video_edges = all_video_edges or {}
        # Debug: log raw data keys to see all available fields
        _LOGGER.debug(
            "VideoEdge %s (%s) raw_data keys: %s",
            video_edge.name,
            video_edge.video_edge_type.value,
            list(video_edge.raw_data.keys()) if video_edge.raw_data else [],
        )

    def get_binary_sensors(self) -> list[dict]:
        """Return binary sensor entities for video edges."""
        sensors = []

        # Ensure channels is a list
        channels = self.video_edge.channels
        if not isinstance(channels, list):
            return sensors

        # Check if this camera is recorded by any NVR (skip AI sensors if so)
        is_recorded_by_nvr = len(self._get_linked_nvrs()) > 0

        # For each channel, we can have AI detection sensors
        for i, channel in enumerate(channels):
            channel_id = channel.get("id", str(i)) if isinstance(channel, dict) else str(i)

            # Get linked camera info for NVR channels
            linked_camera_info = self._get_linked_camera_info(channel)

            # Determine if we should create AI detection sensors for this channel
            if linked_camera_info:
                # This is an NVR channel linked to an Ajax camera
                # Create sensors on the CAMERA device (not NVR) for better UX
                # The value_fn still reads from NVR data via closure
                target_ve_id = linked_camera_info["id"]
                _LOGGER.debug(
                    "Creating AI detection sensors for camera %s (data from NVR %s)",
                    linked_camera_info["name"],
                    self.video_edge.name,
                )
            elif is_recorded_by_nvr:
                # This camera is recorded by an NVR - skip AI sensors here
                # They will be created when processing the NVR
                _LOGGER.debug(
                    "Skipping AI detection sensors for camera %s (recorded by NVR)",
                    self.video_edge.name,
                )
                continue
            else:
                # Standalone camera not recorded by NVR - create sensors here
                target_ve_id = None

            # Use channel name in key if multiple channels
            use_channel_suffix = len(channels) > 1

            # Motion detection
            sensors.append(
                {
                    "key": f"motion_{channel_id}" if use_channel_suffix else "motion",
                    "translation_key": "video_motion",
                    "value_fn": lambda cid=channel_id: self._has_detection_by_id(cid, "VIDEO_MOTION"),
                    "enabled_by_default": True,
                    "channel_id": channel_id,
                    "target_video_edge_id": target_ve_id,
                }
            )

            # Human detection
            sensors.append(
                {
                    "key": f"human_{channel_id}" if use_channel_suffix else "human",
                    "translation_key": "video_human",
                    "value_fn": lambda cid=channel_id: self._has_detection_by_id(cid, "VIDEO_HUMAN"),
                    "enabled_by_default": True,
                    "channel_id": channel_id,
                    "target_video_edge_id": target_ve_id,
                }
            )

            # Vehicle detection
            sensors.append(
                {
                    "key": f"vehicle_{channel_id}" if use_channel_suffix else "vehicle",
                    "translation_key": "video_vehicle",
                    "value_fn": lambda cid=channel_id: self._has_detection_by_id(cid, "VIDEO_VEHICLE"),
                    "enabled_by_default": True,
                    "channel_id": channel_id,
                    "target_video_edge_id": target_ve_id,
                }
            )

            # Pet detection
            sensors.append(
                {
                    "key": f"pet_{channel_id}" if use_channel_suffix else "pet",
                    "translation_key": "video_pet",
                    "value_fn": lambda cid=channel_id: self._has_detection_by_id(cid, "VIDEO_PET"),
                    "enabled_by_default": True,
                    "channel_id": channel_id,
                    "target_video_edge_id": target_ve_id,
                }
            )

        # Lid/tamper sensor (from systemInfo)
        # API returns lidClosed=True when closed, but we want on=tampered (open), off=ok (closed)
        # Use device_class TAMPER without translation_key so HA uses automatic translation
        system_info = self.video_edge.raw_data.get("systemInfo", {}) or {}
        if "lidClosed" in system_info:
            sensors.append(
                {
                    "key": "tamper",
                    "device_class": "tamper",
                    "value_fn": lambda: not self.video_edge.raw_data.get("systemInfo", {}).get("lidClosed", True),
                    "enabled_by_default": True,
                }
            )

        # ONVIF integration enabled
        raw_data = self.video_edge.raw_data
        onvif_settings = raw_data.get("onvif", {}) or {}
        if "userAuthEnabled" in onvif_settings or "enabled" in onvif_settings:
            sensors.append(
                {
                    "key": "onvif_enabled",
                    "translation_key": "video_edge_onvif_enabled",
                    "value_fn": lambda: (
                        self.video_edge.raw_data.get("onvif", {}).get("userAuthEnabled", False)
                        or self.video_edge.raw_data.get("onvif", {}).get("enabled", False)
                    ),
                    "enabled_by_default": True,
                }
            )

        return sensors

    def get_sensors(self) -> list[dict]:
        """Return sensor entities for video edges."""
        sensors = []
        raw_data = self.video_edge.raw_data

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

        # System info sensors
        system_info = raw_data.get("systemInfo", {})
        if system_info:
            # Uptime (parsed from ISO 8601 duration)
            if "uptime" in system_info:
                sensors.append(
                    {
                        "key": "uptime",
                        "translation_key": "video_edge_uptime",
                        "value_fn": lambda: _parse_iso_duration(
                            self.video_edge.raw_data.get("systemInfo", {}).get("uptime")
                        ),
                        "enabled_by_default": True,
                        "entity_category": "diagnostic",
                    }
                )

            # CPU usage
            if "averageCpuConsumption" in system_info:
                sensors.append(
                    {
                        "key": "cpu_usage",
                        "translation_key": "video_edge_cpu_usage",
                        "native_unit_of_measurement": "%",
                        "value_fn": lambda: self.video_edge.raw_data.get("systemInfo", {}).get("averageCpuConsumption"),
                        "enabled_by_default": True,
                        "entity_category": "diagnostic",
                    }
                )

            # RAM usage
            if "ramConsumption" in system_info:
                sensors.append(
                    {
                        "key": "ram_usage",
                        "translation_key": "video_edge_ram_usage",
                        "native_unit_of_measurement": "%",
                        "value_fn": lambda: self.video_edge.raw_data.get("systemInfo", {}).get("ramConsumption"),
                        "enabled_by_default": True,
                        "entity_category": "diagnostic",
                    }
                )

        # Storage info
        storage_devices = raw_data.get("storageDevices", [])
        if storage_devices and len(storage_devices) > 0:
            storage = storage_devices[0]
            # Storage size (convert bytes to GB)
            if "sizeTotal" in storage:
                sensors.append(
                    {
                        "key": "storage_total",
                        "translation_key": "video_edge_storage_total",
                        "native_unit_of_measurement": "GB",
                        "value_fn": lambda: round(
                            self.video_edge.raw_data.get("storageDevices", [{}])[0].get("sizeTotal", 0) / (1024**3),
                            1,
                        ),
                        "enabled_by_default": True,
                        "entity_category": "diagnostic",
                    }
                )

            # Storage temperature
            if "temperature" in storage:
                sensors.append(
                    {
                        "key": "storage_temperature",
                        "translation_key": "video_edge_storage_temperature",
                        "native_unit_of_measurement": "°C",
                        "value_fn": lambda: self.video_edge.raw_data.get("storageDevices", [{}])[0].get("temperature"),
                        "enabled_by_default": True,
                        "entity_category": "diagnostic",
                    }
                )

            # Storage status (state: READY, NEED_FORMAT, FORMATTING, etc.)
            storage_status = storage.get("status", {})
            if storage_status and "state" in storage_status:
                sensors.append(
                    {
                        "key": "storage_status",
                        "translation_key": "video_edge_storage_status",
                        "device_class": "enum",
                        "options": ["ready", "idle", "need_format", "formatting", "none"],
                        "value_fn": lambda: self._get_storage_status(),
                        "enabled_by_default": True,
                        "entity_category": "diagnostic",
                    }
                )

        # Connection state (ONLINE/OFFLINE)
        sensors.append(
            {
                "key": "connection_state",
                "translation_key": "video_edge_connection",
                "device_class": "enum",
                "options": ["online", "offline", "unknown"],
                "value_fn": lambda: self.video_edge.connection_state.lower()
                if self.video_edge.connection_state
                else "unknown",
                "enabled_by_default": True,
                "entity_category": "diagnostic",
            }
        )

        # NVR Cameras sensor - shows connected cameras with detection states
        if self.video_edge.video_edge_type == VideoEdgeType.NVR:
            sensors.append(
                {
                    "key": "cameras",
                    "translation_key": "nvr_cameras",
                    "value_fn": lambda: self._get_nvr_cameras_count(),
                    "extra_state_attributes_fn": lambda: self._get_nvr_cameras_attributes(),
                    "enabled_by_default": True,
                    "entity_category": "diagnostic",
                }
            )

        # WiFi signal strength
        network = raw_data.get("networkInterface", {})
        wifi = network.get("wifi", {})
        if wifi and "signalStrength" in wifi:
            sensors.append(
                {
                    "key": "wifi_signal",
                    "translation_key": "video_edge_wifi_signal",
                    "native_unit_of_measurement": "%",
                    "value_fn": lambda: self.video_edge.raw_data.get("networkInterface", {})
                    .get("wifi", {})
                    .get("signalStrength"),
                    "enabled_by_default": True,
                    "entity_category": "diagnostic",
                }
            )

        # Record mode and policy per channel (as enum sensors with translations)
        channels = self.video_edge.channels
        if isinstance(channels, list):
            for i, channel in enumerate(channels):
                if isinstance(channel, dict):
                    channel_id = channel.get("id", str(i))
                    channel_suffix = f"_{channel_id}" if len(channels) > 1 else ""

                    # Record mode
                    if "recordMode" in channel:
                        sensors.append(
                            {
                                "key": f"record_mode{channel_suffix}",
                                "translation_key": "video_edge_record_mode",
                                "device_class": "enum",
                                "options": ["on_detection", "permanent", "disabled", "unknown"],
                                "value_fn": lambda cid=channel_id: self._get_channel_record_mode(cid),
                                "enabled_by_default": True,
                                "entity_category": "diagnostic",
                            }
                        )

                    # Record policy
                    if "recordPolicy" in channel:
                        sensors.append(
                            {
                                "key": f"record_policy{channel_suffix}",
                                "translation_key": "video_edge_record_policy",
                                "device_class": "enum",
                                "options": ["always", "when_requested", "unknown"],
                                "value_fn": lambda cid=channel_id: self._get_channel_record_policy(cid),
                                "enabled_by_default": True,
                                "entity_category": "diagnostic",
                            }
                        )

        # NVR-specific sensors
        if self.video_edge.video_edge_type.value == "NVR":
            # Archive depth (current archive duration in days)
            if storage_devices and len(storage_devices) > 0:
                storage = storage_devices[0]
                if "archiveDepth" in storage:
                    sensors.append(
                        {
                            "key": "archive_depth",
                            "translation_key": "video_edge_archive_depth",
                            "native_unit_of_measurement": "d",
                            "value_fn": lambda: self.video_edge.raw_data.get("storageDevices", [{}])[0].get(
                                "archiveDepth"
                            ),
                            "enabled_by_default": True,
                        }
                    )

            # LED brightness level
            if "ledBrightnessLevel" in raw_data:
                sensors.append(
                    {
                        "key": "led_brightness",
                        "translation_key": "video_edge_led_brightness",
                        "native_unit_of_measurement": "%",
                        "value_fn": lambda: self.video_edge.raw_data.get("ledBrightnessLevel"),
                        "enabled_by_default": True,
                    }
                )

            # Max display duration (maxPreviewDuration in seconds -> minutes)
            if "maxPreviewDuration" in raw_data:
                sensors.append(
                    {
                        "key": "max_preview_duration",
                        "translation_key": "video_edge_max_preview_duration",
                        "native_unit_of_measurement": "min",
                        "value_fn": lambda: (self.video_edge.raw_data.get("maxPreviewDuration") or 0) // 60,
                        "enabled_by_default": True,
                    }
                )

        return sensors

    def _get_channel_record_mode(self, channel_id: str) -> str | None:
        """Get record mode for a specific channel (translated to lowercase key)."""
        channel = self._get_channel_by_id(channel_id)
        if channel:
            mode = channel.get("recordMode", "UNKNOWN")
            # Convert API value to translation key (lowercase)
            return RECORD_MODE_TRANSLATIONS.get(mode, mode.lower())
        return None

    def _get_channel_record_policy(self, channel_id: str) -> str | None:
        """Get record policy for a specific channel (translated to lowercase key)."""
        channel = self._get_channel_by_id(channel_id)
        if channel:
            policy = channel.get("recordPolicy", "UNKNOWN")
            # Convert API value to translation key (lowercase)
            return RECORD_POLICY_TRANSLATIONS.get(policy, policy.lower())
        return None

    def _get_storage_status(self) -> str:
        """Get storage status (translated to lowercase key)."""
        storage_devices = self.video_edge.raw_data.get("storageDevices", [])
        if storage_devices and len(storage_devices) > 0:
            status = storage_devices[0].get("status", {})
            state = status.get("state", "NONE")
            return STORAGE_STATUS_TRANSLATIONS.get(state, state.lower())
        return "none"

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
        """Check if channel has a specific detection active by channel ID.

        Checks two sources:
        1. ONVIF local detection (from video_edge.detections dict, updated via ONVIF events)
        2. REST API state (from channel.state in raw_data)

        ONVIF detections work even when the alarm is disarmed and provide
        real-time local events without cloud dependency.

        For NVR channels linked to cameras, reads detections from the linked camera.
        """
        onvif_key = detection_type.lower()

        # For NVR, check if channel is linked to a camera and read from that camera
        if self.video_edge.video_edge_type == VideoEdgeType.NVR:
            channel = self._get_channel_by_id(channel_id)
            if channel:
                linked_info = self._get_linked_camera_info(channel)
                if linked_info:
                    linked_camera = self._all_video_edges.get(linked_info["id"])
                    if linked_camera and linked_camera.detections.get(onvif_key, False):
                        return True

        # Check ONVIF local detection state on this video edge
        if self.video_edge.detections.get(onvif_key, False):
            return True

        # Fall back to REST API state
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

    def _get_linked_camera_info(self, channel: dict) -> dict | None:
        """Get info about the Ajax camera linked to this NVR channel.

        Returns dict with camera info {id, name} if this is an NVR channel
        linked to an Ajax camera, None otherwise.
        """
        if not isinstance(channel, dict):
            return None

        # Only for NVR devices
        if self.video_edge.video_edge_type.value != "NVR":
            return None

        source_aliases = channel.get("sourceAliases", {})
        if not isinstance(source_aliases, dict):
            return None

        sources = source_aliases.get("sources", [])
        if not isinstance(sources, list):
            return None

        # Ajax camera types
        ajax_camera_types = {"TURRET", "TURRET_HL", "BULLET", "BULLET_HL", "MINIDOME", "MINIDOME_HL"}

        for source in sources:
            if not isinstance(source, dict):
                continue
            if source.get("sourceType") == "PRIMARY":
                source_type = source.get("type", "")
                source_ve_id = source.get("videoEdgeId", "")
                if source_type in ajax_camera_types and source_ve_id != self.video_edge.id:
                    # Find the camera name from all_video_edges
                    camera_name = channel.get("name", f"Camera {source_ve_id[:6]}")
                    if source_ve_id in self._all_video_edges:
                        camera_name = self._all_video_edges[source_ve_id].name
                    return {"id": source_ve_id, "name": camera_name}

        return None

    def _get_nvr_cameras_count(self) -> int:
        """Get number of cameras connected to this NVR."""
        if self.video_edge.video_edge_type != VideoEdgeType.NVR:
            return 0

        count = 0
        channels = self.video_edge.channels
        if not isinstance(channels, list):
            return 0

        for channel in channels:
            linked_info = self._get_linked_camera_info(channel)
            if linked_info:
                count += 1

        return count

    def _get_nvr_cameras_attributes(self) -> dict:
        """Get detailed attributes for NVR cameras sensor.

        Returns dict with cameras list and their types.
        Detection states are available on each camera's binary_sensor entities.
        """
        if self.video_edge.video_edge_type != VideoEdgeType.NVR:
            return {}

        cameras = []
        channels = self.video_edge.channels
        if not isinstance(channels, list):
            return {"cameras": cameras}

        for i, channel in enumerate(channels):
            linked_info = self._get_linked_camera_info(channel)
            if linked_info:
                camera_id = linked_info["id"]
                camera_name = linked_info["name"]

                # Get camera type
                linked_camera = self._all_video_edges.get(camera_id)
                camera_type = linked_camera.video_edge_type.value if linked_camera else "unknown"

                cameras.append(
                    {
                        "name": camera_name,
                        "channel": i,
                        "type": camera_type,
                    }
                )

        return {"cameras": cameras}

    def _get_linked_nvrs(self) -> list[dict]:
        """Find all NVRs that record this camera.

        Returns a list of dicts with NVR info: {id, name}.
        Used to add linked_nvr attribute to detection sensors.
        """
        linked_nvrs = []

        # Only search for NVR links if this is a camera (not an NVR itself)
        if self.video_edge.video_edge_type.value == "NVR":
            return linked_nvrs

        camera_id = self.video_edge.id

        for _ve_id, ve in self._all_video_edges.items():
            # Only check NVRs
            if ve.video_edge_type.value != "NVR":
                continue

            # Check if any channel of this NVR has our camera as source
            channels = ve.channels if isinstance(ve.channels, list) else []
            for channel in channels:
                if not isinstance(channel, dict):
                    continue
                source_aliases = channel.get("sourceAliases", {})
                if not isinstance(source_aliases, dict):
                    continue
                sources = source_aliases.get("sources", [])
                if not isinstance(sources, list):
                    continue

                for source in sources:
                    if not isinstance(source, dict):
                        continue
                    if source.get("sourceType") == "PRIMARY" and source.get("videoEdgeId") == camera_id:
                        linked_nvrs.append({"id": ve.id, "name": ve.name})
                        break  # Found this NVR, move to next one

        return linked_nvrs

    def _is_channel_linked_to_ajax_camera(self, channel: dict) -> bool:
        """Check if an NVR channel is linked to an EXTERNAL Ajax camera (via sourceAliases).

        NVR channels that record from Ajax cameras (TurretCam, BulletCam, etc.)
        have sourceAliases with a PRIMARY source pointing to the Ajax camera.
        We skip AI detection sensors for these channels because the Ajax camera
        already has its own detection sensors (avoiding duplicates).

        IMPORTANT: We check that the PRIMARY source's videoEdgeId is DIFFERENT
        from the current video edge's ID. This ensures we don't skip sensors
        for cameras that reference themselves in sourceAliases.

        Future NVRs with AI detection on non-Ajax cameras will create sensors
        since they won't have a PRIMARY source with an Ajax video edge type.
        """
        if not isinstance(channel, dict):
            return False

        source_aliases = channel.get("sourceAliases", {})
        if not isinstance(source_aliases, dict):
            return False

        sources = source_aliases.get("sources", [])
        if not isinstance(sources, list):
            return False

        # Check if there's a PRIMARY source with an Ajax camera type
        # that is DIFFERENT from the current video edge (external camera)
        ajax_camera_types = {"TURRET", "TURRET_HL", "BULLET", "BULLET_HL", "MINIDOME", "MINIDOME_HL"}
        current_ve_id = self.video_edge.id

        for source in sources:
            if not isinstance(source, dict):
                continue
            if source.get("sourceType") == "PRIMARY":
                source_type = source.get("type", "")
                source_ve_id = source.get("videoEdgeId", "")
                # Only consider it linked if it's an Ajax camera AND it's a different device
                if source_type in ajax_camera_types and source_ve_id != current_ve_id:
                    return True

        return False
