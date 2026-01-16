"""Ajax sensor platform (refactored).

This module creates sensors for:
- Space-level statistics (device counts, notifications, etc.)
- Device-level measurements using the handler architecture
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import DOMAIN, MANUFACTURER
from .coordinator import AjaxDataCoordinator
from .devices import (
    ButtonHandler,
    DoorbellHandler,
    DoorContactHandler,
    FloodDetectorHandler,
    GlassBreakHandler,
    HubHandler,
    MotionDetectorHandler,
    RepeaterHandler,
    SirenHandler,
    SmokeDetectorHandler,
    SocketHandler,
    VideoEdgeHandler,
    WireInputHandler,
)
from .models import AjaxDevice, AjaxSpace, AjaxVideoEdge, DeviceType

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


def format_event_text(event: dict) -> str:
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


def get_last_event_text(space) -> str:
    """Get the last event formatted as text."""
    if not space.recent_events:
        return "no_event"
    return format_event_text(space.recent_events[0])


def get_last_event_attributes(space) -> dict[str, Any]:
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
    now = datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

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


@dataclass
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
        value_fn=lambda space: space.hub_details.get("warnings", {}).get(
            "allDevices", 0
        )
        if space.hub_details
        else len(space.get_devices_with_malfunctions()),
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
        value_fn=lambda space: space.hub_details.get("battery", {}).get(
            "chargeLevelPercentage"
        )
        if space.hub_details
        else None,
    ),
    # Note: hub_tamper removed - use binary_sensor.tamper from hub device instead
    AjaxSpaceSensorDescription(
        key="hub_external_power",
        translation_key="hub_external_power",
        value_fn=lambda space: "connected"
        if space.hub_details.get("externallyPowered")
        else "disconnected"
        if space.hub_details
        else None,
    ),
    AjaxSpaceSensorDescription(
        key="hub_ethernet_ip",
        translation_key="hub_ethernet_ip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: space.hub_details.get("ethernet", {}).get("ip")
        if space.hub_details and space.hub_details.get("ethernet", {}).get("enabled")
        else None,
    ),
    AjaxSpaceSensorDescription(
        key="hub_wifi",
        translation_key="hub_wifi",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: format_signal_level(
            space.hub_details.get("wifi", {}).get("signalLevel")
        )
        if space.hub_details and space.hub_details.get("wifi", {}).get("enabled")
        else None,
        should_create=lambda space: space.hub_details
        and space.hub_details.get("wifi", {}).get("enabled", False),
    ),
    AjaxSpaceSensorDescription(
        key="hub_gsm",
        translation_key="hub_gsm",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: format_signal_level(
            space.hub_details.get("gsm", {}).get("signalLevel")
        )
        if space.hub_details
        else None,
        should_create=lambda space: space.hub_details
        and space.hub_details.get("gsm") is not None,
    ),
    AjaxSpaceSensorDescription(
        key="hub_led_brightness",
        translation_key="hub_led_brightness",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: space.hub_details.get("ledBrightnessLevel")
        if space.hub_details
        else None,
    ),
    AjaxSpaceSensorDescription(
        key="hub_timezone",
        translation_key="hub_timezone",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: format_timezone(space.hub_details.get("timeZone"))
        if space.hub_details
        else None,
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
        value_fn=lambda space: len(getattr(space, "_users", []))
        if hasattr(space, "_users")
        else None,
        should_create=lambda space: hasattr(space, "_users")
        and len(getattr(space, "_users", [])) > 0,
    ),
    # Grade Mode (security level)
    AjaxSpaceSensorDescription(
        key="hub_grade_mode",
        translation_key="hub_grade_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: {
            "GRADE_1": "Grade 1",
            "GRADE_2": "Grade 2",
            "GRADE_3": "Grade 3",
        }.get(space.hub_details.get("gradeMode"), space.hub_details.get("gradeMode"))
        if space.hub_details
        else None,
        should_create=lambda space: space.hub_details
        and space.hub_details.get("gradeMode"),
    ),
    # Active Channels (WiFi, Ethernet, GSM) - disabled by default (changes too often)
    AjaxSpaceSensorDescription(
        key="hub_active_channels",
        translation_key="hub_active_channels",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda space: ", ".join(space.hub_details.get("activeChannels", []))
        if space.hub_details and space.hub_details.get("activeChannels")
        else None,
        should_create=lambda space: space.hub_details
        and space.hub_details.get("activeChannels"),
    ),
    # Ping Period
    AjaxSpaceSensorDescription(
        key="hub_ping_period",
        translation_key="hub_ping_period",
        native_unit_of_measurement="s",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: space.hub_details.get("pingPeriodSeconds")
        if space.hub_details
        else None,
        should_create=lambda space: space.hub_details
        and space.hub_details.get("pingPeriodSeconds"),
    ),
    # Offline Alarm Delay
    AjaxSpaceSensorDescription(
        key="hub_offline_delay",
        translation_key="hub_offline_delay",
        native_unit_of_measurement="s",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: space.hub_details.get("offlineAlarmSeconds")
        if space.hub_details
        else None,
        should_create=lambda space: space.hub_details
        and space.hub_details.get("offlineAlarmSeconds"),
    ),
    # Noise Level (radio interference)
    AjaxSpaceSensorDescription(
        key="hub_noise_level",
        translation_key="hub_noise_level",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: "high"
        if space.hub_details.get("noiseLevel", {}).get("high", False)
        else "normal"
        if space.hub_details and space.hub_details.get("noiseLevel")
        else None,
        should_create=lambda space: space.hub_details
        and space.hub_details.get("noiseLevel"),
    ),
    # Limits (sensors, rooms, etc.)
    AjaxSpaceSensorDescription(
        key="hub_limits",
        translation_key="hub_limits",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: f"{len(space.devices)}/{space.hub_details.get('limits', {}).get('sensors', 0)}"
        if space.hub_details and space.hub_details.get("limits")
        else None,
        should_create=lambda space: space.hub_details
        and space.hub_details.get("limits"),
    ),
)


# ==============================================================================
# Device Handler Mapping
# ==============================================================================


DEVICE_HANDLERS = {
    DeviceType.MOTION_DETECTOR: MotionDetectorHandler,
    DeviceType.COMBI_PROTECT: MotionDetectorHandler,
    DeviceType.DOOR_CONTACT: DoorContactHandler,
    DeviceType.WIRE_INPUT: WireInputHandler,
    DeviceType.SMOKE_DETECTOR: SmokeDetectorHandler,
    DeviceType.FLOOD_DETECTOR: FloodDetectorHandler,
    DeviceType.GLASS_BREAK: GlassBreakHandler,
    DeviceType.SOCKET: SocketHandler,
    DeviceType.RELAY: SocketHandler,
    DeviceType.WALLSWITCH: SocketHandler,
    DeviceType.SIREN: SirenHandler,
    DeviceType.SPEAKERPHONE: SirenHandler,
    DeviceType.TRANSMITTER: SirenHandler,
    DeviceType.MULTI_TRANSMITTER: SirenHandler,
    DeviceType.KEYPAD: SirenHandler,
    DeviceType.BUTTON: ButtonHandler,
    DeviceType.DOORBELL: DoorbellHandler,
    DeviceType.REPEATER: RepeaterHandler,
    DeviceType.HUB: HubHandler,
}


# ==============================================================================
# Setup
# ==============================================================================


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax sensors from a config entry."""
    coordinator = entry.runtime_data

    entities: list[SensorEntity] = []
    seen_unique_ids: set[str] = set()

    if not coordinator.account:
        _LOGGER.warning("No Ajax account found, no sensors created")
        return

    # Create space-level sensors for each space (hub)
    for space_id, space in coordinator.account.spaces.items():
        for description in SPACE_SENSORS:
            if description.should_create and not description.should_create(space):
                continue
            # recent_events is now created from REST API state changes, not just SQS
            # So we always create it
            entities.append(AjaxSpaceSensor(coordinator, entry, space_id, description))

    # Create device-level sensors using handlers
    for space_id, space in coordinator.account.spaces.items():
        for device_id, device in space.devices.items():
            handler_class = DEVICE_HANDLERS.get(device.type)
            if handler_class:
                handler = handler_class(device)
                # Get device-specific sensors + common sensors (room, etc.)
                sensors = handler.get_sensors() + handler.get_common_sensors()

                for sensor_desc in sensors:
                    unique_id = f"{device_id}_{sensor_desc['key']}"

                    # Skip if we already created this entity in this setup
                    if unique_id in seen_unique_ids:
                        _LOGGER.debug(
                            "Skipping duplicate unique_id %s for device %s",
                            unique_id,
                            device.name,
                        )
                        continue
                    seen_unique_ids.add(unique_id)

                    entities.append(
                        AjaxDeviceSensor(
                            coordinator=coordinator,
                            space_id=space_id,
                            device_id=device_id,
                            sensor_key=sensor_desc["key"],
                            sensor_desc=sensor_desc,
                        )
                    )
                    _LOGGER.debug(
                        "Created sensor '%s' for device: %s (type: %s)",
                        sensor_desc["key"],
                        device.name,
                        device.type.value,
                    )

        # Create sensors for video edges (surveillance cameras)
        for ve_id, video_edge in space.video_edges.items():
            handler = VideoEdgeHandler(video_edge)
            sensors = handler.get_sensors()

            for sensor_desc in sensors:
                unique_id = f"{ve_id}_{sensor_desc['key']}"

                if unique_id in seen_unique_ids:
                    continue
                seen_unique_ids.add(unique_id)

                entities.append(
                    AjaxVideoEdgeSensor(
                        coordinator=coordinator,
                        space_id=space_id,
                        video_edge_id=ve_id,
                        sensor_key=sensor_desc["key"],
                        sensor_desc=sensor_desc,
                    )
                )
                _LOGGER.debug(
                    "Created video edge sensor '%s' for: %s",
                    sensor_desc["key"],
                    video_edge.name,
                )

        # Create hub-level sensors from hub_details
        if space.hub_details:
            hub_id = space.hub_id or space_id
            hub_sensors = _get_hub_sensors(space)

            for sensor_desc in hub_sensors:
                unique_id = f"{hub_id}_{sensor_desc['key']}"

                if unique_id in seen_unique_ids:
                    continue
                seen_unique_ids.add(unique_id)

                entities.append(
                    AjaxHubSensor(
                        coordinator=coordinator,
                        space_id=space_id,
                        sensor_key=sensor_desc["key"],
                        sensor_desc=sensor_desc,
                    )
                )
                _LOGGER.debug(
                    "Created hub sensor '%s' for space: %s",
                    sensor_desc["key"],
                    space.name,
                )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax sensor(s)", len(entities))


