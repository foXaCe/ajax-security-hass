"""The Ajax Security System integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import (
    area_registry as ar,
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ._ids import device_identifier
from ._services import (  # noqa: F401  (SERVICE_* re-exported for tests/back-compat)
    SERVICE_FORCE_ARM,
    SERVICE_FORCE_ARM_NIGHT,
    SERVICE_GET_NVR_RECORDINGS,
    SERVICE_GET_RAW_DEVICES,
    SERVICE_GET_SMART_LOCKS,
    SERVICE_REFRESH_METADATA,
    _async_setup_services,
)
from .api import AjaxRestApi, AjaxRestApiError, AjaxRestAuthError
from .const import (
    AUTH_MODE_DIRECT,
    AUTH_MODE_PROXY_SECURE,
    CONF_API_KEY,
    CONF_AUTH_MODE,
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_DISCOVERED_MACS,
    CONF_DOOR_SENSOR_FAST_POLL,
    CONF_EMAIL,
    CONF_ENABLED_SPACES,
    CONF_PASSWORD,
    CONF_PROXY_URL,
    CONF_QUEUE_NAME,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_USERNAME,
    CONF_TOTP_SECRET,
    CONF_VERIFY_SSL,
    DOMAIN,
    INTEGRATION_VERSION,
    AjaxConfigEntry,
)
from .coordinator import AjaxDataCoordinator
from .models import SecurityState

if TYPE_CHECKING:
    from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.DEVICE_TRACKER,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.LOCK,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
    Platform.VALVE,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Ajax Security System component."""
    # Register services
    await _async_setup_services(hass)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: AjaxConfigEntry) -> bool:
    """Set up Ajax Security System from a config entry."""
    _LOGGER.info("Ajax integration v%s starting...", INTEGRATION_VERSION)

    # Get authentication mode (default to direct for backwards compatibility)
    auth_mode = entry.data.get(CONF_AUTH_MODE, AUTH_MODE_DIRECT)

    # Get common credentials
    email = entry.data[CONF_EMAIL]
    password_hash = entry.data[CONF_PASSWORD]  # Already hashed in config_flow
    totp_secret = entry.data.get(CONF_TOTP_SECRET)

    # Variables for different modes
    api_key: str | None = None
    proxy_url: str | None = None
    proxy_mode = None
    sse_url = None
    aws_access_key_id = None
    aws_secret_access_key = None
    queue_name = None

    if auth_mode == AUTH_MODE_DIRECT:
        # Direct mode: API key provided, optional SQS for real-time events
        api_key = entry.data[CONF_API_KEY]
        aws_access_key_id = entry.data.get(CONF_AWS_ACCESS_KEY_ID)
        aws_secret_access_key = entry.data.get(CONF_AWS_SECRET_ACCESS_KEY)
        queue_name = entry.data.get(CONF_QUEUE_NAME)
        _LOGGER.info("Using direct authentication mode")

    elif auth_mode == AUTH_MODE_PROXY_SECURE:
        # Proxy Secure: All requests via proxy, SSE for real-time events
        proxy_url = entry.data[CONF_PROXY_URL]
        proxy_mode = AUTH_MODE_PROXY_SECURE
        _LOGGER.info("Using proxy secure authentication mode")

    # Get verify_ssl option (default True for backwards compatibility)
    verify_ssl = entry.data.get(CONF_VERIFY_SSL, True)

    # Create REST API instance with HA session for connection reuse
    # password_is_hashed=True because we store only SHA256 hash, never plain password
    # Note: api_key can be None for proxy mode (fetched during login)
    # Note: For proxy mode with HTTPS, we can't use HA session when verify_ssl=False
    #       because async_get_clientsession() doesn't support disabling SSL verification
    if verify_ssl:
        session = async_get_clientsession(hass)
        ir.async_delete_issue(hass, DOMAIN, f"verify_ssl_disabled_{entry.entry_id}")
    else:
        session = None  # API will create its own session with SSL disabled
        _LOGGER.warning("SSL verification disabled - using dedicated session")
        # Surface a persistent Repairs issue: with verify_ssl=False the proxy
        # TLS certificate is not validated, exposing credentials to MITM.
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"verify_ssl_disabled_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="verify_ssl_disabled",
        )

    api = AjaxRestApi(
        api_key=api_key or "",
        email=email,
        password=password_hash,
        password_is_hashed=True,  # Password is already SHA256 hash
        totp_secret=totp_secret,
        proxy_url=proxy_url,
        proxy_mode=proxy_mode,
        session=session,
        verify_ssl=verify_ssl,
    )

    try:
        # Login to get temporary token (and API key + SSE URL if using proxy)
        await api.async_login()
        _LOGGER.info("Successfully logged in to Ajax REST API")

        # Get SSE URL if using proxy mode
        if auth_mode == AUTH_MODE_PROXY_SECURE:
            sse_url = api.sse_url
            if sse_url:
                _LOGGER.info("SSE URL obtained from proxy")
                _LOGGER.debug("SSE URL host: %s", urlsplit(sse_url).netloc)
            else:
                _LOGGER.warning("No SSE URL received from proxy")

        # Test API connection by getting hubs
        await api.async_get_hubs()
        _LOGGER.info("Successfully connected to Ajax REST API")

    except AjaxRestAuthError as err:
        _LOGGER.error("Authentication failed: %s", err)
        await api.close()
        # Raise ConfigEntryAuthFailed so HA triggers the reauth flow automatically
        raise ConfigEntryAuthFailed(str(err)) from err
    except AjaxRestApiError as err:
        _LOGGER.error("API error during setup: %s", err)
        await api.close()
        raise ConfigEntryNotReady from err

    # Create coordinator
    # - REST polling: Baseline updates every 30s (disarmed) or 60s (armed)
    # - AWS SQS: Optional real-time events (direct mode only)
    # - SSE: Real-time events via proxy (proxy modes only)
    enabled_spaces = entry.data.get(CONF_ENABLED_SPACES)  # None = all spaces
    coordinator = AjaxDataCoordinator(
        hass,
        entry,
        api,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        queue_name=queue_name,
        sse_url=sse_url,
        enabled_spaces=enabled_spaces,
    )

    # Apply options to coordinator (default: disabled to reduce API calls)
    door_sensor_fast_poll = entry.options.get(CONF_DOOR_SENSOR_FAST_POLL, False)
    coordinator._door_sensor_fast_poll_enabled = door_sensor_fast_poll
    if door_sensor_fast_poll:
        _LOGGER.info("Door sensor fast polling enabled (5s interval when disarmed)")
    else:
        _LOGGER.debug("Door sensor fast polling disabled (API optimization)")

    # Store coordinator
    entry.runtime_data = coordinator

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Background task: Create HA Areas from Ajax rooms (non-blocking)
    entry.async_create_background_task(
        hass,
        _async_setup_areas(hass, coordinator),
        "ajax_setup_areas",
    )

    # Snapshot of the connection-relevant configuration. The update listener
    # is the single reload decision point (HA deprecates flow-side reload
    # helpers on entries that have an update listener — error in 2026.12):
    # when this snapshot no longer matches, the listener schedules a reload.
    coordinator._reload_config_snapshot = _reload_relevant_config(entry)

    # Listen for config/options updates: live-apply what can be, schedule a
    # reload for connection-relevant changes (credentials, proxy, spaces…).
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    return True


