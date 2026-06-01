"""Ajax update platform for firmware updates.

This module creates update entities for:
- Hub (Security Hub)
- Video Edge devices (TurretCam, BulletCam, MiniDome, NVR)
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.update import (
    DOMAIN as UPDATE_DOMAIN,
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from ._discovery import connect_new_entity_signal
from ._ids import device_identifier
from .const import MANUFACTURER, SIGNAL_NEW_VIDEO_EDGE
from .coordinator import AjaxDataCoordinator
from .models import VIDEO_EDGE_MODEL_NAMES, AjaxSpace, AjaxVideoEdge

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1


def _format_hub_type(hub_subtype: str | None) -> str:
    """Format hub subtype to human-readable model name."""
    if not hub_subtype:
        return "Security Hub"
    hub_models = {
        "HUB": "Hub",
        "HUB_PLUS": "Hub Plus",
        "HUB_2": "Hub 2",
        "HUB_2_PLUS": "Hub 2 Plus",
        "HUB_HYBRID": "Hub Hybrid",
    }
    return hub_models.get(hub_subtype.upper(), hub_subtype)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax update entities from a config entry."""
    coordinator = entry.runtime_data

    entities: list[UpdateEntity] = []

    for space in coordinator.data.spaces.values():
        # Create update entity for Hub
        if space.hub_details and space.hub_details.get("firmware"):
            entities.append(
                AjaxHubFirmwareUpdate(
                    coordinator=coordinator,
                    space=space,
                )
            )

        # Create update entities for video edges
        for video_edge in space.video_edges.values():
            entities.append(
                AjaxVideoEdgeFirmwareUpdate(
                    coordinator=coordinator,
                    video_edge=video_edge,
                    space_id=space.id,
                )
            )

    if entities:
        _LOGGER.debug("Adding %d update entities", len(entities))
        async_add_entities(entities)

    def _build_update(space_id: str, video_edge_id: str) -> list[tuple[str, UpdateEntity]]:
        """Build the firmware update entity for a newly-discovered Video Edge."""
        space = coordinator.get_space(space_id)
        video_edge = space.video_edges.get(video_edge_id) if space else None
        if not video_edge:
            return []
        return [
            (
                f"{video_edge_id}_firmware_update",
                AjaxVideoEdgeFirmwareUpdate(coordinator=coordinator, video_edge=video_edge, space_id=space_id),
            )
        ]

    connect_new_entity_signal(
        hass,
        entry,
        SIGNAL_NEW_VIDEO_EDGE,
        UPDATE_DOMAIN,
        async_add_entities,
        _build_update,
        label="firmware update entit(ies)",
    )


class AjaxVideoEdgeFirmwareUpdate(CoordinatorEntity[AjaxDataCoordinator], UpdateEntity):
    """Firmware update entity for Ajax Video Edge devices."""

    __slots__ = ("_video_edge_id", "_space_id")

    _attr_has_entity_name = True
    _attr_translation_key = "video_edge_firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    # No install feature - Ajax handles updates automatically
    _attr_supported_features = UpdateEntityFeature(0)

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        video_edge: AjaxVideoEdge,
        space_id: str,
    ) -> None:
        """Initialize the update entity."""
        super().__init__(coordinator)
        self._video_edge_id = video_edge.id
        self._space_id = space_id
        self._attr_unique_id = f"{self.coordinator.entry_id}_{video_edge.id}_firmware_update"

        # Get human-readable model name
        model_name = VIDEO_EDGE_MODEL_NAMES.get(video_edge.video_edge_type, "Video Edge")
        color = video_edge.color.title() if video_edge.color else ""
        model_display = f"{model_name} ({color})" if color else model_name

        # Device info
        self._attr_device_info = {
            "identifiers": {device_identifier(self.coordinator.entry_id, video_edge.id)},
            "name": video_edge.name,
            "manufacturer": MANUFACTURER,
            "model": model_display,
            "sw_version": video_edge.firmware_version,
        }

    @property
    def _video_edge(self) -> AjaxVideoEdge | None:
        """Get the current video edge from coordinator data."""
        space = self.coordinator.data.spaces.get(self._space_id)
        if not space:
            return None
        return space.video_edges.get(self._video_edge_id)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_update_success and self._video_edge is not None

    @property
    def installed_version(self) -> str | None:
        """Return the current firmware version."""
        video_edge = self._video_edge
        if video_edge:
            return video_edge.firmware_version
        return None

    @property
    def latest_version(self) -> str | None:
        """Return the latest firmware version available.

        If no update is available, return installed_version.
        If an update is available, return the new version.
        """
        video_edge = self._video_edge
        if not video_edge:
            return None

        firmware_info = video_edge.raw_data.get("firmware") or {}
        update_status = firmware_info.get("updateStatus") or {}

        # Check if critical update is available
        if firmware_info.get("criticalUpdateAvailable", False):
            # Return the version from updateStatus if available
            new_version = update_status.get("version")
            if new_version and new_version != video_edge.firmware_version:
                return new_version  # type: ignore[no-any-return]

        # Check updateStatus state
        state = update_status.get("state", "IDLE")
        if state in ("DOWNLOADING", "INSTALLING", "READY"):
            new_version = update_status.get("version")
            if new_version:
                return new_version  # type: ignore[no-any-return]

        # No update available - return installed version
        return video_edge.firmware_version

    @property
    def in_progress(self) -> bool | None:
        """Return True if an update is in progress."""
        video_edge = self._video_edge
        if not video_edge:
            return False

        firmware_info = video_edge.raw_data.get("firmware") or {}
        update_status = firmware_info.get("updateStatus") or {}
        state = update_status.get("state", "IDLE")

        return state in ("DOWNLOADING", "INSTALLING")

    @property
    def release_summary(self) -> str | None:
        """Return a summary of the release."""
        video_edge = self._video_edge
        if not video_edge:
            return None

        firmware_info = video_edge.raw_data.get("firmware") or {}

        if firmware_info.get("criticalUpdateAvailable", False):
            return "Critical security update available"

        update_status = firmware_info.get("updateStatus") or {}
        state = update_status.get("state", "IDLE")

        if state == "DOWNLOADING":
            progress = update_status.get("progress", 0)
            return f"Downloading update... {progress}%"
        if state == "INSTALLING":
            progress = update_status.get("progress", 0)
            return f"Installing update... {progress}%"
        if state == "READY":
            return "Update ready to install"

        return None


