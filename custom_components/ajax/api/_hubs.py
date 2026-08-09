"""Ajax REST API — Hub / room / user / hub-mode endpoints."""

from __future__ import annotations

import time
from typing import Any

from ._base import (
    AjaxRestApiError,
    AjaxRestClientBase,
)


class _HubsMixin(AjaxRestClientBase):
    """Hub / room / user / hub-mode endpoint methods (mixed into ``AjaxRestApi``)."""

    async def async_get_hubs(self) -> list[dict[str, Any]]:
        """Get all hubs.

        Coalesced by a short-lived in-memory cache (``_hubs_cache``): the
        setup connection test and the coordinator's first refresh fetch the
        hub list back-to-back, so without it every boot pays the round trip
        twice. The entry is served only inside its TTL and never across an
        open cache-bypass window (see ``_cache_entry_usable``), so a post-SSE
        refresh always reaches the real endpoint.

        Returns:
            List of hub dictionaries
        """
        cached = self._hubs_cache
        if cached and self._cache_entry_usable(cached[0], self._hubs_cache_ttl):
            return cached[1]
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")
        data = await self._request("GET", f"user/{self.user_id}/hubs")
        self._hubs_cache = (time.time(), data)
        return data  # type: ignore[no-any-return]

    async def async_get_hub(self, hub_id: str) -> dict[str, Any]:
        """Get hub details.

        Args:
            hub_id: Hub ID

        Returns:
            Hub details dictionary
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")
        return await self._request("GET", f"user/{self.user_id}/hubs/{hub_id}")  # type: ignore[no-any-return]

    async def async_get_space_by_hub(self, hub_id: str) -> dict[str, Any] | None:
        """Get space details by hub ID.

        Args:
            hub_id: Hub ID to find the associated space

        Returns:
            Space binding dictionary with id and name, or None if not found
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")
        # Use query parameter to find space by hubId
        spaces = await self._request("GET", f"user/{self.user_id}/spaces?hubId={hub_id}")
        # Returns array of SpaceBinding, get first one
        if spaces and isinstance(spaces, list) and len(spaces) > 0:
            return spaces[0]  # type: ignore[no-any-return]
        return None

    async def async_get_rooms(self, hub_id: str) -> list[dict[str, Any]]:
        """Get rooms for a hub.

        Args:
            hub_id: Hub ID

        Returns:
            List of room dictionaries
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")
        return await self._request("GET", f"user/{self.user_id}/hubs/{hub_id}/rooms")  # type: ignore[no-any-return]

    async def async_get_users(self, hub_id: str) -> list[dict[str, Any]]:
        """Get users for a hub.

        Args:
            hub_id: Hub ID

        Returns:
            List of user dictionaries
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")
        return await self._request("GET", f"user/{self.user_id}/hubs/{hub_id}/users")  # type: ignore[no-any-return]

    async def async_get_hub_mode(self, hub_id: str) -> dict[str, Any]:
        """Get hub alarm mode.

        Args:
            hub_id: Hub ID

        Returns:
            Hub mode dictionary
        """
        return await self._request("GET", f"hubs/{hub_id}/mode")  # type: ignore[no-any-return]

    async def async_set_hub_mode(self, hub_id: str, mode: str) -> dict[str, Any]:
        """Set hub alarm mode.

        Args:
            hub_id: Hub ID
            mode: Alarm mode (full, partial_1, night, disarmed)

        Returns:
            Updated hub mode
        """
        return await self._request("POST", f"hubs/{hub_id}/mode", {"mode": mode})  # type: ignore[no-any-return]
