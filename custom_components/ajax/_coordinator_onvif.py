"""ONVIF mixin for ``AjaxDataCoordinator``.

Owns the local-AI detection path: ONVIF subscription bootstrap
(`_async_init_onvif`), event handler that routes NVR-channel events
to the right camera and fires the matching HA entity / bus event
(`_handle_onvif_event`), and the NVR-channel-to-camera mapping helper
(`_find_camera_for_nvr_channel`).

The path is optional — ONVIF only initialises when the user has
configured RTSP/ONVIF credentials AND the ``onvif-zeep-async`` package
is installed.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import issue_registry as ir

from ._event_helpers import resolve_camera_entity_id
from .const import CONF_RTSP_PASSWORD, CONF_RTSP_USERNAME, DOMAIN, EVENT_AJAX_CAMERA_DETECTION, EVENT_AJAX_DOORBELL_RING
from .models import VideoEdgeType

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .models import AjaxAccount, AjaxSpace, AjaxVideoEdge
    from .onvif_client import OnvifDetectionEvent
    from .onvif_manager import AjaxOnvifManager

# Optional ONVIF support — mirror of the same guard the coordinator
# uses so the mixin can be imported even when the wheel is missing.
ONVIF_AVAILABLE = False
_AjaxOnvifManager: type | None = None
try:
    from .onvif_manager import AjaxOnvifManager as _AjaxOnvifManager

    ONVIF_AVAILABLE = True
except ImportError:
    pass

_LOGGER = logging.getLogger(__name__)


# Minimum delay between two ONVIF bootstrap retries from the reconcile
# path — keeps a persistently failing setup from re-attempting (and
# re-logging) on every poll cycle.
ONVIF_BOOTSTRAP_RETRY_SECONDS = 300.0


# Ajax camera types observed in NVR `sourceAliases.sources` payloads.
# Promoted to module-level so the helper does not rebuild the set on
# every event (ONVIF can fire many per second on a busy installation).
_AJAX_CAMERA_TYPES_IN_NVR = frozenset(
    {"TURRET", "TURRET_HL", "BULLET", "BULLET_HL", "MINIDOME", "MINIDOME_HL", "DOORBELL"}
)


# Map the lowercased ONVIF detection_type to the HA event_type used by
# `event.<camera>_detection`. Kept here (rather than reusing the
# uppercase table in `_event_helpers`) because the ONVIF path receives
# already-lowercased keys.
_ONVIF_DETECTION_EVENT_TYPES: dict[str, str] = {
    "video_motion": "motion",
    "video_human": "human",
    "video_vehicle": "vehicle",
    "video_pet": "pet",
    "video_line_crossing": "line_crossing",
}


class AjaxOnvifMixin:
    """Coordinator mixin: ONVIF init + event handler + NVR routing."""

    # Host attributes — provided by the coordinator __init__.
    if TYPE_CHECKING:
        account: AjaxAccount | None
        hass: HomeAssistant
        config_entry: ConfigEntry | None
        onvif_manager: AjaxOnvifManager | None
        _onvif_initialized: bool
        _onvif_reconcile_in_progress: bool
        _onvif_last_bootstrap_attempt: float
        _event_entities: dict[str, Any]
        stats: dict[str, int]

        def async_set_updated_data(self, data: AjaxAccount) -> None: ...

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    async def _async_init_onvif(self) -> None:
        """Initialise ONVIF for local AI detection events.

        ONVIF provides local AI detection events (human, vehicle, pet,
        motion) directly from Ajax cameras without relying on the cloud
        API.

        Benefits:
        * Works even when the alarm is DISARMED (SSE/SQS do not).
        * 100 % local; no cloud dependency for detections.
        * Real-time events via ONVIF PullPoint subscription.

        Requires the ``onvif-zeep-async`` package and RTSP/ONVIF
        credentials. On failure the integration falls back silently to
        the REST API.
        """
        if not ONVIF_AVAILABLE:
            _LOGGER.debug("ONVIF not available (onvif-zeep-async not installed)")
            self._onvif_initialized = True
            return

        if not self.config_entry:
            _LOGGER.debug("Config entry not set, cannot get ONVIF credentials")
            self._onvif_initialized = True
            return

        username = self.config_entry.options.get(CONF_RTSP_USERNAME, "")
        password = self.config_entry.options.get(CONF_RTSP_PASSWORD, "")

        if not username or not password:
            _LOGGER.debug("ONVIF credentials not configured, skipping local AI detections")
            self._onvif_initialized = True
            return

        if self.account is None:
            self._onvif_initialized = True
            return

        video_edges = [
            video_edge
            for space in self.account.spaces.values()
            for video_edge in space.video_edges.values()
            if video_edge.ip_address
        ]
        if not video_edges:
            _LOGGER.debug("No video edges with IP addresses found")
            self._onvif_initialized = True
            return

        entry_id = self.config_entry.entry_id
        init_issue = f"onvif_init_failed_{entry_id}"
        no_cam_issue = f"onvif_no_cameras_{entry_id}"
        partial_issue = f"onvif_partial_cameras_{entry_id}"

        try:
            _LOGGER.info("Initializing ONVIF for local AI detections...")

            assert _AjaxOnvifManager is not None  # Validated by ONVIF_AVAILABLE above.
            self.onvif_manager = _AjaxOnvifManager(
                username=username,
                password=password,
                event_callback=self._handle_onvif_event,
            )

            await self.onvif_manager.async_start(video_edges)

            # Init succeeded — clear the "init failed" Repairs issue if it was raised before.
            ir.async_delete_issue(self.hass, DOMAIN, init_issue)

            connected = self.onvif_manager.connected_count
            # Use the manager's target_count (excludes NVRs which are intentionally
            # skipped) rather than len(video_edges) — otherwise the repair issue
            # reports e.g. 2/3 when the user actually has 2 cameras + 1 NVR and
            # both cameras are connected fine.
            total = self.onvif_manager.target_count
            if connected == 0 and total == 0:
                # All video_edges are NVRs / unsupported types — nothing to alert on.
                _LOGGER.info("✓ ONVIF initialized - no individual cameras to connect to (NVR-only setup)")
                ir.async_delete_issue(self.hass, DOMAIN, no_cam_issue)
                ir.async_delete_issue(self.hass, DOMAIN, partial_issue)
                return
            if connected == 0:
                _LOGGER.warning("ONVIF: No cameras connected - Check ONVIF credentials and camera network")
                ir.async_delete_issue(self.hass, DOMAIN, partial_issue)
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    no_cam_issue,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="onvif_no_cameras",
                )
            elif connected < total:
                _LOGGER.info(
                    "✓ ONVIF initialized - %d/%d cameras connected for local AI detections",
                    connected,
                    total,
                )
                ir.async_delete_issue(self.hass, DOMAIN, no_cam_issue)
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    partial_issue,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="onvif_partial_cameras",
                    translation_placeholders={"connected": str(connected), "total": str(total)},
                )
            else:
                _LOGGER.info("✓ ONVIF initialized - %d/%d cameras connected for local AI detections", connected, total)
                ir.async_delete_issue(self.hass, DOMAIN, no_cam_issue)
                ir.async_delete_issue(self.hass, DOMAIN, partial_issue)

        except Exception as err:
            _LOGGER.warning("Failed to initialize ONVIF: %s", err)
            # Tear down any already-connected clients before dropping the
            # reference — otherwise their long-lived poll tasks / PullPoint
            # subscriptions are orphaned with no owner to cancel them on unload.
            if self.onvif_manager:
                with contextlib.suppress(Exception):
                    await self.onvif_manager.async_stop()
            self.onvif_manager = None
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                init_issue,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="onvif_init_failed",
                translation_placeholders={"error": str(err)},
            )
        finally:
            self._onvif_initialized = True

    async def _async_reconcile_onvif(self) -> None:
        """Keep ONVIF clients in sync with the current video-edge inventory.

        Cameras can be added to (or removed from) the Ajax account while
        Home Assistant is running, but ``_async_init_onvif`` only runs once
        at startup. Called on the periodic video-edge refresh so a new
        camera gets its local-AI connection — and a removed one has its
        client shut down instead of leaking its poll task — without a
        reload.

        When the bootstrap never produced a manager (no camera had an IP
        yet, or the init failed), the bootstrap is re-run instead —
        mirroring the SQS self-heal pattern. Its early-return guards
        (ONVIF wheel missing, no credentials, no cameras) keep the retry
        free when ONVIF simply is not in use.
        """
        if self._onvif_reconcile_in_progress:
            return
        self._onvif_reconcile_in_progress = True
        try:
            if self.onvif_manager is None:
                # Throttle bootstrap retries: a persistently failing ONVIF
                # setup must not re-attempt (and re-log) on every poll cycle.
                now = time.monotonic()
                if now - self._onvif_last_bootstrap_attempt < ONVIF_BOOTSTRAP_RETRY_SECONDS:
                    return
                self._onvif_last_bootstrap_attempt = now
                self._onvif_initialized = False
                await self._async_init_onvif()
                return
            if self.account is None:
                return
            video_edges = [
                video_edge
                for space in self.account.spaces.values()
                for video_edge in space.video_edges.values()
                if video_edge.ip_address
            ]
            await self.onvif_manager.async_update_video_edges(video_edges)
        finally:
            self._onvif_reconcile_in_progress = False

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def _handle_onvif_event(self, event: OnvifDetectionEvent) -> None:
        """Handle an ONVIF detection event from a camera or NVR.

        For NVR events, routes the detection to the correct camera based
        on ``channel_id``. Updates the per-channel video-edge detection
        state, fires the matching HA entity, and emits the
        ``ajax_camera_detection`` / ``ajax_doorbell_ring`` bus events.
        """
        self.stats["events_onvif_received"] += 1
        if not self.account:
            return

        _LOGGER.debug(
            "ONVIF event received: %s (channel %s, active=%s)",
            event.detection_type,
            event.channel_id,
            event.active,
        )

        for space in self.account.spaces.values():
            source_ve = space.video_edges.get(event.video_edge_id)
            if not source_ve:
                continue

            target_ve = source_ve

            # If source is the NVR, find the linked camera for this channel.
            if source_ve.video_edge_type == VideoEdgeType.NVR:
                if event.detection_type == "DOORBELL_RING":
                    # DOORBELL_RING always comes from the doorbell, no matter the channel.
                    for ve in space.video_edges.values():
                        if ve.video_edge_type == VideoEdgeType.DOORBELL:
                            target_ve = ve
                            break
                else:
                    target_ve = self._find_camera_for_nvr_channel(space, source_ve, event.channel_id)
                if target_ve and target_ve != source_ve:
                    _LOGGER.debug(
                        "Routing NVR event (channel %s) to camera: %s",
                        event.channel_id,
                        target_ve.name,
                    )

            # Update detection state on the resolved target.
            detection_key = event.detection_type.lower()  # e.g. "video_human"
            target_ve.detections[detection_key] = event.active

            # When motion clears, also clear all AI detections.
            if detection_key == "video_motion" and not event.active:
                for ai_key in ("video_human", "video_vehicle", "video_pet"):
                    target_ve.detections[ai_key] = False

            # Fire HA event entities for ONVIF detections (active=True only).
            if event.active:
                camera_entity_id = resolve_camera_entity_id(self.hass, target_ve.id)
                if detection_key == "doorbell_ring":
                    event_entity = self._event_entities.get(f"{target_ve.id}_doorbell_press")
                    if event_entity:
                        event_entity.fire("ring")
                    # Legacy bus event for automations.
                    doorbell_data: dict[str, str] = {
                        "device_id": target_ve.id,
                        "device_name": target_ve.name,
                        "source": "onvif",
                    }
                    if camera_entity_id:
                        doorbell_data["camera_entity_id"] = camera_entity_id
                        doorbell_data["snapshot_url"] = f"/api/camera_proxy/{camera_entity_id}"
                    self.hass.bus.async_fire(EVENT_AJAX_DOORBELL_RING, doorbell_data)

                event_type = _ONVIF_DETECTION_EVENT_TYPES.get(detection_key)
                if event_type:
                    event_entity = self._event_entities.get(f"{target_ve.id}_detection")
                    if event_entity:
                        attrs = {"rule": event.rule} if event.rule else None
                        event_entity.fire(event_type, attrs)

                    bus_data: dict[str, str] = {
                        "device_id": target_ve.id,
                        "device_name": target_ve.name,
                        "event_type": event_type,
                        "source": "onvif",
                    }
                    if event_entity is not None and event_entity.entity_id:
                        # HA logbook needs entity_id to attach the describer
                        # to the right device row instead of dropping the line.
                        bus_data["entity_id"] = event_entity.entity_id
                    if camera_entity_id:
                        bus_data["camera_entity_id"] = camera_entity_id
                        bus_data["snapshot_url"] = f"/api/camera_proxy/{camera_entity_id}"
                    self.hass.bus.async_fire(EVENT_AJAX_CAMERA_DETECTION, bus_data)

            # Refresh entities so the detection_key change is reflected.
            self.async_set_updated_data(self.account)
            return

    # ------------------------------------------------------------------
    # NVR channel → camera routing
    # ------------------------------------------------------------------

    def _find_camera_for_nvr_channel(self, space: AjaxSpace, nvr: AjaxVideoEdge, channel_id: str) -> AjaxVideoEdge:
        """Find the camera linked to ``channel_id`` on ``nvr``.

        Returns the linked Ajax camera when the NVR's
        ``channels[*].sourceAliases.sources`` lists one with a known
        camera type and a ``videoEdgeId`` that we already track; falls
        back to ``nvr`` when no link can be resolved.
        """
        channels = nvr.channels
        if not isinstance(channels, list):
            return nvr

        def get_linked_camera_from_channel(channel: dict[str, Any]) -> AjaxVideoEdge | None:
            """Extract the linked camera from a channel's ``sourceAliases``."""
            if not isinstance(channel, dict):
                return None
            source_aliases = channel.get("sourceAliases", {})
            if not isinstance(source_aliases, dict):
                return None
            sources = source_aliases.get("sources", [])
            if not isinstance(sources, list):
                return None
            for source in sources:
                if not isinstance(source, dict):
                    continue
                if source.get("sourceType") == "PRIMARY":
                    source_type = source.get("type", "")
                    source_ve_id = source.get("videoEdgeId", "")
                    if source_type in _AJAX_CAMERA_TYPES_IN_NVR and source_ve_id:
                        linked_camera = space.video_edges.get(source_ve_id)
                        if linked_camera:
                            return linked_camera
            return None

        # Channel index lookup (channel_id is usually "0", "1", ...).
        try:
            channel_idx = int(channel_id)
            if 0 <= channel_idx < len(channels):
                linked = get_linked_camera_from_channel(channels[channel_idx])
                if linked:
                    return linked
        except (ValueError, IndexError):
            pass

        # Fallback: scan all channels by id.
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            ch_id = channel.get("id", "")
            if str(ch_id) == str(channel_id):
                linked = get_linked_camera_from_channel(channel)
                if linked:
                    return linked

        return nvr