def _reload_relevant_config(entry: AjaxConfigEntry) -> tuple[dict[str, Any], str, str]:
    """Return the part of the entry config that requires a reload to apply.

    ``entry.data`` mostly holds connection/credential settings (auth mode,
    email/password, API key, proxy, AWS SQS, enabled spaces) that do need a
    reload to take effect. ``CONF_DISCOVERED_MACS`` is excluded: it only
    tracks which DHCP-discovered hubs have been associated with this entry
    for deduplication in the config flow and has no runtime effect, so
    changing it (e.g. associating a newly discovered hub) must not trigger a
    reload. In ``entry.options`` only the RTSP/ONVIF credentials need a
    reload — the ONVIF manager reads them at bootstrap only; everything
    else is either applied live (fast poll) or read dynamically
    (notification settings).
    """
    data = {k: v for k, v in entry.data.items() if k != CONF_DISCOVERED_MACS}
    return (
        data,
        entry.options.get(CONF_RTSP_USERNAME, ""),
        entry.options.get(CONF_RTSP_PASSWORD, ""),
    )


async def _async_update_options(hass: HomeAssistant, entry: AjaxConfigEntry) -> None:
    """Handle config entry updates: reload if needed, else live-apply."""
    coordinator = entry.runtime_data

    # Connection-relevant change (credentials, proxy, spaces, RTSP…) →
    # schedule a reload; the fresh setup re-reads everything and takes a
    # new snapshot. Scheduling (vs awaiting) keeps this listener-safe.
    if _reload_relevant_config(entry) != coordinator._reload_config_snapshot:
        _LOGGER.info("Connection-relevant configuration changed, reloading the Ajax entry")
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return

    # Update door sensor fast polling option
    door_sensor_fast_poll = entry.options.get(CONF_DOOR_SENSOR_FAST_POLL, False)
    old_value = coordinator._door_sensor_fast_poll_enabled
    coordinator._door_sensor_fast_poll_enabled = door_sensor_fast_poll

    if old_value != door_sensor_fast_poll:
        _LOGGER.info(
            "Door sensor fast polling %s (task running: %s)",
            "enabled" if door_sensor_fast_poll else "disabled",
            coordinator._door_sensor_poll_task is not None,
        )
        # Apply immediately: stop polling if disabled, or trigger refresh to restart
        if not door_sensor_fast_poll:
            # Stop any running door sensor polling
            _LOGGER.debug("Stopping door sensor polling task")
            coordinator._manage_door_sensor_polling(False, coordinator._door_sensor_poll_security_state)
        else:
            # Re-evaluate polling based on current security state
            if coordinator.account:
                for space in coordinator.account.spaces.values():
                    is_disarmed_or_night = space.security_state in (
                        SecurityState.DISARMED,
                        SecurityState.NIGHT_MODE,
                    )
                    _LOGGER.debug(
                        "Re-evaluating polling for space %s (state: %s, should_poll: %s)",
                        space.name,
                        space.security_state.value,
                        is_disarmed_or_night,
                    )
                    if is_disarmed_or_night:
                        coordinator._manage_door_sensor_polling(True, space.security_state)
                        break  # Only need one polling task


