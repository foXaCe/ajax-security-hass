"""Unit tests for the dynamic-entity discovery helper.

``connect_new_entity_signal`` owns the registry-filter / dispatch-wire
boilerplate that every entity platform shares. Regressions here would
either drop newly-discovered entities silently, double-create them, or
leak the dispatcher subscription.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.ajax import _discovery
from custom_components.ajax._discovery import connect_new_entity_signal


@pytest.fixture
def fake_hass() -> SimpleNamespace:
    return SimpleNamespace()


@pytest.fixture
def fake_entry() -> SimpleNamespace:
    entry = SimpleNamespace(async_on_unload=MagicMock())
    return entry


def _registry(*existing_unique_ids: str) -> MagicMock:
    """Mock entity registry where listed unique_ids are 'already registered'."""
    reg = MagicMock()

    def _lookup(domain: str, platform_domain: str, unique_id: str) -> str | None:
        return f"entity.{unique_id}" if unique_id in existing_unique_ids else None

    reg.async_get_entity_id.side_effect = _lookup
    return reg


def _connect(hass, entry, builder, *, signal="ajax_new_device", domain="binary_sensor"):
    add = MagicMock()
    connect_new_entity_signal(hass, entry, signal, domain, add, builder, label="thing(s)")
    # The helper registered the inner handler via async_dispatcher_connect;
    # we grab it from the spy on entry.async_on_unload (the handler is the
    # 2nd positional arg to async_dispatcher_connect, captured via patch).
    return add


@patch.object(_discovery, "er")
@patch.object(_discovery, "async_dispatcher_connect")
def test_adds_only_unknown_entities(
    mock_dispatcher_connect,
    mock_er,
    fake_hass,
    fake_entry,
) -> None:
    # Dedup is on entity.unique_id, so fake entities must carry it.
    ent_new = SimpleNamespace(unique_id="uid_new")
    ent_seen = SimpleNamespace(unique_id="uid_seen")
    builder = MagicMock(return_value=[("uid_new", ent_new), ("uid_seen", ent_seen)])
    mock_er.async_get.return_value = _registry("uid_seen")

    async_add = _connect(fake_hass, fake_entry, builder)
    # Grab the @callback registered with the dispatcher.
    handler = mock_dispatcher_connect.call_args.args[2]
    handler("space-1", "obj-1")

    builder.assert_called_once_with("space-1", "obj-1")
    async_add.assert_called_once_with([ent_new])  # uid_seen filtered out


@patch.object(_discovery, "er")
@patch.object(_discovery, "async_dispatcher_connect")
def test_does_not_call_async_add_when_builder_empty(
    mock_dispatcher_connect,
    mock_er,
    fake_hass,
    fake_entry,
) -> None:
    """No builder output → no async_add_entities call at all (avoid empty add)."""
    builder = MagicMock(return_value=[])
    mock_er.async_get.return_value = _registry()

    async_add = _connect(fake_hass, fake_entry, builder)
    handler = mock_dispatcher_connect.call_args.args[2]
    handler("space", "obj")

    async_add.assert_not_called()


@patch.object(_discovery, "er")
@patch.object(_discovery, "async_dispatcher_connect")
def test_does_not_call_async_add_when_all_already_registered(
    mock_dispatcher_connect,
    mock_er,
    fake_hass,
    fake_entry,
) -> None:
    """All candidates already in the registry → no add (avoid empty add)."""
    builder = MagicMock(
        return_value=[("uid_a", SimpleNamespace(unique_id="uid_a")), ("uid_b", SimpleNamespace(unique_id="uid_b"))]
    )
    mock_er.async_get.return_value = _registry("uid_a", "uid_b")

    async_add = _connect(fake_hass, fake_entry, builder)
    handler = mock_dispatcher_connect.call_args.args[2]
    handler("space", "obj")

    async_add.assert_not_called()


@patch.object(_discovery, "er")
@patch.object(_discovery, "async_dispatcher_connect")
def test_registers_unload_callback(
    mock_dispatcher_connect,
    mock_er,
    fake_hass,
    fake_entry,
) -> None:
    """The dispatcher subscription must be tied to the config entry's lifecycle."""
    builder = MagicMock(return_value=[])
    mock_er.async_get.return_value = _registry()
    mock_dispatcher_connect.return_value = "unsub_token"

    _connect(fake_hass, fake_entry, builder)

    fake_entry.async_on_unload.assert_called_once_with("unsub_token")


@patch.object(_discovery, "er")
@patch.object(_discovery, "async_dispatcher_connect")
def test_passes_signal_and_domain_through(
    mock_dispatcher_connect,
    mock_er,
    fake_hass,
    fake_entry,
) -> None:
    builder = MagicMock(return_value=[])
    mock_er.async_get.return_value = _registry()

    _connect(fake_hass, fake_entry, builder, signal="custom_signal", domain="event")

    # The dispatcher signal name passed straight through.
    assert mock_dispatcher_connect.call_args.args[1] == "custom_signal"
    # And the registry lookup uses the platform domain we asked for.
    handler = mock_dispatcher_connect.call_args.args[2]
    builder.return_value = [("uid", SimpleNamespace(unique_id="uid"))]
    handler("s", "o")
    mock_er.async_get.return_value.async_get_entity_id.assert_called_with("event", _discovery.DOMAIN, "uid")
