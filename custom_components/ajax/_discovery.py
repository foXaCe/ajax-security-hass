"""Shared helper for dynamic entity discovery.

Every entity platform reacts to the ``SIGNAL_NEW_DEVICE`` /
``SIGNAL_NEW_VIDEO_EDGE`` / ``SIGNAL_NEW_SMART_LOCK`` dispatcher signals to
create entities for objects that appear *after* the initial setup. The
plumbing — resolve the entity registry, drop ``unique_id``s that already
exist, call ``async_add_entities`` — is identical everywhere; only the
entity construction differs.

``connect_new_entity_signal`` centralises that plumbing so each platform
only supplies a ``builder`` returning ``(unique_id, entity)`` pairs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

if TYPE_CHECKING:
    from .const import AjaxConfigEntry

_LOGGER = logging.getLogger(__name__)

# builder(space_id, obj_id) -> sequence of (unique_id, entity) pairs.
# Sequence is covariant in its item type, so a builder that produces
# ``list[tuple[str, SensorEntity]]`` is accepted where
# ``Sequence[tuple[str, Entity]]`` is expected — list[T] is invariant and
# would reject every platform-specific subclass.
EntityBuilder = Callable[[str, str], "Sequence[tuple[str, Entity]]"]


def connect_new_entity_signal(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    signal: str,
    platform_domain: str,
    async_add_entities: AddEntitiesCallback,
    builder: EntityBuilder,
    *,
    label: str,
) -> None:
    """Create entities dynamically when ``signal`` fires for a new object.

    Args:
        hass: Home Assistant instance.
        entry: The config entry — the dispatcher connection is registered
            with ``entry.async_on_unload`` so it is torn down with it.
        signal: Dispatcher signal to listen to (``SIGNAL_NEW_*``).
        platform_domain: The platform domain (``light``, ``sensor``…) used
            to look entities up in the registry.
        async_add_entities: The platform's add-entities callback.
        builder: ``builder(space_id, obj_id)`` returns ``(unique_id,
            entity)`` pairs for the freshly-discovered object. Pairs whose
            ``unique_id`` already exists in the entity registry are dropped
            before adding — so the builder does not need to deduplicate.
        label: Human-readable plural noun for the info log line.
    """

    @callback
    def _handle(space_id: str, obj_id: str) -> None:
        pairs = builder(space_id, obj_id)
        if not pairs:
            return
        ent_reg = er.async_get(hass)
        # Dedup on the entity's OWN unique_id (entry-namespaced since schema
        # v1.3), never on the builder's key — that way the dedup key and the
        # registered key can never drift apart.
        fresh = [
            entity
            for _key, entity in pairs
            if entity.unique_id is not None
            and ent_reg.async_get_entity_id(platform_domain, DOMAIN, entity.unique_id) is None
        ]
        if fresh:
            async_add_entities(fresh)
            _LOGGER.info("Dynamically added %d %s", len(fresh), label)

    entry.async_on_unload(async_dispatcher_connect(hass, signal, _handle))
