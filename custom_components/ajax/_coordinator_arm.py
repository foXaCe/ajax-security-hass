"""Arm / disarm / panic service mixin for ``AjaxDataCoordinator``.

Carves the per-space arm/disarm/group/night-mode/panic actions out of the
main coordinator into a self-contained mixin so the coordinator file stays
focused on the polling-and-state-update pipeline.

The mixin keeps all state on ``self``: the coordinator's ``__init__``
owns ``_pending_ha_actions`` and ``_arm_locks``, and the methods here
just consume them — there is no parallel state and no behaviour change.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .api import AjaxRestApiError
from .models import GroupState

if TYPE_CHECKING:
    from .api import AjaxRestApi
    from .models import AjaxSpace

_LOGGER = logging.getLogger(__name__)


class AjaxArmServiceMixin:
    """Arm / disarm / night-mode / panic / group actions.

    The mixin is purely an extraction — it relies on the host coordinator
    for the shared state and for ``get_space`` / ``async_request_refresh``
    (provided by ``DataUpdateCoordinator``).
    """

    # Attributes the host coordinator must provide. They are declared here
    # only for the type checker — the values are set in the coordinator's
    # __init__ and the mixin never assigns them.
    if TYPE_CHECKING:
        api: AjaxRestApi
        _pending_ha_actions: dict[str, float]
        _arm_locks: dict[str, asyncio.Lock]

        def get_space(self, space_id: str) -> AjaxSpace | None: ...
        async def async_request_refresh(self) -> None: ...

    # ------------------------------------------------------------------
    # HA-originated action tracking
    # ------------------------------------------------------------------

    def _register_ha_action(self, hub_id: str) -> None:
        """Register that Home Assistant triggered an action on this hub."""
        self._pending_ha_actions[hub_id] = time.time()

    def has_pending_ha_action(self, hub_id: str) -> bool:
        """Return True if HA triggered an action on this hub in the last 10 s.

        Does NOT consume the pending flag — safe to call multiple times.
        """
        timestamp = self._pending_ha_actions.get(hub_id, 0)
        return time.time() - timestamp < 10

    # ------------------------------------------------------------------
    # Per-space locking
    # ------------------------------------------------------------------

    def _arm_lock_for(self, space_id: str) -> asyncio.Lock:
        """Return (and lazily create) the asyncio.Lock for ``space_id``."""
        lock = self._arm_locks.get(space_id)
        if lock is None:
            lock = asyncio.Lock()
            self._arm_locks[space_id] = lock
        return lock

    # ------------------------------------------------------------------
    # Space-level arm/disarm/night/panic
    # ------------------------------------------------------------------

    async def async_arm_space(self, space_id: str, force: bool = True) -> None:
        """Arm a space.

        Args:
            space_id: The space ID to arm.
            force: If True, ignore problems and force arm even with open sensors.
        """
        _LOGGER.info("Arming space %s (force=%s)", space_id, force)

        async with self._arm_lock_for(space_id):
            try:
                self._register_ha_action(space_id)
                await self.api.async_arm(space_id, ignore_problems=force)
                # State will be updated via SQS with "Home Assistant" as source
            except AjaxRestApiError as err:
                _LOGGER.error("Failed to arm space %s: %s", space_id, err)
                raise

    async def async_disarm_space(self, space_id: str) -> None:
        """Disarm a space."""
        _LOGGER.info("Disarming space %s", space_id)

        async with self._arm_lock_for(space_id):
            try:
                self._register_ha_action(space_id)
                await self.api.async_disarm(space_id)
            except AjaxRestApiError as err:
                _LOGGER.error("Failed to disarm space %s: %s", space_id, err)
                raise

    async def async_arm_night_mode(self, space_id: str, force: bool = False) -> None:
        """Activate night mode for a space.

        Args:
            space_id: The space ID to arm in night mode.
            force: If True, ignore alarms and force arm.
        """
        _LOGGER.info("Activating night mode for space %s (force=%s)", space_id, force)

        async with self._arm_lock_for(space_id):
            try:
                self._register_ha_action(space_id)
                await self.api.async_night_mode(space_id, enabled=True)
            except AjaxRestApiError as err:
                _LOGGER.error("Failed to activate night mode for space %s: %s", space_id, err)
                raise

    async def async_press_panic_button(self, space_id: str) -> None:
        """Press panic button (trigger panic alarm) for a space."""
        _LOGGER.warning("PANIC BUTTON pressed for space %s", space_id)

        try:
            await self.api.async_press_panic_button(space_id)
            # No state update needed — panic is instantaneous.
        except AjaxRestApiError as err:
            _LOGGER.error("Failed to trigger panic for space %s: %s", space_id, err)
            raise

    # ------------------------------------------------------------------
    # Group-level arm/disarm
    # ------------------------------------------------------------------

    async def async_arm_group(self, space_id: str, group_id: str, force: bool = True) -> None:
        """Arm a specific group.

        Args:
            space_id: The space ID (hub_id).
            group_id: The group ID to arm.
            force: If True, ignore problems and force arm.
        """
        _LOGGER.info("Arming group %s in space %s (force=%s)", group_id, space_id, force)

        async with self._arm_lock_for(space_id):
            try:
                self._register_ha_action(space_id)
                await self.api.async_arm_group(space_id, group_id, ignore_problems=force)
                space = self.get_space(space_id)
                if space and group_id in space.groups:
                    space.groups[group_id].state = GroupState.ARMED
                await self.async_request_refresh()
            except AjaxRestApiError as err:
                _LOGGER.error("Failed to arm group %s in space %s: %s", group_id, space_id, err)
                raise

    async def async_disarm_group(self, space_id: str, group_id: str) -> None:
        """Disarm a specific group.

        Args:
            space_id: The space ID (hub_id).
            group_id: The group ID to disarm.
        """
        _LOGGER.info("Disarming group %s in space %s", group_id, space_id)

        async with self._arm_lock_for(space_id):
            try:
                self._register_ha_action(space_id)
                await self.api.async_disarm_group(space_id, group_id)
                space = self.get_space(space_id)
                if space and group_id in space.groups:
                    space.groups[group_id].state = GroupState.DISARMED
                await self.async_request_refresh()
            except AjaxRestApiError as err:
                _LOGGER.error("Failed to disarm group %s in space %s: %s", group_id, space_id, err)
                raise