class AjaxHubFirmwareUpdate(CoordinatorEntity[AjaxDataCoordinator], UpdateEntity):
    """Firmware update entity for Ajax Hub."""

    __slots__ = ("_space_id",)

    _attr_has_entity_name = True
    _attr_translation_key = "hub_firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    # No install feature - Ajax handles updates automatically
    _attr_supported_features = UpdateEntityFeature(0)

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space: AjaxSpace,
    ) -> None:
        """Initialize the update entity."""
        super().__init__(coordinator)
        self._space_id = space.id
        # Use space.id (stable) rather than hub_id which may be None at first setup.
        self._attr_unique_id = f"{self.coordinator.entry_id}_{space.id}_firmware_update"

        # Get hub model name
        hub_subtype = space.hub_details.get("hubSubtype") if space.hub_details else None
        model_name = _format_hub_type(hub_subtype)
        hub_color = space.hub_details.get("color", "") if space.hub_details else ""
        model_display = f"{model_name} ({hub_color.title()})" if hub_color else model_name

        # Get firmware version
        firmware_version = None
        if space.hub_details and space.hub_details.get("firmware"):
            firmware_version = space.hub_details["firmware"].get("version")

        # Get hardware version. The device registry requires a string, and this
        # describes the same hub device as the alarm panel — format it identically
        # ("PCB rev.<n>") so the two platforms don't flip-flop the registry entry.
        hw_version = None
        if space.hub_details and space.hub_details.get("hardwareVersions"):
            pcb = space.hub_details["hardwareVersions"].get("pcb")
            if pcb:
                hw_version = f"PCB rev.{pcb}"

        # Device info - always keyed by space.id for stable registry entries.
        self._attr_device_info = DeviceInfo(
            identifiers={device_identifier(self.coordinator.entry_id, self._space_id)},
            name=space.name,
            manufacturer=MANUFACTURER,
            model=model_display,
            sw_version=firmware_version,
            hw_version=hw_version,
        )

    @property
    def _space(self) -> AjaxSpace | None:
        """Get the current space from coordinator data."""
        return self.coordinator.data.spaces.get(self._space_id)

    @property
    def _firmware_info(self) -> dict[str, Any]:
        """Get firmware info from hub_details."""
        space = self._space
        if not space or not space.hub_details:
            return {}
        return space.hub_details.get("firmware") or {}

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_update_success and bool(self._firmware_info)

    @property
    def installed_version(self) -> str | None:
        """Return the current firmware version."""
        return self._firmware_info.get("version")

    @property
    def latest_version(self) -> str | None:
        """Return the latest firmware version available.

        If no update is available, return installed_version.
        If an update is available, return the new version.
        """
        firmware = self._firmware_info
        if not firmware:
            return None

        # Check if new version is available
        if firmware.get("newVersionAvailable", False):
            latest = firmware.get("latestAvailableVersion")
            if latest:
                return latest  # type: ignore[no-any-return]

        # No update available - return installed version
        return firmware.get("version")

    @property
    def auto_update(self) -> bool:
        """Return True if auto-update is enabled."""
        return self._firmware_info.get("autoupdateEnabled", False)  # type: ignore[no-any-return]

    @property
    def release_summary(self) -> str | None:
        """Return a summary of the release."""
        firmware = self._firmware_info
        if not firmware:
            return None

        if firmware.get("newVersionAvailable", False):
            if firmware.get("autoupdateEnabled", False):
                return "Update available (auto-update enabled)"
            return "Update available"

        return None
