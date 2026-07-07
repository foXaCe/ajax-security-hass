"""Extra coverage for the coordinator arm / event-dispatch / event-helper mixins.

These tests fill the gaps left by ``test_coordinator_arm_mixin.py``,
``test_coordinator_events_mixin.py`` and ``test_event_helpers.py``:

* ``_coordinator_arm`` — disarm error path, night mode, panic, group
  arm/disarm (optimistic group-state mutation + refresh).
* ``_coordinator_events`` — ``_create_sqs_notification`` filter matrix
  and the source-name escaping branch.
* ``_event_helpers`` — ``resolve_camera_entity_id`` registry lookup,
  the NVR-channel branches of ``_find_video_edge``, the channel
  auto-creation path of ``_update_video_detection`` and
  ``_reset_doorbell_ring`` happy/short-circuit paths.

Everything is exercised through ``object.__new__`` + MagicMock so we
never spin up the full coordinator or the HA harness.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ajax._coordinator_arm import AjaxArmServiceMixin
from custom_components.ajax._coordinator_events import AjaxEventDispatchMixin
from custom_components.ajax._event_helpers import (
    EventHandlerMixin,
    resolve_camera_entity_id,
)
from custom_components.ajax.api import AjaxRestApiError
from custom_components.ajax.const import (
    CONF_MONITORED_SPACES,
    CONF_NOTIFICATION_FILTER,
    CONF_PERSISTENT_NOTIFICATION,
    NOTIFICATION_FILTER_ALARMS_ONLY,
    NOTIFICATION_FILTER_ALL,
    NOTIFICATION_FILTER_NONE,
)
from custom_components.ajax.models import (
    AjaxAccount,
    AjaxDevice,
    AjaxGroup,
    AjaxSpace,
    AjaxVideoEdge,
    DeviceType,
    GroupState,
)

# ===========================================================================
# _coordinator_arm.py — AjaxArmServiceMixin
# ===========================================================================


def _make_arm_mixin() -> AjaxArmServiceMixin:
    mixin = object.__new__(AjaxArmServiceMixin)
    mixin._pending_ha_actions = {}
    mixin._arm_locks = {}
    mixin.api = MagicMock()
    mixin.get_space = MagicMock(return_value=None)
    mixin.async_request_refresh = AsyncMock()
    return mixin


@pytest.mark.asyncio
async def test_async_arm_space_default_force_true() -> None:
    """The default arm path forces past open sensors (``force=True``)."""
    mixin = _make_arm_mixin()
    mixin.api.async_arm = AsyncMock()
    await mixin.async_arm_space("space_a")
    mixin.api.async_arm.assert_awaited_once_with("space_a", ignore_problems=True)


@pytest.mark.asyncio
async def test_async_disarm_space_propagates_api_error() -> None:
    mixin = _make_arm_mixin()
    mixin.api.async_disarm = AsyncMock(side_effect=AjaxRestApiError("boom"))
    with pytest.raises(AjaxRestApiError):
        await mixin.async_disarm_space("space_a")


@pytest.mark.asyncio
async def test_async_arm_night_mode_calls_api_and_registers_action() -> None:
    mixin = _make_arm_mixin()
    mixin.api.async_night_mode = AsyncMock()
    await mixin.async_arm_night_mode("space_a", force=True)
    mixin.api.async_night_mode.assert_awaited_once_with("space_a", enabled=True)
    assert mixin.has_pending_ha_action("space_a") is True


@pytest.mark.asyncio
async def test_async_arm_night_mode_propagates_api_error() -> None:
    mixin = _make_arm_mixin()
    mixin.api.async_night_mode = AsyncMock(side_effect=AjaxRestApiError("nope"))
    with pytest.raises(AjaxRestApiError):
        await mixin.async_arm_night_mode("space_a")


@pytest.mark.asyncio
async def test_async_press_panic_button_calls_api() -> None:
    mixin = _make_arm_mixin()
    mixin.api.async_press_panic_button = AsyncMock()
    await mixin.async_press_panic_button("space_a")
    mixin.api.async_press_panic_button.assert_awaited_once_with("space_a")


@pytest.mark.asyncio
async def test_async_press_panic_button_propagates_api_error() -> None:
    mixin = _make_arm_mixin()
    mixin.api.async_press_panic_button = AsyncMock(side_effect=AjaxRestApiError("panic fail"))
    with pytest.raises(AjaxRestApiError):
        await mixin.async_press_panic_button("space_a")


@pytest.mark.asyncio
async def test_async_arm_group_mutates_group_state_and_refreshes() -> None:
    """Group arm optimistically flips local group state then forces a refresh."""
    mixin = _make_arm_mixin()
    mixin.api.async_arm_group = AsyncMock()
    group = AjaxGroup(id="g1", name="Ground", space_id="s1", state=GroupState.DISARMED)
    space = AjaxSpace(id="s1", name="Maison", hub_id="hub1")
    space.groups = {"g1": group}
    mixin.get_space = MagicMock(return_value=space)

    await mixin.async_arm_group("s1", "g1", force=True)

    mixin.api.async_arm_group.assert_awaited_once_with("s1", "g1", ignore_problems=True)
    assert space.groups["g1"].state == GroupState.ARMED
    mixin.async_request_refresh.assert_awaited_once()
    assert mixin.has_pending_ha_action("s1") is True


@pytest.mark.asyncio
async def test_async_arm_group_no_space_skips_state_mutation() -> None:
    """When the space is unknown we still call the API + refresh, no crash."""
    mixin = _make_arm_mixin()
    mixin.api.async_arm_group = AsyncMock()
    mixin.get_space = MagicMock(return_value=None)

    await mixin.async_arm_group("s1", "g1")

    mixin.api.async_arm_group.assert_awaited_once()
    mixin.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_arm_group_unknown_group_skips_mutation() -> None:
    """A group id that the space doesn't know must not be created on the fly."""
    mixin = _make_arm_mixin()
    mixin.api.async_arm_group = AsyncMock()
    space = AjaxSpace(id="s1", name="Maison", hub_id="hub1")
    space.groups = {}
    mixin.get_space = MagicMock(return_value=space)

    await mixin.async_arm_group("s1", "g_missing")

    assert "g_missing" not in space.groups
    mixin.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_arm_group_propagates_api_error() -> None:
    mixin = _make_arm_mixin()
    mixin.api.async_arm_group = AsyncMock(side_effect=AjaxRestApiError("group fail"))
    with pytest.raises(AjaxRestApiError):
        await mixin.async_arm_group("s1", "g1")
    mixin.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_disarm_group_mutates_group_state_and_refreshes() -> None:
    mixin = _make_arm_mixin()
    mixin.api.async_disarm_group = AsyncMock()
    group = AjaxGroup(id="g1", name="Ground", space_id="s1", state=GroupState.ARMED)
    space = AjaxSpace(id="s1", name="Maison", hub_id="hub1")
    space.groups = {"g1": group}
    mixin.get_space = MagicMock(return_value=space)

    await mixin.async_disarm_group("s1", "g1")

    mixin.api.async_disarm_group.assert_awaited_once_with("s1", "g1")
    assert space.groups["g1"].state == GroupState.DISARMED
    mixin.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_disarm_group_no_space_skips_state_mutation() -> None:
    mixin = _make_arm_mixin()
    mixin.api.async_disarm_group = AsyncMock()
    mixin.get_space = MagicMock(return_value=None)

    await mixin.async_disarm_group("s1", "g1")

    mixin.api.async_disarm_group.assert_awaited_once()
    mixin.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_disarm_group_propagates_api_error() -> None:
    mixin = _make_arm_mixin()
    mixin.api.async_disarm_group = AsyncMock(side_effect=AjaxRestApiError("group fail"))
    with pytest.raises(AjaxRestApiError):
        await mixin.async_disarm_group("s1", "g1")
    mixin.async_request_refresh.assert_not_awaited()


