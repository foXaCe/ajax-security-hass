"""Ajax REST API — Video-edge, ONVIF/RTSP and smart-lock endpoints."""

from __future__ import annotations

import time
from typing import Any

from ._base import (
    _LOGGER,
    AjaxRestClientBase,
)


class _VideoMixin(AjaxRestClientBase):
    """Video-edge, ONVIF/RTSP and smart-lock endpoint methods (mixed into ``AjaxRestApi``)."""

    async def async_get_space(self, space_id: str) -> dict[str, Any]:
        """Get full space details including devices list.

        Args:
            space_id: Space ID (not hub_id)

        Returns:
            Space dictionary with devices array
        """
        # Skip the in-memory cache while a bypass window is open so the fresh
        # space payload (which feeds video-edges and smart-locks) is fetched.
        if not self._cache_bypass_active():
            cached = self._space_cache.get(space_id)
            if cached and (time.time() - cached[0]) < self._space_cache_ttl:
                return cached[1]
        data = await self._request("GET", f"user/{self.user_id}/spaces/{space_id}")
        self._space_cache[space_id] = (time.time(), data)
        return data  # type: ignore[no-any-return]

    async def async_get_video_edges(self, space_id: str) -> list[dict[str, Any]]:
        """Get all video edge devices for a space.

        Args:
            space_id: Space ID (real_space_id from spaceBinding)

        Returns:
            List of video edge dictionaries
        """
        # First get the space to find VIDEO_EDGE devices
        space_data = await self.async_get_space(space_id)
        devices = space_data.get("devices", [])
        _LOGGER.debug(
            "Space %s has %d devices: %s",
            space_id,
            len(devices),
            [(d.get("id", "?")[:8], d.get("type")) for d in devices],
        )
        video_edges = []
        for device in devices:
            if device.get("type") == "VIDEO_EDGE":
                video_edge_id = device.get("id")
                if video_edge_id:
                    try:
                        video_edge = await self.async_get_video_edge(space_id, video_edge_id)
                        video_edges.append(video_edge)
                    except Exception as err:
                        _LOGGER.warning("Failed to get video edge %s: %s", video_edge_id, err)
        return video_edges

    async def async_get_video_edge(self, space_id: str, video_edge_id: str) -> dict[str, Any]:
        """Get video edge device details.

        Args:
            space_id: Space ID
            video_edge_id: Video edge device ID

        Returns:
            Video edge details dictionary
        """
        return await self._request(  # type: ignore[no-any-return]
            "GET",
            f"user/{self.user_id}/spaces/{space_id}/devices/video-edges/{video_edge_id}",
        )

    async def async_get_video_edge_onvif(self, space_id: str, video_edge_id: str) -> dict[str, Any]:
        """Get video edge ONVIF settings.

        Args:
            space_id: Space ID
            video_edge_id: Video edge device ID

        Returns:
            ONVIF settings dictionary with userAuthEnabled, users, httpPort
        """
        return await self._request(  # type: ignore[no-any-return]
            "GET",
            f"user/{self.user_id}/spaces/{space_id}/devices/video-edges/{video_edge_id}/onvif",
        )

    async def async_get_video_edge_rtsp(self, space_id: str, video_edge_id: str) -> dict[str, Any]:
        """Get video edge RTSP settings.

        Args:
            space_id: Space ID
            video_edge_id: Video edge device ID

        Returns:
            RTSP settings dictionary with httpPort
        """
        return await self._request(  # type: ignore[no-any-return]
            "GET",
            f"user/{self.user_id}/spaces/{space_id}/devices/video-edges/{video_edge_id}/rtsp",
        )

    async def async_get_smart_locks(self, space_id: str, known_ids: set[str] | None = None) -> list[dict[str, Any]]:
        """Get all smart lock devices for a space.

        Smart locks are discovered from the space's devices array
        with type "SMART_LOCK". Only *unknown* ids (not in ``known_ids``)
        are fetched individually via the per-lock detail endpoint — that
        endpoint returns just the id (occasionally a name), which is only
        useful the first time a lock is seen, to feed the Yale-cloud
        discovery filter. Once a lock is known, its real state
        (lockStatus/doorStatus) is read from the enriched devices payload
        fetched on the same tick (see ``_coordinator_devices.py``), so the
        space device entry is returned as-is instead of re-fetching detail.

        Args:
            space_id: Space ID (real_space_id from spaceBinding)
            known_ids: Ids of smart locks already discovered; their detail
                fetch is skipped and the space device entry is used instead.

        Returns:
            List of smart lock dictionaries
        """
        known_ids = known_ids or set()
        space_data = await self.async_get_space(space_id)
        devices = space_data.get("devices", [])
        _LOGGER.debug(
            "Space %s devices for smart lock discovery: %s",
            space_id,
            [(d.get("id", "?")[:8], d.get("type")) for d in devices],
        )
        smart_locks = []
        for device in devices:
            if device.get("type") == "SMART_LOCK":
                smart_lock_id = device.get("id")
                if smart_lock_id:
                    if smart_lock_id in known_ids:
                        # Already discovered: the detail endpoint would add
                        # nothing (see docstring) — reuse the space entry.
                        smart_locks.append(device)
                        continue
                    try:
                        smart_lock = await self.async_get_smart_lock(space_id, smart_lock_id)
                        # Merge name from space device listing if detail endpoint lacks it
                        if not smart_lock.get("name") and device.get("name"):
                            smart_lock["name"] = device["name"]
                        smart_locks.append(smart_lock)
                    except Exception as err:
                        _LOGGER.warning("Failed to get smart lock %s: %s", smart_lock_id, err)
                        # Fallback: use the space device entry directly
                        smart_locks.append(device)
        return smart_locks

    async def async_get_smart_lock(self, space_id: str, smart_lock_id: str) -> dict[str, Any]:
        """Get smart lock device details.

        Args:
            space_id: Space ID
            smart_lock_id: Smart lock device ID

        Returns:
            Smart lock details dictionary
        """
        return await self._request(  # type: ignore[no-any-return]
            "GET",
            f"user/{self.user_id}/spaces/{space_id}/devices/smart-locks/{smart_lock_id}",
        )