# ==============================================================================
# Space-level Sensors
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

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return {}

        hub_display_name = "Ajax Hub" if space.name == "Hub" else space.name
        device_info = {
            "identifiers": {(DOMAIN, self._space_id)},
            "name": hub_display_name,
            "manufacturer": MANUFACTURER,
            "model": format_hub_type(space.hub_details.get("hubSubtype"))
            if space.hub_details
            else "Security Hub",
        }

        if space.hub_details and space.hub_details.get("firmware"):
            firmware = space.hub_details["firmware"]
            if firmware.get("version"):
                device_info["sw_version"] = firmware["version"]

        return device_info


# ==============================================================================
# Device-level Sensors (using handlers)
# ==============================================================================


class AjaxDeviceSensor(CoordinatorEntity[AjaxDataCoordinator], SensorEntity):
    """Representation of an Ajax device sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        device_id: str,
        sensor_key: str,
        sensor_desc: dict,
    ) -> None:
        """Initialize the Ajax device sensor."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id
        self._sensor_key = sensor_key
        self._sensor_desc = sensor_desc

        self._attr_unique_id = f"{device_id}_{sensor_key}"

        # Set device class if provided
        if "device_class" in sensor_desc:
            self._attr_device_class = sensor_desc["device_class"]

        # Set translation key only if explicitly provided
        # If device_class is set and no translation_key, HA will use automatic naming
        if "translation_key" in sensor_desc:
            self._attr_translation_key = sensor_desc["translation_key"]
        elif "device_class" not in sensor_desc:
            # No device_class, use sensor_key as fallback translation key
            self._attr_translation_key = sensor_key

        if "native_unit_of_measurement" in sensor_desc:
            self._attr_native_unit_of_measurement = sensor_desc[
                "native_unit_of_measurement"
            ]

        if "state_class" in sensor_desc:
            self._attr_state_class = sensor_desc["state_class"]

        if "enabled_by_default" in sensor_desc:
            self._attr_entity_registry_enabled_default = sensor_desc[
                "enabled_by_default"
            ]

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        device = self._get_device()
        if not device:
            return None

        value_fn = self._sensor_desc.get("value_fn")
        if value_fn:
            try:
                return value_fn()
            except Exception as err:
                _LOGGER.error(
                    "Error getting value for sensor %s: %s",
                    self._sensor_key,
                    err,
                )
                return None
        return None

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        device = self._get_device()
        if not device:
            return False
        return device.attributes.get("online", True)

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        device = self._get_device()
        if not device:
            return {}

        info = {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": device.name,
            "manufacturer": MANUFACTURER,
            "model": device.raw_type,
            "via_device": (DOMAIN, self._space_id),
            "sw_version": device.firmware_version,
            "hw_version": device.hardware_version,
        }
        # Auto-assign device to HA Area based on Ajax room
        if device.room_name:
            info["suggested_area"] = device.room_name
        return info

    def _get_device(self) -> AjaxDevice | None:
        """Get the device from coordinator data."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None
        return space.devices.get(self._device_id)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


# ==============================================================================
# Video Edge Sensors
# ==============================================================================


class AjaxVideoEdgeSensor(CoordinatorEntity[AjaxDataCoordinator], SensorEntity):
    """Representation of an Ajax Video Edge sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        video_edge_id: str,
        sensor_key: str,
        sensor_desc: dict,
    ) -> None:
        """Initialize the Ajax video edge sensor."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._video_edge_id = video_edge_id
        self._sensor_key = sensor_key
        self._sensor_desc = sensor_desc

        # Set unique ID
        self._attr_unique_id = f"{video_edge_id}_{sensor_key}"

        # Set translation key
        self._attr_translation_key = sensor_desc.get("translation_key", sensor_key)

        # Set entity category if provided (diagnostic, config, etc.)
        if "entity_category" in sensor_desc:
            cat = sensor_desc["entity_category"]
            if cat == "diagnostic":
                self._attr_entity_category = EntityCategory.DIAGNOSTIC
            elif cat == "config":
                self._attr_entity_category = EntityCategory.CONFIG

        # Set enabled by default
        if "enabled_by_default" in sensor_desc:
            self._attr_entity_registry_enabled_default = sensor_desc[
                "enabled_by_default"
            ]

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        video_edge = self._get_video_edge()
        if not video_edge:
            return None

        value_fn = self._sensor_desc.get("value_fn")
        if value_fn:
            try:
                return value_fn()
            except Exception as err:
                _LOGGER.error(
                    "Error getting value for video edge sensor %s: %s",
                    self._sensor_key,
                    err,
                )
                return None
        return None

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        video_edge = self._get_video_edge()
        if video_edge is None:
            return False
        return video_edge.online

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        video_edge = self._get_video_edge()
        if not video_edge:
            return {}

        model_name = video_edge.video_edge_type.value
        if video_edge.color:
            model_name = f"{model_name} ({video_edge.color.title()})"

        info = {
            "identifiers": {(DOMAIN, self._video_edge_id)},
            "name": video_edge.name,
            "manufacturer": MANUFACTURER,
            "model": model_name,
            "via_device": (DOMAIN, self._space_id),
            "sw_version": video_edge.firmware_version,
        }
        if video_edge.room_name:
            info["suggested_area"] = video_edge.room_name
        return info

    def _get_video_edge(self) -> AjaxVideoEdge | None:
        """Get the video edge from coordinator data."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None
        return space.video_edges.get(self._video_edge_id)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


