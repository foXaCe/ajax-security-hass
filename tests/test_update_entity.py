"""Tests for the Ajax firmware update entities.

Both entities are coordinator-backed property bags: they read the current
video-edge / hub firmware blob out of coordinator.data on every access and
derive installed/latest/in-progress/summary from it. We build them with
``object.__new__`` and a fake coordinator so the property logic is exercised
without a running HA instance.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.ajax.models import AjaxVideoEdge, VideoEdgeType
from custom_components.ajax.update import (
    AjaxHubFirmwareUpdate,
    AjaxVideoEdgeFirmwareUpdate,
    _format_hub_type,
)

# ---------------------------------------------------------------------------
# _format_hub_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subtype,expected",
    [
        (None, "Security Hub"),
        ("", "Security Hub"),
        ("HUB", "Hub"),
        ("HUB_2_PLUS", "Hub 2 Plus"),
        ("hub_hybrid", "Hub Hybrid"),  # lookup upper-cases the key
        ("HUB_4G", "HUB_4G"),  # unknown subtype passes through verbatim
    ],
)
def test_format_hub_type(subtype: str | None, expected: str) -> None:
    assert _format_hub_type(subtype) == expected


# ---------------------------------------------------------------------------
# AjaxVideoEdgeFirmwareUpdate
# ---------------------------------------------------------------------------


def _ve(firmware_version: str = "1.0.0", raw_firmware: dict | None = None) -> AjaxVideoEdge:
    ve = AjaxVideoEdge(
        id="ve1",
        name="Front Cam",
        space_id="s1",
        video_edge_type=VideoEdgeType.BULLET,
        firmware_version=firmware_version,
    )
    ve.raw_data = {"firmware": raw_firmware} if raw_firmware is not None else {}
    return ve


def _ve_entity(video_edge: AjaxVideoEdge | None, *, last_update_success: bool = True) -> AjaxVideoEdgeFirmwareUpdate:
    ent = object.__new__(AjaxVideoEdgeFirmwareUpdate)
    ent._video_edge_id = "ve1"
    ent._space_id = "s1"
    spaces = {"s1": SimpleNamespace(video_edges={"ve1": video_edge} if video_edge else {})}
    ent.coordinator = SimpleNamespace(
        data=SimpleNamespace(spaces=spaces),
        last_update_success=last_update_success,
    )
    return ent


def test_ve_available_requires_update_success_and_presence() -> None:
    assert _ve_entity(_ve()).available is True
    assert _ve_entity(_ve(), last_update_success=False).available is False
    assert _ve_entity(None).available is False


def test_ve_installed_version() -> None:
    assert _ve_entity(_ve("2.5.1")).installed_version == "2.5.1"
    assert _ve_entity(None).installed_version is None


def test_ve_latest_version_no_update_returns_installed() -> None:
    assert _ve_entity(_ve("1.0.0", raw_firmware={})).latest_version == "1.0.0"


def test_ve_latest_version_critical_update() -> None:
    fw = {"criticalUpdateAvailable": True, "updateStatus": {"version": "1.1.0"}}
    assert _ve_entity(_ve("1.0.0", raw_firmware=fw)).latest_version == "1.1.0"


def test_ve_latest_version_critical_but_same_version_falls_back() -> None:
    """A critical flag whose version equals installed must not be offered as 'new'."""
    fw = {"criticalUpdateAvailable": True, "updateStatus": {"version": "1.0.0"}}
    assert _ve_entity(_ve("1.0.0", raw_firmware=fw)).latest_version == "1.0.0"


@pytest.mark.parametrize("state", ["DOWNLOADING", "INSTALLING", "READY"])
def test_ve_latest_version_active_state(state: str) -> None:
    fw = {"updateStatus": {"state": state, "version": "3.0.0"}}
    assert _ve_entity(_ve("1.0.0", raw_firmware=fw)).latest_version == "3.0.0"


def test_ve_latest_version_none_when_missing() -> None:
    assert _ve_entity(None).latest_version is None


@pytest.mark.parametrize(
    "state,expected",
    [("DOWNLOADING", True), ("INSTALLING", True), ("READY", False), ("IDLE", False)],
)
def test_ve_in_progress(state: str, expected: bool) -> None:
    fw = {"updateStatus": {"state": state}}
    assert _ve_entity(_ve(raw_firmware=fw)).in_progress is expected


def test_ve_in_progress_false_when_missing() -> None:
    assert _ve_entity(None).in_progress is False


@pytest.mark.parametrize(
    "fw,expected",
    [
        ({"criticalUpdateAvailable": True}, "Critical security update available"),
        ({"updateStatus": {"state": "DOWNLOADING", "progress": 42}}, "Downloading update... 42%"),
        ({"updateStatus": {"state": "INSTALLING", "progress": 7}}, "Installing update... 7%"),
        ({"updateStatus": {"state": "READY"}}, "Update ready to install"),
        ({"updateStatus": {"state": "IDLE"}}, None),
        ({}, None),
    ],
)
def test_ve_release_summary(fw: dict, expected: str | None) -> None:
    assert _ve_entity(_ve(raw_firmware=fw)).release_summary == expected


def test_ve_release_summary_none_when_missing() -> None:
    assert _ve_entity(None).release_summary is None


# ---------------------------------------------------------------------------
# AjaxHubFirmwareUpdate
# ---------------------------------------------------------------------------


def _hub_entity(hub_details: dict | None, *, last_update_success: bool = True) -> AjaxHubFirmwareUpdate:
    ent = object.__new__(AjaxHubFirmwareUpdate)
    ent._space_id = "s1"
    space = SimpleNamespace(hub_details=hub_details) if hub_details is not None else None
    spaces = {"s1": space} if space is not None else {}
    ent.coordinator = SimpleNamespace(
        data=SimpleNamespace(spaces=spaces),
        last_update_success=last_update_success,
    )
    return ent


def test_hub_firmware_info_empty_without_space_or_details() -> None:
    assert _hub_entity(None)._firmware_info == {}
    assert _hub_entity({})._firmware_info == {}


def test_hub_available() -> None:
    assert _hub_entity({"firmware": {"version": "9.0"}}).available is True
    assert _hub_entity({"firmware": {"version": "9.0"}}, last_update_success=False).available is False
    assert _hub_entity({}).available is False  # no firmware info
    assert _hub_entity(None).available is False


def test_hub_installed_version() -> None:
    assert _hub_entity({"firmware": {"version": "9.1"}}).installed_version == "9.1"
    assert _hub_entity({}).installed_version is None


def test_hub_latest_version_new_available() -> None:
    fw = {"firmware": {"version": "9.0", "newVersionAvailable": True, "latestAvailableVersion": "9.2"}}
    assert _hub_entity(fw).latest_version == "9.2"


def test_hub_latest_version_no_update_returns_installed() -> None:
    fw = {"firmware": {"version": "9.0", "newVersionAvailable": False}}
    assert _hub_entity(fw).latest_version == "9.0"


def test_hub_latest_version_none_without_firmware() -> None:
    assert _hub_entity({}).latest_version is None


def test_hub_auto_update() -> None:
    assert _hub_entity({"firmware": {"autoupdateEnabled": True}}).auto_update is True
    assert _hub_entity({"firmware": {}}).auto_update is False


@pytest.mark.parametrize(
    "fw,expected",
    [
        ({"newVersionAvailable": True, "autoupdateEnabled": True}, "Update available (auto-update enabled)"),
        ({"newVersionAvailable": True}, "Update available"),
        ({"newVersionAvailable": False}, None),
        ({}, None),
    ],
)
def test_hub_release_summary(fw: dict, expected: str | None) -> None:
    assert _hub_entity({"firmware": fw}).release_summary == expected


# ---------------------------------------------------------------------------
# __init__ device_info wiring (real constructors)
# ---------------------------------------------------------------------------


def test_ve_init_builds_device_info_with_color() -> None:
    ve = AjaxVideoEdge(
        id="ve9",
        name="Garden",
        space_id="s1",
        video_edge_type=VideoEdgeType.BULLET,
        firmware_version="4.2",
        color="white",
    )
    ent = AjaxVideoEdgeFirmwareUpdate(coordinator=MagicMock(), video_edge=ve, space_id="s1")
    assert ent._attr_unique_id == "ve9_firmware_update"
    assert ent._attr_device_info["identifiers"] == {("ajax", "ve9")}
    assert ent._attr_device_info["sw_version"] == "4.2"
    # color is title-cased and appended to the model name
    assert "(White)" in ent._attr_device_info["model"]


def test_hub_init_builds_device_info_and_space_keyed_unique_id() -> None:
    space = SimpleNamespace(
        id="s1",
        name="Maison",
        hub_details={
            "hubSubtype": "HUB_2_PLUS",
            "color": "black",
            "firmware": {"version": "9.5"},
            "hardwareVersions": {"pcb": "12"},
        },
    )
    ent = AjaxHubFirmwareUpdate(coordinator=MagicMock(), space=space)
    # unique_id is keyed on the stable space id, not hub_id (which may be None at setup)
    assert ent._attr_unique_id == "s1_firmware_update"
    assert ent._attr_device_info["model"] == "Hub 2 Plus (Black)"
    assert ent._attr_device_info["sw_version"] == "9.5"
    assert ent._attr_device_info["hw_version"] == "12"
