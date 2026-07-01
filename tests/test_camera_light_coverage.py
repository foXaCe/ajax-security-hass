"""Tests for the camera and light platforms.

Both platforms expose CoordinatorEntity subclasses. We build them with
``object.__new__`` to avoid a full HA fixture and stub the coordinator /
API / FFmpeg helpers, keeping the tests lightweight (no HA harness).

- AjaxVideoEdgeCamera: stream_source, async_camera_image (cache + FFmpeg),
  available, device_info, unique_id, extra_state_attributes.
- AjaxDimmerLight: is_on, brightness, available, device_info, and the
  optimistic turn_on/turn_off paths with rollback on API error.
"""

from __future__ import annotations

import asyncio
import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

# The HA camera component imports the optional ``turbojpeg`` C extension at
# module load. It is not installed in this test environment, so we provide a
# minimal stub before importing the camera platform under test.
if "turbojpeg" not in sys.modules:
    _turbojpeg_stub = ModuleType("turbojpeg")
    _turbojpeg_stub.TurboJPEG = MagicMock()
    sys.modules["turbojpeg"] = _turbojpeg_stub

# camera.py imports homeassistant.components.ffmpeg, which pulls in the optional
# ``ha-ffmpeg`` (haffmpeg) wheel — not installed in CI. Stub the exact surface
# HA's ffmpeg component imports before loading the camera platform under test.
# ``HAFFmpeg`` must be a real class: HA uses it as a PEP 695 TypeVar bound
# (``class FFmpegBase[_HAFFmpegT: HAFFmpeg]``), which a MagicMock can't satisfy.
if "haffmpeg" not in sys.modules:
    _haffmpeg = ModuleType("haffmpeg")
    _haffmpeg_core = ModuleType("haffmpeg.core")
    _haffmpeg_core.HAFFmpeg = type("HAFFmpeg", (), {})
    _haffmpeg_tools = ModuleType("haffmpeg.tools")
    _haffmpeg_tools.IMAGE_JPEG = "image/jpeg"
    _haffmpeg_tools.FFVersion = type("FFVersion", (), {})
    _haffmpeg_tools.ImageFrame = type("ImageFrame", (), {})
    sys.modules["haffmpeg"] = _haffmpeg
    sys.modules["haffmpeg.core"] = _haffmpeg_core
    sys.modules["haffmpeg.tools"] = _haffmpeg_tools

from custom_components.ajax import camera as camera_module, light as light_module
from custom_components.ajax.camera import AjaxVideoEdgeCamera, async_setup_entry as camera_setup_entry
from custom_components.ajax.const import CONF_RTSP_PASSWORD, CONF_RTSP_USERNAME
from custom_components.ajax.light import AjaxDimmerLight, async_setup_entry as light_setup_entry
from custom_components.ajax.models import (
    AjaxAccount,
    AjaxDevice,
    AjaxSpace,
    AjaxVideoEdge,
    DeviceType,
    VideoEdgeType,
)

# ---------------------------------------------------------------------------
# Camera helpers
# ---------------------------------------------------------------------------


def _video_edge(
    *,
    ve_id: str = "ve1",
    ip: str | None = "10.0.0.5",
    mac: str | None = "9C:75:6E:2A:E2:2D",
    state: str = "ONLINE",
    ve_type: VideoEdgeType = VideoEdgeType.TURRET,
    channels: list | None = None,
    color: str | None = "white",
) -> AjaxVideoEdge:
    return AjaxVideoEdge(
        id=ve_id,
        name="Front Door Cam",
        space_id="s1",
        video_edge_type=ve_type,
        color=color,
        ip_address=ip,
        mac_address=mac,
        firmware_version="1.2.3",
        connection_state=state,
        channels=channels or [],
    )


