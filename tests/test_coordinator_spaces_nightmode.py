"""Regression test for issue #149.

The coordinator split (v0.29.0) moved ``_async_update_spaces_from_hubs``
into ``_coordinator_spaces.py`` but left ``SecurityState`` imported only
under ``TYPE_CHECKING``. The night-mode branch references
``SecurityState.NIGHT_MODE`` at runtime, so the moment a hub reported
night mode the whole coordinator refresh raised
``NameError: name 'SecurityState' is not defined`` and every Ajax entity
went unavailable until night mode was turned off.

mypy --strict did NOT catch this (it evaluates ``TYPE_CHECKING`` as True,
so the name *looks* defined), and the existing tests only exercised
``_parse_security_state`` in a different mixin — never the night-mode
branch of ``_async_update_spaces_from_hubs``.

These tests drive the real method against a mocked API so a re-import
regression fails loudly here, independently of the ruff TC004 guard.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ajax._coordinator_spaces import AjaxSpacesMixin
from custom_components.ajax.api import AjaxRestApiError
from custom_components.ajax.models import AjaxAccount, SecurityState


def _make_mixin(
    *, hub_state: str, groups_enabled: bool = False, night_flag: bool = False
) -> tuple[AjaxSpacesMixin, AjaxAccount]:
    """Build a bare AjaxSpacesMixin wired to a mocked API + empty account."""
    mixin = object.__new__(AjaxSpacesMixin)

    account = AjaxAccount(user_id="u1", name="user", email="u@example.com")

    hub_details: dict = {"state": hub_state, "groupsEnabled": groups_enabled}
    if night_flag:
        # Alternate night-mode trigger: a dedicated boolean field rather than
        # the NIGHT_MODE_ON substring. Also reaches the buggy line 166.
        hub_details["nightMode"] = True

    api = MagicMock()
    api.async_get_hubs = AsyncMock(return_value=[{"hubId": "hub1", "hubName": "Maison"}])
    api.async_get_space_by_hub = AsyncMock(return_value={"id": "real1", "name": "Maison"})
    api.async_get_hub = AsyncMock(return_value=hub_details)
    api.async_get_rooms = AsyncMock(return_value=[])
    api.async_get_users = AsyncMock(return_value=[])
    api.async_get_groups = AsyncMock(return_value=[{"id": "g1", "groupName": "Group 1", "state": "ARMED"}])

    mixin.account = account
    mixin.api = api
    mixin.all_discovered_spaces = {}
    mixin._space_binding_cache = {}
    mixin._enabled_spaces = None
    mixin._skipped_state_change_hubs = set()
    # Initial load NOT done → the new-space/new-group discovery signals are
    # skipped (they need a real hass dispatcher), keeping this test focused
    # on the night-mode state path.
    mixin._initial_load_done = False
    mixin.sse_manager = None
    mixin.sqs_manager = None

    # Methods provided by sibling mixins in the real coordinator.
    mixin._update_polling_interval = MagicMock()
    mixin.has_pending_ha_action = MagicMock(return_value=False)
    mixin._create_event_from_state_change = MagicMock()
    # Real parser would map the string; the night-mode branch short-circuits
    # before calling it, but the disarmed branch needs a working impl.
    mixin._parse_security_state = MagicMock(return_value=SecurityState.DISARMED)

    return mixin, account


@pytest.mark.asyncio
async def test_night_mode_does_not_raise_nameerror() -> None:
    """#149: a hub in night mode must NOT raise NameError on SecurityState.

    This is the exact failure path: state contains NIGHT_MODE_ON, so the
    code hits ``security_state = SecurityState.NIGHT_MODE`` at runtime.
    """
    mixin, account = _make_mixin(hub_state="DISARMED_NIGHT_MODE_ON")
    # Must not raise.
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    assert account.spaces["hub1"].security_state is SecurityState.NIGHT_MODE


@pytest.mark.asyncio
async def test_night_mode_with_groups_enabled_is_the_149_repro() -> None:
    """The user's exact configuration: night mode + groups enabled."""
    mixin, account = _make_mixin(hub_state="DISARMED_NIGHT_MODE_ON", groups_enabled=True)
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    space = account.spaces["hub1"]
    assert space.security_state is SecurityState.NIGHT_MODE
    assert space.group_mode_enabled is True
    # Groups were fetched and parsed without error.
    assert "g1" in space.groups


@pytest.mark.asyncio
async def test_night_mode_via_boolean_flag_also_hits_buggy_line() -> None:
    """The ``nightMode: True`` field is an alternate trigger of the same
    runtime ``SecurityState.NIGHT_MODE`` assignment that broke #149.
    """
    mixin, account = _make_mixin(hub_state="DISARMED", night_flag=True)
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    assert account.spaces["hub1"].security_state is SecurityState.NIGHT_MODE


