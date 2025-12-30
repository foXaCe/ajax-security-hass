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
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AjaxDataCoordinator
from .devices import (
    DoorContactHandler,
    FloodDetectorHandler,
    GlassBreakHandler,
    HubHandler,
    MotionDetectorHandler,
    SirenHandler,
    SmokeDetectorHandler,
    SocketHandler,
)
from .models import AjaxDevice, AjaxSpace, DeviceType

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
    """Format an SQS event into readable French text."""
    event_type = event.get("event_type", "")
    action = event.get("action", "")
    source_name = event.get("source_name", "")
    device_name = event.get("device_name")
    user_name = event.get("user_name") or source_name
    room_name = event.get("room_name")

    # Map actions directly to French messages
    action_messages = {
        # Arming/Disarming (from SQS events)
        "arm": "Armement",
        "armed": "Armement",
        "disarm": "Désarmement",
        "disarmed": "Désarmement",
        "nightmodeon": "Mode nuit activé",
        "nightmodeoff": "Mode nuit désactivé",
        "night_mode": "Mode nuit activé",
        "night_mode_on": "Mode nuit activé",
        "night_mode_off": "Mode nuit désactivé",
        "partiallyarmed": "Armement partiel",
        "partially_armed": "Armement partiel",
        # Alarms
        "motion_detected": "Mouvement détecté",
        "door_opened": "Porte ouverte",
        "door_closed": "Porte fermée",
        "glass_break_detected": "Bris de glace détecté",
        "smoke_detected": "Fumée détectée",
        "leak_detected": "Fuite d'eau détectée",
        "tamper": "Sabotage détecté",
        "tampered": "Sabotage détecté",
        "panic": "Alarme panique",
        # Device status
        "online": "Appareil en ligne",
        "offline": "Appareil hors ligne",
        "low_battery": "Batterie faible",
        "external_power_on": "Alimentation connectée",
        "external_power_off": "Alimentation déconnectée",
    }

    # Get message from action (case-insensitive)
    action_lower = action.lower() if action else ""
    message = action_messages.get(action_lower, action or event_type or "Événement")

    parts = [message]
    if device_name and device_name.strip():
        parts.append(f"- {device_name.strip()}")
    if room_name and room_name.strip():
        parts.append(f"({room_name.strip()})")
    if user_name and user_name.strip():
        parts.append(f"par {user_name.strip()}")

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
    """Format timestamp as 'X minutes ago' in French."""
    now = datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    diff = now - timestamp
    seconds = diff.total_seconds()

    if seconds < 60:
        return "À l'instant"
    elif seconds < 3600:
        minutes = int(seconds / 60)
        return f"Il y a {minutes} min"
    elif seconds < 86400:
        hours = int(seconds / 3600)
        return f"Il y a {hours}h"
    else:
        days = int(seconds / 86400)
        return f"Il y a {days}j"


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
        icon="mdi:devices",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: len(space.devices),
    ),
    AjaxSpaceSensorDescription(
        key="online_devices",
        translation_key="online_devices",
        icon="mdi:check-network",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: len(space.get_online_devices()),
    ),
    AjaxSpaceSensorDescription(
        key="devices_with_malfunctions",
        translation_key="devices_with_malfunctions",
        icon="mdi:alert-circle",
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
        icon="mdi:shield-off",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: len(space.get_bypassed_devices()),
    ),
    AjaxSpaceSensorDescription(
        key="recent_events",
        translation_key="recent_events",
        icon="mdi:bell-ring",
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
    AjaxSpaceSensorDescription(
        key="hub_tamper",
        translation_key="hub_tamper",
        icon="mdi:lock-open-alert",
        value_fn=lambda space: "open"
        if space.hub_details.get("tampered")
        else "closed"
        if space.hub_details
        else None,
    ),
    AjaxSpaceSensorDescription(
        key="hub_external_power",
        translation_key="hub_external_power",
        icon="mdi:power-plug",
        value_fn=lambda space: "connected"
        if space.hub_details.get("externallyPowered")
        else "disconnected"
        if space.hub_details
        else None,
    ),
    AjaxSpaceSensorDescription(
        key="hub_ethernet_ip",
        translation_key="hub_ethernet_ip",
        icon="mdi:ethernet",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: space.hub_details.get("ethernet", {}).get("ip")
        if space.hub_details and space.hub_details.get("ethernet", {}).get("enabled")
        else None,
    ),
    AjaxSpaceSensorDescription(
        key="hub_wifi",
        translation_key="hub_wifi",
        icon="mdi:wifi",
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
        icon="mdi:sim",
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
        icon="mdi:brightness-6",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: space.hub_details.get("ledBrightnessLevel")
        if space.hub_details
        else None,
    ),
    AjaxSpaceSensorDescription(
        key="hub_timezone",
        translation_key="hub_timezone",
        icon="mdi:clock-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: format_timezone(space.hub_details.get("timeZone"))
        if space.hub_details
        else None,
    ),
    # Rooms count
    AjaxSpaceSensorDescription(
        key="hub_rooms",
        translation_key="hub_rooms",
        icon="mdi:door",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: len(space.rooms) if space.rooms else 0,
    ),
    # Users count
    AjaxSpaceSensorDescription(
        key="hub_users",
        translation_key="hub_users",
        icon="mdi:account-group",
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
        icon="mdi:shield-check",
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
        icon="mdi:access-point-network",
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
        icon="mdi:timer-outline",
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
        icon="mdi:timer-alert-outline",
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
        icon="mdi:signal-off",
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
        icon="mdi:counter",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: f"{len(space.devices)}/{space.hub_details.get('limits', {}).get('sensors', 0)} capteurs"
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
    DeviceType.WIRE_INPUT: DoorContactHandler,
    DeviceType.SMOKE_DETECTOR: SmokeDetectorHandler,
    DeviceType.FLOOD_DETECTOR: FloodDetectorHandler,
    DeviceType.GLASS_BREAK: GlassBreakHandler,
    DeviceType.SOCKET: SocketHandler,
    DeviceType.RELAY: SocketHandler,
    DeviceType.WALLSWITCH: SocketHandler,
    DeviceType.SIREN: SirenHandler,
    DeviceType.SPEAKERPHONE: SirenHandler,
    DeviceType.MULTI_TRANSMITTER: SirenHandler,
    DeviceType.KEYPAD: SirenHandler,
    DeviceType.HUB: HubHandler,
}


# ==============================================================================
# Setup
# ==============================================================================


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax sensors from a config entry."""
    coordinator: AjaxDataCoordinator = hass.data[DOMAIN][entry.entry_id]

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
        entry: ConfigEntry,
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

        hub_display_name = (
            "Ajax Hub" if space.name == "Hub" else f"Ajax Hub - {space.name}"
        )
        device_info = {
            "identifiers": {(DOMAIN, self._space_id)},
            "name": hub_display_name,
            "manufacturer": "Ajax Systems",
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
        self._attr_translation_key = sensor_desc.get("translation_key", sensor_key)

        if "device_class" in sensor_desc:
            self._attr_device_class = sensor_desc["device_class"]

        if "native_unit_of_measurement" in sensor_desc:
            self._attr_native_unit_of_measurement = sensor_desc[
                "native_unit_of_measurement"
            ]

        if "state_class" in sensor_desc:
            self._attr_state_class = sensor_desc["state_class"]

        if "icon" in sensor_desc:
            self._attr_icon = sensor_desc["icon"]

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

        device_display_name = (
            f"{device.room_name} - {device.name}" if device.room_name else device.name
        )

        info = {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"Ajax {device_display_name}",
            "manufacturer": "Ajax Systems",
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