def _camera(
    *,
    video_edge: AjaxVideoEdge | None = None,
    channel_id: str | None = None,
    channel_index: int | None = None,
    stream_type: str = "main",
    rtsp_user: str = "admin",
    rtsp_pass: str = "secret",
    in_space: bool = True,
) -> AjaxVideoEdgeCamera:
    """Build a camera with bypassed __init__."""
    if video_edge is None:
        video_edge = _video_edge()

    cam = object.__new__(AjaxVideoEdgeCamera)
    cam._video_edge_id = video_edge.id
    cam._space_id = "s1"
    cam._stream_type = stream_type
    cam._channel_index = channel_index if channel_index is not None else 0
    cam._channel_id = channel_id
    cam._model_name = "TurretCam"
    cam._color = (video_edge.color or "").title()
    cam._snapshot_cache = None
    cam._snapshot_cache_time = 0.0
    cam._snapshot_lock = asyncio.Lock()
    cam._attr_unique_id = f"entry_test_{video_edge.id}_camera_{stream_type}"

    options = {}
    if rtsp_user:
        options[CONF_RTSP_USERNAME] = rtsp_user
    if rtsp_pass:
        options[CONF_RTSP_PASSWORD] = rtsp_pass
    cam._entry = SimpleNamespace(options=options)

    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    if in_space:
        space.video_edges[video_edge.id] = video_edge
    cam.coordinator = SimpleNamespace(
        last_update_success=True,
        entry_id="entry_test",
        data=SimpleNamespace(spaces={"s1": space}),
        get_space=lambda sid: space if sid == "s1" else None,
    )
    return cam


# ---------------------------------------------------------------------------
# Camera: _video_edge / available
# ---------------------------------------------------------------------------


def test_video_edge_property_returns_device() -> None:
    ve = _video_edge()
    cam = _camera(video_edge=ve)
    assert cam._video_edge is ve


def test_video_edge_property_none_when_space_missing() -> None:
    cam = _camera()
    cam.coordinator = SimpleNamespace(last_update_success=True, data=SimpleNamespace(spaces={}))
    assert cam._video_edge is None


def test_video_edge_property_none_when_device_missing() -> None:
    cam = _camera(in_space=False)
    assert cam._video_edge is None


def test_available_true_when_online() -> None:
    assert _camera(video_edge=_video_edge(state="ONLINE")).available is True


def test_available_false_when_offline() -> None:
    assert _camera(video_edge=_video_edge(state="OFFLINE")).available is False


def test_available_false_when_missing() -> None:
    assert _camera(in_space=False).available is False


# ---------------------------------------------------------------------------
# Camera: device_info
# ---------------------------------------------------------------------------


def test_device_info_none_when_missing() -> None:
    assert _camera(in_space=False).device_info is None


def test_device_info_standalone_links_to_hub() -> None:
    ve = _video_edge()
    cam = _camera(video_edge=ve)
    info = cam.device_info
    assert info is not None
    assert info["identifiers"] == {("ajax", "entry_test_ve1")}
    assert info["name"] == "Front Door Cam"
    assert info["model"] == "TurretCam (White)"
    assert info["via_device"] == ("ajax", "entry_test_s1")
    assert info["sw_version"] == "1.2.3"


def test_device_info_without_color() -> None:
    ve = _video_edge(color=None)
    cam = _camera(video_edge=ve)
    cam._color = ""
    assert cam.device_info["model"] == "TurretCam"


def test_device_info_links_to_recording_nvr() -> None:
    ve = _video_edge()
    cam = _camera(video_edge=ve)
    cam._get_recording_nvr_id = lambda: "nvr-99"
    assert cam.device_info["via_device"] == ("ajax", "entry_test_nvr-99")


# ---------------------------------------------------------------------------
# Camera: _get_recording_nvr_id
# ---------------------------------------------------------------------------


def test_get_recording_nvr_id_none_when_data_none() -> None:
    cam = _camera()
    cam.coordinator = SimpleNamespace(last_update_success=True, data=None)
    assert cam._get_recording_nvr_id() is None


def test_get_recording_nvr_id_none_when_space_missing() -> None:
    cam = _camera()
    cam.coordinator = SimpleNamespace(last_update_success=True, data=SimpleNamespace(spaces={}))
    assert cam._get_recording_nvr_id() is None


def test_get_recording_nvr_id_delegates_to_space() -> None:
    ve = _video_edge()
    cam = _camera(video_edge=ve)
    space = cam.coordinator.data.spaces["s1"]
    nvr = _video_edge(
        ve_id="nvr1",
        ve_type=VideoEdgeType.NVR,
        channels=[
            {
                "sourceAliases": {
                    "sources": [{"sourceType": "PRIMARY", "videoEdgeId": "ve1"}],
                },
            }
        ],
    )
    space.video_edges["nvr1"] = nvr
    assert cam._get_recording_nvr_id() == "nvr1"