@pytest.mark.asyncio
async def test_non_night_mode_path_still_works() -> None:
    """The disarmed branch (calls _parse_security_state) must keep working."""
    mixin, account = _make_mixin(hub_state="DISARMED_NIGHT_MODE_OFF")
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    # _parse_security_state was consulted (night mode NOT active).
    mixin._parse_security_state.assert_called_once()
    assert account.spaces["hub1"].security_state is SecurityState.DISARMED


@pytest.mark.asyncio
async def test_security_state_is_imported_at_runtime() -> None:
    """Guard the import directly: SecurityState must be a runtime attribute
    of the module, not a TYPE_CHECKING-only name.
    """
    import custom_components.ajax._coordinator_spaces as mod

    assert hasattr(mod, "SecurityState"), "SecurityState must be importable at runtime"
    assert mod.SecurityState is SecurityState


# ---------------------------------------------------------------------------
# Regression: hub-details fetch failure must degrade gracefully, not crash the
# whole refresh tick with UnboundLocalError (code-review HIGH finding).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("full_refresh", [True, False])
@pytest.mark.asyncio
async def test_hub_details_failure_does_not_raise_unbound_local(full_refresh: bool) -> None:
    """If async_get_hub raises a transient API error, the except handler must
    leave is_new_space / real_space_id bound so the fall-through space
    create/update does not raise UnboundLocalError and crash the tick.
    """
    mixin, account = _make_mixin(hub_state="DISARMED")
    mixin.api.async_get_hub = AsyncMock(side_effect=AjaxRestApiError("transient 429"))

    # Must NOT raise (UnboundLocalError would propagate past the AjaxRestApiError
    # handlers in _async_update_data and flip last_update_success=False).
    await mixin._async_update_spaces_from_hubs(full_refresh=full_refresh)

    # The space is still created, degraded to NONE rather than crashing.
    space = account.spaces["hub1"]
    assert space.security_state is SecurityState.NONE


@pytest.mark.asyncio
async def test_hub_details_failure_on_existing_space_preserves_state() -> None:
    """A transient hub-details failure for an already-known space must not crash
    AND must not downgrade it to NONE: doing so would fire a phantom
    ajax_security_state_changed event and drop real_space_id (disabling its
    cameras/locks) on every network blip. The previous state is kept instead.
    """
    mixin, account = _make_mixin(hub_state="DISARMED_NIGHT_MODE_ON")
    # First successful tick creates the space in NIGHT_MODE with a real_space_id.
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    assert account.spaces["hub1"].security_state is SecurityState.NIGHT_MODE
    assert account.spaces["hub1"].real_space_id == "real1"

    # Next tick: hub-details fetch fails — must not raise, and must keep state.
    mixin.api.async_get_hub = AsyncMock(side_effect=AjaxRestApiError("boom"))
    await mixin._async_update_spaces_from_hubs(full_refresh=False)
    space = account.spaces["hub1"]
    assert space.security_state is SecurityState.NIGHT_MODE  # NOT degraded to NONE
    assert space.real_space_id == "real1"  # NOT cleared
    # No phantom state-change event was created for the transient failure.
    mixin._create_event_from_state_change.assert_not_called()


@pytest.mark.asyncio
async def test_hub_auth_error_propagates_for_reauth() -> None:
    """An AjaxRestAuthError on the per-hub fetch must propagate (not be swallowed
    into a NONE state), so _async_update_data can count it toward reauth.
    """
    from custom_components.ajax.api import AjaxRestAuthError

    mixin, _account = _make_mixin(hub_state="DISARMED")
    mixin.api.async_get_hub = AsyncMock(side_effect=AjaxRestAuthError("token expired"))
    with pytest.raises(AjaxRestAuthError):
        await mixin._async_update_spaces_from_hubs(full_refresh=True)


@pytest.mark.parametrize("bad_state", [None, 123])
@pytest.mark.asyncio
async def test_null_hub_state_does_not_crash(bad_state: object) -> None:
    """If the API returns state=null (or a non-string), the night-mode parse must
    not raise AttributeError on ``.upper()`` and flip every entity unavailable.
    """
    mixin, account = _make_mixin(hub_state="DISARMED")
    mixin.api.async_get_hub = AsyncMock(return_value={"state": bad_state, "groupsEnabled": False})
    # Must not raise (str(None).upper() is safe).
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    assert "hub1" in account.spaces
