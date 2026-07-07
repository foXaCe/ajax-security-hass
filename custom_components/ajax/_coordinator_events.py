"""Event-dispatch mixin for ``AjaxDataCoordinator``.

Owns the bridges from internal state-changes to user-visible plumbing:

* `_fire_security_state_event` — emits the typed HA bus event
  (`ajax_armed`, `ajax_disarmed`, …) with source info when known.
* `_create_event_from_state_change` — records a state-change in the
  per-space history and fans out to `_fire_security_state_event`.
* `_create_sqs_notification` — raises the persistent notification on
  the HA dashboard when notifications are enabled.
* `_escape_markdown` — neutralises user-supplied text before inlining
  it into persistent-notification messages.

State is shared with the coordinator via ``self``; the mixin owns no
fields of its own.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.persistent_notification import async_create

from .const import (
    CONF_MONITORED_SPACES,
    CONF_NOTIFICATION_FILTER,
    CONF_PERSISTENT_NOTIFICATION,
    EVENT_AJAX_ARMED,
    EVENT_AJAX_ARMED_HOME,
    EVENT_AJAX_ARMED_NIGHT,
    EVENT_AJAX_DISARMED,
    EVENT_AJAX_SECURITY_STATE_CHANGED,
    NOTIFICATION_FILTER_ALARMS_ONLY,
    NOTIFICATION_FILTER_ALL,
    NOTIFICATION_FILTER_NONE,
)
from .event_codes import get_event_message, resolve_event_language
from .models import GroupState, SecurityState

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .models import AjaxSpace

_LOGGER = logging.getLogger(__name__)


class AjaxEventDispatchMixin:
    """Coordinator mixin that emits HA events + persistent notifications."""

    # Host attributes — provided by the coordinator __init__.
    if TYPE_CHECKING:
        hass: HomeAssistant
        config_entry: ConfigEntry | None

    # ------------------------------------------------------------------
    # Bus event
    # ------------------------------------------------------------------

    def _fire_security_state_event(
        self,
        space: AjaxSpace,
        old_state: SecurityState,
        new_state: SecurityState,
        source_name: str | None = None,
        source_type: str | None = None,
    ) -> None:
        """Fire a Home Assistant bus event when security state changes.

        Args:
            space: The AjaxSpace object.
            old_state: Previous security state.
            new_state: New security state.
            source_name: Who triggered the change (user name,
                ``"Home Assistant"`` for a local automation, or None
                when not available — e.g. the REST fallback path).
            source_type: ``USER`` / ``KEYPAD`` / ``SPACE_CONTROL`` /
                ``APP`` / ``HA`` (Ajax `sourceObjectType`).
        """
        if new_state == SecurityState.ARMED:
            event_name = EVENT_AJAX_ARMED
        elif new_state == SecurityState.DISARMED:
            event_name = EVENT_AJAX_DISARMED
        elif new_state == SecurityState.NIGHT_MODE:
            event_name = EVENT_AJAX_ARMED_NIGHT
        elif new_state == SecurityState.PARTIALLY_ARMED:
            event_name = EVENT_AJAX_ARMED_HOME
        else:
            event_name = EVENT_AJAX_SECURITY_STATE_CHANGED

        event_data: dict[str, Any] = {
            "space_id": space.id,
            "space_name": space.name,
            "old_state": old_state.value,
            "new_state": new_state.value,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if source_name:
            event_data["source_name"] = source_name
        if source_type:
            event_data["source_type"] = source_type

        if space.group_mode_enabled and space.groups:
            armed_groups = [g.name for g in space.groups.values() if g.state == GroupState.ARMED]
            disarmed_groups = [g.name for g in space.groups.values() if g.state == GroupState.DISARMED]
            event_data["armed_groups"] = armed_groups
            event_data["disarmed_groups"] = disarmed_groups
            event_data["group_mode"] = True
        else:
            event_data["group_mode"] = False

        self.hass.bus.async_fire(event_name, event_data)

    # ------------------------------------------------------------------
    # State-change → space history + bus event
    # ------------------------------------------------------------------

    def _create_event_from_state_change(
        self,
        space: AjaxSpace,
        old_state: SecurityState,
        new_state: SecurityState,
    ) -> None:
        """Record a security state change in the space history + fire the bus event.

        The REST API is the single source of truth for the history; SQS/SSE
        only trigger faster polling, they do not create entries directly.
        """
        action_map = {
            SecurityState.ARMED: "armed",
            SecurityState.DISARMED: "disarmed",
            SecurityState.NIGHT_MODE: "night_mode",
            SecurityState.PARTIALLY_ARMED: "partially_armed",
        }
        action = action_map.get(new_state, new_state.value.lower())

        language = resolve_event_language(self.hass.config.language)
        message = get_event_message(action, language)

        # Note: source_name/user_name not included because REST doesn't
        # tell us WHO triggered the action.
        event = {
            "action": action,
            "message": message,
            "hub_id": space.hub_id or space.id,
            "space_id": space.id,
            "timestamp": datetime.now(UTC).isoformat(),
            "event_time": datetime.now(UTC),
        }

        space.recent_events.insert(0, event)
        space.recent_events = space.recent_events[:10]

        _LOGGER.debug("Event stored: %s (total: %d)", action, len(space.recent_events))

        self._fire_security_state_event(space, old_state, new_state)

    # ------------------------------------------------------------------
    # Persistent notification
    # ------------------------------------------------------------------

    async def _create_sqs_notification(
        self, action: str, source_name: str, space_name: str, space_id: str = ""
    ) -> None:
        """Create a persistent notification in HA for an SQS / SSE event."""
        options = self.config_entry.options if self.config_entry else {}

        if not options.get(CONF_PERSISTENT_NOTIFICATION, True):
            return

        notification_filter = options.get(CONF_NOTIFICATION_FILTER, NOTIFICATION_FILTER_ALL)

        if notification_filter == NOTIFICATION_FILTER_NONE:
            return

        # Per-space filter from the "notifications" options step. An empty
        # selection means "all spaces" (backwards compatible with entries
        # saved before the filter was enforced).
        monitored_spaces = options.get(CONF_MONITORED_SPACES, [])
        if monitored_spaces and space_id and space_id not in monitored_spaces:
            return

        # Arm/disarm are security events, not alarms.
        is_arming_event = action in (
            "armed",
            "disarmed",
            "night_mode",
            "partially_armed",
            # Legacy keys.
            "nightmodeon",
            "partiallyarmed",
        )
        if notification_filter == NOTIFICATION_FILTER_ALARMS_ONLY and is_arming_event:
            return
        # NOTIFICATION_FILTER_SECURITY_EVENTS and NOTIFICATION_FILTER_ALL show everything.

        language = resolve_event_language(self.hass.config.language)

        # Map SecurityState values to action keys in event_codes.
        action_to_key = {
            "armed": "armed",
            "disarmed": "disarmed",
            "night_mode": "night_mode",
            "partially_armed": "partially_armed",
            # Legacy keys for backwards compatibility.
            "nightmodeon": "night_mode",
            "partiallyarmed": "partially_armed",
        }
        action_key = action_to_key.get(action, action)
        message = get_event_message(action_key, language)

        _LOGGER.info(
            "SQS notification: action=%s, action_key=%s, lang=%s, message=%s",
            action,
            action_key,
            language,
            message,
        )

        if source_name:
            by_word = {"fr": "par", "en": "by", "es": "por"}.get(language, "by")
            # Persistent notifications render markdown — escape user-supplied
            # source_name (from Ajax API) to neutralise [text](javascript:...)
            # injection and stray markdown formatting.
            safe_source = self._escape_markdown(source_name)
            message = f"{message} {by_word} {safe_source}"

        title = f"Ajax - {self._escape_markdown(space_name)}"

        async_create(
            self.hass,
            message,
            title=title,
            notification_id=f"ajax_{action}_{space_name}_{source_name}_{int(time.time())}",
        )

    @staticmethod
    def _escape_markdown(text: str | None) -> str:
        """Escape markdown special characters in user-supplied text.

        Persistent notifications in Home Assistant render as markdown, so
        names coming from the Ajax API must be neutralised before being
        interpolated into the message body.
        """
        if not text:
            return ""
        escaped = str(text)
        for ch in ("\\", "`", "*", "_", "[", "]", "(", ")", "{", "}", "#", "+", "!", "|", "<", ">"):
            escaped = escaped.replace(ch, f"\\{ch}")
        return escaped