# ---------------------------------------------------------------------------
# Camera: extra_state_attributes
# ---------------------------------------------------------------------------


def test_extra_state_attributes_none_when_credentials_set() -> None:
    cam = _camera(rtsp_user="admin", rtsp_pass="secret")
    assert cam.extra_state_attributes is None


def test_extra_state_attributes_help_when_credentials_missing() -> None:
    cam = _camera(rtsp_user="", rtsp_pass="")
    attrs = cam.extra_state_attributes
    assert attrs is not None
    assert "configuration_help" in attrs


# ---------------------------------------------------------------------------
# Camera: _build_rtsp_url / stream_source
# ---------------------------------------------------------------------------


def test_build_rtsp_url_none_when_no_video_edge() -> None:
    cam = _camera(in_space=False)
    assert cam._build_rtsp_url() is None


def test_build_rtsp_url_none_when_no_ip() -> None:
    cam = _camera(video_edge=_video_edge(ip=None))
    assert cam._build_rtsp_url() is None


def test_build_rtsp_url_single_camera_main_with_credentials() -> None:
    cam = _camera(video_edge=_video_edge(mac="9C:75:6E:2A:E2:2D"))
    url = cam._build_rtsp_url("main")
    # MAC lowercased without colons, channel 0, main suffix 'm', creds embedded
    assert url == "rtsp://admin:secret@10.0.0.5:8554/9c756e2ae22d-0_m"


def test_build_rtsp_url_sub_suffix() -> None:
    cam = _camera(video_edge=_video_edge())
    url = cam._build_rtsp_url("sub")
    assert url.endswith("_s")


def test_build_rtsp_url_defaults_to_self_stream_type() -> None:
    cam = _camera(video_edge=_video_edge(), stream_type="sub")
    assert cam._build_rtsp_url().endswith("_s")


def test_build_rtsp_url_without_credentials() -> None:
    cam = _camera(video_edge=_video_edge(), rtsp_user="", rtsp_pass="")
    url = cam._build_rtsp_url("main")
    assert url == "rtsp://10.0.0.5:8554/9c756e2ae22d-0_m"
    assert "@" not in url


def test_build_rtsp_url_encodes_special_chars() -> None:
    cam = _camera(video_edge=_video_edge(), rtsp_user="user@x", rtsp_pass="p@ss/word")
    url = cam._build_rtsp_url("main")
    assert "user%40x" in url
    assert "p%40ss%2Fword" in url


def test_build_rtsp_url_nvr_channel_uses_channel_id() -> None:
    cam = _camera(
        video_edge=_video_edge(ve_type=VideoEdgeType.NVR),
        channel_id="mhzE3YtuK8-9c756e2ae22d-0",
    )
    url = cam._build_rtsp_url("main")
    assert url == "rtsp://admin:secret@10.0.0.5:8554/mhzE3YtuK8-9c756e2ae22d-0_m"


def test_build_rtsp_url_none_when_no_mac() -> None:
    cam = _camera(video_edge=_video_edge(mac=None))
    assert cam._build_rtsp_url("main") is None


def test_build_rtsp_url_none_when_invalid_mac() -> None:
    cam = _camera(video_edge=_video_edge(mac="ZZZZ"))
    assert cam._build_rtsp_url("main") is None


@pytest.mark.asyncio
async def test_stream_source_returns_main_url() -> None:
    cam = _camera(video_edge=_video_edge(), stream_type="main")
    assert await cam.stream_source() == "rtsp://admin:secret@10.0.0.5:8554/9c756e2ae22d-0_m"


# ---------------------------------------------------------------------------
# Camera: unique_id
# ---------------------------------------------------------------------------


def test_unique_id() -> None:
    cam = _camera(video_edge=_video_edge(ve_id="abc"), stream_type="main")
    assert cam.unique_id == "entry_test_abc_camera_main"


