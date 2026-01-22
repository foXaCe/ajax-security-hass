"""Fixtures for Ajax Security integration tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from custom_components.ajax.const import (
    AUTH_MODE_DIRECT,
    AUTH_MODE_PROXY_SECURE,
    CONF_API_KEY,
    CONF_AUTH_MODE,
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_ENABLED_SPACES,
    CONF_PROXY_URL,
    CONF_QUEUE_NAME,
)
from custom_components.ajax.models import (
    AjaxAccount,
    AjaxDevice,
    AjaxSpace,
    DeviceType,
    SecurityState,
)

# ----- Mock Data -----


MOCK_EMAIL = "test@example.com"
MOCK_PASSWORD = "testpassword123"
MOCK_PASSWORD_HASH = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"  # SHA256 of "test"
MOCK_API_KEY = "test-api-key-12345"
MOCK_HUB_ID = "hub-12345678"
MOCK_SPACE_ID = "space-12345678"
MOCK_REAL_SPACE_ID = "real-space-12345"
MOCK_DEVICE_ID = "device-12345678"
MOCK_PROXY_URL = "https://proxy.example.com"


def get_mock_hub_data() -> dict[str, Any]:
    """Return mock hub data from API."""
    return {
        "hubId": MOCK_HUB_ID,
        "hubName": "Test Hub",
        "online": True,
        "state": "DISARMED",
        "firmwareVersion": "1.0.0",
        "model": "Hub 2 Plus",
    }


def get_mock_device_data(
    device_id: str = MOCK_DEVICE_ID,
    device_type: str = "DoorProtect",
    name: str = "Test Door Sensor",
) -> dict[str, Any]:
    """Return mock device data from API."""
    return {
        "id": device_id,
        "deviceType": device_type,
        "deviceName": name,
        "online": True,
        "batteryChargeLevelPercentage": 100,
        "signalLevel": 3,
        "firmwareVersion": "1.0.0",
        "roomId": "room-1",
        "state": {
            "reedSwitch": "CLOSED",
            "tampered": False,
        },
    }


def get_mock_space_binding() -> dict[str, Any]:
    """Return mock space binding data."""
    return {
        "id": MOCK_REAL_SPACE_ID,
        "name": "Test Home",
        "hubId": MOCK_HUB_ID,
    }


def get_mock_rooms() -> list[dict[str, Any]]:
    """Return mock rooms data."""
    return [
        {"id": "room-1", "name": "Living Room"},
        {"id": "room-2", "name": "Bedroom"},
    ]


def get_mock_users() -> list[dict[str, Any]]:
    """Return mock users data."""
    return [
        {"id": "user-1", "name": "Admin", "role": "ADMIN"},
    ]


def get_mock_groups() -> list[dict[str, Any]]:
    """Return mock groups data."""
    return [
        {"id": "group-1", "name": "Main Group", "state": "DISARMED"},
    ]


# ----- Config Entry Data -----


def get_direct_config_entry_data() -> dict[str, Any]:
    """Return config entry data for direct mode."""
    return {
        CONF_AUTH_MODE: AUTH_MODE_DIRECT,
        CONF_API_KEY: MOCK_API_KEY,
        CONF_EMAIL: MOCK_EMAIL,
        CONF_PASSWORD: MOCK_PASSWORD_HASH,
        CONF_ENABLED_SPACES: [MOCK_HUB_ID],
    }


def get_direct_config_entry_data_with_sqs() -> dict[str, Any]:
    """Return config entry data for direct mode with SQS."""
    return {
        **get_direct_config_entry_data(),
        CONF_AWS_ACCESS_KEY_ID: "AKIAIOSFODNN7EXAMPLE",
        CONF_AWS_SECRET_ACCESS_KEY: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        CONF_QUEUE_NAME: "ajax-events-test.fifo",
    }


def get_proxy_config_entry_data() -> dict[str, Any]:
    """Return config entry data for proxy mode."""
    return {
        CONF_AUTH_MODE: AUTH_MODE_PROXY_SECURE,
        CONF_PROXY_URL: MOCK_PROXY_URL,
        CONF_EMAIL: MOCK_EMAIL,
        CONF_PASSWORD: MOCK_PASSWORD_HASH,
        CONF_ENABLED_SPACES: [MOCK_HUB_ID],
    }


# ----- Fixtures -----


@pytest.fixture
def mock_ajax_api() -> Generator[AsyncMock, None, None]:
    """Mock AjaxRestApi."""
    with patch("custom_components.ajax.api.AjaxRestApi", autospec=True) as mock_api_class:
        mock_api = mock_api_class.return_value
        mock_api.async_login = AsyncMock()
        mock_api.async_get_hubs = AsyncMock(return_value=[get_mock_hub_data()])
        mock_api.async_get_devices = AsyncMock(return_value=[get_mock_device_data()])
        mock_api.async_get_device = AsyncMock(return_value=get_mock_device_data())
        mock_api.async_get_space_by_hub = AsyncMock(return_value=get_mock_space_binding())
        mock_api.async_get_rooms = AsyncMock(return_value=get_mock_rooms())
        mock_api.async_get_users = AsyncMock(return_value=get_mock_users())
        mock_api.async_get_groups = AsyncMock(return_value=get_mock_groups())
        mock_api.async_get_hub_state = AsyncMock(return_value={"state": "DISARMED"})
        mock_api.async_get_cameras = AsyncMock(return_value=[])
        mock_api.async_get_video_edges = AsyncMock(return_value=[])
        mock_api.async_arm = AsyncMock()
        mock_api.async_disarm = AsyncMock()
        mock_api.async_night_mode = AsyncMock()
        mock_api.async_update_device = AsyncMock()
        mock_api.close = AsyncMock()
        mock_api.user_id = "user-12345"
        yield mock_api


@pytest.fixture
def mock_ajax_api_for_config_flow() -> Generator[MagicMock, None, None]:
    """Mock AjaxRestApi specifically for config flow tests."""
    with patch("custom_components.ajax.config_flow.AjaxRestApi", autospec=True) as mock_api_class:
        mock_api = mock_api_class.return_value
        mock_api.async_login = AsyncMock()
        mock_api.async_get_hubs = AsyncMock(return_value=[get_mock_hub_data()])
        mock_api.async_get_space_by_hub = AsyncMock(return_value=get_mock_space_binding())
        mock_api.close = AsyncMock()
        yield mock_api


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock, None, None]:
    """Mock async_setup_entry."""
    with patch(
        "custom_components.ajax.async_setup_entry",
        return_value=True,
    ) as mock_setup:
        yield mock_setup


@pytest.fixture
def mock_coordinator() -> MagicMock:
    """Create a mock coordinator."""
    coordinator = MagicMock()
    coordinator.data = create_mock_account()
    coordinator.account = coordinator.data
    coordinator.api = MagicMock()
    coordinator.last_update_success = True
    coordinator.async_request_refresh = AsyncMock()
    coordinator.get_space = MagicMock(return_value=coordinator.data.spaces.get(MOCK_HUB_ID))
    return coordinator


def create_mock_account() -> AjaxAccount:
    """Create a mock AjaxAccount with test data."""
    # Create mock device
    device = AjaxDevice(
        id=MOCK_DEVICE_ID,
        name="Test Door Sensor",
        type=DeviceType.DOOR_CONTACT,
        space_id=MOCK_REAL_SPACE_ID,
        hub_id=MOCK_HUB_ID,
        raw_type="DoorProtect",
        room_id="room-1",
        online=True,
        battery_level=100,
        signal_strength=85,
        firmware_version="1.0.0",
        attributes={
            "reed_switch": "CLOSED",
            "tampered": False,
            "temperature": 22.5,
        },
    )

    # Create mock space
    space = AjaxSpace(
        id=MOCK_REAL_SPACE_ID,
        name="Test Home",
        hub_id=MOCK_HUB_ID,
        real_space_id=MOCK_REAL_SPACE_ID,
        security_state=SecurityState.DISARMED,
    )
    space.devices = {MOCK_DEVICE_ID: device}
    space.rooms = {}

    # Create mock account
    account = AjaxAccount(
        user_id="user-12345",
        name="Test User",
        email=MOCK_EMAIL,
    )
    account.spaces = {MOCK_HUB_ID: space}

    return account


@pytest.fixture
def mock_account() -> AjaxAccount:
    """Return a mock AjaxAccount."""
    return create_mock_account()


# ----- Home Assistant Fixtures -----


@pytest.fixture
def hass() -> MagicMock:
    """Return a mock Home Assistant instance for testing."""
    hass = MagicMock(spec=HomeAssistant)
    hass.data = {}
    hass.config_entries = MagicMock()
    hass.config_entries.flow = MagicMock()
    hass.config_entries.options = MagicMock()
    hass.async_block_till_done = AsyncMock()
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()
    hass.services = MagicMock()
    hass.services.has_service = MagicMock(return_value=False)
    hass.services.async_register = MagicMock()
    hass.config = MagicMock()
    hass.config.path = MagicMock(return_value="/tmp/test")
    hass.async_add_executor_job = AsyncMock()
    return hass