async def _async_setup_areas(hass: HomeAssistant, coordinator: AjaxDataCoordinator) -> None:
    """Create HA Areas from Ajax rooms and assign devices to them."""

    area_reg = ar.async_get(hass)
    device_reg = dr.async_get(hass)

    # Collect all rooms from all spaces
    rooms_created = 0
    devices_assigned = 0

    if coordinator.account is None:
        _LOGGER.warning("Cannot sync rooms: coordinator.account is None")
        return

    for _space_id, space in coordinator.account.spaces.items():
        # Get rooms map from space
        rooms_map = space.rooms_map

        for room_id, room_name in rooms_map.items():
            if not room_name:
                continue

            # Create area if it doesn't exist
            area = area_reg.async_get_area_by_name(room_name)
            if not area:
                area = area_reg.async_create(name=room_name)
                rooms_created += 1
                _LOGGER.info("Created HA Area: %s", room_name)

            # Assign devices in this room to the area (only if not already assigned)
            for device_id, device in space.devices.items():
                if device.room_id == room_id:
                    # Find the HA device by identifiers
                    ha_device = device_reg.async_get_device(
                        identifiers={device_identifier(coordinator.entry_id, device_id)}
                    )
                    # Only assign area if device has no area yet (respect user changes)
                    if ha_device and ha_device.area_id is None:
                        device_reg.async_update_device(ha_device.id, area_id=area.id)
                        devices_assigned += 1
                        _LOGGER.debug("Assigned device %s to area %s", device.name, room_name)

    if rooms_created > 0 or devices_assigned > 0:
        _LOGGER.info(
            "Areas setup: %d created, %d devices assigned",
            rooms_created,
            devices_assigned,
        )


