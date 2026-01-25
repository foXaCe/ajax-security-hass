"""Ajax camera platform for Video Edge devices.

This module creates camera entities for Ajax VideoEdge surveillance cameras
using RTSP streaming.
"""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import quote

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import CONF_RTSP_PASSWORD, CONF_RTSP_USERNAME, DOMAIN, MANUFACTURER
from .coordinator import AjaxDataCoordinator
from .models import AjaxVideoEdge, VideoEdgeType

_LOGGER = logging.getLogger(__name__)

# Human-readable model names for video edge devices
VIDEO_EDGE_MODEL_NAMES = {
    VideoEdgeType.NVR: "NVR",
    VideoEdgeType.TURRET: "TurretCam",
    VideoEdgeType.TURRET_HL: "TurretCam HL",
    VideoEdgeType.BULLET: "BulletCam",
    VideoEdgeType.BULLET_HL: "BulletCam HL",
    VideoEdgeType.MINIDOME: "MiniDome",
    VideoEdgeType.MINIDOME_HL: "MiniDome HL",
    VideoEdgeType.INDOOR: "Indoor Camera",
    VideoEdgeType.UNKNOWN: "Video Edge",
}

# Default RTSP port (Ajax cameras use 8554, not standard 554)
DEFAULT_RTSP_PORT = 8554

