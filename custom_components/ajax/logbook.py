"""Logbook integration for Ajax Security System."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.logbook import LOGBOOK_ENTRY_ICON, LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN

# Minimal translation table keyed by HA language (2-letter code).
# We keep it inline here because Home Assistant's logbook API does not
# support translation_key; describe callbacks must return the final
# strings themselves.
_MESSAGES: dict[str, dict[str, str]] = {
    "armed": {
        "en": "armed",
        "fr": "armé",
        "es": "armado",
        "de": "scharf",
        "nl": "ingeschakeld",
        "sv": "tillkopplat",
        "uk": "під охороною",
    },
    "disarmed": {
        "en": "disarmed",
        "fr": "désarmé",
        "es": "desarmado",
        "de": "unscharf",
        "nl": "uitgeschakeld",
        "sv": "frånkopplat",
        "uk": "знято з охорони",
    },
    "armed_night": {
        "en": "armed (night mode)",
        "fr": "armé (mode nuit)",
        "es": "armado (modo nocturno)",
        "de": "scharf (Nachtmodus)",
        "nl": "ingeschakeld (nachtmodus)",
        "sv": "tillkopplat (nattläge)",
        "uk": "під охороною (нічний режим)",
    },
    "armed_home": {
        "en": "armed (home)",
        "fr": "armé (présence)",
        "es": "armado (hogar)",
        "de": "scharf (zuhause)",
        "nl": "ingeschakeld (thuis)",
        "sv": "tillkopplat (hemma)",
        "uk": "під охороною (вдома)",
    },
    "rang": {
        "en": "rang",
        "fr": "a sonné",
        "es": "sonó",
        "de": "geklingelt",
        "nl": "gerinkeld",
        "sv": "ringde",
        "uk": "подзвонив",
    },
    "state_changed": {
        "en": "changed from {old} to {new}",
        "fr": "changé de {old} à {new}",
        "es": "cambiado de {old} a {new}",
        "de": "gewechselt von {old} zu {new}",
        "nl": "gewijzigd van {old} naar {new}",
        "sv": "ändrat från {old} till {new}",
        "uk": "змінено з {old} на {new}",
    },
    "triggered_on": {
        "en": "triggered on {target}",
        "fr": "déclenché sur {target}",
        "es": "activado en {target}",
        "de": "ausgelöst auf {target}",
        "nl": "geactiveerd op {target}",
        "sv": "utlöst på {target}",
        "uk": "спрацювало на {target}",
    },
    "triggered": {
        "en": "triggered",
        "fr": "déclenché",
        "es": "activado",
        "de": "ausgelöst",
        "nl": "geactiveerd",
        "sv": "utlöst",
        "uk": "спрацювало",
    },
}


def _tr(hass: HomeAssistant, key: str, **kwargs: str) -> str:
    """Return the message ``key`` in the user's HA language."""
    lang = (hass.config.language or "en")[:2]
    table = _MESSAGES.get(key, {})
    template = table.get(lang) or table.get("en") or key
    if kwargs:
        try:
            return template.format(**kwargs)
        except KeyError:
            return template
    return template


