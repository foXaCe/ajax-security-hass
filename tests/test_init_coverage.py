"""Tests for the integration-level service handlers in ``__init__.py``.

The service handlers (force_arm, force_arm_night, get_raw_devices,
refresh_metadata, get_nvr_recordings, get_smart_locks) are closures created
inside ``_async_setup_services``. They are exercised here by registering the
services against a fake hass, capturing the registered callbacks, and calling
them directly with a mocked coordinator (``entry.runtime_data``) and a mocked
``ServiceCall``. This keeps the tests lightweight (no full HA harness) while
covering the service logic, target resolution, and file/notification writes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.ajax import (
    DOMAIN,
    SERVICE_FORCE_ARM,
    SERVICE_FORCE_ARM_NIGHT,
    SERVICE_GET_NVR_RECORDINGS,
    SERVICE_GET_RAW_DEVICES,
    SERVICE_GET_SMART_LOCKS,
    SERVICE_REFRESH_METADATA,
    _async_setup_areas,
    _async_setup_services,
    _async_update_options,
    async_remove_config_entry_device,
    async_setup,
    async_unload_entry,
)
from custom_components.ajax.models import SecurityState


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_hass(tmp_path: Any) -> MagicMock:
    """Build a fake hass whose service registry records handlers in a dict."""
    hass = MagicMock()
    handlers: dict[str, Any] = {}

    def _register(domain: str, service: str, handler: Any, **_kw: Any) -> None:
        handlers[service] = handler

    hass.services.has_service.return_value = False
    hass.services.async_register.side_effect = _register
    hass._handlers = handlers  # noqa: SLF001 - test convenience

    hass.config.path.side_effect = lambda name: str(tmp_path / name)

    async def _executor(func: Any, *args: Any) -> Any:
        return func(*args)

    hass.async_add_executor_job.side_effect = _executor
    return hass


async def _handlers(tmp_path: Any) -> dict[str, Any]:
    """Register the services and return the captured handler callbacks."""
    hass = _make_hass(tmp_path)
    await _async_setup_services(hass)
    return hass, hass._handlers  # type: ignore[return-value]


def _call(entry: Any) -> MagicMock:
    """Build a ServiceCall whose hass exposes the given loaded entry."""
    call = MagicMock()
    call.hass.config_entries.async_loaded_entries.return_value = [entry]
    return call


def _entry(coordinator: Any, entry_id: str = "e1") -> SimpleNamespace:
    return SimpleNamespace(entry_id=entry_id, runtime_data=coordinator)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
async def test_all_services_registered(tmp_path: Any) -> None:
    """Calling _async_setup_services registers every documented service."""
    _, handlers = await _handlers(tmp_path)
    assert set(handlers) == {
        SERVICE_FORCE_ARM,
        SERVICE_FORCE_ARM_NIGHT,
        SERVICE_GET_RAW_DEVICES,
        SERVICE_REFRESH_METADATA,
        SERVICE_GET_NVR_RECORDINGS,
        SERVICE_GET_SMART_LOCKS,
    }


async def test_services_not_reregistered_when_present(tmp_path: Any) -> None:
    """If a service already exists, it must not be registered again."""
    hass = _make_hass(tmp_path)
    hass.services.has_service.return_value = True
    await _async_setup_services(hass)
    hass.services.async_register.assert_not_called()


# --------------------------------------------------------------------------- #
# _extract_config_entry (via handlers)
# --------------------------------------------------------------------------- #
async def test_extract_config_entry_no_target_raises(tmp_path: Any) -> None:
    """When no Ajax entry is loaded, the handler raises a validation error."""
    _, handlers = await _handlers(tmp_path)
    call = MagicMock()
    call.hass.config_entries.async_loaded_entries.return_value = []
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        pytest.raises(ServiceValidationError),
    ):
        await handlers[SERVICE_REFRESH_METADATA](call)


async def test_extract_config_entry_filters_by_target_id(tmp_path: Any) -> None:
    """Only entries whose id matches the resolved target ids are kept."""
    _, handlers = await _handlers(tmp_path)
    coord = MagicMock()
    coord.async_force_metadata_refresh = AsyncMock()
    entry = _entry(coord, entry_id="wanted")
    other = _entry(MagicMock(), entry_id="other")
    call = MagicMock()
    call.hass.config_entries.async_loaded_entries.return_value = [entry, other]
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value={"wanted"}),
        ),
        patch("custom_components.ajax._services.async_create"),
    ):
        await handlers[SERVICE_REFRESH_METADATA](call)
    coord.async_force_metadata_refresh.assert_awaited_once()


# --------------------------------------------------------------------------- #
# refresh_metadata
# --------------------------------------------------------------------------- #
async def test_refresh_metadata_calls_coordinator_and_notifies(tmp_path: Any) -> None:
    _, handlers = await _handlers(tmp_path)
    coord = MagicMock()
    coord.async_force_metadata_refresh = AsyncMock()
    call = _call(_entry(coord))
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch("custom_components.ajax._services.async_create") as notify,
    ):
        await handlers[SERVICE_REFRESH_METADATA](call)
    coord.async_force_metadata_refresh.assert_awaited_once()
    notify.assert_called_once()


# --------------------------------------------------------------------------- #
# force_arm / force_arm_night – target resolution
# --------------------------------------------------------------------------- #
def _coord_with_spaces(space_ids: list[str]) -> MagicMock:
    coord = MagicMock()
    spaces = {sid: SimpleNamespace(name=sid, security_state=None) for sid in space_ids}
    coord.account = SimpleNamespace(spaces=spaces)
    coord.async_arm_space = AsyncMock()
    coord.async_arm_night_mode = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    return coord


async def test_force_arm_no_target_uses_all_spaces(tmp_path: Any) -> None:
    """With no referenced entities, all spaces are armed."""
    _, handlers = await _handlers(tmp_path)
    coord = _coord_with_spaces(["s1", "s2"])
    call = _call(_entry(coord))
    selected = SimpleNamespace(referenced=set(), indirectly_referenced=set())
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "custom_components.ajax._services.async_extract_referenced_entity_ids",
            return_value=selected,
        ),
    ):
        await handlers[SERVICE_FORCE_ARM](call)
    assert coord.async_arm_space.await_count == 2
    assert coord.async_request_refresh.await_count == 2


async def test_force_arm_resolves_referenced_entity_to_space(tmp_path: Any) -> None:
    """A referenced alarm_control_panel entity resolves to its space_id."""
    _, handlers = await _handlers(tmp_path)
    coord = _coord_with_spaces(["space42"])
    call = _call(_entry(coord))
    selected = SimpleNamespace(
        referenced={"alarm_control_panel.ajax"},
        indirectly_referenced=set(),
    )
    registry = MagicMock()
    reg_entry = SimpleNamespace(
        domain="alarm_control_panel",
        unique_id="e1_alarm_space42",
    )
    registry.async_get.return_value = reg_entry
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "custom_components.ajax._services.async_extract_referenced_entity_ids",
            return_value=selected,
        ),
        patch("custom_components.ajax._services.er.async_get", return_value=registry),
    ):
        await handlers[SERVICE_FORCE_ARM](call)
    coord.async_arm_space.assert_awaited_once_with("space42")


async def test_force_arm_ignores_non_alarm_and_unknown_entities(tmp_path: Any) -> None:
    """Non-alarm entities, missing registry entries, and unknown spaces are skipped."""
    _, handlers = await _handlers(tmp_path)
    coord = _coord_with_spaces(["known"])
    call = _call(_entry(coord))
    selected = SimpleNamespace(
        referenced={"sensor.foo", "alarm_control_panel.missing", "alarm_control_panel.unknown"},
        indirectly_referenced=set(),
    )
    registry = MagicMock()

    def _get(entity_id: str) -> Any:
        if entity_id == "sensor.foo":
            return SimpleNamespace(domain="sensor", unique_id="x")
        if entity_id == "alarm_control_panel.missing":
            return None
        # references a space that is not in the account
        return SimpleNamespace(domain="alarm_control_panel", unique_id="e1_alarm_ghost")

    registry.async_get.side_effect = _get
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "custom_components.ajax._services.async_extract_referenced_entity_ids",
            return_value=selected,
        ),
        patch("custom_components.ajax._services.er.async_get", return_value=registry),
        pytest.raises(ServiceValidationError),
    ):
        # No resolvable target -> validation error
        await handlers[SERVICE_FORCE_ARM](call)
    coord.async_arm_space.assert_not_awaited()


async def test_force_arm_skips_coordinator_without_spaces(tmp_path: Any) -> None:
    """A coordinator with no account/spaces is skipped and raises if none resolve."""
    _, handlers = await _handlers(tmp_path)
    coord = MagicMock()
    coord.account = None
    call = _call(_entry(coord))
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        pytest.raises(ServiceValidationError),
    ):
        await handlers[SERVICE_FORCE_ARM](call)


async def test_force_arm_collects_failures(tmp_path: Any) -> None:
    """A failing arm call is collected and surfaced as HomeAssistantError."""
    _, handlers = await _handlers(tmp_path)
    coord = _coord_with_spaces(["s1"])
    coord.async_arm_space.side_effect = RuntimeError("boom")
    call = _call(_entry(coord))
    selected = SimpleNamespace(referenced=set(), indirectly_referenced=set())
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "custom_components.ajax._services.async_extract_referenced_entity_ids",
            return_value=selected,
        ),
        pytest.raises(HomeAssistantError),
    ):
        await handlers[SERVICE_FORCE_ARM](call)


async def test_resolve_target_spaces_empty_when_account_none(tmp_path: Any) -> None:
    """force_arm with account=None resolves no targets and raises."""
    _, handlers = await _handlers(tmp_path)
    coord = MagicMock()
    coord.account = SimpleNamespace(spaces={})  # truthy account but no spaces
    call = _call(_entry(coord))
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        pytest.raises(ServiceValidationError),
    ):
        await handlers[SERVICE_FORCE_ARM](call)


async def test_force_arm_night_success(tmp_path: Any) -> None:
    """force_arm_night arms each resolved space in night mode with force=True."""
    _, handlers = await _handlers(tmp_path)
    coord = _coord_with_spaces(["s1", "s2"])
    call = _call(_entry(coord))
    selected = SimpleNamespace(referenced=set(), indirectly_referenced=set())
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "custom_components.ajax._services.async_extract_referenced_entity_ids",
            return_value=selected,
        ),
    ):
        await handlers[SERVICE_FORCE_ARM_NIGHT](call)
    assert coord.async_arm_night_mode.await_count == 2
    coord.async_arm_night_mode.assert_any_await("s1", force=True)


async def test_force_arm_night_no_target_raises(tmp_path: Any) -> None:
    """force_arm_night with no resolvable target raises validation error."""
    _, handlers = await _handlers(tmp_path)
    coord = MagicMock()
    coord.account = None
    call = _call(_entry(coord))
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        pytest.raises(ServiceValidationError),
    ):
        await handlers[SERVICE_FORCE_ARM_NIGHT](call)


async def test_force_arm_night_unresolved_target_raises(tmp_path: Any) -> None:
    """Referenced entities that resolve to no known space raise validation."""
    _, handlers = await _handlers(tmp_path)
    coord = _coord_with_spaces(["known"])
    call = _call(_entry(coord))
    selected = SimpleNamespace(
        referenced={"alarm_control_panel.ghost"},
        indirectly_referenced=set(),
    )
    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(
        domain="alarm_control_panel",
        unique_id="e1_alarm_ghost",  # space not in account
    )
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "custom_components.ajax._services.async_extract_referenced_entity_ids",
            return_value=selected,
        ),
        patch("custom_components.ajax._services.er.async_get", return_value=registry),
        pytest.raises(ServiceValidationError),
    ):
        await handlers[SERVICE_FORCE_ARM_NIGHT](call)
    coord.async_arm_night_mode.assert_not_awaited()


async def test_force_arm_night_collects_failures(tmp_path: Any) -> None:
    """A failing night-arm call surfaces as HomeAssistantError."""
    _, handlers = await _handlers(tmp_path)
    coord = _coord_with_spaces(["s1"])
    coord.async_arm_night_mode.side_effect = RuntimeError("nope")
    call = _call(_entry(coord))
    selected = SimpleNamespace(referenced=set(), indirectly_referenced=set())
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "custom_components.ajax._services.async_extract_referenced_entity_ids",
            return_value=selected,
        ),
        pytest.raises(HomeAssistantError),
    ):
        await handlers[SERVICE_FORCE_ARM_NIGHT](call)


# --------------------------------------------------------------------------- #
# get_raw_devices
# --------------------------------------------------------------------------- #
def _space_for_raw(
    *,
    hub_id: str | None = "hub1",
    real_space_id: str | None = "real1",
) -> SimpleNamespace:
    return SimpleNamespace(hub_id=hub_id, real_space_id=real_space_id, name="Home")


async def test_get_raw_devices_writes_file_and_notifies(tmp_path: Any) -> None:
    """Full happy path: devices + cameras + video edges are written and summarised."""
    _, handlers = await _handlers(tmp_path)
    coord = MagicMock()
    coord.account = SimpleNamespace(spaces={"sp": _space_for_raw()})
    coord.api.async_get_devices = AsyncMock(return_value=[{"id": "d1"}])
    coord.api.async_get_device = AsyncMock(return_value={"id": "d1", "deviceType": "MotionProtect"})
    coord.api.async_get_cameras = AsyncMock(return_value=[{"id": "c1"}])
    coord.api.async_get_camera = AsyncMock(return_value={"id": "c1", "deviceType": "Cam"})
    coord.api.async_get_video_edges = AsyncMock(return_value=[{"id": "ve1"}])
    call = _call(_entry(coord))
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch("custom_components.ajax._services.async_create") as notify,
    ):
        await handlers[SERVICE_GET_RAW_DEVICES](call)

    out = tmp_path / "ajax_raw_devices.json"
    assert out.exists()
    data = json.loads(out.read_text())
    assert {"devices", "cameras", "video_edges"} <= set(data)
    assert len(data["devices"]) == 1
    notify.assert_called_once()


async def test_get_raw_devices_device_fetch_failure_falls_back_to_summary(
    tmp_path: Any,
) -> None:
    """When full device fetch fails, the light summary is kept instead."""
    _, handlers = await _handlers(tmp_path)
    coord = MagicMock()
    coord.account = SimpleNamespace(spaces={"sp": _space_for_raw(real_space_id=None)})
    coord.api.async_get_devices = AsyncMock(return_value=[{"id": "d1"}, {"no": "id"}])
    coord.api.async_get_device = AsyncMock(side_effect=RuntimeError("dev fail"))
    coord.api.async_get_cameras = AsyncMock(return_value=[])
    call = _call(_entry(coord))
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch("custom_components.ajax._services.async_create"),
    ):
        await handlers[SERVICE_GET_RAW_DEVICES](call)
    out = tmp_path / "ajax_raw_devices.json"
    data = json.loads(out.read_text())
    # Fallback summary kept; device without id skipped.
    assert data["devices"] == [{"id": "d1"}]
    assert data["video_edges"] == []


async def test_get_raw_devices_list_fetch_failure_logged(tmp_path: Any) -> None:
    """Failures from get_devices/get_cameras/get_video_edges are swallowed."""
    _, handlers = await _handlers(tmp_path)
    coord = MagicMock()
    coord.account = SimpleNamespace(spaces={"sp": _space_for_raw()})
    coord.api.async_get_devices = AsyncMock(side_effect=RuntimeError("list fail"))
    coord.api.async_get_cameras = AsyncMock(side_effect=RuntimeError("cam fail"))
    coord.api.async_get_video_edges = AsyncMock(side_effect=RuntimeError("ve fail"))
    call = _call(_entry(coord))
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch("custom_components.ajax._services.async_create"),
    ):
        await handlers[SERVICE_GET_RAW_DEVICES](call)
    out = tmp_path / "ajax_raw_devices.json"
    data = json.loads(out.read_text())
    assert data["devices"] == []
    assert data["cameras"] == []
    assert data["video_edges"] == []


async def test_get_raw_devices_camera_full_fetch_failure(tmp_path: Any) -> None:
    """A failing full-camera fetch falls back to the camera summary."""
    _, handlers = await _handlers(tmp_path)
    coord = MagicMock()
    coord.account = SimpleNamespace(spaces={"sp": _space_for_raw(real_space_id=None)})
    coord.api.async_get_devices = AsyncMock(return_value=[])
    coord.api.async_get_cameras = AsyncMock(return_value=[{"id": "c1"}, {"no": "id"}])
    coord.api.async_get_camera = AsyncMock(side_effect=RuntimeError("cam fail"))
    call = _call(_entry(coord))
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch("custom_components.ajax._services.async_create"),
    ):
        await handlers[SERVICE_GET_RAW_DEVICES](call)
    out = tmp_path / "ajax_raw_devices.json"
    data = json.loads(out.read_text())
    assert data["cameras"] == [{"id": "c1"}]


async def test_get_raw_devices_skips_account_none(tmp_path: Any) -> None:
    """A coordinator without an account contributes nothing but still writes a file."""
    _, handlers = await _handlers(tmp_path)
    coord = MagicMock()
    coord.account = None
    call = _call(_entry(coord))
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch("custom_components.ajax._services.async_create"),
    ):
        await handlers[SERVICE_GET_RAW_DEVICES](call)
    out = tmp_path / "ajax_raw_devices.json"
    data = json.loads(out.read_text())
    assert data == {"devices": [], "cameras": [], "video_edges": []}


# --------------------------------------------------------------------------- #
# get_nvr_recordings
# --------------------------------------------------------------------------- #
def _nvr_video_edge(channels: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(
        video_edge_type=SimpleNamespace(value="NVR"),
        name="NVR-1",
        channels=channels,
    )


def _non_nvr_video_edge() -> SimpleNamespace:
    return SimpleNamespace(
        video_edge_type=SimpleNamespace(value="BULLET"),
        name="Cam",
        channels=[],
    )


async def test_get_nvr_recordings_happy_path(tmp_path: Any) -> None:
    """NVR channels are queried and recordings stored in the output file."""
    _, handlers = await _handlers(tmp_path)
    coord = MagicMock()
    space = SimpleNamespace(
        video_edges={
            "ve1": _nvr_video_edge([{"id": "cam1", "name": "Front"}]),
            "ve2": _non_nvr_video_edge(),
        }
    )
    coord.account = SimpleNamespace(spaces={"sp": space})
    coord.api.async_get_nvr_recordings = AsyncMock(return_value=[{"r": 1}, {"r": 2}])
    call = _call(_entry(coord))
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch("custom_components.ajax._services.async_create") as notify,
    ):
        await handlers[SERVICE_GET_NVR_RECORDINGS](call)
    coord.api.async_get_nvr_recordings.assert_awaited_once()
    out = tmp_path / "ajax_nvr_recordings.json"
    data = json.loads(out.read_text())
    assert data[0]["nvr_id"] == "ve1"
    assert data[0]["recordings"][0]["camera_id"] == "cam1"
    notify.assert_called_once()


async def test_get_nvr_recordings_channel_error(tmp_path: Any) -> None:
    """A failing recordings call is captured as an error entry."""
    _, handlers = await _handlers(tmp_path)
    coord = MagicMock()
    space = SimpleNamespace(video_edges={"ve1": _nvr_video_edge([{"id": "cam1"}, "not-a-dict", {"noid": 1}])})
    coord.account = SimpleNamespace(spaces={"sp": space})
    coord.api.async_get_nvr_recordings = AsyncMock(side_effect=RuntimeError("rec fail"))
    call = _call(_entry(coord))
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch("custom_components.ajax._services.async_create"),
    ):
        await handlers[SERVICE_GET_NVR_RECORDINGS](call)
    out = tmp_path / "ajax_nvr_recordings.json"
    data = json.loads(out.read_text())
    assert "error" in data[0]["recordings"][0]


async def test_get_nvr_recordings_no_nvr_and_account_none(tmp_path: Any) -> None:
    """No NVR present and a None account both yield an empty NVR list."""
    _, handlers = await _handlers(tmp_path)
    coord_none = MagicMock()
    coord_none.account = None
    coord_plain = MagicMock()
    coord_plain.account = SimpleNamespace(spaces={"sp": SimpleNamespace(video_edges={"ve": _non_nvr_video_edge()})})
    call = MagicMock()
    call.hass.config_entries.async_loaded_entries.return_value = [
        _entry(coord_none, "a"),
        _entry(coord_plain, "b"),
    ]
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch("custom_components.ajax._services.async_create"),
    ):
        await handlers[SERVICE_GET_NVR_RECORDINGS](call)
    out = tmp_path / "ajax_nvr_recordings.json"
    data = json.loads(out.read_text())
    assert data == []


# --------------------------------------------------------------------------- #
# get_smart_locks
# --------------------------------------------------------------------------- #
def _smart_lock(**kw: Any) -> SimpleNamespace:
    defaults = {
        "name": "Lock",
        "is_locked": True,
        "is_door_open": False,
        "last_event_tag": "tag",
        "last_event_time": None,
        "last_changed_by": "user",
        "raw_data": {"k": "v"},
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


async def test_get_smart_locks_api_and_sse_merge(tmp_path: Any) -> None:
    """API locks and SSE-discovered locks are merged; duplicates de-duplicated."""
    _, handlers = await _handlers(tmp_path)
    coord = MagicMock()
    space = SimpleNamespace(
        name="Home",
        real_space_id="real1",
        smart_locks={
            "api-lock": _smart_lock(name="ApiDup"),  # duplicate of API entry -> skipped
            "sse-lock": _smart_lock(name="SseOnly"),
        },
    )
    coord.account = SimpleNamespace(spaces={"sp": space})
    coord.api.async_get_smart_locks = AsyncMock(return_value=[{"id": "api-lock", "name": "ApiLock"}])
    call = _call(_entry(coord))
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch("custom_components.ajax._services.async_create") as notify,
    ):
        await handlers[SERVICE_GET_SMART_LOCKS](call)
    out = tmp_path / "ajax_smart_locks.json"
    data = json.loads(out.read_text())
    ids = sorted(sl["id"] for sl in data)
    assert ids == ["api-lock", "sse-lock"]
    sources = {sl["id"]: sl["_source"] for sl in data}
    assert sources["api-lock"] == "api"
    assert sources["sse-lock"] == "sse_sqs_discovered"
    notify.assert_called_once()


async def test_get_smart_locks_api_error_keeps_sse(tmp_path: Any) -> None:
    """An API failure still lets SSE-discovered locks be recorded."""
    _, handlers = await _handlers(tmp_path)
    coord = MagicMock()
    space = SimpleNamespace(
        name="Home",
        real_space_id="real1",
        smart_locks={"sse-lock": _smart_lock(name="SseOnly")},
    )
    coord.account = SimpleNamespace(spaces={"sp": space})
    coord.api.async_get_smart_locks = AsyncMock(side_effect=RuntimeError("api fail"))
    call = _call(_entry(coord))
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch("custom_components.ajax._services.async_create"),
    ):
        await handlers[SERVICE_GET_SMART_LOCKS](call)
    out = tmp_path / "ajax_smart_locks.json"
    data = json.loads(out.read_text())
    assert len(data) == 1
    assert data[0]["_source"] == "sse_sqs_discovered"


async def test_get_smart_locks_skips_space_without_real_space_id(tmp_path: Any) -> None:
    """Spaces missing a real_space_id are skipped entirely."""
    _, handlers = await _handlers(tmp_path)
    coord = MagicMock()
    space = SimpleNamespace(name="Home", real_space_id=None, smart_locks={})
    coord.account = SimpleNamespace(spaces={"sp": space})
    call = _call(_entry(coord))
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch("custom_components.ajax._services.async_create"),
    ):
        await handlers[SERVICE_GET_SMART_LOCKS](call)
    out = tmp_path / "ajax_smart_locks.json"
    data = json.loads(out.read_text())
    assert data == []


async def test_get_smart_locks_account_none(tmp_path: Any) -> None:
    """A coordinator without an account contributes no locks."""
    _, handlers = await _handlers(tmp_path)
    coord = MagicMock()
    coord.account = None
    call = _call(_entry(coord))
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch("custom_components.ajax._services.async_create"),
    ):
        await handlers[SERVICE_GET_SMART_LOCKS](call)
    out = tmp_path / "ajax_smart_locks.json"
    assert json.loads(out.read_text()) == []


# --------------------------------------------------------------------------- #
# async_setup
# --------------------------------------------------------------------------- #
async def test_async_setup_registers_services() -> None:
    """async_setup delegates to _async_setup_services and returns True."""
    hass = MagicMock()
    with patch("custom_components.ajax._async_setup_services", AsyncMock()) as setup_services:
        assert await async_setup(hass, {}) is True
    setup_services.assert_awaited_once_with(hass)


# --------------------------------------------------------------------------- #
# async_unload_entry
# --------------------------------------------------------------------------- #
async def test_async_unload_entry_shuts_down_coordinator() -> None:
    """A successful platform unload triggers coordinator shutdown."""
    hass = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    coord = MagicMock()
    coord.async_shutdown = AsyncMock()
    entry = SimpleNamespace(runtime_data=coord)
    assert await async_unload_entry(hass, entry) is True
    coord.async_shutdown.assert_awaited_once()


async def test_async_unload_entry_no_shutdown_when_unload_fails() -> None:
    """If platform unload fails, the coordinator is not shut down."""
    hass = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)
    coord = MagicMock()
    coord.async_shutdown = AsyncMock()
    entry = SimpleNamespace(runtime_data=coord)
    assert await async_unload_entry(hass, entry) is False
    coord.async_shutdown.assert_not_awaited()


# --------------------------------------------------------------------------- #
# async_remove_config_entry_device
# --------------------------------------------------------------------------- #
async def test_remove_device_true_when_coordinator_or_account_none() -> None:
    """A missing coordinator/account allows removing any orphaned device."""
    hass = MagicMock()
    device = SimpleNamespace(identifiers={(DOMAIN, "anything")})

    entry_none = SimpleNamespace(runtime_data=None)
    assert await async_remove_config_entry_device(hass, entry_none, device) is True

    coord = MagicMock()
    coord.account = None
    entry_acc_none = SimpleNamespace(runtime_data=coord)
    assert await async_remove_config_entry_device(hass, entry_acc_none, device) is True


async def test_remove_device_false_when_still_known() -> None:
    """A device still tracked by the coordinator must not be removed."""
    hass = MagicMock()
    coord = MagicMock()
    space = SimpleNamespace(
        id="space1",
        hub_id="hub1",
        devices={"dev1": object()},
        video_edges={"ve1": object()},
        smart_locks={"sl1": object()},
    )
    coord.account = SimpleNamespace(spaces={"space1": space})
    entry = SimpleNamespace(entry_id="entry_test", runtime_data=coord)

    # Identifiers are namespaced f"{entry_id}_{ajax_id}" (schema v1.3).
    known = SimpleNamespace(identifiers={(DOMAIN, "entry_test_dev1")})
    assert await async_remove_config_entry_device(hass, entry, known) is False


async def test_remove_device_true_when_unknown() -> None:
    """A device no longer known (different domain or id) is removable."""
    hass = MagicMock()
    coord = MagicMock()
    space = SimpleNamespace(
        id="space1",
        hub_id=None,
        devices={},
        video_edges={},
        smart_locks={},
    )
    coord.account = SimpleNamespace(spaces={"space1": space})
    entry = SimpleNamespace(entry_id="entry_test", runtime_data=coord)

    # Identifiers are namespaced f"{entry_id}_{ajax_id}" (schema v1.3).
    orphan = SimpleNamespace(identifiers={(DOMAIN, "entry_test_gone"), ("other", "x")})
    assert await async_remove_config_entry_device(hass, entry, orphan) is True


# --------------------------------------------------------------------------- #
# _async_setup_areas
# --------------------------------------------------------------------------- #
async def test_setup_areas_account_none_returns_early() -> None:
    """No account means nothing to sync."""
    hass = MagicMock()
    coord = MagicMock()
    coord.account = None
    with (
        patch("custom_components.ajax.ar.async_get") as area_get,
        patch("custom_components.ajax.dr.async_get") as device_get,
    ):
        await _async_setup_areas(hass, coord)
    # Registries are fetched but no work performed.
    area_get.assert_called_once()
    device_get.assert_called_once()


async def test_setup_areas_creates_area_and_assigns_device() -> None:
    """A new room creates an area and an unassigned device gets the area."""
    hass = MagicMock()
    coord = MagicMock()
    device = SimpleNamespace(name="Sensor", room_id="room1")
    space = SimpleNamespace(
        rooms_map={"room1": "Living Room", "room_empty": ""},
        devices={"dev1": device},
    )
    coord.account = SimpleNamespace(spaces={"sp": space})

    area_reg = MagicMock()
    area_reg.async_get_area_by_name.return_value = None  # area does not exist yet
    created_area = SimpleNamespace(id="area-id")
    area_reg.async_create.return_value = created_area

    device_reg = MagicMock()
    ha_device = SimpleNamespace(id="ha-dev", area_id=None)
    device_reg.async_get_device.return_value = ha_device

    with (
        patch("custom_components.ajax.ar.async_get", return_value=area_reg),
        patch("custom_components.ajax.dr.async_get", return_value=device_reg),
    ):
        await _async_setup_areas(hass, coord)

    area_reg.async_create.assert_called_once_with(name="Living Room")
    device_reg.async_update_device.assert_called_once_with("ha-dev", area_id="area-id")


async def test_setup_areas_respects_existing_area_and_assignment() -> None:
    """Existing areas are reused and already-assigned devices are left alone."""
    hass = MagicMock()
    coord = MagicMock()
    # Two devices: one already assigned, one in a different room (no match).
    assigned = SimpleNamespace(name="A", room_id="room1")
    other_room = SimpleNamespace(name="B", room_id="roomX")
    space = SimpleNamespace(
        rooms_map={"room1": "Kitchen"},
        devices={"d1": assigned, "d2": other_room},
    )
    coord.account = SimpleNamespace(spaces={"sp": space})

    area_reg = MagicMock()
    area_reg.async_get_area_by_name.return_value = SimpleNamespace(id="existing")

    device_reg = MagicMock()
    # d1 already has an area assigned -> skipped
    device_reg.async_get_device.return_value = SimpleNamespace(id="x", area_id="set")

    with (
        patch("custom_components.ajax.ar.async_get", return_value=area_reg),
        patch("custom_components.ajax.dr.async_get", return_value=device_reg),
    ):
        await _async_setup_areas(hass, coord)

    area_reg.async_create.assert_not_called()
    device_reg.async_update_device.assert_not_called()


async def test_setup_areas_missing_ha_device_skipped() -> None:
    """A device matching the room but absent from the registry is skipped."""
    hass = MagicMock()
    coord = MagicMock()
    device = SimpleNamespace(name="Sensor", room_id="room1")
    space = SimpleNamespace(
        rooms_map={"room1": "Garage"},
        devices={"dev1": device},
    )
    coord.account = SimpleNamespace(spaces={"sp": space})

    area_reg = MagicMock()
    area_reg.async_get_area_by_name.return_value = SimpleNamespace(id="a")
    device_reg = MagicMock()
    device_reg.async_get_device.return_value = None  # not in registry

    with (
        patch("custom_components.ajax.ar.async_get", return_value=area_reg),
        patch("custom_components.ajax.dr.async_get", return_value=device_reg),
    ):
        await _async_setup_areas(hass, coord)

    device_reg.async_update_device.assert_not_called()


# --------------------------------------------------------------------------- #
# _async_update_options
# --------------------------------------------------------------------------- #
def _options_entry(*, fast_poll: bool) -> SimpleNamespace:
    return SimpleNamespace(options={"door_sensor_fast_poll": fast_poll})


async def test_update_options_disable_with_entry_runtime() -> None:
    """When disabled, _manage_door_sensor_polling(False, ...) is invoked."""
    entry = _options_entry(fast_poll=False)
    coord = MagicMock()
    coord._door_sensor_fast_poll_enabled = True
    coord._door_sensor_poll_task = MagicMock()
    coord._door_sensor_poll_security_state = SecurityState.DISARMED
    coord._manage_door_sensor_polling = MagicMock()
    entry.runtime_data = coord

    await _async_update_options(MagicMock(), entry)

    coord._manage_door_sensor_polling.assert_called_once_with(False, SecurityState.DISARMED)
    assert coord._door_sensor_fast_poll_enabled is False


async def test_update_options_enable_restarts_polling_for_disarmed_space() -> None:
    """Enabling re-evaluates polling and starts it for a disarmed space."""
    entry = _options_entry(fast_poll=True)
    coord = MagicMock()
    coord._door_sensor_fast_poll_enabled = False
    coord._door_sensor_poll_task = None
    coord._manage_door_sensor_polling = MagicMock()
    space = SimpleNamespace(name="Home", security_state=SecurityState.DISARMED)
    coord.account = SimpleNamespace(spaces={"sp": space})
    entry.runtime_data = coord

    await _async_update_options(MagicMock(), entry)

    coord._manage_door_sensor_polling.assert_called_once_with(True, SecurityState.DISARMED)


async def test_update_options_enable_no_account_no_restart() -> None:
    """Enabling with no account does not start any polling task."""
    entry = _options_entry(fast_poll=True)
    coord = MagicMock()
    coord._door_sensor_fast_poll_enabled = False
    coord._door_sensor_poll_task = None
    coord._manage_door_sensor_polling = MagicMock()
    coord.account = None
    entry.runtime_data = coord

    await _async_update_options(MagicMock(), entry)

    coord._manage_door_sensor_polling.assert_not_called()


async def test_update_options_enable_armed_space_not_polled() -> None:
    """An armed space does not trigger door-sensor polling."""
    entry = _options_entry(fast_poll=True)
    coord = MagicMock()
    coord._door_sensor_fast_poll_enabled = False
    coord._door_sensor_poll_task = None
    coord._manage_door_sensor_polling = MagicMock()
    space = SimpleNamespace(name="Home", security_state=SecurityState.ARMED)
    coord.account = SimpleNamespace(spaces={"sp": space})
    entry.runtime_data = coord

    await _async_update_options(MagicMock(), entry)

    coord._manage_door_sensor_polling.assert_not_called()


async def test_update_options_no_change_is_noop() -> None:
    """When the option value is unchanged, no polling management occurs."""
    entry = _options_entry(fast_poll=False)
    coord = MagicMock()
    coord._door_sensor_fast_poll_enabled = False  # same as new value
    coord._manage_door_sensor_polling = MagicMock()
    entry.runtime_data = coord

    await _async_update_options(MagicMock(), entry)

    coord._manage_door_sensor_polling.assert_not_called()


async def test_force_arm_skips_group_panels(tmp_path: Any) -> None:
    """A group panel must never be parsed as a space target.

    Its unique_id (``{entry}_group_alarm_{group_id}``) also contains the
    ``_alarm_`` marker — before the guard, a group_id that collided with a
    space_id would have been force-armed. Adversarial setup: the space id
    EQUALS the group id.
    """
    _, handlers = await _handlers(tmp_path)
    coord = _coord_with_spaces(["g42"])
    call = _call(_entry(coord))
    selected = SimpleNamespace(
        referenced={"alarm_control_panel.perimeter_group"},
        indirectly_referenced=set(),
    )
    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(
        domain="alarm_control_panel",
        unique_id="e1_group_alarm_g42",
    )
    with (
        patch(
            "custom_components.ajax._services.async_extract_config_entry_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "custom_components.ajax._services.async_extract_referenced_entity_ids",
            return_value=selected,
        ),
        patch("custom_components.ajax._services.er.async_get", return_value=registry),
        pytest.raises(ServiceValidationError),
    ):
        await handlers[SERVICE_FORCE_ARM](call)
    coord.async_arm_space.assert_not_awaited()