# Snapshot cache duration in seconds (reduces FFmpeg calls)
SNAPSHOT_CACHE_DURATION = 30


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax camera entities from a config entry."""
    coordinator = entry.runtime_data

    entities: list[Camera] = []

    # Create camera entities for video edges
    for space in coordinator.data.spaces.values():
        for video_edge in space.video_edges.values():
            # Only create camera if we have an IP address
            if video_edge.ip_address:
                # NVR: create cameras for each channel
                if video_edge.video_edge_type == VideoEdgeType.NVR and video_edge.channels:
                    for i, channel in enumerate(video_edge.channels):
                        channel_name = channel.get("name") if isinstance(channel, dict) else None
                        channel_id = channel.get("id") if isinstance(channel, dict) else None
                        # Main stream for this channel
                        entities.append(
                            AjaxVideoEdgeCamera(
                                coordinator=coordinator,
                                entry=entry,
                                video_edge=video_edge,
                                space_id=space.id,
                                stream_type="main",
                                channel_index=i,
                                channel_name=channel_name,
                                channel_id=channel_id,
                            )
                        )
                        # Sub stream for this channel (disabled by default)
                        entities.append(
                            AjaxVideoEdgeCamera(
                                coordinator=coordinator,
                                entry=entry,
                                video_edge=video_edge,
                                space_id=space.id,
                                stream_type="sub",
                                channel_index=i,
                                channel_name=channel_name,
                                channel_id=channel_id,
                            )
                        )
                else:
                    # Single camera (TurretCam, BulletCam, MiniDome, etc.)
                    # Create main stream camera (high quality, enabled by default)
                    entities.append(
                        AjaxVideoEdgeCamera(
                            coordinator=coordinator,
                            entry=entry,
                            video_edge=video_edge,
                            space_id=space.id,
                            stream_type="main",
                        )
                    )
                    # Create sub stream camera (low quality, disabled by default)
                    # Useful for 3G/4G connections with limited bandwidth
                    entities.append(
                        AjaxVideoEdgeCamera(
                            coordinator=coordinator,
                            entry=entry,
                            video_edge=video_edge,
                            space_id=space.id,
                            stream_type="sub",
                        )
                    )

    if entities:
        _LOGGER.debug("Adding %d camera entities", len(entities))
        async_add_entities(entities)


class AjaxVideoEdgeCamera(CoordinatorEntity[AjaxDataCoordinator], Camera):
    """Camera entity for Ajax Video Edge devices."""

    __slots__ = (
        "_entry",
        "_video_edge_id",
        "_space_id",
        "_stream_type",
        "_channel_index",
        "_channel_id",
        "_model_name",
        "_color",
        "_snapshot_cache",
        "_snapshot_cache_time",
    )

    _attr_has_entity_name = True
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_use_stream_for_stills = True  # Use RTSP stream for snapshot images

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        entry: AjaxConfigEntry,
        video_edge: AjaxVideoEdge,
        space_id: str,
        stream_type: str = "main",
        channel_index: int | None = None,
        channel_name: str | None = None,
        channel_id: str | None = None,
    ) -> None:
        """Initialize the camera entity."""
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)

        self._entry = entry
        self._video_edge_id = video_edge.id
        self._space_id = space_id
        self._stream_type = stream_type
        self._channel_index = channel_index if channel_index is not None else 0
        self._channel_id = channel_id  # NVR channel ID for RTSP path

        # Build unique ID
        if channel_index is not None:
            self._attr_unique_id = f"{video_edge.id}_camera_ch{channel_index}_{stream_type}"
        else:
            self._attr_unique_id = f"{video_edge.id}_camera_{stream_type}"

        # Camera name and default enabled state
        if channel_index is not None:
            # NVR with multiple channels
            ch_label = channel_name or f"Channel {channel_index + 1}"
            if stream_type == "main":
                self._attr_name = ch_label
            else:
                self._attr_name = f"{ch_label} Sub"
                self._attr_entity_registry_enabled_default = False
        elif stream_type == "main":
            self._attr_name = None  # Use device name
        else:
            self._attr_name = "Sub stream"
            # Sub stream disabled by default (for 3G/4G use)
            self._attr_entity_registry_enabled_default = False

        # Get human-readable model name
        self._model_name = VIDEO_EDGE_MODEL_NAMES.get(video_edge.video_edge_type, "Video Edge")
        self._color = video_edge.color.title() if video_edge.color else ""

        # Snapshot cache
        self._snapshot_cache: bytes | None = None
        self._snapshot_cache_time: float = 0

    @property
    def _video_edge(self) -> AjaxVideoEdge | None:
        """Get the current video edge from coordinator data."""
        space = self.coordinator.data.spaces.get(self._space_id)
        if not space:
            return None
        return space.video_edges.get(self._video_edge_id)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        video_edge = self._video_edge
        if not video_edge:
            return False
        return video_edge.connection_state == "ONLINE"

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information."""
        video_edge = self._video_edge
        if not video_edge:
            return None

        model_display = f"{self._model_name} ({self._color})" if self._color else self._model_name

        # Determine via_device: if camera is recorded by NVR, link to NVR
        # Otherwise link to hub (space_id)
        via_device_id = self._space_id
        nvr_id = self._get_recording_nvr_id()
        if nvr_id:
            via_device_id = nvr_id

        return DeviceInfo(
            identifiers={(DOMAIN, video_edge.id)},
            name=video_edge.name,
            manufacturer=MANUFACTURER,
            model=model_display,
            via_device=(DOMAIN, via_device_id),
            sw_version=video_edge.firmware_version,
        )

    def _get_recording_nvr_id(self) -> str | None:
        """Get the ID of the NVR that records this camera (if any).

        Returns the NVR ID if this camera is recorded by an NVR,
        None otherwise (standalone camera or NVR itself).
        """
        video_edge = self._video_edge
        if not video_edge:
            return None

        # NVRs don't have a parent NVR
        if video_edge.video_edge_type.value == "NVR":
            return None

        space = self.coordinator.data.spaces.get(self._space_id)
        if not space:
            return None

        camera_id = self._video_edge_id

        # Check all NVRs to see if any record this camera
        for ve_id, ve in space.video_edges.items():
            if ve.video_edge_type.value != "NVR":
                continue

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
                        return ve_id  # Found the NVR that records this camera

        return None

    @property
    def is_streaming(self) -> bool:
        """Return True if the camera is streaming."""
        return self.available

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return extra state attributes."""
        attrs = {}

        # Check if RTSP credentials are configured
        username = self._entry.options.get(CONF_RTSP_USERNAME, "")
        password = self._entry.options.get(CONF_RTSP_PASSWORD, "")

        if not username or not password:
            attrs["configuration_help"] = (
                "Pour afficher le flux vidéo, configurez les identifiants ONVIF "
                "dans Paramètres → Appareils et services → Ajax → Configurer → "
                "Identifiants RTSP/ONVIF"
            )

        return attrs if attrs else None

    @property
    def is_recording(self) -> bool:
        """Return True if the camera is recording.

        Note: Ajax cameras may record to NVR/cloud but we report False here
        so HA shows 'Streaming' instead of 'Recording' for better UX.
        """
        return False

    async def stream_source(self) -> str | None:
        """Return the RTSP stream source URL.

        Ajax cameras use a specific RTSP URL format:
        Format: rtsp://[user:pass@]IP:8554/{path}_{stream}
        Where for single cameras:
        - path: {mac_without_colons}-{channel} (e.g., 9c756e2ae22d-0)
        For NVR channels:
        - path: {channel_id} (e.g., mhzE3YtuK8-9c756e2ae22d-0)
        Stream suffix:
        - 'm' for main stream, 's' for sub stream
        """
        video_edge = self._video_edge
        if not video_edge or not video_edge.ip_address:
            return None

        # Build RTSP URL
        ip = video_edge.ip_address
        port = DEFAULT_RTSP_PORT
        stream_suffix = "m" if self._stream_type == "main" else "s"

        # Build stream path
        if self._channel_id:
            # NVR channel: use the channel ID directly
            stream_path = f"{self._channel_id}_{stream_suffix}"
        else:
            # Single camera: use MAC address format
            if not video_edge.mac_address:
                _LOGGER.warning("No MAC address for %s, cannot build RTSP URL", video_edge.name)
                return None
            mac_clean = video_edge.mac_address.replace(":", "").replace("-", "").lower()
            # Validate MAC format (should be 12 hex characters)
            if len(mac_clean) != 12 or not all(c in "0123456789abcdef" for c in mac_clean):
                _LOGGER.warning("Invalid MAC address format for %s: %s", video_edge.name, video_edge.mac_address)
                return None
            channel_num = str(self._channel_index)
            stream_path = f"{mac_clean}-{channel_num}_{stream_suffix}"

        # Get RTSP credentials from options
        username = self._entry.options.get(CONF_RTSP_USERNAME, "")
        password = self._entry.options.get(CONF_RTSP_PASSWORD, "")

        # Build URL with or without credentials
        if username and password:
            # URL-encode credentials to handle special characters
            encoded_user = quote(username, safe="")
            encoded_pass = quote(password, safe="")
            rtsp_url = f"rtsp://{encoded_user}:{encoded_pass}@{ip}:{port}/{stream_path}"
            _LOGGER.debug("Stream source for %s: rtsp://***:***@%s:%s/%s", video_edge.name, ip, port, stream_path)
        else:
            rtsp_url = f"rtsp://{ip}:{port}/{stream_path}"
            _LOGGER.debug("Stream source for %s: %s (no credentials configured)", video_edge.name, rtsp_url)

        return rtsp_url

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """Return a still image from the camera via FFmpeg.

        Ajax Video Edge cameras don't have HTTP snapshot endpoint.
        We use FFmpeg to extract a single frame from the RTSP stream.
        Images are cached for SNAPSHOT_CACHE_DURATION seconds to reduce load.
        """
        # Return cached snapshot if still valid
        now = time.time()
        if self._snapshot_cache and (now - self._snapshot_cache_time) < SNAPSHOT_CACHE_DURATION:
            return self._snapshot_cache

        rtsp_url = await self.stream_source()
        if not rtsp_url:
            return self._snapshot_cache  # Return old cache if available

        ffmpeg_manager = get_ffmpeg_manager(self.hass)

        try:
            # FFmpeg command to extract a single frame as JPEG
            # Using TCP transport for reliability
            process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    ffmpeg_manager.binary,
                    "-rtsp_transport",
                    "tcp",
                    "-i",
                    rtsp_url,
                    "-frames:v",
                    "1",
                    "-f",
                    "image2",
                    "-",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                ),
                timeout=15,
            )

            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
            if stdout:
                self._snapshot_cache = stdout
                self._snapshot_cache_time = now
                return stdout
        except TimeoutError:
            _LOGGER.debug("Timeout getting snapshot from %s", self._video_edge.name if self._video_edge else "camera")
        except Exception as err:
            _LOGGER.debug("Error getting snapshot: %s", err)

        # Return old cache on error
        return self._snapshot_cache

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
