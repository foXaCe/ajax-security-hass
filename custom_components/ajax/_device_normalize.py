"""Stateless device-attribute helpers for ``AjaxDataCoordinator``.

Carved out of ``_coordinator_devices`` so the reconciliation pipeline there
stays focused on orchestration. These two methods touch no coordinator state:

* ``_normalize_device_attributes``: maps raw Ajax field names (``reedClosed``,
  ``switchState``, ``glassBreakDetected``, …) to the internal shapes the device
  handlers expect.
* ``_reset_expired_motion_detections``: clears the impulse-based
  ``motion_detected`` flag after the 30 s no-event window.

``AjaxDevicesMixin`` inherits this mixin, so ``self._normalize_device_attributes``
keeps working unchanged for every existing caller.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from .models import AjaxSpace, DeviceType

_LOGGER = logging.getLogger(__name__)


class AjaxDeviceNormalizeMixin:
    """Stateless device-attribute normalisation + motion-impulse expiry."""

    def _normalize_device_attributes(self, api_attributes: dict[str, Any], device_type: DeviceType) -> dict[str, Any]:
        """Normalize Ajax API attributes to internal format.

        The Ajax API uses specific attribute names (e.g., reedClosed) that we
        normalize to more intuitive names (e.g., door_opened) for internal use.

        Args:
            api_attributes: Raw attributes from Ajax API
            device_type: Type of device

        Returns:
            Normalized attributes dict
        """
        normalized = dict(api_attributes)  # Start with original attributes

        # Door contacts: Support both API formats
        if device_type in [DeviceType.DOOR_CONTACT, DeviceType.WIRE_INPUT]:
            # If API already provides door_opened, use it directly
            if "door_opened" not in api_attributes and "reedClosed" in api_attributes:
                # Convert reedClosed (False=open) to door_opened (True=open)
                # Invert the logic: reedClosed=False means door is open
                normalized["door_opened"] = not api_attributes["reedClosed"]

            # MultiTransmitterWireInput uses externalContactState
            if "door_opened" not in normalized and "externalContactState" in api_attributes:
                ext_state = api_attributes["externalContactState"]
                # externalContactState: "OK" = closed, "TRIGGERED" = open
                door_opened = ext_state != "OK"

                # Check wiringSchemeSpecificDetails for more accurate state
                wiring_details = api_attributes.get("wiringSchemeSpecificDetails", {})
                wiring_type = wiring_details.get("wiringSchemeType")

                # Store wiring type for handler to know if tamper sensor is available
                if wiring_type:
                    normalized["wiring_type"] = wiring_type

                if wiring_type == "TWO_EOL":
                    # TWO_EOL: contactTwoDetails is the door contact, contactOneDetails is tamper
                    contact_one = wiring_details.get("contactOneDetails", {})
                    contact_two = wiring_details.get("contactTwoDetails", {})
                    contact_state = contact_two.get("contactState")
                    if contact_state:
                        door_opened = contact_state != "OK"
                    # Parse tamper from contactOneDetails
                    tamper_state = contact_one.get("contactState")
                    if tamper_state:
                        normalized["tampered"] = tamper_state != "OK"
                elif wiring_type == "ONE_EOL":
                    # OR logic: open if externalContactState OR contactDetails says TRIGGERED
                    contact_details = wiring_details.get("contactDetails", {})
                    contact_state = contact_details.get("contactState")
                    if contact_state and contact_state != "OK":
                        door_opened = True
                elif wiring_type == "NO_EOL":
                    # OR logic: open if externalContactState OR contactState says TRIGGERED
                    contact_state = wiring_details.get("contactState")
                    if contact_state and contact_state != "OK":
                        door_opened = True

                normalized["door_opened"] = door_opened

            # External contact: Support both formats
            if "external_contact_opened" not in api_attributes and "extraContactClosed" in api_attributes:
                # Convert extraContactClosed to external_contact_opened
                # extraContactClosed=False means contact is open (alarm state)
                normalized["external_contact_opened"] = not api_attributes["extraContactClosed"]

        # Motion detectors: Support both camelCase and snake_case
        if device_type == DeviceType.MOTION_DETECTOR:
            if "motion_detected" not in api_attributes and "motionDetected" in api_attributes:
                normalized["motion_detected"] = api_attributes["motionDetected"]
            if "motion_detected_at" not in api_attributes and "motionDetectedAt" in api_attributes:
                normalized["motion_detected_at"] = api_attributes["motionDetectedAt"]

        # Smoke detectors: Support both formats
        if (
            device_type == DeviceType.SMOKE_DETECTOR
            and "smoke_detected" not in api_attributes
            and "smokeDetected" in api_attributes
        ):
            normalized["smoke_detected"] = api_attributes["smokeDetected"]

        # Flood detectors: Support both formats
        if (
            device_type == DeviceType.FLOOD_DETECTOR
            and "leak_detected" not in api_attributes
            and "leakDetected" in api_attributes
        ):
            normalized["leak_detected"] = api_attributes["leakDetected"]

        # Glass break detectors: Support both formats
        if (
            device_type == DeviceType.GLASS_BREAK
            and "glass_break_detected" not in api_attributes
            and "glassBreakDetected" in api_attributes
        ):
            normalized["glass_break_detected"] = api_attributes["glassBreakDetected"]

        # Socket/Relay/WallSwitch: Parse switchState to is_on
        if (
            device_type in (DeviceType.SOCKET, DeviceType.RELAY, DeviceType.WALLSWITCH)
            and "switchState" in api_attributes
        ):
            switch_state = api_attributes["switchState"]
            # switchState is a list: [] = on, ["SWITCHED_OFF"] = off
            # Device is OFF only if SWITCHED_OFF is explicitly in the list
            if isinstance(switch_state, list):
                normalized["is_on"] = "SWITCHED_OFF" not in switch_state
            else:
                normalized["is_on"] = True

        # Manual Call Point (MCP): Parse button state and attributes
        if device_type == DeviceType.MANUAL_CALL_POINT:
            # switchState for MCP is a string: "BUTTON_UNPRESSED" or "BUTTON_PRESSED"
            if "switchState" in api_attributes:
                normalized["switchState"] = api_attributes["switchState"]
            # customEvent indicates the alarm type (e.g., "FIRE_ALARM")
            if "customEvent" in api_attributes:
                normalized["customEvent"] = api_attributes["customEvent"]
            # Device color (RED, BLUE, etc.)
            if "color" in api_attributes:
                normalized["color"] = api_attributes["color"]
            # Self-monitoring config
            if "selfMonitoringConfig" in api_attributes:
                normalized["selfMonitoringConfig"] = api_attributes["selfMonitoringConfig"]

        # Note: LightSwitch multi-gang (channelStatuses, buttonOne, buttonTwo)
        # is parsed directly from device_data in _update_devices since these
        # fields are at root level, not inside "attributes"

        return normalized

    def _reset_expired_motion_detections(self, space: AjaxSpace) -> None:
        """Reset motion_detected to False for motion detectors if no recent detection.

        Motion detection events are impulse-based (not persistent state),
        so we reset them after 30 seconds of no new detection.

        Args:
            space: The AjaxSpace to process
        """
        now = datetime.now(UTC)
        expiry_seconds = 30  # Reset motion detection after 30 seconds

        for device in space.devices.values():
            # Only process motion detectors
            if device.type != DeviceType.MOTION_DETECTOR:
                continue

            # Check if motion_detected is currently True
            if not device.attributes.get("motion_detected"):
                continue

            # Get last detection time
            last_detected_at = device.attributes.get("motion_detected_at")
            if not last_detected_at:
                device.attributes["motion_detected"] = False
                continue

            # Parse timestamp
            try:
                last_detected = datetime.fromisoformat(last_detected_at)
                if last_detected.tzinfo is None:
                    last_detected = last_detected.replace(tzinfo=UTC)

                if (now - last_detected).total_seconds() > expiry_seconds:
                    device.attributes["motion_detected"] = False
            except (ValueError, TypeError) as err:
                # Drop the bad value so we stop re-trying (and re-logging)
                # every tick. The next motion event will repopulate it.
                device.attributes.pop("motion_detected_at", None)
                device.attributes["motion_detected"] = False
                _LOGGER.debug(
                    "Discarded unparsable motion_detected_at for %s: %s",
                    device.name,
                    err,
                )
