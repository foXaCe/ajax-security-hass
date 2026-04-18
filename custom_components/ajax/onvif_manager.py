"""ONVIF Manager for Ajax video edge devices.

This module manages ONVIF connections to multiple Ajax cameras,
handling subscriptions and event routing to the coordinator.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from .models import VideoEdgeType
from .onvif_client import AjaxOnvifClient, OnvifDetectionEvent

if TYPE_CHECKING:
    from .models import AjaxVideoEdge

_LOGGER = logging.getLogger(__name__)


class AjaxOnvifManager:
    """Manages ONVIF connections for all Ajax video edge devices."""

    def __init__(
        self,
        username: str,
        password: str,
        event_callback: Callable[[OnvifDetectionEvent], None] | None = None,
    ) -> None:
        """Initialize the ONVIF manager.

        Args:
            username: ONVIF username (same for all cameras)
            password: ONVIF password (same for all cameras)
            event_callback: Callback function for detection events
        """
        self._username = username
        self._password = password
        self._event_callback = event_callback
        self._clients: dict[str, AjaxOnvifClient] = {}
        # Serialise add/remove so concurrent callers cannot duplicate clients.
        self._clients_lock = asyncio.Lock()

    @property
    def connected_count(self) -> int:
        """Return number of connected cameras."""
        return sum(1 for client in self._clients.values() if client.connected)

    def get_client(self, video_edge_id: str) -> AjaxOnvifClient | None:
        """Get ONVIF client for a video edge device."""
        return self._clients.get(video_edge_id)

    async def async_add_video_edge(self, video_edge: AjaxVideoEdge) -> bool:
        """Add a video edge device and start ONVIF connection.

        Args:
            video_edge: The Ajax video edge device to add

        Returns:
            True if connection successful, False otherwise
        """
        if not video_edge.ip_address:
            _LOGGER.warning(
                "ONVIF: Skipping %s - no IP address available",
                video_edge.name,
            )
            return False

        async with self._clients_lock:
            # Skip if already connected
            if video_edge.id in self._clients:
                existing = self._clients[video_edge.id]
                if existing.connected:
                    return True
                # Stop disconnected client before replacing
                await existing.async_stop()
                self._clients.pop(video_edge.id, None)

            # Create new client
            client = AjaxOnvifClient(
                video_edge=video_edge,
                username=self._username,
                password=self._password,
                event_callback=self._event_callback,
            )

            # Try to connect
            if not await client.async_connect():
                return False

            # Subscribe to events
            if not await client.async_subscribe_events():
                await client.async_stop()  # Cleanup on partial failure
                return False

            # Start polling
            await client.async_start_polling()

            self._clients[video_edge.id] = client

        _LOGGER.info(
            "ONVIF client started for %s (%s)",
            video_edge.name,
            video_edge.ip_address,
        )
        return True

    async def async_remove_video_edge(self, video_edge_id: str) -> None:
        """Remove and disconnect a video edge device.

        Args:
            video_edge_id: The ID of the video edge to remove
        """
        async with self._clients_lock:
            client = self._clients.pop(video_edge_id, None)
        if client:
            await client.async_stop()
            _LOGGER.debug("ONVIF client stopped for %s", video_edge_id)

    async def async_start(self, video_edges: list[AjaxVideoEdge]) -> None:
        """Start ONVIF connections for video edges.

        Strategy: connect ONVIF directly to every individual camera and
        doorbell. The NVR is intentionally skipped — its PullPoint events
        require a sourceAliases channel→camera mapping that is unreliable
        (channel 0 has been observed misrouting motion events to the
        doorbell instead of the actual camera). Each Ajax camera runs its
        own AI detection and exposes ONVIF events directly, so the NVR
        adds nothing for the events path.

        Args:
            video_edges: List of video edge devices to connect
        """
        if not self._username or not self._password:
            _LOGGER.info("ONVIF: Credentials not configured - skipping local video detection")
            return

        _LOGGER.info("ONVIF: Starting with credentials (user=%s)", self._username)

        nvrs = [ve for ve in video_edges if ve.video_edge_type == VideoEdgeType.NVR]
        cameras = [ve for ve in video_edges if ve.video_edge_type != VideoEdgeType.NVR]
        targets = cameras

        if nvrs:
            _LOGGER.info(
                "ONVIF: %d NVR(s) detected and skipped for events; connecting directly to %d camera(s)/doorbell(s)",
                len(nvrs),
                len(cameras),
            )
        else:
            _LOGGER.info(
                "ONVIF: No NVR - connecting to %d individual camera(s)",
                len(cameras),
            )

        if not targets:
            _LOGGER.info("ONVIF: No video edge devices found to connect")
            return

        # Connect to selected targets concurrently
        tasks = [self.async_add_video_edge(ve) for ve in targets]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Log results
        success_count = sum(1 for r in results if r is True)
        _LOGGER.info(
            "ONVIF manager started: %d/%d devices connected",
            success_count,
            len(targets),
        )

    async def async_stop(self) -> None:
        """Stop all ONVIF connections."""
        async with self._clients_lock:
            clients = list(self._clients.values())
            self._clients.clear()
        if clients:
            results = await asyncio.gather(*(c.async_stop() for c in clients), return_exceptions=True)
            for res in results:
                if isinstance(res, Exception):
                    _LOGGER.warning("ONVIF client stop failed: %s", res)
        _LOGGER.debug("ONVIF manager stopped")

    async def async_update_video_edges(self, video_edges: list[AjaxVideoEdge]) -> None:
        """Update video edges list (add new, remove old).

        Same strategy as async_start: connect directly to cameras and
        doorbells, never via the NVR (channel mapping is unreliable).

        Args:
            video_edges: Current list of video edge devices
        """
        targets = [ve for ve in video_edges if ve.video_edge_type != VideoEdgeType.NVR]

        current_ids = {ve.id for ve in targets}
        existing_ids = set(self._clients.keys())

        # Remove devices no longer in targets
        for video_edge_id in existing_ids - current_ids:
            await self.async_remove_video_edge(video_edge_id)

        # Add new devices
        for ve in targets:
            if ve.id not in existing_ids and ve.ip_address:
                await self.async_add_video_edge(ve)
