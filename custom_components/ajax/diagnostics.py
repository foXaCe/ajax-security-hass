"""Diagnostics support for Ajax."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import AjaxDataCoordinator

_LOGGER = logging.getLogger(__name__)

TO_REDACT = {
    "email",
    "password",
    "proxy_url",
    "aws_acces_key_id",
    "aws_secret_access_key",
    "que_name",
}


async def get_ajax_raw_data(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Get fresh raw data from all devices."""

    coordinator: AjaxDataCoordinator = hass.data[DOMAIN][entry.entry_id]
    all_devices = []
    all_cameras = []
    all_video_edges = []
    hub_count = 0

    if coordinator.account:
        for _space_id, space in coordinator.account.spaces.items():
            hub_id = space.hub_id
            if hub_id:
                hub_count += 1
                try:
                    # First get device list (light)
                    devices_list = await coordinator.api.async_get_devices(hub_id)
                    # Then get full details for each device
                    for device_summary in devices_list:
                        device_id = device_summary.get("id")
                        if device_id:
                            try:
                                full_device = await coordinator.api.async_get_device(
                                    hub_id, device_id
                                )
                                all_devices.append(full_device)
                            except Exception as dev_err:
                                _LOGGER.warning(
                                    "Failed to get device %s: %s",
                                    device_id,
                                    dev_err,
                                )
                                all_devices.append(device_summary)
                except Exception as err:
                    _LOGGER.error("Failed to get devices for hub %s: %s", hub_id, err)

        # Fetch cameras for each hub (same pattern as devices)
        for _space_id, space in coordinator.account.spaces.items():
            hub_id = space.hub_id
            if hub_id:
                try:
                    cameras_list = await coordinator.api.async_get_cameras(hub_id)
                    for camera_summary in cameras_list:
                        camera_id = camera_summary.get("id")
                        if camera_id:
                            try:
                                full_camera = await coordinator.api.async_get_camera(
                                    hub_id, camera_id
                                )
                                all_cameras.append(full_camera)
                            except Exception as cam_err:
                                _LOGGER.warning(
                                    "Failed to get camera %s: %s",
                                    camera_id,
                                    cam_err,
                                )
                                all_cameras.append(camera_summary)
                except Exception as err:
                    _LOGGER.warning("Failed to get cameras for hub %s: %s", hub_id, err)

        # Fetch video edges for each space (requires real_space_id)
        for _space_id, space in coordinator.account.spaces.items():
            real_space_id = space.real_space_id
            if real_space_id:
                try:
                    video_edges_list = await coordinator.api.async_get_video_edges(
                        real_space_id
                    )
                    all_video_edges.extend(video_edges_list)
                except Exception as err:
                    _LOGGER.warning(
                        "Failed to get video edges for space %s: %s",
                        real_space_id,
                        err,
                    )

        type_counts: dict[str, int] = {}
        for device in all_devices:
            dtype = device.get("deviceType", "unknown")
            type_counts[dtype] = type_counts.get(dtype, 0) + 1
            type_list = {dtype: count for dtype, count in sorted(type_counts.items())}

        summary = {
            "hubs": hub_count,
            "devices": len(all_devices),
            "cameras": len(all_cameras),
            "video_edges": len(all_video_edges),
            "device_types": type_list,
        }
    return {
        "devices": all_devices,
        "cameras": all_cameras,
        "video_edges": all_video_edges,
        "summary": summary,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""

    ajax_data = await get_ajax_raw_data(hass, entry)

    return {
        "config_entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "ajax_data": async_redact_data(ajax_data, TO_REDACT),
    }