# ---------------------------------------------------------------------------
# Camera: async_camera_image
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_camera_image_returns_fresh_cache() -> None:
    cam = _camera(video_edge=_video_edge())
    cam._snapshot_cache = b"cached-jpeg"
    cam._snapshot_cache_time = time.time()
    # Cache is still fresh -> FFmpeg should not be invoked
    assert await cam.async_camera_image() == b"cached-jpeg"


@pytest.mark.asyncio
async def test_async_camera_image_returns_old_cache_when_no_url() -> None:
    cam = _camera(video_edge=_video_edge(ip=None))
    cam._snapshot_cache = b"old-jpeg"
    cam._snapshot_cache_time = 0.0  # expired
    assert await cam.async_camera_image() == b"old-jpeg"


@pytest.mark.asyncio
async def test_async_camera_image_invokes_ffmpeg_and_caches() -> None:
    cam = _camera(video_edge=_video_edge())
    cam.hass = MagicMock()

    process = MagicMock()
    process.returncode = 0
    process.communicate = AsyncMock(return_value=(b"jpeg-bytes", b""))

    with (
        patch(
            "custom_components.ajax.camera.get_ffmpeg_manager",
            return_value=SimpleNamespace(binary="/usr/bin/ffmpeg"),
        ),
        patch(
            "custom_components.ajax.camera.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
    ):
        result = await cam.async_camera_image()

    assert result == b"jpeg-bytes"
    assert cam._snapshot_cache == b"jpeg-bytes"


@pytest.mark.asyncio
async def test_async_camera_image_timeout_returns_old_cache() -> None:
    cam = _camera(video_edge=_video_edge())
    cam.hass = MagicMock()
    cam._snapshot_cache = b"stale"

    with (
        patch(
            "custom_components.ajax.camera.get_ffmpeg_manager",
            return_value=SimpleNamespace(binary="/usr/bin/ffmpeg"),
        ),
        patch(
            "custom_components.ajax.camera.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=TimeoutError()),
        ),
    ):
        assert await cam.async_camera_image() == b"stale"


@pytest.mark.asyncio
async def test_async_camera_image_exception_scrubs_and_kills_process() -> None:
    cam = _camera(video_edge=_video_edge())
    cam.hass = MagicMock()

    process = MagicMock()
    process.returncode = None  # still running -> finally must kill it
    process.communicate = AsyncMock(side_effect=RuntimeError("boom"))
    process.kill = MagicMock()
    process.wait = AsyncMock()

    with (
        patch(
            "custom_components.ajax.camera.get_ffmpeg_manager",
            return_value=SimpleNamespace(binary="/usr/bin/ffmpeg"),
        ),
        patch(
            "custom_components.ajax.camera.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
    ):
        result = await cam.async_camera_image()

    assert result is None  # no cache, error -> returns None
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_camera_image_empty_stdout_returns_cache() -> None:
    cam = _camera(video_edge=_video_edge())
    cam.hass = MagicMock()
    cam._snapshot_cache = b"prev"

    process = MagicMock()
    process.returncode = 0
    process.communicate = AsyncMock(return_value=(b"", b""))

    with (
        patch(
            "custom_components.ajax.camera.get_ffmpeg_manager",
            return_value=SimpleNamespace(binary="/usr/bin/ffmpeg"),
        ),
        patch(
            "custom_components.ajax.camera.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
    ):
        assert await cam.async_camera_image() == b"prev"


# ---------------------------------------------------------------------------
# Light helpers
# ---------------------------------------------------------------------------


def _device(attributes: dict | None = None, *, online: bool = True) -> AjaxDevice:
    return AjaxDevice(
        id="d1",
        name="Dimmer Lounge",
        type=DeviceType.WALLSWITCH,
        space_id="s1",
        hub_id="hub1",
        raw_type="LightSwitchDimmer",
        firmware_version="2.0.0",
        online=online,
        attributes=attributes if attributes is not None else {},
    )


def _light(
    device: AjaxDevice | None,
    *,
    hub_id: str | None = "hub1",
    update_success: bool = True,
    api_error: Exception | None = None,
) -> AjaxDimmerLight:
    light = object.__new__(AjaxDimmerLight)
    light._space_id = "s1"
    light._device_id = "d1"
    light._attr_unique_id = "entry_test_d1_light"

    space = AjaxSpace(id="s1", name="Home", hub_id=hub_id)
    if device is not None:
        space.devices["d1"] = device

    api = SimpleNamespace(
        async_set_dimmer_brightness=AsyncMock(side_effect=api_error),
    )
    light.coordinator = SimpleNamespace(
        entry_id="entry_test",
        get_space=lambda sid: space if sid == "s1" else None,
        last_update_success=update_success,
        api=api,
        async_request_refresh=AsyncMock(),
    )
    light.async_write_ha_state = lambda: None
    return light


# ---------------------------------------------------------------------------
# Light: _get_device / device_info / available
# ---------------------------------------------------------------------------


def test_light_get_device_none_when_space_missing() -> None:
    light = _light(_device())
    light.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: None)
    assert light._get_device() is None


def test_light_device_info_none_when_missing() -> None:
    assert _light(None).device_info is None


def test_light_device_info() -> None:
    info = _light(_device()).device_info
    assert info is not None
    assert info["identifiers"] == {("ajax", "entry_test_d1")}
    assert info["name"] == "Dimmer Lounge"
    assert info["model"] == "LightSwitchDimmer"
    assert info["sw_version"] == "2.0.0"
    assert info["via_device"] == ("ajax", "entry_test_s1")


def test_light_device_info_default_model_when_no_raw_type() -> None:
    device = _device()
    device.raw_type = None
    assert _light(device).device_info["model"] == "LightSwitch Dimmer"


def test_light_available_true() -> None:
    assert _light(_device(online=True)).available is True


def test_light_available_false_when_offline() -> None:
    assert _light(_device(online=False)).available is False


def test_light_available_false_when_missing() -> None:
    assert _light(None).available is False


def test_light_available_false_when_update_failed() -> None:
    assert _light(_device(), update_success=False).available is False


# ---------------------------------------------------------------------------
# Light: is_on / brightness
# ---------------------------------------------------------------------------


def test_light_is_on_true() -> None:
    assert _light(_device({"channelStatuses": ["CHANNEL_1_ON"]})).is_on is True


def test_light_is_on_false() -> None:
    assert _light(_device({"channelStatuses": []})).is_on is False


def test_light_is_on_false_when_missing() -> None:
    assert _light(None).is_on is False


def test_light_brightness_converts_percent_to_255() -> None:
    # 100% -> 255, 50% -> 128 (round)
    assert _light(_device({"actualBrightnessCh1": 100})).brightness == 255
    assert _light(_device({"actualBrightnessCh1": 50})).brightness == 128


def test_light_brightness_none_when_missing_device() -> None:
    assert _light(None).brightness is None


def test_light_brightness_none_when_attr_missing() -> None:
    assert _light(_device({})).brightness is None


def test_light_brightness_none_when_attr_not_numeric() -> None:
    assert _light(_device({"actualBrightnessCh1": "bad"})).brightness is None


# ---------------------------------------------------------------------------
# Light: async_turn_on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_on_with_explicit_brightness() -> None:
    from homeassistant.components.light import ATTR_BRIGHTNESS

    device = _device({})
    light = _light(device)
    await light.async_turn_on(**{ATTR_BRIGHTNESS: 255})
    # 255 -> 100%
    assert device.attributes["actualBrightnessCh1"] == 100
    assert device.attributes["channelStatuses"] == ["CHANNEL_1_ON"]
    assert device.is_optimistic("actualBrightnessCh1") is True
    light.coordinator.api.async_set_dimmer_brightness.assert_awaited_once_with(
        hub_id="hub1", device_id="d1", brightness=100
    )


@pytest.mark.asyncio
async def test_turn_on_uses_current_brightness_when_unset() -> None:
    device = _device({"actualBrightnessCh1": 42})
    light = _light(device)
    await light.async_turn_on()
    light.coordinator.api.async_set_dimmer_brightness.assert_awaited_once_with(
        hub_id="hub1", device_id="d1", brightness=42
    )


@pytest.mark.asyncio
async def test_turn_on_defaults_to_100_when_no_current() -> None:
    device = _device({})
    light = _light(device)
    await light.async_turn_on()
    light.coordinator.api.async_set_dimmer_brightness.assert_awaited_once_with(
        hub_id="hub1", device_id="d1", brightness=100
    )


@pytest.mark.asyncio
async def test_turn_on_raises_when_device_missing() -> None:
    light = _light(None)
    with pytest.raises(HomeAssistantError):
        await light.async_turn_on()


@pytest.mark.asyncio
async def test_turn_on_raises_when_hub_missing() -> None:
    light = _light(_device(), hub_id=None)
    with pytest.raises(HomeAssistantError):
        await light.async_turn_on()


@pytest.mark.asyncio
async def test_turn_on_rollback_restores_existing_attrs() -> None:
    device = _device({"actualBrightnessCh1": 30, "channelStatuses": ["CHANNEL_1_ON"]})
    light = _light(device, api_error=RuntimeError("network"))
    with pytest.raises(HomeAssistantError):
        await light.async_turn_on()
    # Old values restored
    assert device.attributes["actualBrightnessCh1"] == 30
    assert device.attributes["channelStatuses"] == ["CHANNEL_1_ON"]
    # Optimistic guards cleared
    assert "_optimistic_until" not in device.attributes
    assert device.is_optimistic("actualBrightnessCh1") is False
    light.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_turn_on_rollback_pops_unset_attrs() -> None:
    device = _device({})  # no actualBrightnessCh1, no channelStatuses
    light = _light(device, api_error=RuntimeError("network"))
    with pytest.raises(HomeAssistantError):
        await light.async_turn_on()
    assert "actualBrightnessCh1" not in device.attributes
    assert "channelStatuses" not in device.attributes


# ---------------------------------------------------------------------------
# Light: async_turn_off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_off_sets_zero_and_calls_api() -> None:
    device = _device({"actualBrightnessCh1": 80, "channelStatuses": ["CHANNEL_1_ON"]})
    light = _light(device)
    await light.async_turn_off()
    assert device.attributes["actualBrightnessCh1"] == 0
    assert device.attributes["channelStatuses"] == []
    light.coordinator.api.async_set_dimmer_brightness.assert_awaited_once_with(
        hub_id="hub1", device_id="d1", brightness=0
    )


@pytest.mark.asyncio
async def test_turn_off_raises_when_device_missing() -> None:
    light = _light(None)
    with pytest.raises(HomeAssistantError):
        await light.async_turn_off()


@pytest.mark.asyncio
async def test_turn_off_raises_when_hub_missing() -> None:
    light = _light(_device(), hub_id=None)
    with pytest.raises(HomeAssistantError):
        await light.async_turn_off()


@pytest.mark.asyncio
async def test_turn_off_rollback_restores_existing_attrs() -> None:
    device = _device({"actualBrightnessCh1": 70, "channelStatuses": ["CHANNEL_1_ON"]})
    light = _light(device, api_error=RuntimeError("network"))
    with pytest.raises(HomeAssistantError):
        await light.async_turn_off()
    assert device.attributes["actualBrightnessCh1"] == 70
    assert device.attributes["channelStatuses"] == ["CHANNEL_1_ON"]
    assert "_optimistic_until" not in device.attributes
    light.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_turn_off_rollback_pops_unset_attrs() -> None:
    device = _device({})
    light = _light(device, api_error=RuntimeError("network"))
    with pytest.raises(HomeAssistantError):
        await light.async_turn_off()
    assert "actualBrightnessCh1" not in device.attributes
    assert "channelStatuses" not in device.attributes


# ---------------------------------------------------------------------------
# Camera: async_setup_entry
# ---------------------------------------------------------------------------


def _setup_coordinator(spaces: dict[str, AjaxSpace], *, account: bool = False) -> SimpleNamespace:
    coord = SimpleNamespace(
        entry_id="entry_test",
        data=SimpleNamespace(spaces=spaces),
        get_space=lambda sid: spaces.get(sid),
    )
    if account:
        coord.account = AjaxAccount(user_id="u", name="n", email="e", spaces=spaces)
    return coord


@pytest.mark.asyncio
async def test_camera_setup_creates_main_and_sub_for_single_camera() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    space.video_edges["ve1"] = _video_edge(ve_type=VideoEdgeType.TURRET)
    coord = _setup_coordinator({"s1": space})
    entry = SimpleNamespace(runtime_data=coord, options={})
    added: list = []

    with patch.object(camera_module, "connect_new_entity_signal"):
        await camera_setup_entry(MagicMock(), entry, lambda ents: added.extend(ents))

    # main + sub for a single camera
    assert len(added) == 2
    assert {e.unique_id for e in added} == {"entry_test_ve1_camera_main", "entry_test_ve1_camera_sub"}


@pytest.mark.asyncio
async def test_camera_setup_creates_channels_for_nvr() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    space.video_edges["nvr1"] = _video_edge(
        ve_id="nvr1",
        ve_type=VideoEdgeType.NVR,
        channels=[{"name": "Cam A", "id": "chid-1"}, {"name": "Cam B", "id": "chid-2"}],
    )
    coord = _setup_coordinator({"s1": space})
    entry = SimpleNamespace(runtime_data=coord, options={})
    added: list = []

    with patch.object(camera_module, "connect_new_entity_signal"):
        await camera_setup_entry(MagicMock(), entry, lambda ents: added.extend(ents))

    # 2 channels x (main + sub)
    assert len(added) == 4


@pytest.mark.asyncio
async def test_camera_setup_skips_video_edge_without_ip() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    space.video_edges["ve1"] = _video_edge(ip=None)
    coord = _setup_coordinator({"s1": space})
    entry = SimpleNamespace(runtime_data=coord, options={})
    add_cb = MagicMock()

    with patch.object(camera_module, "connect_new_entity_signal"):
        await camera_setup_entry(MagicMock(), entry, add_cb)

    add_cb.assert_not_called()


@pytest.mark.asyncio
async def test_camera_build_callback_creates_entities() -> None:
    """Exercise the _build_cameras discovery callback."""
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    space.video_edges["ve1"] = _video_edge()
    coord = _setup_coordinator({"s1": space})
    entry = SimpleNamespace(runtime_data=coord, options={})
    captured: dict = {}

    def _capture(hass, entry, signal, domain, add, builder, *, label):  # noqa: ANN001
        captured["builder"] = builder

    with patch.object(camera_module, "connect_new_entity_signal", _capture):
        await camera_setup_entry(MagicMock(), entry, MagicMock())

    builder = captured["builder"]
    pairs = builder("s1", "ve1")
    assert {uid for uid, _ in pairs} == {"entry_test_ve1_camera_main", "entry_test_ve1_camera_sub"}
    # Unknown space / video edge -> empty
    assert builder("nope", "ve1") == []
    assert builder("s1", "nope") == []


@pytest.mark.asyncio
async def test_camera_build_callback_nvr_channels() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    space.video_edges["nvr1"] = _video_edge(
        ve_id="nvr1",
        ve_type=VideoEdgeType.NVR,
        channels=[{"name": "Cam A", "id": "chid-1"}],
    )
    coord = _setup_coordinator({"s1": space})
    entry = SimpleNamespace(runtime_data=coord, options={})
    captured: dict = {}

    def _capture(hass, entry, signal, domain, add, builder, *, label):  # noqa: ANN001
        captured["builder"] = builder

    with patch.object(camera_module, "connect_new_entity_signal", _capture):
        await camera_setup_entry(MagicMock(), entry, MagicMock())

    pairs = captured["builder"]("s1", "nvr1")
    # one channel x (main + sub)
    assert len(pairs) == 2


@pytest.mark.asyncio
async def test_camera_setup_nvr_channel_without_name_uses_translation() -> None:
    """An NVR channel without a 'name' falls back to translation keys."""
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    space.video_edges["nvr1"] = _video_edge(
        ve_id="nvr1",
        ve_type=VideoEdgeType.NVR,
        channels=[{"id": "chid-1"}],  # no "name"
    )
    coord = _setup_coordinator({"s1": space})
    entry = SimpleNamespace(runtime_data=coord, options={})
    added: list = []

    with patch.object(camera_module, "connect_new_entity_signal"):
        await camera_setup_entry(MagicMock(), entry, lambda ents: added.extend(ents))

    assert len(added) == 2  # main + sub
    by_uid = {e.unique_id: e for e in added}
    main = by_uid["entry_test_nvr1_camera_ch0_main"]
    sub = by_uid["entry_test_nvr1_camera_ch0_sub"]
    # Fallback uses translation key + placeholder, no static name
    assert main._attr_translation_key == "nvr_channel"
    assert main._attr_name is None
    assert main._attr_translation_placeholders == {"number": "1"}
    assert sub._attr_translation_key == "nvr_channel_sub"
    assert sub._attr_entity_registry_enabled_default is False


@pytest.mark.asyncio
async def test_camera_setup_nvr_channel_with_name_sets_static_name() -> None:
    """An NVR channel with a 'name' uses it directly (main + ' Sub')."""
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    space.video_edges["nvr1"] = _video_edge(
        ve_id="nvr1",
        ve_type=VideoEdgeType.NVR,
        channels=[{"id": "chid-1", "name": "Garden"}],
    )
    coord = _setup_coordinator({"s1": space})
    entry = SimpleNamespace(runtime_data=coord, options={})
    added: list = []

    with patch.object(camera_module, "connect_new_entity_signal"):
        await camera_setup_entry(MagicMock(), entry, lambda ents: added.extend(ents))

    by_uid = {e.unique_id: e for e in added}
    assert by_uid["entry_test_nvr1_camera_ch0_main"]._attr_name == "Garden"
    assert by_uid["entry_test_nvr1_camera_ch0_sub"]._attr_name == "Garden Sub"


# ---------------------------------------------------------------------------
# Light: async_setup_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_light_setup_returns_early_without_account() -> None:
    coord = SimpleNamespace(account=None)
    entry = SimpleNamespace(runtime_data=coord)
    add_cb = MagicMock()
    # Must not touch connect_new_entity_signal nor add entities
    await light_setup_entry(MagicMock(), entry, add_cb)
    add_cb.assert_not_called()


@pytest.mark.asyncio
async def test_light_setup_creates_dimmer_entity() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    space.devices["d1"] = _device()  # WALLSWITCH + LightSwitchDimmer raw_type
    coord = _setup_coordinator({"s1": space}, account=True)
    entry = SimpleNamespace(runtime_data=coord)
    added: list = []

    with patch.object(light_module, "connect_new_entity_signal"):
        await light_setup_entry(MagicMock(), entry, lambda ents: added.extend(ents))

    assert len(added) == 1
    assert added[0].unique_id == "entry_test_d1_light"


@pytest.mark.asyncio
async def test_light_setup_skips_non_dimmer_device() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    non_dimmer = _device()
    non_dimmer.type = DeviceType.RELAY
    non_dimmer.raw_type = "relay"
    space.devices["d1"] = non_dimmer
    coord = _setup_coordinator({"s1": space}, account=True)
    entry = SimpleNamespace(runtime_data=coord)
    add_cb = MagicMock()

    with patch.object(light_module, "connect_new_entity_signal"):
        await light_setup_entry(MagicMock(), entry, add_cb)

    add_cb.assert_not_called()


@pytest.mark.asyncio
async def test_light_build_callback() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1")
    space.devices["d1"] = _device()
    coord = _setup_coordinator({"s1": space}, account=True)
    entry = SimpleNamespace(runtime_data=coord)
    captured: dict = {}

    def _capture(hass, entry, signal, domain, add, builder, *, label):  # noqa: ANN001
        captured["builder"] = builder

    with patch.object(light_module, "connect_new_entity_signal", _capture):
        await light_setup_entry(MagicMock(), entry, MagicMock())

    builder = captured["builder"]
    pairs = builder("s1", "d1")
    assert len(pairs) == 1
    # NOTE: the light builder returns the raw (non-namespaced) registry key
    # ``f"{device_id}_light"``, whereas the entity's own ``unique_id`` is
    # namespaced (``entry_test_d1_light``). This reflects the current source.
    assert pairs[0][0] == "d1_light"
    assert pairs[0][1].unique_id == "entry_test_d1_light"
    # Unknown space / device -> empty
    assert builder("nope", "d1") == []
    assert builder("s1", "nope") == []
