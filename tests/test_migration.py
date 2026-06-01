"""Tests for ``async_migrate_entry`` (ConfigEntry schema migrations).

Lifecycle rule: every schema version must have migration coverage so an
upgrade can never silently lose or mis-key a user's config.

* **v1.1 -> v1.2** populates the entry ``unique_id`` from the e-mail
  (lower-cased to match the config flow's ``async_set_unique_id``).
* **v1.2 -> v1.3** namespaces every entity ``unique_id`` and device
  ``identifier`` with the config entry id (multi-account collision safety).

GUARANTEE (the reason this migration is safe for automations): the
v1.2 -> v1.3 step renames each entity's ``unique_id`` *in place* to the exact
format the entity builds at runtime — ``f"{entry_id}_{legacy}"`` — so the
``entity_id``, history and automations are preserved. If it diverged, Home
Assistant would orphan the migrated rows and create fresh entities with new
``entity_id``s, silently breaking the user. The runtime side is pinned by the
per-platform coverage tests; the migration side (prepend exactly
``"{entry_id}_"``, skip already-namespaced ids, survive a collision) is pinned
here, so the two are provably identical.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.ajax import async_migrate_entry
from custom_components.ajax.const import CONF_EMAIL, DOMAIN


def _entry(*, version: int, minor_version: int, email: str | None = None) -> SimpleNamespace:
    data: dict[str, Any] = {}
    if email is not None:
        data[CONF_EMAIL] = email
    return SimpleNamespace(entry_id="ENTRY", version=version, minor_version=minor_version, data=data)


def _hass() -> MagicMock:
    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    return hass


def _reg_entry(unique_id: str, *, entity_id: str = "sensor.test", domain: str = "sensor") -> SimpleNamespace:
    return SimpleNamespace(entity_id=entity_id, unique_id=unique_id, domain=domain)


@pytest.fixture
def registries():
    """Patch the entity / device registry helpers the v1.2 -> v1.3 step calls."""
    ent_reg = MagicMock()
    ent_reg.async_get_entity_id.return_value = None  # no namespaced twin by default
    dev_reg = MagicMock()
    with (
        patch("custom_components.ajax.er.async_get", return_value=ent_reg),
        patch("custom_components.ajax.er.async_entries_for_config_entry", return_value=[]) as ent_entries,
        patch("custom_components.ajax.dr.async_get", return_value=dev_reg),
        patch("custom_components.ajax.dr.async_entries_for_config_entry", return_value=[]) as dev_entries,
    ):
        yield SimpleNamespace(ent_reg=ent_reg, ent_entries=ent_entries, dev_reg=dev_reg, dev_entries=dev_entries)


# ---------------------------------------------------------------------------
# v1.1 -> v1.2
# ---------------------------------------------------------------------------


async def test_migrate_v1_1_to_v1_2_sets_lowercased_unique_id(registries: SimpleNamespace) -> None:
    """A mixed-case e-mail must be stored lower-cased to match the config flow."""
    hass = _hass()
    assert await async_migrate_entry(hass, _entry(version=1, minor_version=1, email="Foo@Bar.COM")) is True
    calls = hass.config_entries.async_update_entry.call_args_list
    assert any(c.kwargs.get("unique_id") == "foo@bar.com" for c in calls)


async def test_migrate_without_email_yields_none_unique_id(registries: SimpleNamespace) -> None:
    """A missing e-mail must not produce an empty-string unique_id."""
    hass = _hass()
    assert await async_migrate_entry(hass, _entry(version=1, minor_version=1, email=None)) is True
    calls = hass.config_entries.async_update_entry.call_args_list
    assert any("unique_id" in c.kwargs and c.kwargs["unique_id"] is None for c in calls)


async def test_migrate_is_idempotent_when_already_current(registries: SimpleNamespace) -> None:
    """Re-running migration on an already-current (v1.3) entry must be a no-op."""
    hass = _hass()
    assert await async_migrate_entry(hass, _entry(version=1, minor_version=3, email="user@example.com")) is True
    hass.config_entries.async_update_entry.assert_not_called()
    registries.ent_reg.async_update_entity.assert_not_called()


# ---------------------------------------------------------------------------
# v1.2 -> v1.3 : registry namespacing
# ---------------------------------------------------------------------------


async def test_migrate_v1_2_to_v1_3_bumps_minor_version(registries: SimpleNamespace) -> None:
    hass = _hass()
    assert await async_migrate_entry(hass, _entry(version=1, minor_version=2, email="user@example.com")) is True
    assert any(c.kwargs.get("minor_version") == 3 for c in hass.config_entries.async_update_entry.call_args_list)


# Representative v1.2 (bare) entity unique_id formats — one per entity family.
# These mirror exactly what each entity builds at runtime once the entry-id
# prefix is stripped; the coverage tests assert the prefixed runtime side.
_LEGACY_UNIQUE_IDS = [
    "dev123_battery",  # AjaxDeviceSensor / AjaxBinarySensor
    "dev123_chimes",  # AjaxSwitch
    "dev123_tilt_degrees",  # AjaxNumber
    "ve9_motion",  # AjaxVideoEdge*
    "hub1_signal",  # AjaxHub*
    "lock5_door",  # AjaxSmartLockBinarySensor
    "lock5_lock",  # AjaxLock
    "dev123_doorbell",  # AjaxEventEntity
    "ve9_camera_main",  # AjaxCamera
    "ve9_firmware_update",  # AjaxVideoEdgeFirmwareUpdate
    "s1_location",  # AjaxDeviceTracker
]


@pytest.mark.parametrize("legacy", _LEGACY_UNIQUE_IDS)
async def test_v1_3_renames_legacy_unique_id_in_place(registries: SimpleNamespace, legacy: str) -> None:
    """GUARANTEE: a legacy id is renamed in place to exactly ``{entry_id}_{legacy}``.

    Renaming in place (not delete+create) is what preserves the entity_id and the
    user's automations/history.
    """
    registries.ent_entries.return_value = [_reg_entry(legacy)]
    hass = _hass()
    await async_migrate_entry(hass, _entry(version=1, minor_version=2))
    registries.ent_reg.async_update_entity.assert_called_once_with("sensor.test", new_unique_id=f"ENTRY_{legacy}")
    registries.ent_reg.async_remove.assert_not_called()


@pytest.mark.parametrize("already", ["ENTRY_s1_state", "ENTRY_alarm_s1", "ENTRY_panic_s1", "ENTRY_dev123_battery"])
async def test_v1_3_skips_already_namespaced_entity(registries: SimpleNamespace, already: str) -> None:
    """Ids already carrying the entry prefix (space/alarm/panic, or a re-run) are untouched."""
    registries.ent_entries.return_value = [_reg_entry(already)]
    hass = _hass()
    await async_migrate_entry(hass, _entry(version=1, minor_version=2))
    registries.ent_reg.async_update_entity.assert_not_called()
    registries.ent_reg.async_remove.assert_not_called()


async def test_v1_3_drops_legacy_orphan_when_namespaced_twin_exists(registries: SimpleNamespace) -> None:
    """Collision path: a namespaced twin already exists -> remove the legacy orphan, no crash."""
    registries.ent_entries.return_value = [_reg_entry("dev123_battery", entity_id="sensor.legacy")]
    registries.ent_reg.async_get_entity_id.return_value = "sensor.namespaced_twin"  # target taken
    hass = _hass()
    assert await async_migrate_entry(hass, _entry(version=1, minor_version=2)) is True
    registries.ent_reg.async_remove.assert_called_once_with("sensor.legacy")
    registries.ent_reg.async_update_entity.assert_not_called()


async def test_v1_3_namespaces_device_identifiers(registries: SimpleNamespace) -> None:
    """Ajax device identifiers get the entry prefix; foreign domains stay intact."""
    device = SimpleNamespace(id="ha_dev_1", identifiers={(DOMAIN, "hub1"), ("other_integration", "keep_me")})
    registries.dev_entries.return_value = [device]
    hass = _hass()
    await async_migrate_entry(hass, _entry(version=1, minor_version=2))
    registries.dev_reg.async_update_device.assert_called_once()
    _, kwargs = registries.dev_reg.async_update_device.call_args
    assert kwargs["new_identifiers"] == {(DOMAIN, "ENTRY_hub1"), ("other_integration", "keep_me")}


async def test_v1_3_skips_device_update_when_already_namespaced(registries: SimpleNamespace) -> None:
    """A device whose identifiers are already namespaced must not be re-written."""
    registries.dev_entries.return_value = [SimpleNamespace(id="ha_dev_1", identifiers={(DOMAIN, "ENTRY_hub1")})]
    hass = _hass()
    await async_migrate_entry(hass, _entry(version=1, minor_version=2))
    registries.dev_reg.async_update_device.assert_not_called()


async def test_v1_3_device_collision_is_survived(registries: SimpleNamespace) -> None:
    """A device-identifier collision must be logged, not crash the migration."""
    registries.dev_entries.return_value = [SimpleNamespace(id="ha_dev_1", identifiers={(DOMAIN, "hub1")})]
    registries.dev_reg.async_update_device.side_effect = ValueError("identifier already in use")
    hass = _hass()
    # Must still complete and bump the version despite the device collision.
    assert await async_migrate_entry(hass, _entry(version=1, minor_version=2)) is True
    assert any(c.kwargs.get("minor_version") == 3 for c in hass.config_entries.async_update_entry.call_args_list)