# ===========================================================================
# _coordinator_events.py — _create_sqs_notification
# ===========================================================================


def _make_event_mixin(*, language: str = "fr", options: dict | None = None) -> AjaxEventDispatchMixin:
    mixin = object.__new__(AjaxEventDispatchMixin)
    mixin.hass = MagicMock()
    mixin.hass.config = SimpleNamespace(language=language)
    mixin.config_entry = SimpleNamespace(options=options if options is not None else {})
    return mixin


@pytest.mark.asyncio
@patch("custom_components.ajax._coordinator_events.async_create")
async def test_create_sqs_notification_skips_when_disabled(mock_create) -> None:
    mixin = _make_event_mixin(options={CONF_PERSISTENT_NOTIFICATION: False})
    await mixin._create_sqs_notification("armed", "Stéphane", "Maison")
    mock_create.assert_not_called()


@pytest.mark.asyncio
@patch("custom_components.ajax._coordinator_events.async_create")
async def test_create_sqs_notification_skips_when_filter_none(mock_create) -> None:
    mixin = _make_event_mixin(options={CONF_NOTIFICATION_FILTER: NOTIFICATION_FILTER_NONE})
    await mixin._create_sqs_notification("armed", "Stéphane", "Maison")
    mock_create.assert_not_called()


@pytest.mark.asyncio
@patch("custom_components.ajax._coordinator_events.async_create")
async def test_create_sqs_notification_alarms_only_drops_arming_event(mock_create) -> None:
    """ALARMS_ONLY filter must suppress arm/disarm events, keep real alarms."""
    mixin = _make_event_mixin(options={CONF_NOTIFICATION_FILTER: NOTIFICATION_FILTER_ALARMS_ONLY})
    await mixin._create_sqs_notification("disarmed", "Stéphane", "Maison")
    mock_create.assert_not_called()