# ==============================================================================
# Hub-level Sensors (from hub_details)
# ==============================================================================


def _get_hub_sensors(space: AjaxSpace) -> list[dict]:
    """Get hub sensor definitions based on available hub_details fields."""
    sensors = []
    hub_details = space.hub_details or {}

    # Hub battery level
    battery = hub_details.get("battery", {})
    if battery and "chargeLevelPercentage" in battery:
        sensors.append(
            {
                "key": "hub_battery",
                "device_class": SensorDeviceClass.BATTERY,
                "native_unit_of_measurement": PERCENTAGE,
                "state_class": SensorStateClass.MEASUREMENT,
                "value_fn": lambda hd=hub_details: hd.get("battery", {}).get(
                    "chargeLevelPercentage"
                ),
                "enabled_by_default": True,
            }
        )

    # GSM signal level
    gsm = hub_details.get("gsm", {})
    if gsm and "signalLevel" in gsm:
        sensors.append(
            {
                "key": "gsm_signal",
                "translation_key": "gsm_signal_level",
                "value_fn": lambda hd=hub_details: hd.get("gsm", {})
                .get("signalLevel", "")
                .lower()
                if hd.get("gsm", {}).get("signalLevel")
                else None,
                "enabled_by_default": True,
            }
        )

    # GSM network status (2G, 3G, 4G)
    if gsm and "networkStatus" in gsm:
        sensors.append(
            {
                "key": "gsm_network",
                "translation_key": "gsm_type",
                "value_fn": lambda hd=hub_details: hd.get("gsm", {})
                .get("networkStatus", "")
                .lower()
                if hd.get("gsm", {}).get("networkStatus")
                else None,
                "enabled_by_default": True,
            }
        )

    # SIM card state
    if gsm and "simCardState" in gsm:
        sensors.append(
            {
                "key": "sim_status",
                "translation_key": "sim_status",
                "value_fn": lambda hd=hub_details: hd.get("gsm", {})
                .get("simCardState", "")
                .lower()
                if hd.get("gsm", {}).get("simCardState")
                else None,
                "enabled_by_default": True,
            }
        )

    # Active channels (connection types)
    if "activeChannels" in hub_details:
        sensors.append(
            {
                "key": "active_connection",
                "translation_key": "active_connection",
                "value_fn": lambda hd=hub_details: ", ".join(
                    hd.get("activeChannels", [])
                )
                if hd.get("activeChannels")
                else None,
                "enabled_by_default": True,
            }
        )

    # Firmware version
    firmware = hub_details.get("firmware", {})
    if firmware and "version" in firmware:
        sensors.append(
            {
                "key": "hub_firmware",
                "translation_key": "firmware_version",
                "value_fn": lambda hd=hub_details: hd.get("firmware", {}).get(
                    "version"
                ),
                "enabled_by_default": False,
            }
        )

    return sensors


