"""Coverage tests for the coordinator state / init / spaces mixins.

These exercise the three carved-out mixins directly via ``object.__new__``
and hand-wired mocks (the canonical light-weight pattern for this project):

* ``_coordinator_state.py`` — parsers, video-edge / smart-lock pollers,
  ``get_smart_lock``, the no-op notification updater.
* ``_coordinator_init.py`` — account synthesis, smart-lock store
  save/restore/migrate, optional SSE/SQS bootstrap.
* ``_coordinator_spaces.py`` — extra branches of the per-tick refresh not
  already covered by ``test_coordinator_spaces_nightmode.py`` (real-time
  protection, group parsing, light vs full refresh, AjaxRestAuthError
  re-raise before the generic AjaxRestApiError handler).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ajax import _coordinator_init as init_mod
from custom_components.ajax._coordinator_init import (
    SMART_LOCK_STORE_VERSION,
    AjaxBootstrapMixin,
)
from custom_components.ajax._coordinator_spaces import AjaxSpacesMixin
from custom_components.ajax._coordinator_state import AjaxStateUpdaterMixin
from custom_components.ajax.api import AjaxRestApiError, AjaxRestAuthError
from custom_components.ajax.const import DOMAIN
from custom_components.ajax.models import (
    AjaxAccount,
    AjaxSmartLock,
    AjaxSpace,
    AjaxVideoEdge,
    GroupState,
    SecurityState,
    VideoEdgeType,
)

# ---------------------------------------------------------------------------
# _coordinator_state.py — parsers
# ---------------------------------------------------------------------------


def _state_mixin() -> AjaxStateUpdaterMixin:
    return AjaxStateUpdaterMixin()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("DISARMED", SecurityState.DISARMED),
        ("DISARMED_NIGHT_MODE_ON", SecurityState.DISARMED),
        ("PARTIALLY_ARMED", SecurityState.PARTIALLY_ARMED),
        ("ARMED_NIGHT_MODE_ON", SecurityState.NIGHT_MODE),
        ("NIGHT_MODE", SecurityState.NIGHT_MODE),
        ("ARMED", SecurityState.ARMED),
    ],
)
def test_parse_security_state(raw: str, expected: SecurityState) -> None:
    assert _state_mixin()._parse_security_state(raw) is expected


@pytest.mark.parametrize("bogus", [None, 0, "", "NOPE"])
def test_parse_security_state_none(bogus: object) -> None:
    assert _state_mixin()._parse_security_state(bogus) is SecurityState.NONE


def test_parse_device_type_exact_and_partial() -> None:
    mixin = _state_mixin()
    from custom_components.ajax.models import DeviceType

    # Exact cleaned hit.
    assert mixin._parse_device_type("MotionProtect") is DeviceType.MOTION_DETECTOR
    # type_lower hit (the cleaned first-token differs but full lower matches a key).
    assert mixin._parse_device_type("smart_lock") is DeviceType.SMART_LOCK
    # Partial substring fallback.
    assert mixin._parse_device_type("xx-keypad-xx") is DeviceType.KEYPAD
    # Non-string + garbage.
    assert mixin._parse_device_type(None) is DeviceType.UNKNOWN  # type: ignore[arg-type]
    assert mixin._parse_device_type("zzzzzz") is DeviceType.UNKNOWN


# ---------------------------------------------------------------------------
# _coordinator_state.py — video edges
# ---------------------------------------------------------------------------


def _ve_mixin(space: AjaxSpace | None, *, initial_load_done: bool = True) -> AjaxStateUpdaterMixin:
    mixin = object.__new__(AjaxStateUpdaterMixin)
    account = AjaxAccount(user_id="u", name="u", email="u@e.com")
    if space is not None:
        account.spaces[space.id] = space
    mixin.account = account
    mixin.api = MagicMock()
    mixin.hass = MagicMock()
    mixin.entry_id = "entry_test"
    mixin._initial_load_done = initial_load_done
    return mixin


@pytest.mark.asyncio
async def test_video_edges_no_account() -> None:
    mixin = object.__new__(AjaxStateUpdaterMixin)
    mixin.account = None
    # Must return early without touching api.
    await mixin._async_update_video_edges("space1")


@pytest.mark.asyncio
async def test_video_edges_unknown_space() -> None:
    mixin = _ve_mixin(None)
    await mixin._async_update_video_edges("missing")


@pytest.mark.asyncio
async def test_video_edges_no_real_space_id() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id=None)
    mixin = _ve_mixin(space)
    await mixin._async_update_video_edges("s1")
    # api not consulted when real_space_id missing.
    assert not space.video_edges


@pytest.mark.asyncio
async def test_video_edges_add_new_with_full_payload() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id="real1")
    space.rooms = {"r1": MagicMock(name="kitchen")}
    space.rooms["r1"].name = "Kitchen"
    mixin = _ve_mixin(space)
    mixin.api.async_get_video_edges = AsyncMock(
        return_value=[
            {
                "id": "ve1",
                "name": "Front Cam",
                "type": "TURRET",
                "color": "white",
                "connectionState": "ONLINE",
                "firmware": {"currentVersion": "1.2.3"},
                "networkInterface": {
                    "ethernet": {
                        "macAddress": "AA:BB",
                        "configuration": {"v4": {"address": "192.168.1.10"}},
                    },
                    "wifi": {},
                },
                "channels": [{"spaceSettings": {"roomId": "r1"}}],
            }
        ]
    )
    with patch.object(init_mod, "ir"):  # unrelated, keep import alive
        await mixin._async_update_video_edges("s1")

    ve = space.video_edges["ve1"]
    assert ve.video_edge_type is VideoEdgeType.TURRET
    assert ve.ip_address == "192.168.1.10"
    assert ve.mac_address == "AA:BB"
    assert ve.firmware_version == "1.2.3"
    assert ve.room_id == "r1"
    assert ve.room_name == "Kitchen"
    assert ve.connection_state == "ONLINE"


@pytest.mark.asyncio
async def test_video_edges_wifi_fallback_and_unknown_type_and_dict_channels() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id="real1")
    mixin = _ve_mixin(space)
    mixin.api.async_get_video_edges = AsyncMock(
        return_value=[
            {
                "id": "ve2",
                "type": "NOT_A_REAL_TYPE",  # -> UNKNOWN via ValueError branch
                "networkInterface": {
                    "ethernet": {},  # no ip -> wifi fallback
                    "wifi": {
                        "macAddress": "WIFIMAC",
                        "configuration": {"v4": {"address": "10.0.0.5"}},
                    },
                },
                "channels": {"spaceSettings": {"roomId": "unknown_room"}},  # dict -> wrapped in list
            }
        ]
    )
    await mixin._async_update_video_edges("s1")
    ve = space.video_edges["ve2"]
    assert ve.video_edge_type is VideoEdgeType.UNKNOWN
    assert ve.ip_address == "10.0.0.5"
    assert ve.mac_address == "WIFIMAC"
    # roomId not in space.rooms -> room_name stays None
    assert ve.room_name is None
    # default name derived from id.
    assert ve.name.startswith("Camera ")


@pytest.mark.asyncio
async def test_video_edges_skips_entry_without_id_and_bad_channels() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id="real1")
    mixin = _ve_mixin(space)
    mixin.api.async_get_video_edges = AsyncMock(
        return_value=[
            {"name": "no id here"},  # skipped (no id)
            {"id": "ve3", "channels": "garbage"},  # channels not list/dict -> []
        ]
    )
    await mixin._async_update_video_edges("s1")
    assert "ve3" in space.video_edges
    assert space.video_edges["ve3"].channels == []
    assert len(space.video_edges) == 1


@pytest.mark.asyncio
async def test_video_edges_update_existing() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id="real1")
    space.video_edges["ve1"] = AjaxVideoEdge(id="ve1", name="Old", space_id="s1")
    mixin = _ve_mixin(space)
    mixin.api.async_get_video_edges = AsyncMock(
        return_value=[{"id": "ve1", "name": "New Name", "type": "BULLET", "connectionState": "OFFLINE"}]
    )
    await mixin._async_update_video_edges("s1")
    ve = space.video_edges["ve1"]
    assert ve.name == "New Name"
    assert ve.video_edge_type is VideoEdgeType.BULLET
    assert ve.connection_state == "OFFLINE"


@pytest.mark.asyncio
async def test_video_edges_dispatches_signal_on_initial_load_done() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id="real1")
    mixin = _ve_mixin(space, initial_load_done=True)
    mixin.api.async_get_video_edges = AsyncMock(return_value=[{"id": "ve9", "type": "INDOOR"}])
    with patch("custom_components.ajax._coordinator_state.async_dispatcher_send") as send:
        await mixin._async_update_video_edges("s1")
    send.assert_called_once()


@pytest.mark.asyncio
async def test_video_edges_no_dispatch_before_initial_load() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id="real1")
    mixin = _ve_mixin(space, initial_load_done=False)
    mixin.api.async_get_video_edges = AsyncMock(return_value=[{"id": "ve9", "type": "INDOOR"}])
    with patch("custom_components.ajax._coordinator_state.async_dispatcher_send") as send:
        await mixin._async_update_video_edges("s1")
    send.assert_not_called()


@pytest.mark.asyncio
async def test_video_edges_cleanup_removes_stale_with_ha_device() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id="real1")
    space.video_edges["stale"] = AjaxVideoEdge(id="stale", name="Gone", space_id="s1")
    mixin = _ve_mixin(space)
    mixin.api.async_get_video_edges = AsyncMock(return_value=[{"id": "kept", "type": "NVR"}])

    registry = MagicMock()
    ha_device = MagicMock(id="dev-entry")
    registry.async_get_device.return_value = ha_device
    with patch("custom_components.ajax._coordinator_state.dr.async_get", return_value=registry):
        await mixin._async_update_video_edges("s1")

    assert "stale" not in space.video_edges
    assert "kept" in space.video_edges
    registry.async_remove_device.assert_called_once_with("dev-entry")
    # The stale device is looked up by its entry-namespaced identifier.
    registry.async_get_device.assert_called_once_with(identifiers={(DOMAIN, "entry_test_stale")})


@pytest.mark.asyncio
async def test_video_edges_cleanup_no_ha_device() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id="real1")
    space.video_edges["stale"] = AjaxVideoEdge(id="stale", name="Gone", space_id="s1")
    mixin = _ve_mixin(space)
    mixin.api.async_get_video_edges = AsyncMock(return_value=[{"id": "kept", "type": "NVR"}])

    registry = MagicMock()
    registry.async_get_device.return_value = None
    with patch("custom_components.ajax._coordinator_state.dr.async_get", return_value=registry):
        await mixin._async_update_video_edges("s1")

    assert "stale" not in space.video_edges
    registry.async_remove_device.assert_not_called()


@pytest.mark.asyncio
async def test_video_edges_auth_error_propagates() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id="real1")
    mixin = _ve_mixin(space)
    mixin.api.async_get_video_edges = AsyncMock(side_effect=AjaxRestAuthError("expired"))
    with pytest.raises(AjaxRestAuthError):
        await mixin._async_update_video_edges("s1")


@pytest.mark.asyncio
async def test_video_edges_generic_error_swallowed() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id="real1")
    mixin = _ve_mixin(space)
    mixin.api.async_get_video_edges = AsyncMock(side_effect=RuntimeError("boom"))
    # Must NOT raise — generic errors are logged and swallowed.
    await mixin._async_update_video_edges("s1")


# ---------------------------------------------------------------------------
# _coordinator_state.py — smart locks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smart_locks_no_account() -> None:
    mixin = object.__new__(AjaxStateUpdaterMixin)
    mixin.account = None
    await mixin._async_update_smart_locks("s1")


@pytest.mark.asyncio
async def test_smart_locks_unknown_space_and_no_real_id() -> None:
    mixin = _ve_mixin(None)
    await mixin._async_update_smart_locks("missing")

    space = AjaxSpace(id="s1", name="Maison", real_space_id=None)
    mixin2 = _ve_mixin(space)
    await mixin2._async_update_smart_locks("s1")
    assert not space.smart_locks


@pytest.mark.asyncio
async def test_smart_locks_add_new_and_skip_yale() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id="real1")
    mixin = _ve_mixin(space, initial_load_done=True)
    mixin.api.async_get_smart_locks = AsyncMock(
        return_value=[
            {"id": "lock1", "name": "Front Door", "type": "smartlock"},  # full -> added
            {"id": "yale1"},  # minimal -> Yale cloud, skipped
            {"name": "no id"},  # no id -> skipped
        ]
    )
    with patch("custom_components.ajax._coordinator_state.async_dispatcher_send") as send:
        await mixin._async_update_smart_locks("s1")

    assert "lock1" in space.smart_locks
    assert "yale1" not in space.smart_locks
    send.assert_called_once()


@pytest.mark.asyncio
async def test_smart_locks_update_existing() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id="real1")
    space.smart_locks["lock1"] = AjaxSmartLock(id="lock1", name="Old", space_id="s1")
    mixin = _ve_mixin(space)
    mixin.api.async_get_smart_locks = AsyncMock(return_value=[{"id": "lock1", "name": "Renamed", "type": "smartlock"}])
    await mixin._async_update_smart_locks("s1")
    assert space.smart_locks["lock1"].name == "Renamed"
    assert space.smart_locks["lock1"].raw_data["type"] == "smartlock"


@pytest.mark.asyncio
async def test_smart_locks_update_existing_keeps_name_when_absent() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id="real1")
    space.smart_locks["lock1"] = AjaxSmartLock(id="lock1", name="Keep", space_id="s1")
    mixin = _ve_mixin(space)
    mixin.api.async_get_smart_locks = AsyncMock(return_value=[{"id": "lock1", "type": "smartlock"}])
    await mixin._async_update_smart_locks("s1")
    assert space.smart_locks["lock1"].name == "Keep"


@pytest.mark.asyncio
async def test_smart_locks_cleanup_removes_api_lock_preserves_sse_lock() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id="real1")
    # API-discovered lock that will disappear (has raw_data) -> removed.
    space.smart_locks["api_gone"] = AjaxSmartLock(
        id="api_gone", name="API Gone", space_id="s1", raw_data={"id": "api_gone", "name": "x"}
    )
    # SSE-discovered lock (no raw_data) -> preserved even if absent from API.
    space.smart_locks["sse_lock"] = AjaxSmartLock(id="sse_lock", name="SSE", space_id="s1")
    mixin = _ve_mixin(space)
    mixin.api.async_get_smart_locks = AsyncMock(return_value=[{"id": "kept", "name": "Kept", "type": "smartlock"}])

    registry = MagicMock()
    registry.async_get_device.return_value = MagicMock(id="dev-1")
    with patch("custom_components.ajax._coordinator_state.dr.async_get", return_value=registry):
        await mixin._async_update_smart_locks("s1")

    assert "api_gone" not in space.smart_locks
    assert "sse_lock" in space.smart_locks  # preserved
    assert "kept" in space.smart_locks
    registry.async_remove_device.assert_called_once_with("dev-1")
    # The stale API lock is looked up by its entry-namespaced identifier.
    registry.async_get_device.assert_called_once_with(identifiers={(DOMAIN, "entry_test_api_gone")})


@pytest.mark.asyncio
async def test_smart_locks_cleanup_no_ha_device() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id="real1")
    space.smart_locks["api_gone"] = AjaxSmartLock(
        id="api_gone", name="API Gone", space_id="s1", raw_data={"id": "api_gone", "name": "x"}
    )
    mixin = _ve_mixin(space)
    mixin.api.async_get_smart_locks = AsyncMock(return_value=[{"id": "kept", "name": "Kept", "type": "smartlock"}])
    registry = MagicMock()
    registry.async_get_device.return_value = None
    with patch("custom_components.ajax._coordinator_state.dr.async_get", return_value=registry):
        await mixin._async_update_smart_locks("s1")
    assert "api_gone" not in space.smart_locks


@pytest.mark.asyncio
async def test_smart_locks_auth_error_propagates() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id="real1")
    mixin = _ve_mixin(space)
    mixin.api.async_get_smart_locks = AsyncMock(side_effect=AjaxRestAuthError("expired"))
    with pytest.raises(AjaxRestAuthError):
        await mixin._async_update_smart_locks("s1")


@pytest.mark.asyncio
async def test_smart_locks_generic_error_swallowed() -> None:
    space = AjaxSpace(id="s1", name="Maison", real_space_id="real1")
    mixin = _ve_mixin(space)
    mixin.api.async_get_smart_locks = AsyncMock(side_effect=RuntimeError("boom"))
    await mixin._async_update_smart_locks("s1")


# ---------------------------------------------------------------------------
# _coordinator_state.py — get_smart_lock + notifications no-op
# ---------------------------------------------------------------------------


def test_get_smart_lock_found_and_missing() -> None:
    mixin = object.__new__(AjaxStateUpdaterMixin)
    space = AjaxSpace(id="s1", name="Maison")
    lock = AjaxSmartLock(id="lock1", name="L", space_id="s1")
    space.smart_locks["lock1"] = lock
    mixin.get_space = lambda sid: space if sid == "s1" else None

    assert mixin.get_smart_lock("s1", "lock1") is lock
    assert mixin.get_smart_lock("s1", "nope") is None
    assert mixin.get_smart_lock("other", "lock1") is None


@pytest.mark.asyncio
async def test_update_notifications_counts_unread() -> None:
    mixin = object.__new__(AjaxStateUpdaterMixin)
    account = AjaxAccount(user_id="u", name="u", email="u@e.com")
    space = AjaxSpace(id="s1", name="Maison")
    space.notifications = [
        MagicMock(read=False),
        MagicMock(read=True),
        MagicMock(read=False),
    ]
    account.spaces["s1"] = space
    mixin.account = account

    await mixin._async_update_notifications("s1")
    assert space.unread_notifications == 2


@pytest.mark.asyncio
async def test_update_notifications_no_account_and_unknown_space() -> None:
    mixin = object.__new__(AjaxStateUpdaterMixin)
    mixin.account = None
    await mixin._async_update_notifications("s1")  # no account branch

    account = AjaxAccount(user_id="u", name="u", email="u@e.com")
    mixin.account = account
    await mixin._async_update_notifications("missing")  # unknown space branch


# ---------------------------------------------------------------------------
# _coordinator_init.py — account synthesis
# ---------------------------------------------------------------------------


def _bootstrap() -> AjaxBootstrapMixin:
    return object.__new__(AjaxBootstrapMixin)


@pytest.mark.asyncio
async def test_init_account_from_login() -> None:
    mixin = _bootstrap()
    mixin.api = MagicMock(user_id="user-12345678-rest", email="john@example.com")
    await mixin._async_init_account()
    assert mixin.account is not None
    assert mixin.account.user_id == "user-12345678-rest"
    assert mixin.account.name == "john"
    assert mixin.account.email == "john@example.com"


@pytest.mark.asyncio
async def test_init_account_handles_missing_fields() -> None:
    mixin = _bootstrap()
    mixin.api = MagicMock(user_id=None, email=None)
    await mixin._async_init_account()
    assert mixin.account.user_id == ""
    assert mixin.account.name == "Unknown"
    assert mixin.account.email == ""


# ---------------------------------------------------------------------------
# _coordinator_init.py — smart-lock store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migrate_smart_locks_store_passthrough() -> None:
    out = await AjaxBootstrapMixin._async_migrate_smart_locks_store(0, 0, {"s1": []})
    assert out == {"s1": []}
    # None payload -> empty dict.
    out_none = await AjaxBootstrapMixin._async_migrate_smart_locks_store(0, 0, None)  # type: ignore[arg-type]
    assert out_none == {}
    assert SMART_LOCK_STORE_VERSION == 1


@pytest.mark.asyncio
async def test_save_smart_locks_persists_only_sse_locks() -> None:
    mixin = _bootstrap()
    account = AjaxAccount(user_id="u", name="u", email="u@e.com")
    space = AjaxSpace(id="s1", name="Maison")
    # SSE lock (no raw_data) -> persisted.
    sse_lock = AjaxSmartLock(id="sse1", name="SSE Lock", space_id="s1")
    sse_lock.is_locked = True
    sse_lock.is_door_open = False
    sse_lock.last_changed_by = "Alice"
    # API lock (has raw_data) -> skipped.
    api_lock = AjaxSmartLock(id="api1", name="API Lock", space_id="s1", raw_data={"id": "api1"})
    space.smart_locks = {"sse1": sse_lock, "api1": api_lock}
    account.spaces["s1"] = space
    mixin.account = account
    mixin._smart_lock_store = MagicMock()
    mixin._smart_lock_store.async_save = AsyncMock()

    await mixin._async_save_smart_locks()

    mixin._smart_lock_store.async_save.assert_awaited_once()
    saved = mixin._smart_lock_store.async_save.await_args.args[0]
    assert list(saved.keys()) == ["s1"]
    assert saved["s1"] == [
        {
            "id": "sse1",
            "name": "SSE Lock",
            "is_locked": True,
            "is_door_open": False,
            "last_changed_by": "Alice",
        }
    ]


@pytest.mark.asyncio
async def test_save_smart_locks_no_account_or_empty() -> None:
    mixin = _bootstrap()
    mixin.account = None
    mixin._smart_lock_store = MagicMock()
    mixin._smart_lock_store.async_save = AsyncMock()
    await mixin._async_save_smart_locks()
    mixin._smart_lock_store.async_save.assert_not_awaited()

    # Account with only API locks -> nothing to persist -> no save call.
    account = AjaxAccount(user_id="u", name="u", email="u@e.com")
    space = AjaxSpace(id="s1", name="Maison")
    space.smart_locks = {"api1": AjaxSmartLock(id="api1", name="x", space_id="s1", raw_data={"id": "api1"})}
    account.spaces["s1"] = space
    mixin.account = account
    await mixin._async_save_smart_locks()
    mixin._smart_lock_store.async_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_smart_locks_repopulates() -> None:
    mixin = _bootstrap()
    account = AjaxAccount(user_id="u", name="u", email="u@e.com")
    space = AjaxSpace(id="s1", name="Maison")
    # Existing lock with same id is skipped.
    space.smart_locks["dup"] = AjaxSmartLock(id="dup", name="existing", space_id="s1")
    account.spaces["s1"] = space
    mixin.account = account
    mixin._smart_lock_store = MagicMock()
    mixin._smart_lock_store.async_load = AsyncMock(
        return_value={
            "s1": [
                {"id": "new1", "name": "New", "is_locked": True, "is_door_open": False, "last_changed_by": "Bob"},
                {"id": "dup", "name": "ignored"},  # already present -> skipped
                {"name": "no id"},  # no id -> skipped
            ],
            "unknown_space": [{"id": "ghost"}],  # space not in account -> skipped
        }
    )

    await mixin._async_restore_smart_locks()

    assert "new1" in space.smart_locks
    restored = space.smart_locks["new1"]
    assert restored.is_locked is True
    assert restored.is_door_open is False
    assert restored.last_changed_by == "Bob"
    assert space.smart_locks["dup"].name == "existing"  # untouched


@pytest.mark.asyncio
async def test_restore_smart_locks_empty_and_no_account() -> None:
    mixin = _bootstrap()
    mixin.account = None
    mixin._smart_lock_store = MagicMock()
    mixin._smart_lock_store.async_load = AsyncMock(return_value={})
    await mixin._async_restore_smart_locks()  # no account branch

    account = AjaxAccount(user_id="u", name="u", email="u@e.com")
    mixin.account = account
    # Empty / non-dict payloads -> early return.
    mixin._smart_lock_store.async_load = AsyncMock(return_value=None)
    await mixin._async_restore_smart_locks()
    mixin._smart_lock_store.async_load = AsyncMock(return_value=["not", "a", "dict"])
    await mixin._async_restore_smart_locks()


# ---------------------------------------------------------------------------
# _coordinator_init.py — SQS bootstrap
# ---------------------------------------------------------------------------


def _sqs_mixin() -> AjaxBootstrapMixin:
    mixin = _bootstrap()
    mixin.api = MagicMock()
    mixin.hass = MagicMock()
    mixin.hass.config.language = "fr"
    mixin.config_entry = MagicMock(entry_id="entry-1")
    mixin.sqs_manager = None
    mixin.sse_manager = None
    mixin._aws_access_key_id = "AKIA"
    mixin._aws_secret_access_key = "secret"
    mixin._queue_name = "queue"
    mixin._sqs_initialized = False
    mixin._sse_url = "https://proxy/sse"
    mixin._sse_initialized = False
    return mixin


@pytest.mark.asyncio
async def test_init_sqs_not_available() -> None:
    mixin = _sqs_mixin()
    with patch.object(init_mod, "SQS_AVAILABLE", False):
        await mixin._async_init_sqs()
    assert mixin._sqs_initialized is True
    assert mixin.sqs_manager is None


@pytest.mark.asyncio
async def test_init_sqs_no_credentials() -> None:
    mixin = _sqs_mixin()
    mixin._aws_access_key_id = None
    with patch.object(init_mod, "SQS_AVAILABLE", True):
        await mixin._async_init_sqs()
    assert mixin._sqs_initialized is True
    assert mixin.sqs_manager is None


@pytest.mark.asyncio
async def test_init_sqs_success() -> None:
    mixin = _sqs_mixin()
    manager = MagicMock()
    manager.set_language = MagicMock()
    manager.start = AsyncMock(return_value=True)
    with (
        patch.object(init_mod, "SQS_AVAILABLE", True),
        patch.object(init_mod, "_AjaxSQSClient", MagicMock()),
        patch.object(init_mod, "_SQSManager", MagicMock(return_value=manager)),
        patch.object(init_mod, "ir") as ir_mod,
    ):
        await mixin._async_init_sqs()

    assert mixin.sqs_manager is manager
    manager.set_language.assert_called_once_with("fr")
    ir_mod.async_delete_issue.assert_called_once()
    assert mixin._sqs_initialized is True


@pytest.mark.asyncio
async def test_init_sqs_start_returns_false_creates_issue() -> None:
    mixin = _sqs_mixin()
    manager = MagicMock()
    manager.start = AsyncMock(return_value=False)
    with (
        patch.object(init_mod, "SQS_AVAILABLE", True),
        patch.object(init_mod, "_AjaxSQSClient", MagicMock()),
        patch.object(init_mod, "_SQSManager", MagicMock(return_value=manager)),
        patch.object(init_mod, "ir") as ir_mod,
    ):
        await mixin._async_init_sqs()

    assert mixin.sqs_manager is None
    ir_mod.async_create_issue.assert_called_once()
    assert mixin._sqs_initialized is True


@pytest.mark.asyncio
async def test_init_sqs_exception_creates_issue() -> None:
    mixin = _sqs_mixin()
    with (
        patch.object(init_mod, "SQS_AVAILABLE", True),
        patch.object(init_mod, "_AjaxSQSClient", MagicMock(side_effect=RuntimeError("boom"))),
        patch.object(init_mod, "_SQSManager", MagicMock()),
        patch.object(init_mod, "ir") as ir_mod,
    ):
        await mixin._async_init_sqs()

    assert mixin.sqs_manager is None
    ir_mod.async_create_issue.assert_called_once()
    assert mixin._sqs_initialized is True


@pytest.mark.asyncio
async def test_init_sqs_no_config_entry_uses_unknown() -> None:
    mixin = _sqs_mixin()
    mixin.config_entry = None
    manager = MagicMock()
    manager.start = AsyncMock(return_value=True)
    with (
        patch.object(init_mod, "SQS_AVAILABLE", True),
        patch.object(init_mod, "_AjaxSQSClient", MagicMock()),
        patch.object(init_mod, "_SQSManager", MagicMock(return_value=manager)),
        patch.object(init_mod, "ir"),
    ):
        await mixin._async_init_sqs()
    assert mixin.sqs_manager is manager


# ---------------------------------------------------------------------------
# _coordinator_init.py — SSE bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_sse_not_available() -> None:
    mixin = _sqs_mixin()
    with patch.object(init_mod, "SSE_AVAILABLE", False):
        await mixin._async_init_sse()
    assert mixin._sse_initialized is True
    assert mixin.sse_manager is None


@pytest.mark.asyncio
async def test_init_sse_no_url() -> None:
    mixin = _sqs_mixin()
    mixin._sse_url = None
    with patch.object(init_mod, "SSE_AVAILABLE", True):
        await mixin._async_init_sse()
    assert mixin._sse_initialized is True
    assert mixin.sse_manager is None


@pytest.mark.asyncio
async def test_init_sse_no_session_token() -> None:
    mixin = _sqs_mixin()
    mixin.api.session_token = None
    with patch.object(init_mod, "SSE_AVAILABLE", True):
        await mixin._async_init_sse()
    assert mixin._sse_initialized is True
    assert mixin.sse_manager is None


@pytest.mark.asyncio
async def test_init_sse_success() -> None:
    mixin = _sqs_mixin()
    mixin.api.session_token = "tok"
    mixin.api.user_id = "uid"
    mixin.api.verify_ssl = True
    manager = MagicMock()
    manager.start = AsyncMock(return_value=True)
    with (
        patch.object(init_mod, "SSE_AVAILABLE", True),
        patch.object(init_mod, "_AjaxSSEClient", MagicMock()),
        patch.object(init_mod, "_SSEManager", MagicMock(return_value=manager)),
        patch.object(init_mod, "ir") as ir_mod,
    ):
        await mixin._async_init_sse()

    assert mixin.sse_manager is manager
    manager.set_language.assert_called_once_with("fr")
    ir_mod.async_delete_issue.assert_called_once()
    assert mixin._sse_initialized is True


@pytest.mark.asyncio
async def test_init_sse_start_false_creates_issue() -> None:
    mixin = _sqs_mixin()
    mixin.api.session_token = "tok"
    manager = MagicMock()
    manager.start = AsyncMock(return_value=False)
    with (
        patch.object(init_mod, "SSE_AVAILABLE", True),
        patch.object(init_mod, "_AjaxSSEClient", MagicMock()),
        patch.object(init_mod, "_SSEManager", MagicMock(return_value=manager)),
        patch.object(init_mod, "ir") as ir_mod,
    ):
        await mixin._async_init_sse()

    assert mixin.sse_manager is None
    ir_mod.async_create_issue.assert_called_once()


@pytest.mark.asyncio
async def test_init_sse_exception_creates_issue() -> None:
    mixin = _sqs_mixin()
    mixin.api.session_token = "tok"
    with (
        patch.object(init_mod, "SSE_AVAILABLE", True),
        patch.object(init_mod, "_AjaxSSEClient", MagicMock(side_effect=RuntimeError("boom"))),
        patch.object(init_mod, "_SSEManager", MagicMock()),
        patch.object(init_mod, "ir") as ir_mod,
    ):
        await mixin._async_init_sse()

    assert mixin.sse_manager is None
    ir_mod.async_create_issue.assert_called_once()
    assert mixin._sse_initialized is True


# ---------------------------------------------------------------------------
# _coordinator_spaces.py — additional branches
# ---------------------------------------------------------------------------


def _spaces_mixin() -> tuple[AjaxSpacesMixin, AjaxAccount]:
    mixin = object.__new__(AjaxSpacesMixin)
    account = AjaxAccount(user_id="u1", name="user", email="u@e.com")
    api = MagicMock()
    api.async_get_hubs = AsyncMock(return_value=[{"hubId": "hub1", "hubName": "Maison"}])
    api.async_get_space_by_hub = AsyncMock(return_value={"id": "real1", "name": "Maison"})
    api.async_get_hub = AsyncMock(return_value={"state": "ARMED", "groupsEnabled": False})
    api.async_get_rooms = AsyncMock(return_value=[{"id": "r1", "roomName": "Kitchen", "imageId": "img"}])
    api.async_get_users = AsyncMock(return_value=[{"id": "user-a"}])
    api.async_get_groups = AsyncMock(return_value=[])
    mixin.account = account
    mixin.api = api
    mixin.all_discovered_spaces = {}
    mixin._space_binding_cache = {}
    mixin._enabled_spaces = None
    mixin._skipped_state_change_hubs = set()
    mixin._initial_load_done = False
    mixin.sse_manager = None
    mixin.sqs_manager = None
    mixin._update_polling_interval = MagicMock()
    mixin.has_pending_ha_action = MagicMock(return_value=False)
    mixin._create_event_from_state_change = MagicMock()
    mixin._fire_security_state_event = MagicMock()
    mixin._parse_security_state = MagicMock(return_value=SecurityState.ARMED)
    return mixin, account


@pytest.mark.asyncio
async def test_spaces_no_account_returns_early() -> None:
    mixin = object.__new__(AjaxSpacesMixin)
    mixin.account = None
    mixin.api = MagicMock()
    mixin.api.async_get_hubs = AsyncMock()
    await mixin._async_update_spaces_from_hubs()
    mixin.api.async_get_hubs.assert_not_called()


@pytest.mark.asyncio
async def test_spaces_skips_hub_without_id() -> None:
    mixin, account = _spaces_mixin()
    mixin.api.async_get_hubs = AsyncMock(return_value=[{"hubName": "no hub id"}])
    await mixin._async_update_spaces_from_hubs()
    assert account.spaces == {}


@pytest.mark.asyncio
async def test_spaces_skips_disabled_space() -> None:
    mixin, account = _spaces_mixin()
    mixin._enabled_spaces = ["other-hub"]  # hub1 not enabled -> skipped
    await mixin._async_update_spaces_from_hubs()
    assert "hub1" not in account.spaces
    # But it's still discovered for the options flow.
    assert "hub1" in mixin.all_discovered_spaces


@pytest.mark.asyncio
async def test_spaces_creates_new_space_with_rooms_and_users() -> None:
    mixin, account = _spaces_mixin()
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    space = account.spaces["hub1"]
    assert space.security_state is SecurityState.ARMED
    assert space.real_space_id == "real1"
    assert "r1" in space.rooms
    assert space.rooms["r1"].name == "Kitchen"
    assert space.users == [{"id": "user-a"}]
    mixin._update_polling_interval.assert_called()


@pytest.mark.asyncio
async def test_spaces_rooms_fetch_error_is_logged_not_raised() -> None:
    mixin, account = _spaces_mixin()
    mixin.api.async_get_rooms = AsyncMock(side_effect=RuntimeError("rooms boom"))
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    # Space still created despite room failure.
    assert "hub1" in account.spaces


@pytest.mark.asyncio
async def test_spaces_users_fetch_api_error_sets_empty_list() -> None:
    mixin, account = _spaces_mixin()
    mixin.api.async_get_users = AsyncMock(side_effect=AjaxRestApiError("users boom"))
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    assert account.spaces["hub1"].users == []


@pytest.mark.asyncio
async def test_spaces_users_auth_error_propagates() -> None:
    mixin, _account = _spaces_mixin()
    mixin.api.async_get_users = AsyncMock(side_effect=AjaxRestAuthError("token"))
    with pytest.raises(AjaxRestAuthError):
        await mixin._async_update_spaces_from_hubs(full_refresh=True)


@pytest.mark.asyncio
async def test_spaces_space_binding_auth_error_propagates() -> None:
    """AjaxRestAuthError on async_get_space_by_hub must re-raise before the
    generic AjaxRestApiError handler swallows it."""
    mixin, _account = _spaces_mixin()
    mixin.api.async_get_space_by_hub = AsyncMock(side_effect=AjaxRestAuthError("token"))
    with pytest.raises(AjaxRestAuthError):
        await mixin._async_update_spaces_from_hubs(full_refresh=True)


@pytest.mark.asyncio
async def test_spaces_space_binding_api_error_is_swallowed() -> None:
    mixin, account = _spaces_mixin()
    mixin.api.async_get_space_by_hub = AsyncMock(side_effect=AjaxRestApiError("transient"))
    # Falls back to hubName; must not raise.
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    assert "hub1" in account.spaces


@pytest.mark.asyncio
async def test_spaces_groups_parsing_armed_disarmed_none() -> None:
    mixin, account = _spaces_mixin()
    mixin.api.async_get_hub = AsyncMock(return_value={"state": "ARMED", "groupsEnabled": True})
    mixin.api.async_get_groups = AsyncMock(
        return_value=[
            {"id": "g_armed", "groupName": "Armed", "state": "ARMED"},
            {"id": "g_disarmed", "groupName": "Disarmed", "state": "DISARMED"},
            {"id": "g_none", "groupName": "Other", "state": "WEIRD"},
            {"groupName": "no id"},  # skipped
        ]
    )
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    groups = account.spaces["hub1"].groups
    assert groups["g_armed"].state is GroupState.ARMED
    assert groups["g_disarmed"].state is GroupState.DISARMED
    assert groups["g_none"].state is GroupState.NONE
    assert account.spaces["hub1"].group_mode_enabled is True


@pytest.mark.asyncio
async def test_spaces_groups_protect_optimistic_when_ha_action_pending() -> None:
    mixin, account = _spaces_mixin()
    mixin.api.async_get_hub = AsyncMock(return_value={"state": "ARMED", "groupsEnabled": True})
    # Seed an existing group in ARMED.
    space = AjaxSpace(id="hub1", name="Maison", hub_id="hub1")
    from custom_components.ajax.models import AjaxGroup

    space.groups["g1"] = AjaxGroup(id="g1", name="G1", space_id="hub1", state=GroupState.ARMED)
    account.spaces["hub1"] = space
    mixin.has_pending_ha_action = MagicMock(return_value=True)
    mixin.api.async_get_groups = AsyncMock(return_value=[{"id": "g1", "groupName": "G1", "state": "DISARMED"}])
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    # REST said DISARMED but HA action pending -> kept ARMED.
    assert account.spaces["hub1"].groups["g1"].state is GroupState.ARMED


@pytest.mark.asyncio
async def test_spaces_groups_api_error_swallowed() -> None:
    mixin, account = _spaces_mixin()
    mixin.api.async_get_hub = AsyncMock(return_value={"state": "ARMED", "groupsEnabled": True})
    mixin.api.async_get_groups = AsyncMock(side_effect=AjaxRestApiError("groups boom"))
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    assert "hub1" in account.spaces


@pytest.mark.asyncio
async def test_spaces_groups_auth_error_propagates() -> None:
    mixin, _account = _spaces_mixin()
    mixin.api.async_get_hub = AsyncMock(return_value={"state": "ARMED", "groupsEnabled": True})
    mixin.api.async_get_groups = AsyncMock(side_effect=AjaxRestAuthError("token"))
    with pytest.raises(AjaxRestAuthError):
        await mixin._async_update_spaces_from_hubs(full_refresh=True)


@pytest.mark.asyncio
async def test_spaces_groups_fetched_on_light_tick_for_group_hub() -> None:
    """Group-mode hubs refetch groups on light ticks too (issue #150).

    The per-group arm state self-heals every poll instead of waiting for the
    hourly metadata refresh, even while SSE/SQS is active — a real-time group
    event may have been deduped/dropped or read back stale.
    """
    mixin, account = _spaces_mixin()
    # Pre-create the space so light refresh path is taken (is_new_space False).
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    mixin.sse_manager = MagicMock()
    mixin.sse_manager.is_state_protected = MagicMock(return_value=False)
    mixin.api.async_get_hub = AsyncMock(return_value={"state": "ARMED", "groupsEnabled": True})
    mixin.api.async_get_groups = AsyncMock(return_value=[{"id": "g1", "groupName": "G1", "state": "ARMED"}])
    await mixin._async_update_spaces_from_hubs(full_refresh=False)
    mixin.api.async_get_groups.assert_called_once()


@pytest.mark.asyncio
async def test_spaces_groups_self_heal_stale_state_on_light_tick() -> None:
    """A stale per-group state corrects on the next light tick (issue #150).

    Simulates the bug: a group is shown DISARMED (a real-time arm event was
    dropped), but the next state-only poll re-reads ARMED from /groups and
    fixes the indicator without a full metadata refresh.
    """
    from custom_components.ajax.models import AjaxGroup

    mixin, account = _spaces_mixin()
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    space = account.spaces["hub1"]
    space.groups["g1"] = AjaxGroup(id="g1", name="G1", space_id="hub1", state=GroupState.DISARMED)
    # Realtime active, no pending HA action -> REST is authoritative.
    mixin.sse_manager = MagicMock()
    mixin.sse_manager.is_state_protected = MagicMock(return_value=False)
    mixin.has_pending_ha_action = MagicMock(return_value=False)
    mixin.api.async_get_hub = AsyncMock(return_value={"state": "ARMED", "groupsEnabled": True})
    mixin.api.async_get_groups = AsyncMock(return_value=[{"id": "g1", "groupName": "G1", "state": "ARMED"}])
    await mixin._async_update_spaces_from_hubs(full_refresh=False)
    assert account.spaces["hub1"].groups["g1"].state is GroupState.ARMED


@pytest.mark.asyncio
async def test_spaces_groups_not_fetched_for_non_group_hub_on_light_tick() -> None:
    """Non-group hubs keep the light-tick optimisation: no /groups call."""
    mixin, account = _spaces_mixin()
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    mixin.sse_manager = MagicMock()
    mixin.sse_manager.is_state_protected = MagicMock(return_value=False)
    mixin.api.async_get_hub = AsyncMock(return_value={"state": "ARMED", "groupsEnabled": False})
    mixin.api.async_get_groups = AsyncMock(return_value=[])
    await mixin._async_update_spaces_from_hubs(full_refresh=False)
    mixin.api.async_get_groups.assert_not_called()


@pytest.mark.asyncio
async def test_spaces_state_change_protected_by_realtime() -> None:
    """A real-time-protected hub must not have its state overwritten by REST."""
    mixin, account = _spaces_mixin()
    # Create the space in DISARMED first.
    mixin.api.async_get_hub = AsyncMock(return_value={"state": "DISARMED", "groupsEnabled": False})
    mixin._parse_security_state = MagicMock(return_value=SecurityState.DISARMED)
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    assert account.spaces["hub1"].security_state is SecurityState.DISARMED

    # Now REST says ARMED but SSE protects the state.
    mixin.api.async_get_hub = AsyncMock(return_value={"state": "ARMED", "groupsEnabled": False})
    mixin._parse_security_state = MagicMock(return_value=SecurityState.ARMED)
    mixin.sse_manager = MagicMock()
    mixin.sse_manager.is_state_protected = MagicMock(return_value=True)
    await mixin._async_update_spaces_from_hubs(full_refresh=False)
    # State kept (protected) -> still DISARMED, no event created.
    assert account.spaces["hub1"].security_state is SecurityState.DISARMED


@pytest.mark.asyncio
async def test_spaces_state_change_creates_event() -> None:
    mixin, account = _spaces_mixin()
    mixin.api.async_get_hub = AsyncMock(return_value={"state": "DISARMED", "groupsEnabled": False})
    mixin._parse_security_state = MagicMock(return_value=SecurityState.DISARMED)
    await mixin._async_update_spaces_from_hubs(full_refresh=True)

    # New state ARMED, no protection -> event is created.
    mixin.api.async_get_hub = AsyncMock(return_value={"state": "ARMED", "groupsEnabled": False})
    mixin._parse_security_state = MagicMock(return_value=SecurityState.ARMED)
    await mixin._async_update_spaces_from_hubs(full_refresh=False)
    assert account.spaces["hub1"].security_state is SecurityState.ARMED
    mixin._create_event_from_state_change.assert_called_once()


@pytest.mark.asyncio
async def test_spaces_state_change_skipped_hub_no_event() -> None:
    mixin, account = _spaces_mixin()
    mixin.api.async_get_hub = AsyncMock(return_value={"state": "DISARMED", "groupsEnabled": False})
    mixin._parse_security_state = MagicMock(return_value=SecurityState.DISARMED)
    await mixin._async_update_spaces_from_hubs(full_refresh=True)

    mixin._skipped_state_change_hubs = {"hub1"}
    mixin.api.async_get_hub = AsyncMock(return_value={"state": "ARMED", "groupsEnabled": False})
    mixin._parse_security_state = MagicMock(return_value=SecurityState.ARMED)
    await mixin._async_update_spaces_from_hubs(full_refresh=False)
    # State updated but no event because SSE/SQS handler already created it.
    assert account.spaces["hub1"].security_state is SecurityState.ARMED
    mixin._create_event_from_state_change.assert_not_called()


@pytest.mark.asyncio
async def test_spaces_night_mode_branch() -> None:
    """Night-mode hub state assigns SecurityState.NIGHT_MODE at runtime (line 177)."""
    mixin, account = _spaces_mixin()
    mixin.api.async_get_hub = AsyncMock(return_value={"state": "DISARMED_NIGHT_MODE_ON", "groupsEnabled": False})
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    assert account.spaces["hub1"].security_state is SecurityState.NIGHT_MODE


@pytest.mark.asyncio
async def test_spaces_hub_details_api_error_creates_placeholder_none() -> None:
    """Transient hub-details failure on a brand-new space -> NONE placeholder (lines 185-203)."""
    mixin, account = _spaces_mixin()
    mixin.api.async_get_hub = AsyncMock(side_effect=AjaxRestApiError("429"))
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    assert account.spaces["hub1"].security_state is SecurityState.NONE


@pytest.mark.asyncio
async def test_spaces_hub_details_api_error_existing_space_preserved() -> None:
    """Transient hub-details failure on an existing space keeps state (line 192 continue)."""
    mixin, account = _spaces_mixin()
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    assert account.spaces["hub1"].security_state is SecurityState.ARMED
    mixin.api.async_get_hub = AsyncMock(side_effect=AjaxRestApiError("boom"))
    await mixin._async_update_spaces_from_hubs(full_refresh=False)
    assert account.spaces["hub1"].security_state is SecurityState.ARMED


@pytest.mark.asyncio
async def test_spaces_hub_details_auth_error_propagates() -> None:
    """AjaxRestAuthError on the per-hub fetch propagates (line 180-181)."""
    mixin, _account = _spaces_mixin()
    mixin.api.async_get_hub = AsyncMock(side_effect=AjaxRestAuthError("token"))
    with pytest.raises(AjaxRestAuthError):
        await mixin._async_update_spaces_from_hubs(full_refresh=True)


@pytest.mark.asyncio
async def test_spaces_new_space_dispatch_signal_when_loaded() -> None:
    """When initial load is done, a freshly discovered space fans out SIGNAL_NEW_SPACE."""
    mixin, account = _spaces_mixin()
    mixin._initial_load_done = True
    mixin.hass = MagicMock()
    with patch("custom_components.ajax._coordinator_spaces.async_dispatcher_send") as send:
        await mixin._async_update_spaces_from_hubs(full_refresh=True)
    assert "hub1" in account.spaces
    # At least the SIGNAL_NEW_SPACE dispatch fired.
    assert send.call_count >= 1


@pytest.mark.asyncio
async def test_spaces_new_group_dispatch_signal_when_loaded() -> None:
    """A brand-new group discovered after initial load fans out SIGNAL_NEW_GROUP."""
    mixin, account = _spaces_mixin()
    mixin._initial_load_done = True
    mixin.hass = MagicMock()
    mixin.api.async_get_hub = AsyncMock(return_value={"state": "ARMED", "groupsEnabled": True})
    mixin.api.async_get_groups = AsyncMock(return_value=[{"id": "g_new", "groupName": "New Group", "state": "ARMED"}])
    with patch("custom_components.ajax._coordinator_spaces.async_dispatcher_send") as send:
        await mixin._async_update_spaces_from_hubs(full_refresh=True)
    assert "g_new" in account.spaces["hub1"].groups
    # Both SIGNAL_NEW_SPACE and SIGNAL_NEW_GROUP fired.
    assert send.call_count >= 2


@pytest.mark.asyncio
async def test_spaces_existing_space_light_refresh_reuses_metadata() -> None:
    mixin, account = _spaces_mixin()
    await mixin._async_update_spaces_from_hubs(full_refresh=True)
    # Light refresh should not re-fetch rooms/users.
    mixin.api.async_get_rooms.reset_mock()
    mixin.api.async_get_users.reset_mock()
    await mixin._async_update_spaces_from_hubs(full_refresh=False)
    mixin.api.async_get_rooms.assert_not_called()
    mixin.api.async_get_users.assert_not_called()
    # real_space_id preserved from the full refresh.
    assert account.spaces["hub1"].real_space_id == "real1"
