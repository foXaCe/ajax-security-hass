"""Ajax data models for Home Assistant integration.

This module defines the data models that mirror the Ajax app structure:
- Space (Hub/System)
- Room (Zone/Piece)
- Device (Capteur/Detecteur)
- Notification (Event/Alerte)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class SecurityState(Enum):
    """Security states for Ajax spaces."""

    NONE = "none"
    ARMED = "armed"
    DISARMED = "disarmed"
    NIGHT_MODE = "night_mode"
    PARTIALLY_ARMED = "partially_armed"
    AWAITING_EXIT_TIMER = "awaiting_exit_timer"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    ARMING_INCOMPLETE = "arming_incomplete"


class DeviceType(Enum):
    """Device types supported by Ajax."""

    # Security Sensors
    MOTION_DETECTOR = "motion_detector"
    DOOR_CONTACT = "door_contact"
    GLASS_BREAK = "glass_break"
    SMOKE_DETECTOR = "smoke_detector"
    FLOOD_DETECTOR = "flood_detector"
    TEMPERATURE_SENSOR = "temperature_sensor"

    # Security Devices
    KEYPAD = "keypad"
    REMOTE_CONTROL = "remote_control"
    SIREN = "siren"
    TRANSMITTER = "transmitter"
    REPEATER = "repeater"

    # Smart Devices
    SOCKET = "socket"
    RELAY = "relay"
    THERMOSTAT = "thermostat"

    # Cameras
    CAMERA = "camera"

    # Hub
    HUB = "hub"

    # Unknown
    UNKNOWN = "unknown"


class NotificationType(Enum):
    """Notification types from Ajax."""

    ALARM = "alarm"
    WARNING = "warning"
    INFO = "info"
    SECURITY_EVENT = "security_event"
    SYSTEM_EVENT = "system_event"


@dataclass
class AjaxRoom:
    """Represents a room/zone in an Ajax space."""

    id: str
    name: str
    space_id: str
    image_id: str | None = None
    image_url: str | None = None
    device_ids: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"Room({self.name}, devices={len(self.device_ids)})"


class GroupState(Enum):
    """Security states for Ajax groups."""

    NONE = "none"
    ARMED = "armed"
    DISARMED = "disarmed"


@dataclass
class AjaxGroup:
    """Represents a security group in an Ajax space."""

    id: str
    name: str
    space_id: str
    state: GroupState = GroupState.NONE
    night_mode_enabled: bool = False
    bulk_arm_involved: bool = False
    bulk_disarm_involved: bool = False
    image_id: str | None = None
    image_url: str | None = None
    device_ids: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"Group({self.name}, state={self.state.value}, devices={len(self.device_ids)})"


@dataclass
class AjaxDevice:
    """Represents a device in an Ajax space."""

    id: str
    name: str
    type: DeviceType
    space_id: str
    hub_id: str
    raw_type: str | None = None  # Raw device type before parsing (for debugging unknown devices)
    room_id: str | None = None
    group_id: str | None = None

    # Status
    online: bool = True
    bypassed: bool = False
    malfunctions: int = 0

    # Battery
    battery_level: int | None = None
    battery_state: str | None = None

    # Signal
    signal_strength: int | None = None

    # Firmware
    firmware_version: str | None = None
    hardware_version: str | None = None

    # Device specific attributes
    states: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    # Real-time events from notifications
    last_notification: AjaxNotification | None = None
    last_trigger_time: datetime | None = None

    # Photo data for camera devices (MotionCam, etc.)
    last_photo_url: str | None = None
    photo_urls: list[str] = field(default_factory=list)

    # Metadata
    device_color: str | None = None
    device_label: str | None = None
    device_marketing_id: str | None = None

    def __str__(self) -> str:
        return f"Device({self.name}, type={self.type.value}, online={self.online})"

    @property
    def has_battery(self) -> bool:
        """Check if device has battery info."""
        return self.battery_level is not None

    @property
    def is_low_battery(self) -> bool:
        """Check if device has low battery."""
        return self.battery_level is not None and self.battery_level < 20

    @property
    def is_triggered(self) -> bool:
        """Check if device is currently triggered based on notifications."""
        if not self.last_notification:
            return False

        # Check notification type and timing
        # Motion/door sensors auto-reset after a short time
        if self.last_trigger_time:
            from datetime import timedelta, timezone

            # Make sure both timestamps are timezone-aware
            now = datetime.now(timezone.utc)
            trigger_time = self.last_trigger_time
            if trigger_time.tzinfo is None:
                trigger_time = trigger_time.replace(tzinfo=timezone.utc)

            # Auto-reset after 30 seconds
            if (now - trigger_time) > timedelta(seconds=30):
                return False

        # Check notification message for trigger keywords
        message = self.last_notification.message.lower() if self.last_notification.message else ""
        title = self.last_notification.title.lower() if self.last_notification.title else ""

        trigger_keywords = ["motion", "detected", "triggered", "opened", "alarm", "movement"]
        return any(keyword in message or keyword in title for keyword in trigger_keywords)


@dataclass
class AjaxNotification:
    """Represents a notification from Ajax."""

    id: str
    space_id: str
    type: NotificationType
    title: str
    message: str
    timestamp: datetime
    device_id: str | None = None
    device_name: str | None = None
    read: bool = False
    media_url: str | None = None

    def __str__(self) -> str:
        return f"Notification({self.type.value}: {self.title})"


@dataclass
class AjaxSpace:
    """Represents an Ajax space (hub/system).

    This is the top-level entity that contains:
    - Security mode and state
    - Rooms (zones)
    - Devices
    - Notifications
    """

    id: str
    name: str
    hub_id: str | None = None

    # Security
    security_state: SecurityState = SecurityState.NONE
    can_arm: bool = True
    can_disarm: bool = True

    # Group mode (if system uses groups instead of simple armed/disarmed)
    group_mode_enabled: bool = False
    night_mode_enabled: bool = False

    # Notifications
    unread_notifications: int = 0

    # Collections
    rooms: dict[str, AjaxRoom] = field(default_factory=dict)
    groups: dict[str, AjaxGroup] = field(default_factory=dict)
    devices: dict[str, AjaxDevice] = field(default_factory=dict)
    notifications: list[AjaxNotification] = field(default_factory=list)

    # Metadata
    address: str | None = None
    timezone: str | None = None

    def __str__(self) -> str:
        return f"Space({self.name}, state={self.security_state.value}, devices={len(self.devices)})"

    def get_devices_in_room(self, room_id: str) -> list[AjaxDevice]:
        """Get all devices in a specific room."""
        return [d for d in self.devices.values() if d.room_id == room_id]

    def get_online_devices(self) -> list[AjaxDevice]:
        """Get all online devices."""
        return [d for d in self.devices.values() if d.online]

    def get_devices_with_malfunctions(self) -> list[AjaxDevice]:
        """Get all devices with malfunctions."""
        return [d for d in self.devices.values() if d.malfunctions > 0]

    def get_bypassed_devices(self) -> list[AjaxDevice]:
        """Get all bypassed devices."""
        return [d for d in self.devices.values() if d.bypassed]

    def get_devices_by_type(self, device_type: DeviceType) -> list[AjaxDevice]:
        """Get all devices of a specific type."""
        return [d for d in self.devices.values() if d.type == device_type]

    def get_unread_notifications(self) -> list[AjaxNotification]:
        """Get all unread notifications."""
        return [n for n in self.notifications if not n.read]


@dataclass
class AjaxAccount:
    """Represents an Ajax user account."""

    user_id: str
    name: str
    email: str
    phone: str | None = None
    locale: str = "en"

    # Spaces owned by this account
    spaces: dict[str, AjaxSpace] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"Account({self.name}, spaces={len(self.spaces)})"

    def get_total_devices(self) -> int:
        """Get total number of devices across all spaces."""
        return sum(len(space.devices) for space in self.spaces.values())

    def get_total_unread_notifications(self) -> int:
        """Get total unread notifications across all spaces."""
        return sum(space.unread_notifications for space in self.spaces.values())
