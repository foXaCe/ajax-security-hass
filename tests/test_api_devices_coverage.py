"""Tests for AjaxRestApi device/video/lock/automation/arming endpoints.

We mock ``_request`` / ``_request_no_response`` and verify URL routing,
payload shapes, and the small bits of business logic that wrap them
(DEVICE_UPDATE_EXCLUDE_FIELDS filtering, deep merge for nested settings,
video-edge / smart-lock discovery from the space payload).

A regression here usually means a wrong endpoint URL (silent 404 the
coordinator masks) or a malformed command payload the hub rejects.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.ajax.api import AjaxRestApi, AjaxRestApiError


def _api(user_id: str | None = "USER123") -> AjaxRestApi:
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    api.user_id = user_id
    api._request = AsyncMock()  # type: ignore[method-assign]
    api._request_no_response = AsyncMock()  # type: ignore[method-assign]
    return api


# ---------------------------------------------------------------------------
# Device update: field filtering + routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_update_device_requires_user_id() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_update_device("h1", "d1", {"alwaysActive": True})


@pytest.mark.asyncio
async def test_async_update_device_merges_settings_and_strips_excluded() -> None:
    api = _api()
    api._request.return_value = {
        "id": "d1",
        "hubId": "h1",
        "deviceType": "DoorProtect",
        "type": "DOOR_PROTECT",
        "online": True,
        "batteryLevel": 80,
        "alwaysActive": False,
        "name": "Front Door",
    }
    await api.async_update_device("h1", "d1", {"alwaysActive": True})

    api._request.assert_awaited_once_with("GET", "user/USER123/hubs/h1/devices/d1")
    method, endpoint, body = api._request_no_response.await_args[0]
    assert method == "PUT"
    assert endpoint == "user/USER123/hubs/h1/devices/d1"
    # The user override wins over the fetched value.
    assert body["alwaysActive"] is True
    # deviceType is required by the API and must survive filtering.
    assert body["deviceType"] == "DoorProtect"
    assert body["name"] == "Front Door"
    # Read-only / computed fields are stripped.
    for stripped in ("id", "hubId", "type", "online", "batteryLevel"):
        assert stripped not in body


@pytest.mark.asyncio
async def test_async_update_device_strips_every_exclude_field() -> None:
    """All DEVICE_UPDATE_EXCLUDE_FIELDS present on the device are removed."""
    api = _api()
    device = dict.fromkeys(AjaxRestApi.DEVICE_UPDATE_EXCLUDE_FIELDS, "x")
    device["deviceType"] = "Relay"
    device["keep"] = "yes"
    api._request.return_value = device

    await api.async_update_device("h1", "d1", {})
    body = api._request_no_response.await_args[0][2]
    assert body == {"deviceType": "Relay", "keep": "yes"}


@pytest.mark.asyncio
async def test_async_update_device_settings_can_be_filtered_out() -> None:
    """A setting that collides with an excluded field gets stripped too."""
    api = _api()
    api._request.return_value = {"deviceType": "Relay"}
    await api.async_update_device("h1", "d1", {"online": True, "custom": 1})
    body = api._request_no_response.await_args[0][2]
    assert "online" not in body
    assert body["custom"] == 1


# ---------------------------------------------------------------------------
# Device update nested: deep merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_update_device_nested_requires_user_id() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_update_device_nested("h1", "d1", {"a": {"b": 1}})


@pytest.mark.asyncio
async def test_async_update_device_nested_deep_merges() -> None:
    api = _api()
    api._request.return_value = {
        "deviceType": "Relay",
        "wiredDeviceSettings": {"keep": 1, "override": "old"},
    }
    await api.async_update_device_nested(
        "h1",
        "d1",
        {"wiredDeviceSettings": {"override": "new", "added": 2}},
    )
    body = api._request_no_response.await_args[0][2]
    # Existing nested keys preserved, updated keys overwritten, new keys added.
    assert body["wiredDeviceSettings"] == {"keep": 1, "override": "new", "added": 2}
    assert api._request_no_response.await_args[0][0] == "PUT"
    assert api._request_no_response.await_args[0][1] == "user/USER123/hubs/h1/devices/d1"


@pytest.mark.asyncio
async def test_async_update_device_nested_strips_excluded() -> None:
    api = _api()
    api._request.return_value = {"deviceType": "Relay", "online": True, "id": "d1"}
    await api.async_update_device_nested("h1", "d1", {"name": "X"})
    body = api._request_no_response.await_args[0][2]
    assert "online" not in body
    assert "id" not in body
    assert body["name"] == "X"


def test_deep_merge_replaces_non_dict_values() -> None:
    api = _api()
    base = {"a": {"x": 1}, "b": [1, 2], "c": "old"}
    updates = {"a": {"y": 2}, "b": [3], "c": "new"}
    merged = api._deep_merge(base, updates)
    assert merged == {"a": {"x": 1, "y": 2}, "b": [3], "c": "new"}
    # Base is not mutated.
    assert base["a"] == {"x": 1}


def test_deep_merge_replaces_dict_with_scalar() -> None:
    api = _api()
    merged = api._deep_merge({"a": {"x": 1}}, {"a": 5})
    assert merged == {"a": 5}


# ---------------------------------------------------------------------------
# Video Edge endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_get_video_edge_routes() -> None:
    api = _api()
    await api.async_get_video_edge("space1", "ve1")
    api._request.assert_awaited_once_with(
        "GET",
        "user/USER123/spaces/space1/devices/video-edges/ve1",
    )


@pytest.mark.asyncio
async def test_async_get_video_edge_onvif_routes() -> None:
    api = _api()
    await api.async_get_video_edge_onvif("space1", "ve1")
    api._request.assert_awaited_once_with(
        "GET",
        "user/USER123/spaces/space1/devices/video-edges/ve1/onvif",
    )


@pytest.mark.asyncio
async def test_async_get_video_edge_rtsp_routes() -> None:
    api = _api()
    await api.async_get_video_edge_rtsp("space1", "ve1")
    api._request.assert_awaited_once_with(
        "GET",
        "user/USER123/spaces/space1/devices/video-edges/ve1/rtsp",
    )


@pytest.mark.asyncio
async def test_async_get_video_edges_filters_by_type_and_fetches_each() -> None:
    api = _api()

    async def fake_request(method: str, endpoint: str, *args: object) -> object:
        if endpoint == "user/USER123/spaces/space1":
            return {
                "devices": [
                    {"id": "ve1", "type": "VIDEO_EDGE"},
                    {"id": "lock1", "type": "SMART_LOCK"},
                    {"id": "ve2", "type": "VIDEO_EDGE"},
                ]
            }
        return {"id": endpoint.rsplit("/", 1)[-1]}

    api._request.side_effect = fake_request
    result = await api.async_get_video_edges("space1")
    ids = {ve["id"] for ve in result}
    assert ids == {"ve1", "ve2"}


@pytest.mark.asyncio
async def test_async_get_video_edges_skips_devices_without_id() -> None:
    api = _api()

    async def fake_request(method: str, endpoint: str, *args: object) -> object:
        if endpoint == "user/USER123/spaces/space1":
            return {"devices": [{"type": "VIDEO_EDGE"}]}  # no id
        return {"id": "should-not-happen"}

    api._request.side_effect = fake_request
    result = await api.async_get_video_edges("space1")
    assert result == []


@pytest.mark.asyncio
async def test_async_get_video_edges_swallows_per_device_errors() -> None:
    api = _api()

    async def fake_request(method: str, endpoint: str, *args: object) -> object:
        if endpoint == "user/USER123/spaces/space1":
            return {"devices": [{"id": "ve1", "type": "VIDEO_EDGE"}]}
        raise AjaxRestApiError("boom")

    api._request.side_effect = fake_request
    # One failing device must not break the whole listing.
    result = await api.async_get_video_edges("space1")
    assert result == []


@pytest.mark.asyncio
async def test_async_get_video_edges_empty_when_no_devices_key() -> None:
    api = _api()
    api._request.return_value = {}
    result = await api.async_get_video_edges("space1")
    assert result == []


# ---------------------------------------------------------------------------
# Smart lock endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_get_smart_lock_routes() -> None:
    api = _api()
    await api.async_get_smart_lock("space1", "lock1")
    api._request.assert_awaited_once_with(
        "GET",
        "user/USER123/spaces/space1/devices/smart-locks/lock1",
    )


@pytest.mark.asyncio
async def test_async_get_smart_locks_discovers_and_fetches() -> None:
    api = _api()

    async def fake_request(method: str, endpoint: str, *args: object) -> object:
        if endpoint == "user/USER123/spaces/space1":
            return {
                "devices": [
                    {"id": "lock1", "type": "SMART_LOCK", "name": "Door"},
                    {"id": "ve1", "type": "VIDEO_EDGE"},
                ]
            }
        return {"id": "lock1", "name": "Detailed"}

    api._request.side_effect = fake_request
    result = await api.async_get_smart_locks("space1")
    assert len(result) == 1
    assert result[0]["name"] == "Detailed"


@pytest.mark.asyncio
async def test_async_get_smart_locks_merges_name_from_space_listing() -> None:
    """When the detail endpoint omits the name, it is back-filled from the space."""
    api = _api()

    async def fake_request(method: str, endpoint: str, *args: object) -> object:
        if endpoint == "user/USER123/spaces/space1":
            return {"devices": [{"id": "lock1", "type": "SMART_LOCK", "name": "Front"}]}
        return {"id": "lock1"}  # no name

    api._request.side_effect = fake_request
    result = await api.async_get_smart_locks("space1")
    assert result[0]["name"] == "Front"


@pytest.mark.asyncio
async def test_async_get_smart_locks_falls_back_to_space_device_on_error() -> None:
    api = _api()
    space_device = {"id": "lock1", "type": "SMART_LOCK", "name": "Fallback"}

    async def fake_request(method: str, endpoint: str, *args: object) -> object:
        if endpoint == "user/USER123/spaces/space1":
            return {"devices": [space_device]}
        raise AjaxRestApiError("detail boom")

    api._request.side_effect = fake_request
    result = await api.async_get_smart_locks("space1")
    assert result == [space_device]


@pytest.mark.asyncio
async def test_async_get_smart_locks_skips_device_without_id() -> None:
    api = _api()
    api._request.return_value = {"devices": [{"type": "SMART_LOCK"}]}
    result = await api.async_get_smart_locks("space1")
    assert result == []


@pytest.mark.asyncio
async def test_async_get_smart_locks_known_id_skips_detail_fetch() -> None:
    """A lock already known (known_ids) must not trigger the per-lock detail GET.

    The detail endpoint only returns the id (occasionally a name) — useful
    once, at discovery, to feed the Yale-cloud filter. For an already-known
    lock, its real state comes from the enriched devices payload fetched on
    the same tick, so the space device entry is reused as-is.
    """
    api = _api()
    space_device = {"id": "lock1", "type": "SMART_LOCK", "name": "Front"}
    api._request.return_value = {"devices": [space_device]}

    result = await api.async_get_smart_locks("space1", known_ids={"lock1"})

    assert result == [space_device]
    # Only the space GET happened; no detail GET for the known lock.
    api._request.assert_awaited_once_with("GET", "user/USER123/spaces/space1")


@pytest.mark.asyncio
async def test_async_get_smart_locks_unknown_id_still_fetches_detail() -> None:
    """An id absent from known_ids preserves discovery behavior (detail GET)."""
    api = _api()

    async def fake_request(method: str, endpoint: str, *args: object) -> object:
        if endpoint == "user/USER123/spaces/space1":
            return {"devices": [{"id": "lock1", "type": "SMART_LOCK", "name": "Door"}]}
        return {"id": "lock1", "name": "Detailed"}

    api._request.side_effect = fake_request
    result = await api.async_get_smart_locks("space1", known_ids=set())

    assert len(result) == 1
    assert result[0]["name"] == "Detailed"
    assert api._request.await_count == 2


@pytest.mark.asyncio
async def test_async_get_smart_locks_mixed_known_and_unknown() -> None:
    """Known locks reuse the space entry; unknown ones still get a single detail GET."""
    api = _api()
    known_device = {"id": "lock-known", "type": "SMART_LOCK", "name": "Known"}
    unknown_device = {"id": "lock-unknown", "type": "SMART_LOCK", "name": "Unknown"}

    async def fake_request(method: str, endpoint: str, *args: object) -> object:
        if endpoint == "user/USER123/spaces/space1":
            return {"devices": [known_device, unknown_device]}
        return {"id": "lock-unknown", "name": "Detailed"}

    api._request.side_effect = fake_request
    result = await api.async_get_smart_locks("space1", known_ids={"lock-known"})

    assert known_device in result
    assert any(sl.get("name") == "Detailed" for sl in result)
    # 1 space GET + exactly 1 detail GET (for the unknown lock only).
    assert api._request.await_count == 2


# ---------------------------------------------------------------------------
# Automation endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_get_automations_routes() -> None:
    api = _api()
    api._request.return_value = [{"id": "a1"}]
    result = await api.async_get_automations("h1")
    api._request.assert_awaited_once_with("GET", "hubs/h1/automations")
    assert result == [{"id": "a1"}]


@pytest.mark.asyncio
async def test_async_trigger_automation_posts() -> None:
    api = _api()
    await api.async_trigger_automation("h1", "a1")
    api._request.assert_awaited_once_with("POST", "hubs/h1/automations/a1/trigger")


# ---------------------------------------------------------------------------
# NVR recordings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_get_nvr_recordings_builds_query() -> None:
    api = _api()
    api._request.return_value = []
    await api.async_get_nvr_recordings("nvr1", "cam1", "2026-01-01T00:00:00", "2026-01-02T00:00:00")
    method, endpoint = api._request.await_args[0]
    assert method == "GET"
    assert endpoint.startswith("devices/nvr1/recordings?")
    assert "cameraId=cam1" in endpoint
    assert "start=2026-01-01T00%3A00%3A00" in endpoint
    assert "end=2026-01-02T00%3A00%3A00" in endpoint


# ---------------------------------------------------------------------------
# Dimmer brightness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_set_dimmer_brightness_requires_user_id() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_set_dimmer_brightness("h1", "d1", 50)


@pytest.mark.asyncio
async def test_async_set_dimmer_brightness_payload() -> None:
    api = _api()
    await api.async_set_dimmer_brightness("h1", "d1", 42)
    method, endpoint, body = api._request_no_response.await_args[0]
    assert method == "POST"
    assert endpoint == "user/USER123/hubs/h1/devices/d1/command"
    assert body["command"] == "BRIGHTNESS"
    assert body["deviceType"] == "LightSwitchDimmer"
    ap = body["additionalParam"]
    assert ap["additionalParamType"] == "BRIGHTNESS_STATUS"
    assert ap["brightnessInPercentage"] == 42
    assert ap["channels"] == ["CHANNEL_1"]
    assert ap["brightnessType"] == "BRIGHTNESS_TYPE_ABSOLUTE"


# ---------------------------------------------------------------------------
# Socket power
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_get_socket_power_routes() -> None:
    api = _api()
    api._request.return_value = {"power": 12.3}
    result = await api.async_get_socket_power("d1")
    api._request.assert_awaited_once_with("GET", "devices/d1/power")
    assert result == {"power": 12.3}


# ---------------------------------------------------------------------------
# Arming commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_arm_requires_user_id() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_arm("h1")


@pytest.mark.asyncio
async def test_async_arm_default_payload() -> None:
    api = _api()
    await api.async_arm("h1")
    method, endpoint, body = api._request_no_response.await_args[0]
    assert method == "PUT"
    assert endpoint == "user/USER123/hubs/h1/commands/arming"
    assert body == {"command": "ARM", "ignoreProblems": True}


@pytest.mark.asyncio
async def test_async_arm_respects_ignore_problems_flag() -> None:
    api = _api()
    await api.async_arm("h1", ignore_problems=False)
    body = api._request_no_response.await_args[0][2]
    assert body == {"command": "ARM", "ignoreProblems": False}


@pytest.mark.asyncio
async def test_async_disarm_requires_user_id() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_disarm("h1")


@pytest.mark.asyncio
async def test_async_disarm_payload() -> None:
    api = _api()
    await api.async_disarm("h1", ignore_problems=False)
    method, endpoint, body = api._request_no_response.await_args[0]
    assert method == "PUT"
    assert endpoint == "user/USER123/hubs/h1/commands/arming"
    assert body == {"command": "DISARM", "ignoreProblems": False}


@pytest.mark.asyncio
async def test_async_night_mode_requires_user_id() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_night_mode("h1")


@pytest.mark.asyncio
async def test_async_night_mode_on_payload() -> None:
    api = _api()
    await api.async_night_mode("h1", enabled=True)
    body = api._request_no_response.await_args[0][2]
    assert body == {"command": "NIGHT_MODE_ON", "ignoreProblems": True}


@pytest.mark.asyncio
async def test_async_night_mode_off_payload() -> None:
    api = _api()
    await api.async_night_mode("h1", enabled=False)
    body = api._request_no_response.await_args[0][2]
    assert body == {"command": "NIGHT_MODE_OFF", "ignoreProblems": True}


@pytest.mark.asyncio
async def test_async_press_panic_button_requires_user_id() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_press_panic_button("h1")


@pytest.mark.asyncio
async def test_async_press_panic_button_payload() -> None:
    api = _api()
    await api.async_press_panic_button("h1")
    method, endpoint, body = api._request_no_response.await_args[0]
    assert method == "PUT"
    assert endpoint == "user/USER123/hubs/h1/commands/arming"
    assert body == {"command": "PANIC"}


# ---------------------------------------------------------------------------
# Group commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_get_groups_requires_user_id() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_get_groups("h1")


@pytest.mark.asyncio
async def test_async_get_groups_routes() -> None:
    api = _api()
    api._request.return_value = [{"id": "g1"}]
    result = await api.async_get_groups("h1")
    api._request.assert_awaited_once_with("GET", "user/USER123/hubs/h1/groups")
    assert result == [{"id": "g1"}]


@pytest.mark.asyncio
async def test_async_arm_group_requires_user_id() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_arm_group("h1", "g1")


@pytest.mark.asyncio
async def test_async_arm_group_payload() -> None:
    api = _api()
    await api.async_arm_group("h1", "g1", ignore_problems=False)
    method, endpoint, body = api._request_no_response.await_args[0]
    assert method == "PUT"
    assert endpoint == "user/USER123/hubs/h1/groups/g1/commands/arming"
    assert body == {"command": "ARM", "ignoreProblems": False}


@pytest.mark.asyncio
async def test_async_disarm_group_requires_user_id() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_disarm_group("h1", "g1")


@pytest.mark.asyncio
async def test_async_disarm_group_payload() -> None:
    api = _api()
    await api.async_disarm_group("h1", "g1")
    method, endpoint, body = api._request_no_response.await_args[0]
    assert method == "PUT"
    assert endpoint == "user/USER123/hubs/h1/groups/g1/commands/arming"
    assert body == {"command": "DISARM", "ignoreProblems": True}


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_get_events_default_limit() -> None:
    api = _api()
    api._request.return_value = []
    await api.async_get_events("h1")
    api._request.assert_awaited_once_with("GET", "hubs/h1/events?limit=100")


@pytest.mark.asyncio
async def test_async_get_events_custom_limit() -> None:
    api = _api()
    api._request.return_value = []
    await api.async_get_events("h1", limit=25)
    api._request.assert_awaited_once_with("GET", "hubs/h1/events?limit=25")
