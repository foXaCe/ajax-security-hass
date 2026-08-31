"""Local pytest fixtures for Ajax tests."""

from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace

import pytest
from homeassistant.helpers import device_registry as dr


@pytest.fixture(autouse=True, scope="session")
def mock_zeroconf_resolver() -> Generator[None]:
    """Override HA's pycares resolver fixture, which hangs during teardown here."""
    yield


@pytest.fixture(autouse=True)
def enable_event_loop_debug() -> None:
    """Override HA fixture; these tests do not need an event loop."""


@pytest.fixture(autouse=True)
def verify_cleanup() -> Generator[None]:
    """Override HA cleanup fixture for non-HA translation tests."""
    yield


class _FakeDeviceRegistry:
    """Minimal device registry for the entity unit tests.

    ``DeviceInfo`` links its parent with ``via_device_id``, a device-registry
    id, so building a ``device_info`` now needs a registry lookup. The entity
    unit tests bypass the HA harness entirely (``object.__new__`` + stubbed
    coordinator), so they get this stand-in instead: every identifier resolves
    to a stable, predictable device id.
    """

    def async_get_device_by_identifier(self, identifier: tuple[str, str], config_entry_id: str) -> SimpleNamespace:
        """Resolve any identifier to a device whose id derives from it."""
        return SimpleNamespace(id=f"dev_{identifier[1]}")

    def async_get_device(self, identifiers: set[tuple[str, str]]) -> SimpleNamespace:
        """Same resolution through the pre-2026.8 lookup."""
        return self.async_get_device_by_identifier(next(iter(identifiers)), "")


def fake_device_id(entry_id: str, raw_id: str) -> str:
    """Return the device id ``_FakeDeviceRegistry`` resolves an Ajax object to."""
    return f"dev_{entry_id}_{raw_id}"


def fake_hass() -> SimpleNamespace:
    """Return a stand-in ``hass`` that ``device_registry.async_get`` accepts."""
    return SimpleNamespace(data={dr.DATA_REGISTRY: _FakeDeviceRegistry()})
