"""ONVIF mixin for ``AjaxDataCoordinator``.

Owns the local-AI detection path: ONVIF subscription bootstrap
(`_async_init_onvif`), event handler that fires the matching HA entity /
bus event (`_handle_onvif_event`), and the handling of NVR events for the
channels that have no Ajax camera behind them (`_handle_nvr_event`).

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
from ._nvr_channels import find_channel_by_token, is_third_party_channel
from .const import CONF_RTSP_PASSWORD, CONF_RTSP_USERNAME, DOMAIN, EVENT_AJAX_CAMERA_DETECTION, EVENT_AJAX_DOORBELL_RING
from .models import VideoEdgeType

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .models import AjaxAccount, AjaxVideoEdge
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

            self._update_onvif_issues(log=True)

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

    def _update_onvif_issues(self, log: bool = False) -> None:
        """Raise or clear the "no camera" / "partial cameras" Repairs issues.

        Called at startup and after every video-edge refresh, so the issue
        follows the real state: it appears when a camera keeps refusing ONVIF
        after startup, and clears itself once the camera is back.
        """
        config_entry = getattr(self, "config_entry", None)
        if self.onvif_manager is None or config_entry is None:
            return
        entry_id = config_entry.entry_id
        no_cam_issue = f"onvif_no_cameras_{entry_id}"
        partial_issue = f"onvif_partial_cameras_{entry_id}"
        connected = self.onvif_manager.connected_count
        # Use the manager's target_count (excludes NVRs which are intentionally
        # skipped) rather than len(video_edges) — otherwise the repair issue
        # reports e.g. 2/3 when the user actually has 2 cameras + 1 NVR and
        # both cameras are connected fine.
        total = self.onvif_manager.target_count
        if total == 0:
            # All video_edges are NVRs / unsupported types — nothing to alert on.
            if log:
                _LOGGER.info("✓ ONVIF initialized - no individual cameras to connect to (NVR-only setup)")
            ir.async_delete_issue(self.hass, DOMAIN, no_cam_issue)
            ir.async_delete_issue(self.hass, DOMAIN, partial_issue)
        elif connected == 0:
            if log:
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
            if log:
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
            if log:
                _LOGGER.info("✓ ONVIF initialized - %d/%d cameras connected for local AI detections", connected, total)
            ir.async_delete_issue(self.hass, DOMAIN, no_cam_issue)
            ir.async_delete_issue(self.hass, DOMAIN, partial_issue)

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
            self._update_onvif_issues()
        finally:
            self._onvif_reconcile_in_progress = False

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def _handle_onvif_event(self, event: OnvifDetectionEvent) -> None:
        """Handle an ONVIF detection event from a camera or NVR.

        Updates the detection state of the camera (NVR events: of the
        channel, see ``_handle_nvr_event``), fires the matching HA entity, and emits the
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

            if source_ve.video_edge_type == VideoEdgeType.NVR:
                self._handle_nvr_event(self.account, source_ve, event)
                return

            target_ve = source_ve

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
    # NVR channels without an Ajax camera
    # ------------------------------------------------------------------

    def _handle_nvr_event(self, account: AjaxAccount, nvr: AjaxVideoEdge, event: OnvifDetectionEvent) -> None:
        """Store an NVR detection on the third-party channel it belongs to.

        Events of the channels backed by an Ajax camera are ignored: the camera
        delivers its own. An event that matches no channel is dropped rather
        than attributed to the wrong one.
        """
        channel = find_channel_by_token(nvr, event.channel_id)
        if channel is None:
            _LOGGER.debug("%s: no channel matches ONVIF source '%s'", nvr.name, event.channel_id)
            return
        if not is_third_party_channel(nvr, channel):
            return
        event_type = _ONVIF_DETECTION_EVENT_TYPES.get(event.detection_type.lower())
        if event_type is None:
            _LOGGER.debug("%s: %s is not supported on an NVR channel", nvr.name, event.detection_type)
            return

        channel_id = str(channel["id"])
        _LOGGER.debug(
            "%s: %s %s on channel '%s' (%s)",
            nvr.name,
            event.detection_type,
            "active" if event.active else "cleared",
            channel.get("name"),
            channel_id,
        )
        states = nvr.channel_detections.setdefault(channel_id, {})
        detection_key = event.detection_type.lower()
        states[detection_key] = event.active
        if detection_key == "video_motion" and not event.active:
            for ai_key in ("video_human", "video_vehicle", "video_pet"):
                states[ai_key] = False

        if event.active:
            event_entity = self._event_entities.get(f"{nvr.id}_detection")
            if event_entity:
                attrs = {"channel": channel.get("name") or channel_id}
                if event.rule:
                    attrs["rule"] = event.rule
                event_entity.fire(event_type, attrs)
            # No camera entity here: the one resolved for an NVR is its first
            # channel, not necessarily the one that detected something.
            bus_data: dict[str, str] = {
                "device_id": nvr.id,
                "device_name": nvr.name,
                "channel_id": channel_id,
                "channel_name": channel.get("name") or channel_id,
                "event_type": event_type,
                "source": "onvif",
            }
            if event_entity is not None and event_entity.entity_id:
                bus_data["entity_id"] = event_entity.entity_id
            self.hass.bus.async_fire(EVENT_AJAX_CAMERA_DETECTION, bus_data)

        self.async_set_updated_data(account)
