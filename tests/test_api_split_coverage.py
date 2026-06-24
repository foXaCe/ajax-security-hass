"""Coverage tests for the split AjaxRestApi package.

Targets the error-path / cache / snapshot branches in api/_cameras.py,
api/_devices.py, api/_hubs.py and api/_video.py that the api.py → api/ split
isolated into their own files. Mirrors the style of
tests/test_api_devices_coverage.py (real ``AjaxRestApi`` with ``_request`` /
``_request_no_response`` mocked; assert URL routing and payload shapes).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.ajax.api import (
    AjaxRestApi,
    AjaxRestApiError,
    AjaxRestConnectionError,
)


def _api(user_id: str | None = "USER123") -> AjaxRestApi:
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    api.user_id = user_id
    api._request = AsyncMock()  # type: ignore[method-assign]
    api._request_no_response = AsyncMock()  # type: ignore[method-assign]
    return api


# ---------------------------------------------------------------------------
# _cameras.py
# ---------------------------------------------------------------------------


async def test_get_cameras_routes() -> None:
    api = _api()
    api._request.return_value = [{"id": "cam1"}]
    assert await api.async_get_cameras("h1") == [{"id": "cam1"}]
    api._request.assert_awaited_once_with("GET", "user/USER123/hubs/h1/cameras")


async def test_get_cameras_no_user_id_raises() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_get_cameras("h1")
    api._request.assert_not_awaited()


async def test_get_camera_routes() -> None:
    api = _api()
    api._request.return_value = {"id": "cam1"}
    assert await api.async_get_camera("h1", "cam1") == {"id": "cam1"}
    api._request.assert_awaited_once_with("GET", "user/USER123/hubs/h1/cameras/cam1")


async def test_get_camera_no_user_id_raises() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_get_camera("h1", "cam1")


def _snapshot_session(responses: list[tuple[int, bytes]]) -> MagicMock:
    """Mock aiohttp session whose .get() yields an async-CM response per call."""
    session = MagicMock()
    cms = []
    for status, body in responses:
        resp = MagicMock()
        resp.status = status
        resp.read = AsyncMock(return_value=body)
        resp.raise_for_status = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        cms.append(cm)
    session.get = MagicMock(side_effect=cms)
    return session


async def test_get_camera_snapshot_not_logged_in_raises() -> None:
    api = _api()
    api.session_token = None
    with pytest.raises(AjaxRestApiError):
        await api.async_get_camera_snapshot("h1", "cam1")


async def test_get_camera_snapshot_success_returns_bytes() -> None:
    api = _api()
    api.session_token = "tok"
    api._proactive_token_refresh = AsyncMock()  # type: ignore[method-assign]
    api._get_session = AsyncMock(return_value=_snapshot_session([(200, b"JPEG")]))  # type: ignore[method-assign]
    assert await api.async_get_camera_snapshot("h1", "cam1") == b"JPEG"


async def test_get_camera_snapshot_401_recovers_and_retries() -> None:
    api = _api()
    api.session_token = "tok"
    api._proactive_token_refresh = AsyncMock()  # type: ignore[method-assign]
    api._recover_auth = AsyncMock()  # type: ignore[method-assign]
    api._get_session = AsyncMock(  # type: ignore[method-assign]
        return_value=_snapshot_session([(401, b""), (200, b"AFTER")])
    )
    assert await api.async_get_camera_snapshot("h1", "cam1") == b"AFTER"
    api._recover_auth.assert_awaited_once()


async def test_get_camera_snapshot_client_error_wrapped() -> None:
    api = _api()
    api.session_token = "tok"
    api._proactive_token_refresh = AsyncMock()  # type: ignore[method-assign]
    session = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("boom"))
    cm.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=cm)
    api._get_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
    with pytest.raises(AjaxRestConnectionError):
        await api.async_get_camera_snapshot("h1", "cam1")


async def test_get_camera_stream_url_extracts_url() -> None:
    api = _api()
    api._request.return_value = {"url": "rtsp://x"}
    assert await api.async_get_camera_stream_url("h1", "cam1") == "rtsp://x"
    api._request.assert_awaited_once_with("GET", "user/USER123/hubs/h1/cameras/cam1/stream")


async def test_get_camera_stream_url_missing_returns_empty() -> None:
    api = _api()
    api._request.return_value = {}
    assert await api.async_get_camera_stream_url("h1", "cam1") == ""


# ---------------------------------------------------------------------------
# _devices.py
# ---------------------------------------------------------------------------


async def test_get_devices_no_user_id_raises() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_get_devices("h1")


async def test_get_device_no_user_id_raises() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_get_device("h1", "d1")


async def test_send_device_command_routes_and_payload() -> None:
    api = _api()
    await api.async_send_device_command("h1", "d1", "SWITCH_ON", "Socket")
    api._request_no_response.assert_awaited_once_with(
        "POST",
        "user/USER123/hubs/h1/devices/d1/command",
        {"command": "SWITCH_ON", "deviceType": "Socket"},
    )


async def test_send_device_command_no_user_id_raises() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_send_device_command("h1", "d1", "SWITCH_ON", "Socket")
    api._request_no_response.assert_not_awaited()


async def test_set_switch_state_maps_to_command() -> None:
    api = _api()
    await api.async_set_switch_state("h1", "d1", state=True, device_type="Relay")
    method, endpoint, payload = api._request_no_response.await_args[0]
    assert payload == {"command": "SWITCH_ON", "deviceType": "Relay"}


async def test_set_channel_state_builds_channels_payload() -> None:
    api = _api()
    await api.async_set_channel_state("h1", "d1", 1, state=False, device_type="LightSwitchTwoGang")
    method, endpoint, payload = api._request_no_response.await_args[0]
    assert endpoint == "user/USER123/hubs/h1/devices/d1/command"
    assert payload["command"] == "SWITCH_OFF"
    # 0-based channel 1 → "CHANNEL_2"
    assert payload["additionalParam"]["channels"] == ["CHANNEL_2"]


async def test_set_channel_state_no_user_id_raises() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_set_channel_state("h1", "d1", 0, state=True, device_type="LightSwitchTwoGang")
    api._request_no_response.assert_not_awaited()


async def test_set_waterstop_open_maps_to_switch_on() -> None:
    api = _api()
    await api.async_set_waterstop_state("h1", "d1", open_valve=True)
    method, endpoint, payload = api._request_no_response.await_args[0]
    assert payload == {"command": "SWITCH_ON", "deviceType": "WaterStop"}


# ---------------------------------------------------------------------------
# _hubs.py
# ---------------------------------------------------------------------------


async def test_get_hub_no_user_id_raises() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_get_hub("h1")


async def test_get_space_by_hub_returns_first_binding() -> None:
    api = _api()
    api._request.return_value = [{"id": "sp1"}, {"id": "sp2"}]
    assert await api.async_get_space_by_hub("h1") == {"id": "sp1"}
    api._request.assert_awaited_once_with("GET", "user/USER123/spaces?hubId=h1")


async def test_get_space_by_hub_empty_returns_none() -> None:
    api = _api()
    api._request.return_value = []
    assert await api.async_get_space_by_hub("h1") is None


async def test_get_space_by_hub_no_user_id_raises() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_get_space_by_hub("h1")


async def test_get_rooms_no_user_id_raises() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_get_rooms("h1")


async def test_get_users_no_user_id_raises() -> None:
    api = _api(user_id=None)
    with pytest.raises(AjaxRestApiError):
        await api.async_get_users("h1")


# ---------------------------------------------------------------------------
# _video.py — async_get_space short-lived cache
# ---------------------------------------------------------------------------


async def test_get_space_serves_second_call_from_cache() -> None:
    api = _api()
    api._request.return_value = {"id": "sp1", "devices": []}
    first = await api.async_get_space("sp1")
    second = await api.async_get_space("sp1")  # cache hit — no second network call
    assert first == second == {"id": "sp1", "devices": []}
    api._request.assert_awaited_once()
