"""Repair issues for Ajax integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


def async_create_firmware_issue(
    hass: HomeAssistant,
    device_id: str,
    device_name: str,
    current_version: str,
    new_version: str,
    is_critical: bool = False,
) -> None:
    """Create a repair issue for firmware update available.

    Args:
        hass: Home Assistant instance
        device_id: Device ID
        device_name: Human-readable device name
        current_version: Current firmware version
        new_version: Available firmware version
        is_critical: Whether this is a critical security update
    """
    issue_id = f"firmware_update_{device_id}"
    severity = ir.IssueSeverity.CRITICAL if is_critical else ir.IssueSeverity.WARNING
    translation_key = "critical_firmware_update" if is_critical else "firmware_update"

    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,  # User must update via Ajax app
        severity=severity,
        translation_key=translation_key,
        translation_placeholders={
            "device_name": device_name,
            "current_version": current_version,
            "new_version": new_version,
        },
    )


def async_delete_firmware_issue(hass: HomeAssistant, device_id: str) -> None:
    """Delete a firmware update repair issue.

    Args:
        hass: Home Assistant instance
        device_id: Device ID
    """
    ir.async_delete_issue(hass, DOMAIN, f"firmware_update_{device_id}")


def async_create_offline_issue(
    hass: HomeAssistant,
    device_id: str,
    device_name: str,
) -> None:
    """Create a repair issue for device offline.

    Args:
        hass: Home Assistant instance
        device_id: Device ID
        device_name: Human-readable device name
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"device_offline_{device_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="device_offline",
        translation_placeholders={
            "device_name": device_name,
        },
    )


def async_delete_offline_issue(hass: HomeAssistant, device_id: str) -> None:
    """Delete a device offline repair issue.

    Args:
        hass: Home Assistant instance
        device_id: Device ID
    """
    ir.async_delete_issue(hass, DOMAIN, f"device_offline_{device_id}")