@pytest.mark.asyncio
@patch("custom_components.ajax._coordinator_events.async_create")
async def test_create_sqs_notification_alarms_only_keeps_alarm_event(mock_create) -> None:
    mixin = _make_event_mixin(options={CONF_NOTIFICATION_FILTER: NOTIFICATION_FILTER_ALARMS_ONLY})
    await mixin._create_sqs_notification("intrusion", "Stéphane", "Maison")
    mock_create.assert_called_once()


@pytest.mark.asyncio
@patch("custom_components.ajax._coordinator_events.async_create")
async def test_create_sqs_notification_monitored_spaces_drops_other_space(mock_create) -> None:
    """A space outside the monitored_spaces selection must be silenced."""
    mixin = _make_event_mixin(options={CONF_MONITORED_SPACES: ["s1"]})
    await mixin._create_sqs_notification("armed", "Stéphane", "Maison", space_id="s2")
    mock_create.assert_not_called()


@pytest.mark.asyncio
@patch("custom_components.ajax._coordinator_events.async_create")
async def test_create_sqs_notification_monitored_spaces_keeps_selected_space(mock_create) -> None:
    mixin = _make_event_mixin(options={CONF_MONITORED_SPACES: ["s1"]})
    await mixin._create_sqs_notification("armed", "Stéphane", "Maison", space_id="s1")
    mock_create.assert_called_once()


@pytest.mark.asyncio
@patch("custom_components.ajax._coordinator_events.async_create")
async def test_create_sqs_notification_empty_monitored_spaces_means_all(mock_create) -> None:
    """Empty selection (or entries saved before the filter) keeps everything."""
    mixin = _make_event_mixin(options={CONF_MONITORED_SPACES: []})
    await mixin._create_sqs_notification("armed", "Stéphane", "Maison", space_id="s2")
    mock_create.assert_called_once()


@pytest.mark.asyncio
@patch("custom_components.ajax._coordinator_events.async_create")
async def test_create_sqs_notification_appends_escaped_source_name(mock_create) -> None:
    """ALL filter renders the message and appends the markdown-escaped source."""
    mixin = _make_event_mixin(language="fr", options={CONF_NOTIFICATION_FILTER: NOTIFICATION_FILTER_ALL})
    await mixin._create_sqs_notification("armed", "*Sté*", "Mai[son]")

    mock_create.assert_called_once()
    args, kwargs = mock_create.call_args
    # Positional: (hass, message). Source name appended after a "par" connector (fr).
    message = args[1]
    assert "par" in message
    assert "\\*Sté\\*" in message
    # Title carries the escaped space name.
    assert kwargs["title"] == "Ajax - Mai\\[son\\]"


@pytest.mark.asyncio
@patch("custom_components.ajax._coordinator_events.async_create")
async def test_create_sqs_notification_no_source_name_omits_connector(mock_create) -> None:
    mixin = _make_event_mixin(language="en", options={})
    await mixin._create_sqs_notification("armed", "", "Maison")

    mock_create.assert_called_once()
    message = mock_create.call_args[0][1]
    assert " by " not in message


