"""Coverage tests for the event and button platforms.

Exercises both ``async_setup_entry`` discovery (regular devices via their
handler's ``get_events``, Video Edge cameras incl. doorbell + AI detection,
smart locks) and the dynamic ``_build_*`` closures, plus the
``AjaxEventEntity`` / ``AjaxPanicButton`` instance behaviour (``fire``,
dispatch-map (un)registration, ``device_info``). Entities are built with
``object.__new__`` and the coordinator/account are lightweight mocks — no
running HA instance.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ajax.button import (
    PANIC_COOLDOWN_SECONDS,
    AjaxPanicButton,
    async_setup_entry as button_async_setup_entry,
)
from custom_components.ajax.const import DOMAIN
from custom_components.ajax.event import (
    AjaxEventEntity,
    async_setup_entry as event_async_setup_entry,
)
from custom_components.ajax.models import (
    AjaxAccount,
    AjaxDevice,
    AjaxSmartLock,
    AjaxSpace,
    AjaxVideoEdge,
    DeviceType,
    VideoEdgeType,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _button_device(device_id: str = "btn1") -> AjaxDevice:
    return AjaxDevice(
        id=device_id,
        name="Panic Remote",
        type=DeviceType.BUTTON,
        space_id="s1",
        hub_id="hub1",
        raw_type="BUTTON",
    )


def _video_edge(ve_id: str, ve_type: VideoEdgeType) -> AjaxVideoEdge:
    return AjaxVideoEdge(
        id=ve_id,
        name=f"Camera {ve_id}",
        space_id="s1",
        video_edge_type=ve_type,
    )


def _smart_lock(sl_id: str = "lock1") -> AjaxSmartLock:
    return AjaxSmartLock(id=sl_id, name="Front Door Lock", space_id="s1")


def _account(
    *,
    devices: dict[str, AjaxDevice] | None = None,
    video_edges: dict[str, AjaxVideoEdge] | None = None,
    smart_locks: dict[str, AjaxSmartLock] | None = None,
) -> AjaxAccount:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    space.devices.update(devices or {})
    space.video_edges.update(video_edges or {})
    space.smart_locks.update(smart_locks or {})
    account = AjaxAccount(user_id="u1", name="U", email="u@e.com")
    account.spaces["s1"] = space
    return account


def _event_coordinator(account: AjaxAccount | None):
    space = account.spaces["s1"] if account else None
    return SimpleNamespace(
        entry_id="entry_test",
        account=account,
        get_space=lambda sid: space if sid == "s1" else None,
        _event_entities={},
    )


# ===========================================================================
# event.py — AjaxEventEntity instance behaviour
# ===========================================================================


def _make_event(
    *,
    event_types: tuple[str, ...] = ("ring",),
    device_id: str = "d1",
    coordinator=None,
) -> AjaxEventEntity:
    entity = object.__new__(AjaxEventEntity)
    entity._space_id = "s1"
    entity._device_id = device_id
    entity._event_key = "doorbell"
    entity._event_desc = {"key": "doorbell", "event_types": list(event_types)}
    entity._attr_unique_id = f"entry_test_{device_id}_doorbell"
    entity._attr_event_types = list(event_types)
    entity.hass = None
    entity._trigger_event = MagicMock()
    entity.coordinator = coordinator or SimpleNamespace(entry_id="entry_test", _event_entities={}, account=None)
    return entity


def test_fire_writes_ha_state_when_hass_present() -> None:
    entity = _make_event(event_types=("ring",))
    entity.hass = object()  # truthy, so the guard passes
    entity.async_write_ha_state = MagicMock()
    entity.fire("ring")
    entity._trigger_event.assert_called_once_with("ring", None)
    entity.async_write_ha_state.assert_called_once()


def test_device_info_returns_none_when_account_missing() -> None:
    entity = _make_event(coordinator=SimpleNamespace(entry_id="entry_test", _event_entities={}, account=None))
    assert entity.device_info is None


def test_device_info_for_regular_device() -> None:
    device = _button_device("d1")
    account = _account(devices={"d1": device})
    entity = _make_event(device_id="d1", coordinator=_event_coordinator(account))
    info = entity.device_info
    assert info is not None
    assert (DOMAIN, "entry_test_d1") in info["identifiers"]
    assert info["name"] == "Panic Remote"
    # device.type.value drives the model string for regular devices.
    assert info["model"] == DeviceType.BUTTON.value


def test_device_info_for_video_edge_uses_model_name() -> None:
    ve = _video_edge("ve1", VideoEdgeType.TURRET)
    account = _account(video_edges={"ve1": ve})
    entity = _make_event(device_id="ve1", coordinator=_event_coordinator(account))
    info = entity.device_info
    assert info is not None
    assert info["model"] == "TurretCam"
    assert info["name"] == "Camera ve1"


def test_device_info_for_smart_lock() -> None:
    lock = _smart_lock("lock1")
    account = _account(smart_locks={"lock1": lock})
    entity = _make_event(device_id="lock1", coordinator=_event_coordinator(account))
    info = entity.device_info
    assert info is not None
    assert info["model"] == "LockBridge Jeweller"
    assert info["name"] == "Front Door Lock"


def test_device_info_returns_none_when_device_not_found() -> None:
    account = _account()  # empty space, the device_id matches nothing
    entity = _make_event(device_id="ghost", coordinator=_event_coordinator(account))
    assert entity.device_info is None


def test_init_populates_attrs_from_descriptor() -> None:
    coordinator = MagicMock()
    coordinator.entry_id = "entry_test"
    desc = {
        "key": "detection",
        "translation_key": "camera_detection",
        "device_class": None,
        "event_types": ["motion", "human"],
        "name": "Detection",
        "enabled_by_default": False,
    }

    def _set_coordinator(self, coordinator, *args, **kwargs):
        # The real CoordinatorEntity.__init__ assigns self.coordinator; mirror
        # that so the namespaced unique_id/device_info can read entry_id.
        self.coordinator = coordinator

    with patch(
        "homeassistant.helpers.update_coordinator.CoordinatorEntity.__init__",
        side_effect=_set_coordinator,
        autospec=True,
    ):
        entity = AjaxEventEntity(
            coordinator=coordinator,
            space_id="s1",
            device_id="ve1",
            event_key="detection",
            event_desc=desc,
        )
    assert entity._attr_unique_id == "entry_test_ve1_detection"
    assert entity._attr_translation_key == "camera_detection"
    assert entity._attr_event_types == ["motion", "human"]
    assert entity._attr_name == "Detection"
    assert entity._attr_entity_registry_enabled_default is False


def test_init_translation_key_falls_back_to_event_key() -> None:
    coordinator = MagicMock()
    coordinator.entry_id = "entry_test"
    desc = {"key": "smart_lock_event", "event_types": ["doorbell_pressed"]}

    def _set_coordinator(self, coordinator, *args, **kwargs):
        # The real CoordinatorEntity.__init__ assigns self.coordinator; mirror
        # that so the namespaced unique_id/device_info can read entry_id.
        self.coordinator = coordinator

    with patch(
        "homeassistant.helpers.update_coordinator.CoordinatorEntity.__init__",
        side_effect=_set_coordinator,
        autospec=True,
    ):
        entity = AjaxEventEntity(
            coordinator=coordinator,
            space_id="s1",
            device_id="lock1",
            event_key="smart_lock_event",
            event_desc=desc,
        )
    # No translation_key in descriptor -> falls back to event_key.
    assert entity._attr_translation_key == "smart_lock_event"
    assert entity._attr_entity_registry_enabled_default is True  # default True


# ===========================================================================
# event.py — async_setup_entry discovery
# ===========================================================================


@pytest.mark.asyncio
async def test_event_setup_entry_no_account_returns_early() -> None:
    coordinator = SimpleNamespace(account=None)
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    await event_async_setup_entry(MagicMock(), entry, add)
    # No account → the platform returns early without registering any entity.
    add.assert_not_called()


@pytest.mark.asyncio
async def test_event_setup_entry_creates_all_event_types() -> None:
    devices = {"btn1": _button_device("btn1")}
    video_edges = {
        "doorbell1": _video_edge("doorbell1", VideoEdgeType.DOORBELL),
        "turret1": _video_edge("turret1", VideoEdgeType.TURRET),
        "nvr1": _video_edge("nvr1", VideoEdgeType.NVR),  # skipped
    }
    smart_locks = {"lock1": _smart_lock("lock1")}
    coordinator = _event_coordinator(_account(devices=devices, video_edges=video_edges, smart_locks=smart_locks))
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    with patch("custom_components.ajax.event.connect_new_entity_signal"):
        await event_async_setup_entry(MagicMock(), entry, add)
    add.assert_called_once()
    created = add.call_args[0][0]
    uids = {e._attr_unique_id for e in created}
    # button handler event
    assert "entry_test_btn1_button_press" in uids
    # doorbell camera: doorbell_press + detection
    assert "entry_test_doorbell1_doorbell_press" in uids
    assert "entry_test_doorbell1_detection" in uids
    # plain camera: detection only (no doorbell_press)
    assert "entry_test_turret1_detection" in uids
    assert "entry_test_turret1_doorbell_press" not in uids
    # NVR skipped entirely
    assert not any(uid.startswith("entry_test_nvr1_") for uid in uids)
    # smart lock event
    assert "entry_test_lock1_smart_lock_event" in uids


@pytest.mark.asyncio
async def test_event_setup_entry_dedupes_duplicate_unique_ids() -> None:
    # Two doorbell cameras with the SAME id collapse into one set of entities;
    # the second occurrence hits the ``seen_unique_ids`` guard.
    ve = _video_edge("dup", VideoEdgeType.DOORBELL)
    account = _account(video_edges={"dup": ve})
    # Inject a second space that reuses the same video-edge id to force a clash.
    space2 = AjaxSpace(id="s2", name="Garage", hub_id="hub2")
    space2.video_edges["dup"] = _video_edge("dup", VideoEdgeType.DOORBELL)
    account.spaces["s2"] = space2
    coordinator = SimpleNamespace(
        entry_id="entry_test",
        account=account,
        get_space=lambda sid: account.spaces.get(sid),
        _event_entities={},
    )
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    with patch("custom_components.ajax.event.connect_new_entity_signal"):
        await event_async_setup_entry(MagicMock(), entry, add)
    created = add.call_args[0][0]
    uids = [e._attr_unique_id for e in created]
    # No duplicates despite the id clash across spaces.
    assert len(uids) == len(set(uids))
    assert uids.count("entry_test_dup_detection") == 1


# --- _build_* discovery closures -------------------------------------------


async def _capture_event_builders(coordinator):
    """Run event async_setup_entry and capture every builder by signal."""
    entry = SimpleNamespace(runtime_data=coordinator)
    captured: dict = {}

    def _fake_connect(hass, entry_, signal, domain, add, builder, label):
        captured[signal] = builder

    with patch("custom_components.ajax.event.connect_new_entity_signal", _fake_connect):
        await event_async_setup_entry(MagicMock(), entry, MagicMock())
    return captured


@pytest.mark.asyncio
async def test_build_device_closure() -> None:
    from custom_components.ajax.const import SIGNAL_NEW_DEVICE

    device = _button_device("btn1")
    coordinator = _event_coordinator(_account(devices={"btn1": device}))
    builders = await _capture_event_builders(coordinator)
    pairs = builders[SIGNAL_NEW_DEVICE]("s1", "btn1")
    uids = {uid for uid, _ in pairs}
    assert "btn1_button_press" in uids
    # Unknown space -> empty.
    assert builders[SIGNAL_NEW_DEVICE]("nope", "btn1") == []
    # Known space, missing device -> empty.
    assert builders[SIGNAL_NEW_DEVICE]("s1", "ghost") == []


@pytest.mark.asyncio
async def test_build_device_closure_no_handler_returns_empty() -> None:
    from custom_components.ajax.const import SIGNAL_NEW_DEVICE

    device = AjaxDevice(
        id="mystery",
        name="Mystery",
        type=DeviceType.UNKNOWN,
        space_id="s1",
        hub_id="hub1",
        raw_type="MYSTERY",
    )
    coordinator = _event_coordinator(_account(devices={"mystery": device}))
    builders = await _capture_event_builders(coordinator)
    assert builders[SIGNAL_NEW_DEVICE]("s1", "mystery") == []


@pytest.mark.asyncio
async def test_build_video_edge_closure() -> None:
    from custom_components.ajax.const import SIGNAL_NEW_VIDEO_EDGE

    video_edges = {
        "doorbell1": _video_edge("doorbell1", VideoEdgeType.DOORBELL),
        "turret1": _video_edge("turret1", VideoEdgeType.TURRET),
        "nvr1": _video_edge("nvr1", VideoEdgeType.NVR),
    }
    coordinator = _event_coordinator(_account(video_edges=video_edges))
    builders = await _capture_event_builders(coordinator)
    build = builders[SIGNAL_NEW_VIDEO_EDGE]

    doorbell_uids = {uid for uid, _ in build("s1", "doorbell1")}
    assert doorbell_uids == {"doorbell1_doorbell_press", "doorbell1_detection"}

    turret_uids = {uid for uid, _ in build("s1", "turret1")}
    assert turret_uids == {"turret1_detection"}

    # NVR is skipped -> empty.
    assert build("s1", "nvr1") == []
    # Unknown space -> empty.
    assert build("nope", "turret1") == []
    # Missing video edge -> empty.
    assert build("s1", "ghost") == []


@pytest.mark.asyncio
async def test_build_smart_lock_closure() -> None:
    from custom_components.ajax.const import SIGNAL_NEW_SMART_LOCK

    coordinator = _event_coordinator(_account(smart_locks={"lock1": _smart_lock("lock1")}))
    builders = await _capture_event_builders(coordinator)
    pairs = builders[SIGNAL_NEW_SMART_LOCK]("s1", "lock1")
    assert len(pairs) == 1
    uid, entity = pairs[0]
    assert uid == "lock1_smart_lock_event"
    assert isinstance(entity, AjaxEventEntity)
    assert entity._attr_event_types == ["ring", "door_left_open"]


# ===========================================================================
# button.py — AjaxPanicButton instance behaviour
# ===========================================================================


def _make_panic(*, space) -> AjaxPanicButton:
    button = object.__new__(AjaxPanicButton)
    button._space_id = "s1"
    button._entry = SimpleNamespace(entry_id="entry1")
    button._last_press_ts = 0.0
    button.coordinator = SimpleNamespace(
        entry_id="entry_test",
        async_press_panic_button=AsyncMock(),
        get_space=lambda sid: space if sid == "s1" else None,
    )
    return button


def test_panic_device_info_returns_none_when_space_missing() -> None:
    button = _make_panic(space=None)
    assert button.device_info is None


def test_panic_device_info_built_from_space() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    button = _make_panic(space=space)
    info = button.device_info
    assert info is not None
    assert (DOMAIN, "entry_test_s1") in info["identifiers"]
    assert info["name"] == "Home"
    assert info["model"] == "Security Hub"


@pytest.mark.asyncio
async def test_panic_cooldown_uses_module_constant() -> None:
    # Sanity-pin the cooldown value referenced by the rejection branch.
    assert PANIC_COOLDOWN_SECONDS == 5.0


# ===========================================================================
# button.py — async_setup_entry
# ===========================================================================


@pytest.mark.asyncio
async def test_button_setup_entry_no_account_skips() -> None:
    coordinator = SimpleNamespace(account=None)
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    await button_async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


@pytest.mark.asyncio
async def test_button_setup_entry_empty_account_skips() -> None:
    coordinator = SimpleNamespace(account=_account())  # space exists but loop adds one button
    entry = SimpleNamespace(entry_id="entry1", runtime_data=coordinator)
    add = MagicMock()
    await button_async_setup_entry(MagicMock(), entry, add)
    # One space -> one panic button created.
    add.assert_called_once()
    created = add.call_args[0][0]
    assert len(created) == 1
    assert isinstance(created[0], AjaxPanicButton)


@pytest.mark.asyncio
async def test_button_setup_entry_no_spaces_skips_add() -> None:
    account = AjaxAccount(user_id="u1", name="U", email="u@e.com")  # zero spaces
    coordinator = SimpleNamespace(account=account)
    entry = SimpleNamespace(runtime_data=coordinator)
    add = MagicMock()
    await button_async_setup_entry(MagicMock(), entry, add)
    add.assert_not_called()


@pytest.mark.asyncio
async def test_button_setup_entry_one_per_space() -> None:
    account = AjaxAccount(user_id="u1", name="U", email="u@e.com")
    account.spaces["s1"] = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    account.spaces["s2"] = AjaxSpace(id="s2", name="Garage", hub_id="hub2")
    coordinator = SimpleNamespace(account=account)
    entry = SimpleNamespace(entry_id="entry1", runtime_data=coordinator)
    add = MagicMock()
    await button_async_setup_entry(MagicMock(), entry, add)
    created = add.call_args[0][0]
    assert len(created) == 2
    assert {b._space_id for b in created} == {"s1", "s2"}
