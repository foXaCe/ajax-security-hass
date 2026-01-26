"""Ajax device type handlers.

This module organizes device handlers by Ajax device type for easier maintenance
and extensibility. Each device type (MotionProtect, DoorProtect, FireProtect, etc.)
has its own module that defines which entities it should create.

Structure:
- base.py: Base device handler class
- motion_detector.py: MotionProtect, MotionProtect Plus, CombiProtect
- door_contact.py: DoorProtect, DoorProtect Plus
- smoke_detector.py: FireProtect, FireProtect Plus, FireProtect 2
- flood_detector.py: LeaksProtect
- manual_call_point.py: ManualCallPoint (fire alarm button)
- socket.py: Socket, Relay, WallSwitch
- dimmer.py: LightSwitchDimmer (dimmable wall switch)
- hub.py: Hub/Hub 2/Hub Plus with alarm control panel
- keypad.py: KeyPad, KeyPad Plus
- button.py: Button, DoubleButton, SpaceControl
- repeater.py: Rex, Rex2 range extenders
- life_quality.py: LifeQuality air quality sensor
- waterstop.py: WaterStop smart water valve
"""

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
from .manual_call_point import ManualCallPointHandler
from .motion_detector import MotionDetectorHandler
from .repeater import RepeaterHandler
from .siren import SirenHandler
from .smoke_detector import SmokeDetectorHandler
from .socket import SocketHandler
from .transmitter import TransmitterHandler
from .video_edge import VideoEdgeHandler
from .waterstop import WaterStopHandler

__all__ = [
    "AjaxDeviceHandler",
    "ButtonHandler",
    "DimmerHandler",
    "DoorbellHandler",
    "DoorContactHandler",
    "FloodDetectorHandler",
    "GlassBreakHandler",
    "HubHandler",
    "LifeQualityHandler",
    "LightHandler",
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
]
