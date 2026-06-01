"""Diagnostics support for Ajax."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import AjaxConfigEntry
from .const import CONF_AUTH_MODE, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Read the integration version straight from the manifest so the value
# stays accurate across releases without a second source of truth.
_MANIFEST_VERSION: str | None = None


def _integration_version() -> str:
    global _MANIFEST_VERSION  # noqa: PLW0603
    if _MANIFEST_VERSION is None:
        try:
            manifest = json.loads((Path(__file__).parent / "manifest.json").read_text())
            _MANIFEST_VERSION = str(manifest.get("version", "unknown"))
        except (OSError, ValueError):
            _MANIFEST_VERSION = "unknown"
    return _MANIFEST_VERSION


def _seconds_since(epoch: float | None) -> float | None:
    """Return ``time.time() - epoch`` (or None when ``epoch`` is falsy)."""
    if not epoch:
        return None
    return round(time.time() - epoch, 1)


def _runtime_snapshot(coordinator: Any) -> dict[str, Any]:
    """Pure-Python view of the coordinator's runtime state (no IO)."""
    update_interval = coordinator.update_interval.total_seconds() if coordinator.update_interval else None
    last_exception = getattr(coordinator, "last_exception", None)
    return {
        "integration_version": _integration_version(),
        "auth_mode": coordinator.config_entry.data.get(CONF_AUTH_MODE) if coordinator.config_entry else None,
        "last_update_success": coordinator.last_update_success,
        "last_exception": repr(last_exception) if last_exception else None,
        "update_interval_seconds": update_interval,
        "cycle_counter": coordinator._cycle_counter,
        "consecutive_auth_errors": coordinator._consecutive_auth_errors,
        "seconds_since_last_metadata_refresh": _seconds_since(coordinator._last_metadata_refresh),
        "spaces": len(coordinator.account.spaces) if coordinator.account else 0,
    }


def _sqs_connected(sqs_client: Any) -> bool:
    """Whether the SQS receiver thread is alive and holds a queue URL.

    ``AjaxSQSClient`` exposes no ``is_connected`` accessor, so derive the
    connection state from its existing attributes (mirroring the logic of
    ``AjaxSSEClient.is_connected``).
    """
    if sqs_client is None:
        return False
    thread = getattr(sqs_client, "_thread", None)
    queue_url = getattr(sqs_client, "_queue_url", None)
    return thread is not None and thread.is_alive() and queue_url is not None


def _connectivity_snapshot(coordinator: Any) -> dict[str, Any]:
    """SSE / SQS / ONVIF connection status without touching the network."""
    sse = coordinator.sse_manager
    sqs = coordinator.sqs_manager
    onvif = getattr(coordinator, "onvif_manager", None)

    sse_client = sse.sse_client if sse else None
    sqs_client = sqs.sqs_client if sqs else None
    return {
        "sse": {
            "enabled": sse is not None,
            "connected": getattr(sse_client, "is_connected", lambda: False)() if sse_client else False,
        },
        "sqs": {
            "enabled": sqs is not None,
            "connected": _sqs_connected(sqs_client),
            "seconds_since_last_event": _seconds_since(getattr(sqs, "_last_event_time", None)) if sqs else None,
        },
        "onvif": {
            "configured_count": len(onvif._clients) if onvif else 0,
            "connected_count": onvif.connected_count if onvif else 0,
        },
    }


def _cache_snapshot(coordinator: Any) -> dict[str, Any]:
    """Sizes of the short-TTL API caches (per-hub + per-space)."""
    api = coordinator.api
    return {
        "devices_cache_entries": len(getattr(api, "_devices_cache", {}) or {}),
        "devices_cache_ttl_seconds": getattr(api, "_devices_cache_ttl", None),
        "space_cache_entries": len(getattr(api, "_space_cache", {}) or {}),
        "space_cache_ttl_seconds": getattr(api, "_space_cache_ttl", None),
    }