# Event types fired by the integration
EVENT_AJAX_ARMED = "ajax_armed"
EVENT_AJAX_DISARMED = "ajax_disarmed"
EVENT_AJAX_ARMED_NIGHT = "ajax_armed_night"
EVENT_AJAX_ARMED_HOME = "ajax_armed_home"
EVENT_AJAX_SECURITY_STATE_CHANGED = "ajax_security_state_changed"
EVENT_AJAX_BUTTON_PRESSED = "ajax_button_pressed"
EVENT_AJAX_DOORBELL_RING = "ajax_doorbell_ring"
EVENT_AJAX_SCENARIO_TRIGGERED = "ajax_scenario_triggered"


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Describe logbook events."""

    @callback
    def async_describe_armed(event: Event) -> dict[str, str]:
        space = event.data.get("space_name", "Ajax")
        return {
            LOGBOOK_ENTRY_NAME: space,
            LOGBOOK_ENTRY_MESSAGE: _tr(hass, "armed"),
            LOGBOOK_ENTRY_ICON: "mdi:shield-lock",
        }

    @callback
    def async_describe_disarmed(event: Event) -> dict[str, str]:
        space = event.data.get("space_name", "Ajax")
        return {
            LOGBOOK_ENTRY_NAME: space,
            LOGBOOK_ENTRY_MESSAGE: _tr(hass, "disarmed"),
            LOGBOOK_ENTRY_ICON: "mdi:shield-off",
        }

    @callback
    def async_describe_armed_night(event: Event) -> dict[str, str]:
        space = event.data.get("space_name", "Ajax")
        return {
            LOGBOOK_ENTRY_NAME: space,
            LOGBOOK_ENTRY_MESSAGE: _tr(hass, "armed_night"),
            LOGBOOK_ENTRY_ICON: "mdi:shield-moon",
        }

    @callback
    def async_describe_armed_home(event: Event) -> dict[str, str]:
        space = event.data.get("space_name", "Ajax")
        return {
            LOGBOOK_ENTRY_NAME: space,
            LOGBOOK_ENTRY_MESSAGE: _tr(hass, "armed_home"),
            LOGBOOK_ENTRY_ICON: "mdi:shield-home",
        }

    @callback
    def async_describe_state_changed(event: Event) -> dict[str, str]:
        space = event.data.get("space_name", "Ajax")
        old = event.data.get("old_state", "unknown")
        new = event.data.get("new_state", "unknown")
        return {
            LOGBOOK_ENTRY_NAME: space,
            LOGBOOK_ENTRY_MESSAGE: _tr(hass, "state_changed", old=old, new=new),
            LOGBOOK_ENTRY_ICON: "mdi:shield-sync",
        }

    @callback
    def async_describe_button(event: Event) -> dict[str, str]:
        device = event.data.get("device_name", "Button")
        action = event.data.get("action", "pressed")
        return {
            LOGBOOK_ENTRY_NAME: device,
            LOGBOOK_ENTRY_MESSAGE: action,
            LOGBOOK_ENTRY_ICON: "mdi:gesture-tap-button",
        }

    @callback
    def async_describe_doorbell(event: Event) -> dict[str, str]:
        device = event.data.get("device_name", "Doorbell")
        return {
            LOGBOOK_ENTRY_NAME: device,
            LOGBOOK_ENTRY_MESSAGE: _tr(hass, "rang"),
            LOGBOOK_ENTRY_ICON: "mdi:doorbell",
        }

    @callback
    def async_describe_scenario(event: Event) -> dict[str, str]:
        scenario = event.data.get("scenario_name", "Scenario")
        target = event.data.get("target_name", "")
        msg = _tr(hass, "triggered_on", target=target) if target else _tr(hass, "triggered")
        return {
            LOGBOOK_ENTRY_NAME: scenario,
            LOGBOOK_ENTRY_MESSAGE: msg,
            LOGBOOK_ENTRY_ICON: "mdi:play-circle",
        }

    async_describe_event(DOMAIN, EVENT_AJAX_ARMED, async_describe_armed)
    async_describe_event(DOMAIN, EVENT_AJAX_DISARMED, async_describe_disarmed)
    async_describe_event(DOMAIN, EVENT_AJAX_ARMED_NIGHT, async_describe_armed_night)
    async_describe_event(DOMAIN, EVENT_AJAX_ARMED_HOME, async_describe_armed_home)
    async_describe_event(DOMAIN, EVENT_AJAX_SECURITY_STATE_CHANGED, async_describe_state_changed)
    async_describe_event(DOMAIN, EVENT_AJAX_BUTTON_PRESSED, async_describe_button)
    async_describe_event(DOMAIN, EVENT_AJAX_DOORBELL_RING, async_describe_doorbell)
    async_describe_event(DOMAIN, EVENT_AJAX_SCENARIO_TRIGGERED, async_describe_scenario)
