"""Unit tests for the registry-identifier helpers.

``_ids`` is the single source of truth for how an Ajax object is addressed
in the device registry: the namespaced identifier, the parent link, and the
entry-scoped lookup. Two of the three have a version-dependent shape — the
integration supports cores both before and after the 2026.8 deprecation of
``DeviceInfo.via_device`` — so both branches are exercised here.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from custom_components.ajax._ids import device_identifier, find_device, via_device_info
from custom_components.ajax.const import DOMAIN
from tests.conftest import _FakeDeviceRegistry, fake_device_id, fake_hass


def test_device_identifier_is_entry_namespaced() -> None:
    assert device_identifier("entry_test", "s1") == (DOMAIN, "entry_test_s1")


# ---------------------------------------------------------------------------
# via_device_info
# ---------------------------------------------------------------------------


def test_via_device_info_uses_registry_id_on_recent_core() -> None:
    info = via_device_info(fake_hass(), "entry_test", "s1")

    assert info == {"via_device_id": fake_device_id("entry_test", "s1")}


def test_via_device_info_omits_link_when_parent_unregistered() -> None:
    """A dangling via_device_id is rejected by HA, so the key must be dropped."""
    info = via_device_info(fake_hass(missing=["s1"]), "entry_test", "s1")

    assert info == {}


def test_via_device_info_falls_back_to_tuple_on_old_core() -> None:
    """Cores older than 2026.8 do not know via_device_id at all."""
    with patch("custom_components.ajax._ids._SUPPORTS_VIA_DEVICE_ID", False):
        info = via_device_info(fake_hass(), "entry_test", "s1")

    assert info == {"via_device": (DOMAIN, "entry_test_s1")}


def test_via_device_info_old_core_does_not_touch_the_registry() -> None:
    """The tuple form is resolved by HA itself — no lookup to make here."""
    hass = SimpleNamespace(data={})  # dr.async_get would raise on this one

    with patch("custom_components.ajax._ids._SUPPORTS_VIA_DEVICE_ID", False):
        assert via_device_info(hass, "entry_test", "s1") == {"via_device": (DOMAIN, "entry_test_s1")}


# ---------------------------------------------------------------------------
# find_device
# ---------------------------------------------------------------------------


def test_find_device_resolves_through_the_entry_scoped_lookup() -> None:
    registry = _FakeDeviceRegistry()

    device = find_device(registry, "entry_test", "d1")

    assert device is not None
    assert device.id == fake_device_id("entry_test", "d1")


def test_find_device_returns_none_for_an_unknown_object() -> None:
    registry = _FakeDeviceRegistry(missing=["d1"])

    assert find_device(registry, "entry_test", "d1") is None


def test_find_device_falls_back_to_the_legacy_lookup_on_old_core() -> None:
    """Pre-2026.8 registries only expose async_get_device(identifiers=...)."""
    registry = SimpleNamespace(
        async_get_device=lambda identifiers: SimpleNamespace(id=f"legacy_{next(iter(identifiers))[1]}")
    )

    with patch("custom_components.ajax._ids._SUPPORTS_VIA_DEVICE_ID", False):
        device = find_device(registry, "entry_test", "d1")

    assert device.id == "legacy_entry_test_d1"