class AjaxHubSensor(CoordinatorEntity[AjaxDataCoordinator], SensorEntity):
    """Representation of an Ajax Hub sensor.

    This is for hub-level sensors that come from space.hub_details,
    not from a device in space.devices.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        sensor_key: str,
        sensor_desc: dict,
    ) -> None:
        """Initialize the Ajax hub sensor."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._sensor_key = sensor_key
        self._sensor_desc = sensor_desc

        # Get space for hub_id
        space = coordinator.get_space(space_id)
        hub_id = space.hub_id if space else space_id

        # Set unique ID
        self._attr_unique_id = f"{hub_id}_{sensor_key}"

        # Set device class if provided
        if "device_class" in sensor_desc:
            self._attr_device_class = sensor_desc["device_class"]

        # Set translation key
        if "translation_key" in sensor_desc:
            self._attr_translation_key = sensor_desc["translation_key"]
        elif "device_class" not in sensor_desc:
            self._attr_translation_key = sensor_key

        # Set unit of measurement
        if "native_unit_of_measurement" in sensor_desc:
            self._attr_native_unit_of_measurement = sensor_desc[
                "native_unit_of_measurement"
            ]

        # Set state class
        if "state_class" in sensor_desc:
            self._attr_state_class = sensor_desc["state_class"]

        # Set enabled by default
        if "enabled_by_default" in sensor_desc:
            self._attr_entity_registry_enabled_default = sensor_desc[
                "enabled_by_default"
            ]

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        space = self.coordinator.get_space(self._space_id)
        if not space or not space.hub_details:
            return None

        value_fn = self._sensor_desc.get("value_fn")
        if value_fn:
            try:
                return value_fn(space.hub_details)
            except Exception as err:
                _LOGGER.error(
                    "Error getting value for hub sensor %s: %s",
                    self._sensor_key,
                    err,
                )
                return None
        return None

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        space = self.coordinator.get_space(self._space_id)
        return space is not None and space.hub_details is not None

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information linking to the hub/space device."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return {}

        # Link to the space device (hub)
        return {
            "identifiers": {(DOMAIN, self._space_id)},
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
