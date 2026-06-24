"""Ajax REST API — Arming, groups, automations, events and NVR endpoints."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from ._base import (
    AjaxRestApiError,
    AjaxRestClientBase,
)


class _ArmMixin(AjaxRestClientBase):
    """Arming, groups, automations, events and NVR endpoint methods (mixed into ``AjaxRestApi``)."""

    async def async_get_automations(self, hub_id: str) -> list[dict[str, Any]]:
        """Get hub automations/scenarios.

        Args:
            hub_id: Hub ID

        Returns:
            List of automation dictionaries
        """
        return await self._request("GET", f"hubs/{hub_id}/automations")  # type: ignore[no-any-return]

    async def async_trigger_automation(
        self,
        hub_id: str,
        automation_id: str,
    ) -> dict[str, Any]:
        """Trigger automation/scenario.

        Args:
            hub_id: Hub ID
            automation_id: Automation ID

        Returns:
            Automation trigger response
        """
        return await self._request("POST", f"hubs/{hub_id}/automations/{automation_id}/trigger")  # type: ignore[no-any-return]

    async def async_get_events(self, hub_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Get hub events history.

        Args:
            hub_id: Hub ID
            limit: Maximum number of events to return

        Returns:
            List of event dictionaries
        """
        return await self._request("GET", f"hubs/{hub_id}/events?limit={limit}")  # type: ignore[no-any-return]

    async def async_get_nvr_recordings(
        self,
        nvr_id: str,
        camera_id: str,
        start: str,
        end: str,
    ) -> list[dict[str, Any]]:
        """Get NVR recordings for a camera.

        Args:
            nvr_id: NVR device ID
            camera_id: Camera ID
            start: Start time (ISO format)
            end: End time (ISO format)

        Returns:
            List of recording dictionaries
        """
        query = urlencode({"cameraId": camera_id, "start": start, "end": end})
        return await self._request("GET", f"devices/{nvr_id}/recordings?{query}")  # type: ignore[no-any-return]

    async def async_arm(self, hub_id: str, ignore_problems: bool = True) -> None:
        """Arm the hub.

        Args:
            hub_id: Hub ID
            ignore_problems: Whether to ignore sensor problems when arming
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")

        await self._request_no_response(
            "PUT",
            f"user/{self.user_id}/hubs/{hub_id}/commands/arming",
            {"command": "ARM", "ignoreProblems": ignore_problems},
        )

    async def async_disarm(self, hub_id: str, ignore_problems: bool = True) -> None:
        """Disarm the hub.

        Args:
            hub_id: Hub ID
            ignore_problems: Whether to ignore sensor problems
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")

        await self._request_no_response(
            "PUT",
            f"user/{self.user_id}/hubs/{hub_id}/commands/arming",
            {"command": "DISARM", "ignoreProblems": ignore_problems},
        )

    async def async_night_mode(self, hub_id: str, enabled: bool = True) -> None:
        """Set night mode on/off.

        Args:
            hub_id: Hub ID
            enabled: True for night mode on, False for off
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")

        command = "NIGHT_MODE_ON" if enabled else "NIGHT_MODE_OFF"
        await self._request_no_response(
            "PUT",
            f"user/{self.user_id}/hubs/{hub_id}/commands/arming",
            {"command": command, "ignoreProblems": True},
        )

    async def async_press_panic_button(self, hub_id: str) -> None:
        """Trigger panic alarm for a hub.

        Args:
            hub_id: Hub ID
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")

        await self._request_no_response(
            "PUT",
            f"user/{self.user_id}/hubs/{hub_id}/commands/arming",
            {"command": "PANIC"},
        )

    async def async_get_groups(self, hub_id: str) -> list[dict[str, Any]]:
        """Get all groups for a hub.

        Args:
            hub_id: Hub ID

        Returns:
            List of group data dictionaries
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")

        return await self._request(  # type: ignore[no-any-return]
            "GET",
            f"user/{self.user_id}/hubs/{hub_id}/groups",
        )

    async def async_arm_group(self, hub_id: str, group_id: str, ignore_problems: bool = True) -> None:
        """Arm a specific group.

        Args:
            hub_id: Hub ID
            group_id: Group ID to arm
            ignore_problems: Whether to ignore sensor problems when arming
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")

        await self._request_no_response(
            "PUT",
            f"user/{self.user_id}/hubs/{hub_id}/groups/{group_id}/commands/arming",
            {"command": "ARM", "ignoreProblems": ignore_problems},
        )

    async def async_disarm_group(self, hub_id: str, group_id: str, ignore_problems: bool = True) -> None:
        """Disarm a specific group.

        Args:
            hub_id: Hub ID
            group_id: Group ID to disarm
            ignore_problems: Whether to ignore sensor problems
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")

        await self._request_no_response(
            "PUT",
            f"user/{self.user_id}/hubs/{hub_id}/groups/{group_id}/commands/arming",
            {"command": "DISARM", "ignoreProblems": ignore_problems},
        )
