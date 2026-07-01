"""Space-level Ajax sensors.

Event/text formatting helpers, the ``SPACE_SENSORS`` description table and
``AjaxSpaceSensor``. Split out of ``sensor.py`` (platform module keeps only
``async_setup_entry`` + re-exports).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from ._ids import device_identifier
from .const import MANUFACTURER
from .coordinator import AjaxDataCoordinator
from .models import (
    AjaxSpace,
)

_LOGGER = logging.getLogger(__name__)


# ==============================================================================
# Helper Functions
# ==============================================================================
def format_timezone(tz_string: str | None) -> str | None:
    """Format timezone string to be more readable."""
    if not tz_string:
        return None
    parts = tz_string.split("_")
    if len(parts) >= 2:
        region = parts[0].title()
        city = "_".join(parts[1:]).replace("_", " ").title().replace(" ", "_")
        return f"{region}/{city}"
    return tz_string


def format_hub_type(hub_type: str | None) -> str | None:
    """Format hub type string to be more readable."""
    if not hub_type:
        return None
    return hub_type.replace("_", " ").title()


def format_signal_level(signal: str | None) -> str | None:
    """Format signal level string to be more readable."""
    if not signal:
        return None
    # Return lowercase for translation keys
    return signal.lower()


def format_event_text(event: dict[str, Any]) -> str:
    """Format an SQS event into readable text."""
    event_type = event.get("event_type", "")
    action = event.get("action", "")
    source_name = event.get("source_name", "")
    device_name = event.get("device_name")
    user_name = event.get("user_name") or source_name
    room_name = event.get("room_name")

    # Use translated message if available (from SQS/coordinator)
    # Otherwise fall back to action-based lookup
    message = event.get("message")
    if not message:
        # Fallback: Map actions to English messages
        action_messages = {
            # Arming/Disarming (from SQS events)
            "arm": "Armed",
            "armed": "Armed",
            "disarm": "Disarmed",
            "disarmed": "Disarmed",
            "grouparm": "Group armed",
            "group_armed": "Group armed",
            "groupdisarm": "Group disarmed",
            "group_disarmed": "Group disarmed",
            "nightmodeon": "Night mode on",
            "nightmodeoff": "Night mode off",
            "night_mode": "Night mode on",
            "night_mode_on": "Night mode on",
            "night_mode_off": "Night mode off",
            "partiallyarmed": "Partially armed",
            "partially_armed": "Partially armed",
            # Alarms
            "motion_detected": "Motion detected",
            "door_opened": "Door opened",
            "door_closed": "Door closed",
            "glass_break_detected": "Glass break detected",
            "smoke_detected": "Smoke detected",
            "leak_detected": "Water leak detected",
            "tamper": "Tamper detected",
            "tampered": "Tamper detected",
            "panic": "Panic alarm",
            # Device status
            "online": "Device online",
            "offline": "Device offline",
            "low_battery": "Low battery",
            "external_power_on": "Power connected",
            "external_power_off": "Power disconnected",
        }

        # Get message from action (case-insensitive)
        action_lower = action.lower() if action else ""
        message = action_messages.get(action_lower, action or event_type or "Event")

    parts = [message]
    if device_name and device_name.strip():
        parts.append(f"- {device_name.strip()}")
    if room_name and room_name.strip():
        parts.append(f"({room_name.strip()})")
    if user_name and user_name.strip():
        # Use French "par" if message appears to be French, otherwise "by"
        french_words = (
            "Armé",
            "Désarmé",
            "Groupe",
            "Mode nuit",
            "Armement",
            "Ouverture",
        )
        by_word = "par" if any(fw in message for fw in french_words) else "by"
        parts.append(f"{by_word} {user_name.strip()}")

    return " ".join(parts)


def get_last_event_text(space: AjaxSpace) -> str:
    """Get the last event formatted as text."""
    if not space.recent_events:
        return "no_event"
    return format_event_text(space.recent_events[0])


def get_last_event_attributes(space: AjaxSpace) -> dict[str, Any]:
    """Get attributes for the last event sensor."""
    if not space.recent_events:
        return {"events_count": 0}

    last_event = space.recent_events[0]
    attrs = {
        "event_type": last_event.get("event_type", ""),
        "event_tag": last_event.get("event_tag", ""),
        "action": last_event.get("action", ""),
        "source_name": last_event.get("source_name", ""),
        "source_type": last_event.get("source_type", ""),
        "room_name": last_event.get("room_name", ""),
        "transition": last_event.get("transition", ""),
        "events_count": len(space.recent_events),
    }

    # Format timestamp
    timestamp = last_event.get("timestamp")
    if timestamp:
        if isinstance(timestamp, datetime):
            attrs["timestamp"] = timestamp.isoformat()
            attrs["time_ago"] = _format_time_ago(timestamp)
        else:
            attrs["timestamp"] = str(timestamp)

    # Add recent events history (last 5)
    history = []
    for event in space.recent_events[:5]:
        entry = {
            "message": event.get("message", ""),
            "source": event.get("source_name", ""),
            "room": event.get("room_name", ""),
        }
        ts = event.get("timestamp")
        if ts and isinstance(ts, datetime):
            entry["time"] = ts.strftime("%H:%M:%S")
        history.append(entry)
    attrs["recent_history"] = history

    return attrs


def _format_time_ago(timestamp: datetime) -> str:
    """Format timestamp as 'X minutes ago'."""
    now = datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)

    diff = now - timestamp
    seconds = diff.total_seconds()

    if seconds < 60:
        return "Just now"
    elif seconds < 3600:
        minutes = int(seconds / 60)
        return f"{minutes} min ago"
    elif seconds < 86400:
        hours = int(seconds / 3600)
        return f"{hours}h ago"
    else:
        days = int(seconds / 86400)
        return f"{days}d ago"


# ==============================================================================
# Space-level Sensor Descriptions
# ==============================================================================
@dataclass(frozen=True)
class AjaxSpaceSensorDescription(SensorEntityDescription):
    """Description for Ajax space-level sensors."""

    value_fn: Callable[[AjaxSpace], Any] | None = None
    should_create: Callable[[AjaxSpace], bool] | None = None
    entity_category: EntityCategory | None = None


SPACE_SENSORS: tuple[AjaxSpaceSensorDescription, ...] = (
    AjaxSpaceSensorDescription(
        key="total_devices",
        translation_key="total_devices",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: len(space.devices),
    ),
    AjaxSpaceSensorDescription(
        key="online_devices",
        translation_key="online_devices",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: len(space.get_online_devices()),
    ),
    AjaxSpaceSensorDescription(
        key="devices_with_malfunctions",
        translation_key="devices_with_malfunctions",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: (
            space.hub_details.get("warnings", {}).get("allDevices", 0)
            if space.hub_details
            else len(space.get_devices_with_malfunctions())
        ),
    ),
    AjaxSpaceSensorDescription(
        key="bypassed_devices",
        translation_key="bypassed_devices",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: len(space.get_bypassed_devices()),
    ),
    AjaxSpaceSensorDescription(
        key="recent_events",
        translation_key="recent_events",
        value_fn=lambda space: get_last_event_text(space),
    ),
    AjaxSpaceSensorDescription(
        key="hub_battery",
        translation_key="hub_battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda space: (
            space.hub_details.get("battery", {}).get("chargeLevelPercentage") if space.hub_details else None
        ),
    ),
    # Note: hub_tamper removed - use binary_sensor.tamper from hub device instead
    AjaxSpaceSensorDescription(
        key="hub_external_power",
        translation_key="hub_external_power",
        value_fn=lambda space: (
            "connected" if space.hub_details.get("externallyPowered") else "disconnected" if space.hub_details else None
        ),
    ),
    AjaxSpaceSensorDescription(
        key="hub_ethernet_ip",
        translation_key="hub_ethernet_ip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: (
            space.hub_details.get("ethernet", {}).get("ip")
            if space.hub_details and space.hub_details.get("ethernet", {}).get("enabled")
            else None
        ),
    ),
    AjaxSpaceSensorDescription(
        key="hub_wifi",
        translation_key="hub_wifi",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: (
            format_signal_level(space.hub_details.get("wifi", {}).get("signalLevel"))
            if space.hub_details and space.hub_details.get("wifi", {}).get("enabled")
            else None
        ),
        should_create=lambda space: bool(space.hub_details and space.hub_details.get("wifi", {}).get("enabled", False)),
    ),
    AjaxSpaceSensorDescription(
        key="hub_gsm",
        translation_key="hub_gsm",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: (
            format_signal_level(space.hub_details.get("gsm", {}).get("signalLevel")) if space.hub_details else None
        ),
        should_create=lambda space: bool(space.hub_details and space.hub_details.get("gsm") is not None),
    ),
    AjaxSpaceSensorDescription(
        key="hub_led_brightness",
        translation_key="hub_led_brightness",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: space.hub_details.get("ledBrightnessLevel") if space.hub_details else None,
    ),
    AjaxSpaceSensorDescription(
        key="hub_timezone",
        translation_key="hub_timezone",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: format_timezone(space.hub_details.get("timeZone")) if space.hub_details else None,
    ),
    # Rooms count
    AjaxSpaceSensorDescription(
        key="hub_rooms",
        translation_key="hub_rooms",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: len(space.rooms) if space.rooms else 0,
    ),
    # Users count
    AjaxSpaceSensorDescription(
        key="hub_users",
        translation_key="hub_users",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: len(space.users) if space.users else None,
        should_create=lambda space: len(space.users) > 0,
    ),
    # Grade Mode (security level)
    AjaxSpaceSensorDescription(
        key="hub_grade_mode",
        translation_key="hub_grade_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: (
            {
                "GRADE_1": "Grade 1",
                "GRADE_2": "Grade 2",
                "GRADE_3": "Grade 3",
            }.get(str(space.hub_details.get("gradeMode", "")), space.hub_details.get("gradeMode"))
            if space.hub_details
            else None
        ),
        should_create=lambda space: bool(space.hub_details and space.hub_details.get("gradeMode")),
    ),
    # Active Channels (WiFi, Ethernet, GSM) - disabled by default (changes too often)
    # IMPORTANT: sorted() prevents state changes from random API order
    AjaxSpaceSensorDescription(
        key="hub_active_channels",
        translation_key="hub_active_channels",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda space: (
            ", ".join(sorted(space.hub_details.get("activeChannels", [])))
            if space.hub_details and space.hub_details.get("activeChannels")
            else None
        ),
        should_create=lambda space: bool(space.hub_details and space.hub_details.get("activeChannels")),
    ),
    # Ping Period
    AjaxSpaceSensorDescription(
        key="hub_ping_period",
        translation_key="hub_ping_period",
        native_unit_of_measurement="s",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: space.hub_details.get("pingPeriodSeconds") if space.hub_details else None,
        should_create=lambda space: bool(space.hub_details and space.hub_details.get("pingPeriodSeconds")),
    ),
    # Offline Alarm Delay
    AjaxSpaceSensorDescription(
        key="hub_offline_delay",
        translation_key="hub_offline_delay",
        native_unit_of_measurement="s",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: space.hub_details.get("offlineAlarmSeconds") if space.hub_details else None,
        should_create=lambda space: bool(space.hub_details and space.hub_details.get("offlineAlarmSeconds")),
    ),
    # Noise Level (radio interference)
    AjaxSpaceSensorDescription(
        key="hub_noise_level",
        translation_key="hub_noise_level",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: (
            "high"
            if space.hub_details.get("noiseLevel", {}).get("high", False)
            else "normal"
            if space.hub_details and space.hub_details.get("noiseLevel")
            else None
        ),
        should_create=lambda space: bool(space.hub_details and space.hub_details.get("noiseLevel")),
    ),
    # Limits (sensors, rooms, etc.)
    AjaxSpaceSensorDescription(
        key="hub_limits",
        translation_key="hub_limits",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: (
            f"{len(space.devices)}/{space.hub_details.get('limits', {}).get('sensors', 0)}"
            if space.hub_details and space.hub_details.get("limits")
            else None
        ),
        should_create=lambda space: bool(space.hub_details and space.hub_details.get("limits")),
    ),
)


# ==============================================================================
# Setup
# ==============================================================================


class AjaxSpaceSensor(CoordinatorEntity[AjaxDataCoordinator], SensorEntity):
    """Representation of an Ajax space-level sensor (statistics about the space/hub)."""

    entity_description: AjaxSpaceSensorDescription

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        entry: AjaxConfigEntry,
        space_id: str,
        description: AjaxSpaceSensorDescription,
    ) -> None:
        """Initialize the space sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._space_id = space_id
        self._entry = entry

        self._attr_has_entity_name = True
        self._attr_translation_key = description.translation_key
        self._attr_unique_id = f"{entry.entry_id}_{space_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        space = self.coordinator.get_space(self._space_id)
        if not space or not self.entity_description.value_fn:
            return None
        return self.entity_description.value_fn(space)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes for recent_events sensor."""
        if self.entity_description.key != "recent_events":
            return None

        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None

        return get_last_event_attributes(space)

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None

        hub_display_name = "Ajax Hub" if space.name == "Hub" else space.name
        sw_version: str | None = None

        if space.hub_details and space.hub_details.get("firmware"):
            firmware = space.hub_details["firmware"]
            if firmware.get("version"):
                sw_version = firmware["version"]

        return DeviceInfo(
            identifiers={device_identifier(self.coordinator.entry_id, self._space_id)},
            name=hub_display_name,
            manufacturer=MANUFACTURER,
            model=format_hub_type(space.hub_details.get("hubSubtype")) if space.hub_details else "Security Hub",
            sw_version=sw_version,
        )


# ==============================================================================
# Device-level Sensors (using handlers)
# ==============================================================================
