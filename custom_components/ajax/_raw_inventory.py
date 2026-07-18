"""Shared raw-devices collection for diagnostics and the dump service.

Both ``diagnostics.get_ajax_raw_data`` and ``_services.handle_get_raw_devices``
walk the same triple loop over ``coordinator.account.spaces`` — devices,
cameras and video edges, each with a light "list" call followed by a "detail"
call per item (falling back to the light summary on failure). This module is
the single place that traversal lives; callers only differ in whether they
filter by a single device id and how they aggregate/format the result.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .coordinator import AjaxDataCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_collect_raw_inventory(
    coordinator: AjaxDataCoordinator,
    target_device_id: str | None = None,
) -> dict[str, Any]:
    """Fetch full raw payloads for devices, cameras and video edges.

    ``target_device_id`` filters devices and cameras; video edges are always
    returned in full (parity with the historical diagnostics behaviour).

    Returns ``{"devices": [...], "cameras": [...], "video_edges": [...],
    "hub_count": int}``.
    """
    all_devices: list[dict[str, Any]] = []
    all_cameras: list[dict[str, Any]] = []
    all_video_edges: list[dict[str, Any]] = []
    hub_count = 0

    if not coordinator.account:
        return {
            "devices": all_devices,
            "cameras": all_cameras,
            "video_edges": all_video_edges,
            "hub_count": hub_count,
        }

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
                    if target_device_id is not None and target_device_id != device_id:
                        continue
                    if device_id:
                        try:
                            full_device = await coordinator.api.async_get_device(hub_id, device_id)
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
                    if target_device_id is not None and target_device_id != camera_id:
                        continue
                    if camera_id:
                        try:
                            full_camera = await coordinator.api.async_get_camera(hub_id, camera_id)
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
                video_edges_list = await coordinator.api.async_get_video_edges(real_space_id)
                all_video_edges.extend(video_edges_list)
            except Exception as err:
                _LOGGER.warning(
                    "Failed to get video edges for space %s: %s",
                    real_space_id,
                    err,
                )

    return {
        "devices": all_devices,
        "cameras": all_cameras,
        "video_edges": all_video_edges,
        "hub_count": hub_count,
    }