async def async_unload_entry(hass: HomeAssistant, entry: AjaxConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = entry.runtime_data

        # Shutdown coordinator (closes SQS manager, API connection, and all tasks)
        await coordinator.async_shutdown()

    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: AjaxConfigEntry) -> bool:
    """Migrate an old config entry up to the current schema.

    Each migration step moves the entry forward by one minor (or major)
    version. The loop handles users who skip versions — we only need to
    keep the step-by-step transitions correct.
    """
    _LOGGER.debug(
        "Migrating ConfigEntry from v%s.%s",
        entry.version,
        entry.minor_version,
    )

    # v1.1 → v1.2 : populate unique_id from e-mail.
    # Lower-case it to match the config flow (async_set_unique_id(email.lower())),
    # otherwise a migrated entry with a mixed-case e-mail would not collide with a
    # fresh setup of the same account and a duplicate entry could be created.
    if entry.version == 1 and entry.minor_version < 2:
        hass.config_entries.async_update_entry(
            entry,
            unique_id=(entry.data.get(CONF_EMAIL) or "").lower() or None,
            minor_version=2,
        )
        _LOGGER.info("ConfigEntry migrated to v1.2 (added unique_id)")

    # v1.2 → v1.3 : namespace entity unique_id and device identifiers with the
    # config entry id so two Ajax accounts can no longer collide in the entity /
    # device registries. The runtime format (see _ids.py + each entity __init__)
    # is f"{entry_id}_{...}"; we reproduce it here by prepending f"{entry_id}_"
    # to every legacy id not already namespaced — idempotent, and a no-op for the
    # space / alarm / panic entities that already carried the entry id.
    if entry.version == 1 and entry.minor_version < 3:
        prefix = f"{entry.entry_id}_"

        # Rename entity unique_ids in place (preserves entity_id, history and
        # automations). A manual loop — rather than er.async_migrate_entries —
        # so we can survive a collision: if a namespaced twin already exists
        # (e.g. an earlier interrupted upgrade let the platform recreate the
        # entity), drop the stale legacy row instead of raising.
        ent_reg = er.async_get(hass)
        for reg_entry in list(er.async_entries_for_config_entry(ent_reg, entry.entry_id)):
            if reg_entry.unique_id.startswith(prefix):
                continue
            new_unique_id = f"{prefix}{reg_entry.unique_id}"
            if ent_reg.async_get_entity_id(reg_entry.domain, DOMAIN, new_unique_id) is not None:
                ent_reg.async_remove(reg_entry.entity_id)
                continue
            ent_reg.async_update_entity(reg_entry.entity_id, new_unique_id=new_unique_id)

        dev_reg = dr.async_get(hass)
        for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
            new_identifiers = {
                (domain, raw) if domain != DOMAIN or raw.startswith(prefix) else (domain, f"{prefix}{raw}")
                for domain, raw in device.identifiers
            }
            if new_identifiers != device.identifiers:
                try:
                    dev_reg.async_update_device(device.id, new_identifiers=new_identifiers)
                except ValueError:
                    # Namespaced twin device already present — leave the stale
                    # one for _async_cleanup_stale_devices to prune later.
                    _LOGGER.warning("Skipped device id migration for %s (target already exists)", device.id)

        hass.config_entries.async_update_entry(entry, minor_version=3)
        _LOGGER.info("ConfigEntry migrated to v1.3 (namespaced registry ids)")

    # Future migrations go here, following the same pattern:
    # if entry.version == 1 and entry.minor_version < 4: ...

    _LOGGER.debug(
        "ConfigEntry migration finished at v%s.%s",
        entry.version,
        entry.minor_version,
    )
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: AjaxConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow the user to delete an Ajax device from Home Assistant.

    Returns True when the device is no longer known by the coordinator
    (either because the hardware was removed on the Ajax side or because
    a prior integration release no longer exposes it). Returning True
    lets Home Assistant clear the orphaned entries from the registry.
    """
    coordinator = config_entry.runtime_data
    if coordinator is None or coordinator.account is None:
        return True

    known_ids: set[str] = set()
    for space in coordinator.account.spaces.values():
        known_ids.add(space.id)
        if space.hub_id:
            known_ids.add(space.hub_id)
        known_ids.update(space.devices.keys())
        known_ids.update(space.video_edges.keys())
        known_ids.update(space.smart_locks.keys())

    # Identifiers are namespaced f"{entry_id}_{ajax_id}" (schema v1.3); strip the
    # prefix before comparing against the bare Ajax ids the coordinator tracks.
    prefix = f"{config_entry.entry_id}_"
    return all(
        not (domain == DOMAIN and identifier.removeprefix(prefix) in known_ids)
        for domain, identifier in device_entry.identifiers
    )


# Re-export the typed config entry alias so platforms can `from . import AjaxConfigEntry`
# without mypy --strict complaining about implicit re-export.
__all__ = ["AjaxConfigEntry"]