@pytest.mark.asyncio
@patch("custom_components.ajax._coordinator_events.async_create")
async def test_create_sqs_notification_no_config_entry_defaults_to_enabled(mock_create) -> None:
    """A missing config_entry falls back to ``options = {}`` (enabled, ALL)."""
    mixin = object.__new__(AjaxEventDispatchMixin)
    mixin.hass = MagicMock()
    mixin.hass.config = SimpleNamespace(language="es")
    mixin.config_entry = None

    await mixin._create_sqs_notification("armed", "Juan", "Casa")

    mock_create.assert_called_once()
    message = mock_create.call_args[0][1]
    assert " por " in message  # Spanish connector


# ===========================================================================
# _event_helpers.py — resolve_camera_entity_id
# ===========================================================================


def test_resolve_camera_entity_id_standalone_camera() -> None:
    """Standalone camera (``_camera_main``) wins over the NVR channel fallback."""
    registry = MagicMock()
    registry.async_get_entity_id.side_effect = lambda dom, plat, uid: (
        "camera.cam_main" if uid == "ve1_camera_main" else None
    )
    with patch("homeassistant.helpers.entity_registry.async_get", return_value=registry):
        out = resolve_camera_entity_id(MagicMock(), "ve1")
    assert out == "camera.cam_main"


def test_resolve_camera_entity_id_nvr_channel_fallback() -> None:
    registry = MagicMock()
    registry.async_get_entity_id.side_effect = lambda dom, plat, uid: (
        "camera.cam_ch0" if uid == "ve1_camera_ch0_main" else None
    )
    with patch("homeassistant.helpers.entity_registry.async_get", return_value=registry):
        out = resolve_camera_entity_id(MagicMock(), "ve1")
    assert out == "camera.cam_ch0"


def test_resolve_camera_entity_id_returns_none_when_unregistered() -> None:
    registry = MagicMock()
    registry.async_get_entity_id.return_value = None
    with patch("homeassistant.helpers.entity_registry.async_get", return_value=registry):
        out = resolve_camera_entity_id(MagicMock(), "ve1")
    assert out is None


# ===========================================================================
# _event_helpers.py — EventHandlerMixin lookup / state / doorbell
# ===========================================================================


class _StubManager(EventHandlerMixin):
    """Concrete subclass with a fake coordinator/hass."""

    def __init__(self, *, account: AjaxAccount | None = None) -> None:
        self.coordinator = SimpleNamespace(
            hass=SimpleNamespace(bus=SimpleNamespace(async_fire=MagicMock())),
            account=account,
            async_set_updated_data=MagicMock(),
            _event_entities={},
        )


def _video_edge(channels=None) -> AjaxVideoEdge:
    return AjaxVideoEdge(
        id="ve1",
        name="Entrance",
        space_id="s1",
        channels=channels if channels is not None else [],
    )


def _space_with_edge(ve: AjaxVideoEdge) -> AjaxSpace:
    space = AjaxSpace(id="s1", name="Maison", hub_id="hub1")
    space.video_edges = {ve.id: ve}
    return space


def test_find_video_edge_by_direct_id() -> None:
    mgr = _StubManager()
    ve = _video_edge()
    space = _space_with_edge(ve)
    found, channel = mgr._find_video_edge(space, source_name="", source_id="ve1")
    assert found is ve
    assert channel is None


def test_find_video_edge_by_nvr_channel_id() -> None:
    mgr = _StubManager()
    ve = _video_edge(channels=[{"id": "ch7", "name": "Garage"}])
    space = _space_with_edge(ve)
    found, channel = mgr._find_video_edge(space, source_name="", source_id="ch7")
    assert found is ve
    assert channel == "ch7"


def test_find_video_edge_by_name() -> None:
    mgr = _StubManager()
    ve = _video_edge()
    space = _space_with_edge(ve)
    found, channel = mgr._find_video_edge(space, source_name="Entrance", source_id="")
    assert found is ve
    assert channel is None


def test_find_video_edge_by_channel_name() -> None:
    mgr = _StubManager()
    ve = _video_edge(channels=[{"id": "ch3", "name": "Backyard"}])
    space = _space_with_edge(ve)
    found, channel = mgr._find_video_edge(space, source_name="Backyard", source_id="")
    assert found is ve
    assert channel == "ch3"


