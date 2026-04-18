"""Ajax device type handlers.

This module organizes device handlers by Ajax device type for easier maintenance
and extensibility. Each device type (MotionProtect, DoorProtect, FireProtect, etc.)
has its own module that defines which entities it should create.

It also provides centralized device-type-to-handler mapping (DEVICE_HANDLERS)
and helper functions (get_device_handler, is_dimmer_device) used by all entity
platforms (binary_sensor, sensor, switch, light, etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import AjaxDeviceHandler
from .button import ButtonHandler
from .dimmer import DimmerHandler
from .door_contact import DoorContactHandler, WireInputHandler
from .doorbell import DoorbellHandler
from .flood_detector import FloodDetectorHandler
from .glass_break import GlassBreakHandler
from .hub import HubHandler
from .life_quality import LifeQualityHandler
from .light import LightHandler
from .lightswitch import LightSwitchHandler
from .manual_call_point import ManualCallPointHandler
from .motion_detector import MotionDetectorHandler
from .repeater import RepeaterHandler
from .siren import SirenHandler
from .smoke_detector import SmokeDetectorHandler
from .socket import SocketHandler
from .transmitter import TransmitterHandler
from .video_edge import VideoEdgeHandler
from .waterstop import WaterStopHandler

if TYPE_CHECKING:
    from ..models import AjaxDevice

from ..models import DeviceType

# Canonical mapping of DeviceType → handler class.
# Used by binary_sensor, sensor, switch, and other entity platforms.
DEVICE_HANDLERS: dict[DeviceType, type[AjaxDeviceHandler]] = {
    DeviceType.MOTION_DETECTOR: MotionDetectorHandler,
    DeviceType.COMBI_PROTECT: MotionDetectorHandler,
    DeviceType.DOOR_CONTACT: DoorContactHandler,
    DeviceType.WIRE_INPUT: WireInputHandler,
    DeviceType.SMOKE_DETECTOR: SmokeDetectorHandler,
    DeviceType.FLOOD_DETECTOR: FloodDetectorHandler,
    DeviceType.MANUAL_CALL_POINT: ManualCallPointHandler,
    DeviceType.GLASS_BREAK: GlassBreakHandler,
    DeviceType.SOCKET: SocketHandler,
    DeviceType.RELAY: SocketHandler,
    DeviceType.WALLSWITCH: SocketHandler,
    DeviceType.SIREN: SirenHandler,
    DeviceType.SPEAKERPHONE: SirenHandler,
    DeviceType.TRANSMITTER: TransmitterHandler,
    DeviceType.MULTI_TRANSMITTER: TransmitterHandler,
    DeviceType.KEYPAD: ButtonHandler,
    DeviceType.BUTTON: ButtonHandler,
    DeviceType.REMOTE_CONTROL: ButtonHandler,
    DeviceType.DOORBELL: DoorbellHandler,
    DeviceType.REPEATER: RepeaterHandler,
    DeviceType.HUB: HubHandler,
    DeviceType.WATERSTOP: WaterStopHandler,
    DeviceType.LIFE_QUALITY: LifeQualityHandler,
}

DIMMER_RAW_TYPES = {"lightswitchdimmer", "light_switch_dimmer"}


def is_dimmer_device(device: AjaxDevice) -> bool:
    """Check if device is a LightSwitchDimmer."""
    raw_type = (device.raw_type or "").lower().replace("_", "")
    return raw_type in DIMMER_RAW_TYPES or "dimmer" in raw_type


def get_device_handler(device: AjaxDevice) -> type[AjaxDeviceHandler] | None:
    """Get the appropriate handler for a device.

    Returns DimmerHandler for dimmer devices, or the standard handler for the
    device type.  Returns None if no handler is registered.
    """
    if is_dimmer_device(device):
        return DimmerHandler
    return DEVICE_HANDLERS.get(device.type)


__all__ = [
    "AjaxDeviceHandler",
    "ButtonHandler",
    "DEVICE_HANDLERS",
    "DIMMER_RAW_TYPES",
    "DimmerHandler",
    "DoorbellHandler",
    "DoorContactHandler",
    "FloodDetectorHandler",
    "GlassBreakHandler",
    "HubHandler",
    "LifeQualityHandler",
    "LightHandler",
    "LightSwitchHandler",
    "ManualCallPointHandler",
    "MotionDetectorHandler",
    "RepeaterHandler",
    "SirenHandler",
    "SmokeDetectorHandler",
    "SocketHandler",
    "TransmitterHandler",
    "VideoEdgeHandler",
    "WaterStopHandler",
    "WireInputHandler",
    "get_device_handler",
    "is_dimmer_device",
]
