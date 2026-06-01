# Architecture — Ajax Security System integration

A `cloud_push` Home Assistant integration for Ajax Systems. State is fetched
over the Ajax REST API and kept fresh in real time by **either** an SSE stream
(proxy mode) **or** an AWS SQS queue (direct mode) — the two transports are
mutually exclusive per config entry.

## Data flow

```
Ajax cloud ──REST──▶ api.AjaxRestApi ──▶ coordinator.AjaxDataCoordinator ──▶ entities (CoordinatorEntity)
                ▲                              ▲   (coordinator.account: AjaxAccount)
                │                              │
   SSE (proxy) ─┘   sse_client → sse_manager ──┤  real-time pushes mutate the in-memory
   SQS (direct) ─   sqs_client → sqs_manager ──┘  account, then async_set_updated_data()
```

- **Polling** is adaptive (`AjaxDataCoordinator.update_interval`): faster when armed / for door sensors when disarmed (Ajax pushes no events while disarmed).
- Real-time managers mutate `coordinator.account` in place and notify HA via the synchronous `@callback async_set_updated_data` (NOT a coroutine — never wrap it in a task).

## File map

| File / dir | Role |
|---|---|
| `__init__.py` | `async_setup_entry` / `async_unload_entry`, **`async_migrate_entry`** (ConfigEntry schema migrations), HA-area sync. |
| `_services.py` | Integration service registration (`force_arm`, `force_arm_night`, `get_raw_devices`, `refresh_metadata`, `get_nvr_recordings`, `get_smart_locks`) + handlers. |
| `coordinator.py` | `AjaxDataCoordinator` — composed from the `_coordinator_*` mixins. Exposes `entry_id: str` for entity namespacing. |
| `_coordinator_init.py` | Coordinator init, stores. |
| `_coordinator_devices.py` | Device reconciliation pipeline, stale-device cleanup. Inherits `_device_normalize`. |
| `_device_normalize.py` | `AjaxDeviceNormalizeMixin` — stateless attribute normalisation (raw Ajax field names → handler shapes) + motion-impulse expiry. |
| `_coordinator_door_poll.py` | `AjaxDoorPollingMixin` — fast 5 s door/transmitter/wire-input polling while disarmed / night mode. |
| `_coordinator_state.py` | Payload parsers (security state, device type), video-edge / smart-lock pollers. |
| `_coordinator_spaces.py` | Space / hub / users / groups parsing, night mode. |
| `_coordinator_arm.py` | Arm / disarm / night / panic / group services + per-space locks + HA-action tracking. |
| `_coordinator_events.py` | SSE/SQS event-filter options, persistent-notification dispatch. |
| `_coordinator_onvif.py` | ONVIF orchestration across spaces, partial-cameras repair issue. |
| `api.py` | `AjaxRestApi` — auth (login/2FA/refresh/recover), transport (`_request` with retry/backoff/401-reauth), endpoint wrappers. |
| `models.py` | Dataclasses: `AjaxAccount`, `AjaxSpace`, `AjaxDevice`, `AjaxVideoEdge`, `AjaxSmartLock`, enums; optimistic-update helpers (`mark_optimistic` / `is_optimistic`). |
| `sse_client.py` / `sse_manager.py` | SSE transport + event handlers (proxy mode). |
| `sqs_client.py` / `sqs_manager.py` | AWS SQS transport (daemon thread) + event handlers (direct mode). |
| `event_maps.py` | **Single source of truth** for the event-tag / event-code lookup tables shared by both managers (no HA/transport imports). |
| `_event_helpers.py` | `EventHandlerMixin` shared by both managers (video-edge lookup, detection state, doorbell/video reset, discovery throttle). |
| `_discovery.py` | `connect_new_entity_signal` — dynamic-entity discovery; dedupes on **`entity.unique_id`**. |
| `_ids.py` | **Single source of truth** for config-entry-scoped registry ids (`device_identifier`, `entity_unique_id`). |
| `event_codes.py` | Vendored Ajax event-code table (excluded from coverage). |
| `config_flow.py` / `config_flow_options.py` | `ConfigFlow` (user/direct/proxy/2FA/select_spaces/dhcp/reauth/reconfigure); `OptionsFlow` lives in `config_flow_options.py`. |
| `diagnostics.py` | Redacted config-entry / device diagnostics. |
| `devices/` | One handler per Ajax device type (`base.py` + 18 handlers); `DEVICE_HANDLERS` map + `get_device_handler` / `is_dimmer_device`. |
| Platforms | `sensor.py`, `binary_sensor.py`, `switch.py` (+ `_switch_entity.py` / `_switch_dimmer.py`), `number.py`, `select.py`, `light.py`, `valve.py`, `lock.py`, `camera.py`, `event.py`, `alarm_control_panel.py`, `device_tracker.py`, `update.py`, `button.py`. |

## Identifier namespacing (schema v1.3)

Every entity `unique_id` is `f"{entry_id}_{...}"` and every device identifier is
`device_identifier(entry_id, raw)` → `(DOMAIN, f"{entry_id}_{raw}")`, so multiple
Ajax accounts never collide. The `async_migrate_entry` v1.2→v1.3 step renames
existing registry rows **in place** (preserving `entity_id` / history). The
`_ids.py` helper is the single source of truth shared by runtime and migration.

Any code that **looks up or scans** the device registry must account for the
prefix: `_async_cleanup_stale_devices`, `async_remove_config_entry_device` and
`diagnostics.target_device_id` strip it before comparing against bare Ajax ids.
The `_event_entities` dispatch map is intentionally keyed by the **bare**
`{device_id}_{event_key}` (it is per-coordinator, so needs no namespacing, and
the SSE/SQS managers fire by bare id).

## How to add a new device type

1. Add the type to `models.DeviceType` and the alias table in `_coordinator_state._DEVICE_TYPE_MAP`.
2. Create `devices/<type>.py` subclassing `AjaxDeviceHandler`; implement the relevant `get_sensors` / `get_binary_sensors` / `get_switches` / `get_selects` / `get_numbers` / `get_valves`.
3. Register it in `devices/__init__.DEVICE_HANDLERS`.
4. Detector real-time values must read **both** the SSE key and the SQS/REST key (e.g. `smoke_detected` *or* `smoke_alarm`).
5. Add tests under `tests/` and translations in `strings.json` + `translations/*.json`.

## How to add a new platform

1. Create `<platform>.py` with `async_setup_entry` that iterates `coordinator.account` and instantiates entities.
2. Entities subclass `CoordinatorEntity[AjaxDataCoordinator]`; build `unique_id` as `f"{self.coordinator.entry_id}_{...}"` and `device_info` identifiers via `device_identifier(self.coordinator.entry_id, raw)`.
3. Wire dynamic discovery with `connect_new_entity_signal` (builders return `(key, entity)` pairs; the key is informational — dedup is on `entity.unique_id`).
4. Add the platform to `PLATFORMS` in `__init__.py`.
