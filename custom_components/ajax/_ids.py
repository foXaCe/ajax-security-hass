"""Config-entry-scoped registry identifiers (multi-account safety).

Historically every Ajax entity used the bare Ajax object id for its
``unique_id`` and its device-registry ``identifiers`` (e.g.
``(DOMAIN, device_id)``). Home Assistant treats both as *global* keys, so
two config entries (two Ajax accounts) whose APIs hand out the same short
id would collide in the registries — entities fail to register and devices
get merged across accounts.

Both the entity ``unique_id`` and the device ``identifiers`` are therefore
namespaced with the config entry id. Entities build their ``unique_id``
inline as ``f"{entry_id}_{...}"``; this module is the single source of
truth for the device-registry side of that format so the runtime code and
the v1.2 -> v1.3 migration (`async_migrate_entry`) stay byte-for-byte
identical.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# ``DeviceInfo.via_device`` (an identifier tuple) is deprecated since HA 2026.8
# in favour of ``via_device_id`` (a device-registry id). On a Home Assistant
# recent enough to raise on the deprecated form, the new key is used; older
# supported cores (>= 2025.11) do not know ``via_device_id`` at all, so the
# tuple form is kept there.
_SUPPORTS_VIA_DEVICE_ID = hasattr(dr.DeviceRegistry, "async_get_device_by_identifier")


def device_identifier(entry_id: str, raw_id: str) -> tuple[str, str]:
    """Return the namespaced device-registry identifier for an Ajax object.

    ``raw_id`` is the bare Ajax id (hub/space/device/video-edge/smart-lock).
    """
    return (DOMAIN, f"{entry_id}_{raw_id}")


def find_device(device_registry: dr.DeviceRegistry, entry_id: str, raw_id: str) -> dr.DeviceEntry | None:
    """Return the registry entry for an Ajax object, or ``None``.

    ``DeviceRegistry.async_get_device`` is deprecated since HA 2026.8 because a
    bare identifier is not unique across config entries; the entry-scoped
    lookup replaces it where available. Ajax identifiers are already namespaced
    with the entry id (see ``device_identifier``), so both forms resolve to the
    same device — only the deprecation warning differs.
    """
    identifier = device_identifier(entry_id, raw_id)
    if _SUPPORTS_VIA_DEVICE_ID:
        return device_registry.async_get_device_by_identifier(identifier, entry_id)
    return device_registry.async_get_device(identifiers={identifier})


def via_device_info(hass: HomeAssistant, entry_id: str, raw_id: str) -> DeviceInfo:
    """Return the parent-device part of a ``DeviceInfo``, to merge into one.

    On a core that supports it the link is expressed as ``via_device_id`` (a
    device-registry id); the parents (hubs and NVRs) are pre-registered in
    ``async_setup_entry`` before the platforms are forwarded, which is what
    makes the lookup succeed regardless of platform setup order.

    Returns an empty mapping when the parent is not registered: an unknown
    ``via_device_id`` is rejected by the device registry, so the key must be
    omitted entirely rather than set to ``None``.
    """
    if not _SUPPORTS_VIA_DEVICE_ID:
        return cast(DeviceInfo, {"via_device": device_identifier(entry_id, raw_id)})

    device = find_device(dr.async_get(hass), entry_id, raw_id)
    if device is None:
        return DeviceInfo()
    return cast(DeviceInfo, {"via_device_id": device.id})
