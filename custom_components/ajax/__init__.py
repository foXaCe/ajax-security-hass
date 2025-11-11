"""The Ajax Security System integration."""
from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .api import AjaxApi, AjaxApiError, AjaxAuthError
from .const import DOMAIN
from .coordinator import AjaxDataCoordinator

if TYPE_CHECKING:
    from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Ajax Security System component."""
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry to new version."""
    _LOGGER.info("Migrating Ajax config entry from version %s", entry.version)

    if entry.version == 1:
        # Version 1 -> 2: Hash the password if it's not already hashed
        new_data = {**entry.data}
        password = new_data.get(CONF_PASSWORD, "")

        # Check if password is already a SHA256 hash (64 hex characters)
        if len(password) != 64 or not all(c in "0123456789abcdef" for c in password.lower()):
            # Password is plain text, hash it
            password_hash = hashlib.sha256(password.encode()).hexdigest()
            new_data[CONF_PASSWORD] = password_hash
            _LOGGER.info("Migrated password to SHA256 hash for security")
        else:
            _LOGGER.info("Password already appears to be hashed, skipping migration")

        hass.config_entries.async_update_entry(entry, data=new_data, version=2)
        _LOGGER.info("Migration to version 2 successful")

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Ajax Security System from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    email = entry.data[CONF_EMAIL]
    password = entry.data[CONF_PASSWORD]
    device_id = entry.entry_id  # Use entry ID as unique device ID

    # Create API instance
    # Password is already hashed (SHA256) in config entry
    api = AjaxApi(email=email, password=password, device_id=device_id, password_is_hashed=True)

    try:
        # Authenticate
        await api.async_login()
        _LOGGER.info("Successfully authenticated with Ajax API")

    except AjaxAuthError as err:
        _LOGGER.error("Authentication failed: %s", err)
        return False
    except AjaxApiError as err:
        _LOGGER.error("API error during setup: %s", err)
        raise ConfigEntryNotReady from err

    # Create coordinator
    coordinator = AjaxDataCoordinator(hass, api)

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    # Store coordinator
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    async def async_handle_force_arm(call: ServiceCall) -> None:
        """Handle the force_arm service call."""
        entity_id = call.data.get("entity_id")
        if not entity_id:
            _LOGGER.error("No entity_id provided for force_arm service")
            return

        # Get the alarm control panel entity
        entity_registry = hass.helpers.entity_registry.async_get(hass)
        entity_entry = entity_registry.async_get(entity_id)

        if not entity_entry:
            _LOGGER.error("Entity %s not found", entity_id)
            return

        # Get the space_id from the entity's unique_id
        # Format: {entry_id}_alarm_{space_id}
        unique_id_parts = entity_entry.unique_id.split("_")
        if len(unique_id_parts) < 3 or unique_id_parts[1] != "alarm":
            _LOGGER.error("Invalid entity unique_id format: %s", entity_entry.unique_id)
            return

        space_id = unique_id_parts[2]

        # Call coordinator method with force=True
        try:
            await coordinator.async_arm_space(space_id, force=True)
        except Exception as err:
            _LOGGER.error("Failed to force arm: %s", err)

    async def async_handle_force_arm_night(call: ServiceCall) -> None:
        """Handle the force_arm_night service call."""
        entity_id = call.data.get("entity_id")
        if not entity_id:
            _LOGGER.error("No entity_id provided for force_arm_night service")
            return

        # Get the alarm control panel entity
        entity_registry = hass.helpers.entity_registry.async_get(hass)
        entity_entry = entity_registry.async_get(entity_id)

        if not entity_entry:
            _LOGGER.error("Entity %s not found", entity_id)
            return

        # Get the space_id from the entity's unique_id
        unique_id_parts = entity_entry.unique_id.split("_")
        if len(unique_id_parts) < 3 or unique_id_parts[1] != "alarm":
            _LOGGER.error("Invalid entity unique_id format: %s", entity_entry.unique_id)
            return

        space_id = unique_id_parts[2]

        # Call coordinator method with force=True
        try:
            await coordinator.async_arm_night_mode(space_id, force=True)
        except Exception as err:
            _LOGGER.error("Failed to force arm night mode: %s", err)

    async def async_handle_generate_device_info(call: ServiceCall) -> None:
        """Generate device info diagnostic report (without sensitive data)."""
        import json
        from pathlib import Path

        _LOGGER.info("Generating device info diagnostic report")

        # Collect device information from all spaces
        devices_info = []

        for space_id, space in coordinator.account.spaces.items():
            for device_id, device in space.devices.items():
                # Get room name if available
                room_name = None
                if device.room_id and device.room_id in space.rooms:
                    room_name = space.rooms[device.room_id].name

                device_info = {
                    "type": device.type.value if device.type else "unknown",
                    "raw_type": device.raw_type or "unknown",
                    "has_room": bool(room_name),
                    "firmware_version": device.firmware_version or "unknown",
                    "hardware_version": device.hardware_version or "unknown",
                    "attributes": {
                        "battery": device.battery_level is not None,
                        "signal_strength": device.signal_strength is not None,
                        "temperature": device.attributes.get("temperature") is not None,
                        "humidity": device.attributes.get("humidity") is not None,
                        "co2": device.attributes.get("co2") is not None,
                        "tamper": device.attributes.get("tamper") is not None,
                        "online": device.online,
                        "bypassed": device.bypassed,
                        "malfunctions": device.malfunctions > 0,
                    },
                }

                # Add device-specific attributes based on type
                if device.type:
                    type_value = device.type.value
                    if "motion" in type_value.lower():
                        device_info["attributes"]["motion_detected"] = "motion_detected" in device.states
                    elif "door" in type_value.lower() or "contact" in type_value.lower():
                        device_info["attributes"]["opened"] = "opened" in device.states
                    elif "smoke" in type_value.lower():
                        device_info["attributes"]["smoke_detected"] = "smoke_detected" in device.states
                    elif "leak" in type_value.lower() or "water" in type_value.lower():
                        device_info["attributes"]["leak_detected"] = "leak_detected" in device.states

                # Add any additional states
                if device.states:
                    device_info["states"] = device.states

                devices_info.append(device_info)

        # Generate report
        report = {
            "total_devices": len(devices_info),
            "spaces": len(coordinator.account.spaces),
            "devices_by_type": {},
            "firmware_versions": {},
            "hardware_versions": {},
            "unknown_device_raw_types": {},  # Track raw types of unknown devices for debugging
            "devices": devices_info,
        }

        # Count by type
        for dev in devices_info:
            dev_type = dev["type"]
            report["devices_by_type"][dev_type] = report["devices_by_type"].get(dev_type, 0) + 1

            # Track raw types for unknown devices
            if dev_type == "unknown":
                raw_type = dev.get("raw_type", "unknown")
                report["unknown_device_raw_types"][raw_type] = report["unknown_device_raw_types"].get(raw_type, 0) + 1

            # Count firmware versions
            fw = dev["firmware_version"]
            if fw != "unknown":
                report["firmware_versions"][fw] = report["firmware_versions"].get(fw, 0) + 1

            # Count hardware versions
            hw = dev["hardware_version"]
            if hw != "unknown":
                report["hardware_versions"][hw] = report["hardware_versions"].get(hw, 0) + 1

        # Save to file in config directory
        config_path = Path(hass.config.config_dir)
        report_path = config_path / "ajax_device_info.json"

        def write_report():
            """Write the report to file (runs in executor)."""
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)

        try:
            await hass.async_add_executor_job(write_report)
            _LOGGER.info("Device info report saved to: %s", report_path)

            # Send persistent notification to user
            unknown_count = report["devices_by_type"].get("unknown", 0)
            message = (
                f"Device information report has been generated.\n\n"
                f"Location: {report_path}\n"
                f"Total devices: {report['total_devices']}\n"
                f"Device types: {len(report['devices_by_type'])}"
            )

            if unknown_count > 0:
                message += f"\n\n⚠️ Warning: {unknown_count} unknown device(s) detected!\n"
                message += "Raw types: " + ", ".join(report["unknown_device_raw_types"].keys())

            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Ajax Device Info Generated",
                    "message": message,
                    "notification_id": "ajax_device_info_generated",
                },
            )

        except Exception as err:
            _LOGGER.error("Failed to save device info report: %s", err)
            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Ajax Device Info Error",
                    "message": f"Failed to generate device info report: {err}",
                    "notification_id": "ajax_device_info_error",
                },
            )

    # Register services
    hass.services.async_register(
        DOMAIN,
        "force_arm",
        async_handle_force_arm,
        schema=vol.Schema({
            vol.Required("entity_id"): cv.entity_id,
        }),
    )

    hass.services.async_register(
        DOMAIN,
        "force_arm_night",
        async_handle_force_arm_night,
        schema=vol.Schema({
            vol.Required("entity_id"): cv.entity_id,
        }),
    )

    hass.services.async_register(
        DOMAIN,
        "generate_device_info",
        async_handle_generate_device_info,
        schema=vol.Schema({}),
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: AjaxDataCoordinator = hass.data[DOMAIN].pop(entry.entry_id)

        # Close API connection
        await coordinator.api.close()

        # Unregister services if this is the last config entry
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, "force_arm")
            hass.services.async_remove(DOMAIN, "force_arm_night")
            hass.services.async_remove(DOMAIN, "generate_device_info")

    return unload_ok
