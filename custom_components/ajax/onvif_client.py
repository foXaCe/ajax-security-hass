"""ONVIF client for Ajax video edge devices.

This module provides a simplified ONVIF client for connecting to Ajax cameras
and receiving AI detection events (human, vehicle, pet) locally.

Benefits over cloud API:
- Works even when alarm is DISARMED
- 100% local, no cloud dependency for detections
- Real-time events via ONVIF PullPoint subscription
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from onvif import ONVIFCamera, __file__ as onvif_path
from onvif.exceptions import ONVIFError
from zeep.exceptions import Fault

# Get WSDL directory from onvif package location
WSDL_DIR = os.path.join(os.path.dirname(onvif_path), "wsdl")

if TYPE_CHECKING:
    from .models import AjaxVideoEdge

_LOGGER = logging.getLogger(__name__)

# ONVIF ports - Ajax cameras use 8080 for ONVIF
DEFAULT_ONVIF_PORT = 8080

# PullPoint subscription settings
SUBSCRIPTION_TIME = timedelta(minutes=10)
PULLPOINT_POLL_TIMEOUT = timedelta(seconds=60)  # Long poll timeout
PULLPOINT_POLL_INTERVAL = 0.5  # Short delay between polls (long poll handles waiting)

# Ajax-specific ONVIF event topics
AJAX_OBJECT_DETECTION_TOPIC = "tns1:RuleEngine/ObjectDetection/Object"
AJAX_MOTION_DETECTION_TOPIC = "tns1:RuleEngine/tnsajax:MotionDetector/Detection"
AJAX_LINE_CROSSING_TOPIC = "tns1:RuleEngine/tnsajax:LineDetector/Crossing"
AJAX_RING_DETECTION_TOPIC = "tns1:RuleEngine/RingDetector/Detection"


@dataclass
class OnvifDetectionEvent:
    """Represents an ONVIF detection event from Ajax camera."""

    video_edge_id: str
    channel_id: str
    detection_type: str  # VIDEO_HUMAN, VIDEO_VEHICLE, VIDEO_PET, VIDEO_MOTION
    active: bool
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __str__(self) -> str:
        return f"OnvifDetectionEvent({self.detection_type}, active={self.active})"


class AjaxOnvifClient:
    """ONVIF client for a single Ajax camera."""

    def __init__(
        self,
        video_edge: AjaxVideoEdge,
        username: str,
        password: str,
        event_callback: Callable[[OnvifDetectionEvent], None] | None = None,
    ) -> None:
        """Initialize the ONVIF client.

        Args:
            video_edge: The Ajax video edge device
            username: ONVIF username
            password: ONVIF password
            event_callback: Callback function for detection events
        """
        self.video_edge = video_edge
        self._username = username
        self._password = password
        self._event_callback = event_callback

        self._camera: ONVIFCamera | None = None
        self._pullpoint_manager: Any = None
        self._running = False
        self._poll_task: asyncio.Task | None = None
        self._last_states: dict[str, bool] = {}  # Cache to filter duplicate events

    @property
    def connected(self) -> bool:
        """Return True if connected to the camera."""
        return self._camera is not None and self._pullpoint_manager is not None

    async def async_connect(self) -> bool:
        """Connect to the camera via ONVIF.

        Returns:
            True if connection successful, False otherwise
        """
        if not self.video_edge.ip_address:
            _LOGGER.warning(
                "Cannot connect to %s: no IP address",
                self.video_edge.name,
            )
            return False

        try:
            _LOGGER.info(
                "ONVIF: Connecting to %s at %s:%s (user=%s)",
                self.video_edge.name,
                self.video_edge.ip_address,
                DEFAULT_ONVIF_PORT,
                self._username,
            )

            # Create ONVIF camera instance with correct WSDL path
            self._camera = ONVIFCamera(
                self.video_edge.ip_address,
                DEFAULT_ONVIF_PORT,
                self._username,
                self._password,
                wsdl_dir=WSDL_DIR,
            )

            _LOGGER.info("ONVIF: Camera instance created, updating xaddrs...")

            # Update camera services
            await self._camera.update_xaddrs()

            _LOGGER.info(
                "ONVIF: Successfully connected to %s",
                self.video_edge.name,
            )
            return True

        except (ONVIFError, Fault, TimeoutError, OSError) as err:
            _LOGGER.error(
                "ONVIF: Failed to connect to %s at %s:%s - %s: %s",
                self.video_edge.name,
                self.video_edge.ip_address,
                DEFAULT_ONVIF_PORT,
                type(err).__name__,
                err,
            )
            self._camera = None
            return False

    async def async_subscribe_events(self) -> bool:
        """Subscribe to ONVIF events via PullPoint Manager.

        Returns:
            True if subscription successful, False otherwise
        """
        if not self._camera:
            return False

        try:
            _LOGGER.info("ONVIF: Creating PullPoint subscription for %s...", self.video_edge.name)

            # Create PullPoint manager (like HA's official ONVIF integration)
            # subscription_lost_callback is called when subscription expires
            self._pullpoint_manager = await self._camera.create_pullpoint_manager(
                SUBSCRIPTION_TIME,
                self._on_subscription_lost,
            )

            _LOGGER.info("ONVIF: PullPoint manager created, setting sync point...")

            # Set synchronization point to get current state
            await self._pullpoint_manager.set_synchronization_point()

            _LOGGER.info(
                "ONVIF: PullPoint subscription active for %s",
                self.video_edge.name,
            )
            return True

        except (ONVIFError, Fault, TimeoutError, OSError) as err:
            _LOGGER.error(
                "ONVIF: Failed to create PullPoint subscription for %s - %s: %s",
                self.video_edge.name,
                type(err).__name__,
                err,
            )
            return False

    def _on_subscription_lost(self) -> None:
        """Handle subscription lost event.

        Called by the pullpoint manager when the subscription expires or is lost.
        """
        _LOGGER.warning(
            "%s: ONVIF subscription lost, will recreate on next poll",
            self.video_edge.name,
        )

    async def async_start_polling(self) -> None:
        """Start polling for events."""
        if self._running:
            return

        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())

        _LOGGER.info(
            "ONVIF: Started event polling for %s",
            self.video_edge.name,
        )

    async def async_stop(self) -> None:
        """Stop polling and disconnect."""
        self._running = False

        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None

        # Shutdown pullpoint manager (handles unsubscribe)
        if self._pullpoint_manager and not self._pullpoint_manager.closed:
            with contextlib.suppress(Exception):
                await self._pullpoint_manager.shutdown()

        self._pullpoint_manager = None
        self._camera = None

        _LOGGER.debug(
            "%s: Stopped ONVIF client",
            self.video_edge.name,
        )

    async def _poll_loop(self) -> None:
        """Poll for events continuously."""
        while self._running:
            try:
                await self._pull_messages()
            except asyncio.CancelledError:
                break
            except Exception as err:
                _LOGGER.debug(
                    "%s: Error polling ONVIF events: %s",
                    self.video_edge.name,
                    err,
                )

            await asyncio.sleep(PULLPOINT_POLL_INTERVAL)

    async def _pull_messages(self) -> None:
        """Pull messages from the PullPoint."""
        if not self._pullpoint_manager or self._pullpoint_manager.closed:
            return

        try:
            # Get the service from the manager
            service = self._pullpoint_manager.get_service()

            response = await service.PullMessages(
                {
                    "MessageLimit": 100,
                    "Timeout": timedelta(seconds=30),
                }
            )

            if response and hasattr(response, "NotificationMessage"):
                messages = response.NotificationMessage
                if messages:
                    _LOGGER.debug("ONVIF: Received %d message(s) from %s", len(messages), self.video_edge.name)
                    for msg in messages:
                        await self._process_message(msg)

        except (ONVIFError, Fault, TimeoutError) as err:
            # Timeout is normal when no events
            if "timeout" not in str(err).lower():
                _LOGGER.debug(
                    "%s: PullMessages error: %s",
                    self.video_edge.name,
                    err,
                )

    async def _process_message(self, msg: Any) -> None:
        """Process an ONVIF notification message.

        Args:
            msg: The ONVIF notification message
        """
        try:
            # Extract topic
            topic = None
            if hasattr(msg, "Topic") and msg.Topic:
                topic_value = msg.Topic._value_1
                if topic_value:
                    topic = str(topic_value)

            if not topic:
                return

            # Extract message data
            message_data = None
            if hasattr(msg, "Message") and msg.Message:
                message_data = msg.Message._value_1

            if not message_data:
                return

            # Extract source info (channel)
            channel_id = self._extract_channel_id(message_data)

            # Parse based on topic
            event = self._parse_event(topic, message_data, channel_id)

            if event and self._event_callback:
                # Filter duplicate events using state cache
                state_key = f"{event.channel_id}_{event.detection_type}"
                last_state = self._last_states.get(state_key)

                if last_state != event.active:
                    # State changed, update cache and notify
                    self._last_states[state_key] = event.active
                    _LOGGER.debug(
                        "%s: ONVIF %s %s (channel %s)",
                        self.video_edge.name,
                        event.detection_type,
                        "active" if event.active else "cleared",
                        event.channel_id,
                    )
                    self._event_callback(event)

        except Exception as err:
            _LOGGER.debug(
                "%s: Error processing ONVIF message: %s",
                self.video_edge.name,
                err,
            )

    def _extract_channel_id(self, message_data: Any) -> str:
        """Extract channel ID from message source."""
        channel_id = "0"
        try:
            if hasattr(message_data, "Source") and message_data.Source:
                source = message_data.Source
                if hasattr(source, "SimpleItem"):
                    for item in source.SimpleItem:
                        if hasattr(item, "Name") and item.Name == "VideoSourceToken":
                            # Format: 9c756e1c8456-0 -> extract channel number
                            value = str(item.Value) if hasattr(item, "Value") else ""
                            if "-" in value:
                                channel_id = value.split("-")[-1]
                            break
        except Exception:
            pass
        return channel_id

    def _parse_event(self, topic: str, message_data: Any, channel_id: str) -> OnvifDetectionEvent | None:
        """Parse ONVIF event into detection event.

        Args:
            topic: The event topic
            message_data: The message data
            channel_id: The channel ID

        Returns:
            OnvifDetectionEvent if parsed successfully, None otherwise
        """
        try:
            # Extract data items
            data_items = {}
            if hasattr(message_data, "Data") and message_data.Data:
                data = message_data.Data
                if hasattr(data, "SimpleItem"):
                    for item in data.SimpleItem:
                        name = getattr(item, "Name", None)
                        value = getattr(item, "Value", None)
                        if name and value is not None:
                            data_items[str(name)] = str(value)

            # Parse Ajax Object Detection (Human, Vehicle, Pet)
            # Topic: tns1:RuleEngine/ObjectDetection/Object
            # Data: ClassTypes = Human/Vehicle/dog/cat
            if "ObjectDetection/Object" in topic:
                class_type = data_items.get("ClassTypes", "").lower()
                detection_type = None

                if class_type in ("human", "person"):
                    detection_type = "VIDEO_HUMAN"
                elif class_type == "vehicle":
                    detection_type = "VIDEO_VEHICLE"
                elif class_type in ("dog", "cat", "pet"):
                    detection_type = "VIDEO_PET"

                if detection_type:
                    return OnvifDetectionEvent(
                        video_edge_id=self.video_edge.id,
                        channel_id=channel_id,
                        detection_type=detection_type,
                        active=True,
                    )
                # Empty ClassTypes = no active detection, ignore
                return None

            # Parse Ajax Motion Detection
            # Topic: tns1:RuleEngine/tnsajax:MotionDetector/Detection
            # Data: Detected = true/false
            if "MotionDetector/Detection" in topic:
                detected = data_items.get("Detected", "false").lower() == "true"
                return OnvifDetectionEvent(
                    video_edge_id=self.video_edge.id,
                    channel_id=channel_id,
                    detection_type="VIDEO_MOTION",
                    active=detected,
                )

            # Parse VideoSource/MotionAlarm (alternative motion detection topic)
            # Topic: tns1:VideoSource/MotionAlarm
            # Data: State = true/false
            if "VideoSource/MotionAlarm" in topic:
                state = data_items.get("State", "false").lower() == "true"
                return OnvifDetectionEvent(
                    video_edge_id=self.video_edge.id,
                    channel_id=channel_id,
                    detection_type="VIDEO_MOTION",
                    active=state,
                )

            # Parse Ajax Line Crossing Detection
            # Topic: tns1:RuleEngine/tnsajax:LineDetector/Crossing
            # Data: Crossed = true/false (or similar)
            if "LineDetector/Crossing" in topic:
                crossed = data_items.get("Crossed", data_items.get("State", "false")).lower() == "true"
                return OnvifDetectionEvent(
                    video_edge_id=self.video_edge.id,
                    channel_id=channel_id,
                    detection_type="VIDEO_LINE_CROSSING",
                    active=crossed,
                )

            # Parse Ajax Doorbell Ring
            # Topic: tns1:RuleEngine/RingDetector/Detection
            # Data: Detected = true/false
            if "RingDetector/Detection" in topic:
                detected = data_items.get("Detected", "false").lower() == "true"
                return OnvifDetectionEvent(
                    video_edge_id=self.video_edge.id,
                    channel_id=channel_id,
                    detection_type="DOORBELL_RING",
                    active=detected,
                )

        except Exception as err:
            _LOGGER.debug(
                "%s: Error parsing ONVIF event: %s",
                self.video_edge.name,
                err,
            )

        return None
