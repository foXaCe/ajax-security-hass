"""Pre-registration of the devices other Ajax devices hang off.

Entities express their parent link with ``via_device_id``, a device-registry
id — which only resolves once the parent device is registered. Platform setup
order does not guarantee that: the ``camera`` platform runs before ``sensor``,
yet an NVR channel camera's parent (the NVR itself) is first described by a
sensor entity. Registering hubs and NVRs up front removes the ordering
constraint; the entities that own those devices then simply enrich them.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from ._ids import device_identifier
from .const import MANUFACTURER, AjaxConfigEntry
from .coordinator import AjaxDataCoordinator
from .models import VIDEO_EDGE_MODEL_NAMES, VideoEdgeType

_LOGGER = logging.getLogger(__name__)


@callback
def async_register_parent_devices(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    coordinator: AjaxDataCoordinator,
) -> None:
    """Register every hub and NVR of the account in the device registry.

    Hubs first, then NVRs (whose own parent is a hub). Safe to call again:
    ``async_get_or_create`` updates the existing device instead of duplicating
    it, so the discovery path can reuse this for objects appearing later.
    """
    if coordinator.data is None:
        return

    device_reg = dr.async_get(hass)
    entry_id = coordinator.entry_id

    for space in coordinator.data.spaces.values():
        device_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={device_identifier(entry_id, space.id)},
            name=space.name,
            manufacturer=MANUFACTURER,
        )

    for space in coordinator.data.spaces.values():
        for video_edge in space.video_edges.values():
            if video_edge.video_edge_type != VideoEdgeType.NVR:
                continue
            model_name = VIDEO_EDGE_MODEL_NAMES.get(video_edge.video_edge_type, video_edge.video_edge_type.value)
            # The NVR's own link back to its hub is left to the entities that
            # own the device: by the time they build their DeviceInfo the hub
            # is registered, so the link resolves there without duplicating
            # the version-compat handling of via_device_info here.
            device_reg.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={device_identifier(entry_id, video_edge.id)},
                name=video_edge.name,
                manufacturer=MANUFACTURER,
                model=model_name,
                sw_version=video_edge.firmware_version,
            )
            _LOGGER.debug("Pre-registered NVR device %s", video_edge.name)