def _spaces_summary(coordinator: Any) -> list[dict[str, Any]]:
    """Per-space counts (devices / video_edges / smart_locks / groups).

    No hub_id, room names or device names — those land in the redacted
    ``ajax_data`` section so we keep this summary leak-free.
    """
    if not coordinator.account:
        return []
    summary = []
    for space in coordinator.account.spaces.values():
        summary.append(
            {
                "security_state": space.security_state.value if space.security_state else None,
                "group_mode_enabled": space.group_mode_enabled,
                "devices": len(space.devices),
                "video_edges": len(space.video_edges),
                "smart_locks": len(space.smart_locks),
                "groups": len(space.groups),
                "recent_events": len(space.recent_events),
            }
        )
    return summary


TO_REDACT = {
    "email",
    "password",
    "proxy_url",
    "aws_access_key_id",
    "aws_secret_access_key",
    "queue_name",
    "api_key",
    "apiKey",
    "rtsp_username",
    "rtsp_password",
    "sessionToken",
    "refreshToken",
    "refresh_token",
    "session_token",
    "userId",
    "user_id",
    "sse_url",
    "sseUrl",
    "serial_number",
    "serialNumber",
    "hub_id",
    "hubId",
    "mac_address",
    "macAddress",
    "mac",
    "ip_address",
    "ipAddress",
    "ip",
    "address",
    "ssid",
    "gateway",
    "netmask",
    "dns",
    "networkInterface",
    "discovered_macs",
    "X-Api-Key",
    "X-Session-Token",
    "Authorization",
}


async def get_ajax_raw_data(
    hass: HomeAssistant, entry: AjaxConfigEntry, device: DeviceEntry | None = None
) -> dict[str, Any]:
    """Get fresh raw data from all devices."""

    coordinator = entry.runtime_data
    all_devices = []
    all_cameras = []
    all_video_edges = []
    hub_count = 0

    # Device identifiers are namespaced f"{entry_id}_{ajax_id}" (schema v1.3);
    # strip the prefix so it matches the bare Ajax ids compared against below.
    target_device_id = (
        next(
            (
                str(value).removeprefix(f"{coordinator.entry_id}_")
                for domain, value in device.identifiers
                if domain == DOMAIN
            ),
            None,
        )
        if device is not None
        else None
    )

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

        type_counts: dict[str, int] = {}
        for device_data in all_devices:
            dtype = device_data.get("deviceType", "unknown")
            type_counts[dtype] = type_counts.get(dtype, 0) + 1
        type_list = dict(sorted(type_counts.items()))

        summary = {
            "hubs": hub_count,
            "devices": len(all_devices),
            "cameras": len(all_cameras),
            "video_edges": len(all_video_edges),
            "device_types": type_list,
        }
    else:
        summary = {
            "hubs": 0,
            "devices": 0,
            "cameras": 0,
            "video_edges": 0,
            "device_types": {},
        }

    return {
        "devices": all_devices,
        "cameras": all_cameras,
        "video_edges": all_video_edges,
        "summary": summary,
    }


def _runtime_diagnostics(coordinator: Any) -> dict[str, Any]:
    """The bundle of zero-IO snapshots — the part useful for triage.

    Lives alongside the heavy ``ajax_data`` dump so the user can grab a
    cheap diag without re-hitting the Ajax API.
    """
    return {
        "runtime": _runtime_snapshot(coordinator),
        "connectivity": _connectivity_snapshot(coordinator),
        "stats": dict(coordinator.stats),
        "cache": _cache_snapshot(coordinator),
        "spaces": _spaces_summary(coordinator),
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: AjaxConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""

    coordinator = entry.runtime_data
    ajax_data = await get_ajax_raw_data(hass, entry)

    return {
        "config_entry_data": async_redact_data(entry.data, TO_REDACT),
        "diagnostics": _runtime_diagnostics(coordinator),
        "ajax_data": async_redact_data(ajax_data, TO_REDACT),
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: AjaxConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a specific device."""

    coordinator = entry.runtime_data
    device_info = {
        "manufacturer": device.manufacturer,
        "model": device.model,
        "model_id": device.model_id,
        "serial_number": device.serial_number,
        "firmware_version": device.sw_version,
        "hardware_version": device.hw_version,
    }

    ajax_data = await get_ajax_raw_data(hass, entry, device)

    return {
        "device_info": async_redact_data(device_info, TO_REDACT),
        "config_entry_data": async_redact_data(entry.data, TO_REDACT),
        "diagnostics": _runtime_diagnostics(coordinator),
        "ajax_data": async_redact_data(ajax_data, TO_REDACT),
    }