def test_find_video_edge_not_found() -> None:
    mgr = _StubManager()
    ve = _video_edge(channels=[{"id": "ch3", "name": "Backyard"}])
    space = _space_with_edge(ve)
    found, channel = mgr._find_video_edge(space, source_name="Nope", source_id="unknown")
    assert found is None
    assert channel is None


def test_find_video_edge_ignores_non_dict_channels() -> None:
    """Malformed channels (not dicts) must be skipped, not crash."""
    mgr = _StubManager()
    ve = _video_edge(channels=["garbage", {"id": "ch1", "name": "Door"}])
    space = _space_with_edge(ve)
    found, channel = mgr._find_video_edge(space, source_name="Door", source_id="")
    assert found is ve
    assert channel == "ch1"


def test_update_video_detection_autocreates_channel_when_empty() -> None:
    """No channels + no channel_id → a synthetic channel ``0`` is created."""
    mgr = _StubManager()
    ve = _video_edge(channels=[])
    mgr._update_video_detection(ve, channel_id=None, detection_type="VIDEO_HUMAN", active=True)
    assert ve.channels == [{"id": "0", "state": [{"type": "VIDEO_HUMAN", "active": True}]}]


def test_update_video_detection_returns_when_channel_id_not_matched() -> None:
    """A specific channel_id with no match (and channels present) is a no-op."""
    mgr = _StubManager()
    ve = _video_edge(channels=[{"id": "0", "state": []}])
    mgr._update_video_detection(ve, channel_id="99", detection_type="VIDEO_HUMAN", active=True)
    assert ve.channels == [{"id": "0", "state": []}]


def test_update_video_detection_normalises_non_list_state() -> None:
    """A channel whose ``state`` is not a list is reset before appending."""
    mgr = _StubManager()
    ve = _video_edge(channels=[{"id": "0", "state": "broken"}])
    mgr._update_video_detection(ve, channel_id="0", detection_type="VIDEO_PET", active=True)
    assert ve.channels[0]["state"] == [{"type": "VIDEO_PET", "active": True}]


def _make_device(device_id: str = "dev1") -> AjaxDevice:
    return AjaxDevice(
        id=device_id,
        name="Doorbell",
        type=DeviceType.UNKNOWN,
        space_id="s1",
        hub_id="hub1",
        attributes={"doorbell_ring": True},
    )


def test_reset_doorbell_ring_clears_flag_and_pushes_update() -> None:
    device = _make_device()
    space = AjaxSpace(id="s1", name="Maison", hub_id="hub1")
    space.devices = {"dev1": device}
    account = AjaxAccount(user_id="u1", name="Acc", email="a@e.com")
    account.spaces = {"s1": space}
    mgr = _StubManager(account=account)

    mgr._reset_doorbell_ring("s1", "dev1")

    assert device.attributes["doorbell_ring"] is False
    mgr.coordinator.async_set_updated_data.assert_called_once_with(account)


def test_reset_doorbell_ring_noop_when_no_account() -> None:
    mgr = _StubManager(account=None)
    # Must not raise nor push an update.
    mgr._reset_doorbell_ring("s1", "dev1")
    mgr.coordinator.async_set_updated_data.assert_not_called()


def test_reset_doorbell_ring_noop_when_space_missing() -> None:
    account = AjaxAccount(user_id="u1", name="Acc", email="a@e.com")
    account.spaces = {}
    mgr = _StubManager(account=account)
    mgr._reset_doorbell_ring("s1", "dev1")
    mgr.coordinator.async_set_updated_data.assert_not_called()


def test_reset_doorbell_ring_noop_when_device_missing() -> None:
    space = AjaxSpace(id="s1", name="Maison", hub_id="hub1")
    space.devices = {}
    account = AjaxAccount(user_id="u1", name="Acc", email="a@e.com")
    account.spaces = {"s1": space}
    mgr = _StubManager(account=account)
    mgr._reset_doorbell_ring("s1", "missing")
    mgr.coordinator.async_set_updated_data.assert_not_called()


def test_reset_doorbell_ring_swallows_unexpected_errors() -> None:
    """Best-effort reset: an exploding ``account.spaces`` must be swallowed."""
    mgr = _StubManager()
    boom = MagicMock()
    boom.spaces.get.side_effect = RuntimeError("kaboom")
    mgr.coordinator.account = boom
    # Must not raise.
    mgr._reset_doorbell_ring("s1", "dev1")
    mgr.coordinator.async_set_updated_data.assert_not_called()
