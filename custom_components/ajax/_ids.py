"""Config-entry-scoped registry identifiers (multi-account safety).

Historically every Ajax entity used the bare Ajax object id for its
``unique_id`` and its device-registry ``identifiers`` (e.g.
``(DOMAIN, device_id)``). Home Assistant treats both as *global* keys, so
two config entries (two Ajax accounts) whose APIs hand out the same short
id would collide in the registries — entities fail to register and devices
get merged across accounts.

Both the entity ``unique_id`` and the device ``identifiers`` are therefore
namespaced with the config entry id. This module is the single source of
truth for that format so the runtime code and the v1.2 -> v1.3 migration
(`async_migrate_entry`) stay byte-for-byte identical.
"""

from __future__ import annotations

from .const import DOMAIN


def device_identifier(entry_id: str, raw_id: str) -> tuple[str, str]:
    """Return the namespaced device-registry identifier for an Ajax object.

    ``raw_id`` is the bare Ajax id (hub/space/device/video-edge/smart-lock).
    """
    return (DOMAIN, f"{entry_id}_{raw_id}")


def entity_unique_id(entry_id: str, *parts: str) -> str:
    """Return a config-entry-scoped entity ``unique_id``.

    The first segment is always the entry id; the migration prepends exactly
    ``f"{entry_id}_"`` to legacy ids, so this must match that format.
    """
    return "_".join((entry_id, *parts))
