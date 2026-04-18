# Changelog

All notable changes to this project will be documented in this file.

## [0.26.4] - 2026-04-18

### Fixed
- **Camera detection events now fire reliably** (resolves #33). `_handle_video_event` in both SSE and SQS managers used to update channel state but never trigger `event.<camera>_detection` — the entity stayed in `unknown` for users without ONVIF. Added `_fire_video_detection_event` helper in `EventHandlerMixin` so SSE and SQS reach parity with the doorbell handler.
- **ONVIF NVR routing no longer mis-attributes events** (likely fixes #114). NVR `sourceAliases.sources[0]` could point to the doorbell, so motion on the front camera was triggering `event.sonnette_2` instead of `event.camera_devant`. The integration now skips the NVR for ONVIF events entirely and connects directly to each camera + doorbell — every Ajax camera runs its own AI and exposes ONVIF events directly, so the NVR added nothing for the events path.
- **SSE proxy users no longer miss button presses or wire-input alarms.** SSE dispatch was missing `BUTTON_EVENTS` and `WIRE_INPUT_EVENTS` (only SQS had them). Imported the mappings and added `_handle_button_event` / `_handle_wire_input_event` to `sse_manager`.
- **SSE doorbell now fires the event entity** like SQS already did.
- **Firmware sensors are now correctly categorised `DIAGNOSTIC`.** `AjaxDeviceSensor` and `AjaxBinarySensor` ignored the `entity_category` key (43 occurrences across `devices/`), so smoke, flood, socket, dimmer, lightswitch and waterstop firmware sensors landed in the main entity list. Added `resolve_entity_category()` helper in `devices/base.py` consumed by both sensor classes plus the existing video-edge sensor (replaces its inline str→enum mapping).
- **Concurrent arm/disarm calls can no longer reach the API out-of-order.** Added per-`space_id` `asyncio.Lock` for `async_arm_space`, `async_disarm_space`, `async_arm_night_mode`, `async_arm_group`, `async_disarm_group`.
- **Optimistic switch updates are no longer overwritten by the next poll.** `night_mode_arm`, `always_active`, `chimes_enabled`, `siren_triggers`, `settingsSwitch` were silently rolled back. Added `mark_optimistic` / `is_optimistic` helpers on `AjaxDevice` reserving an attribute against polling overwrite for 15 s.
- **Panic button now rejects double-taps within 5 s** with a translated `HomeAssistantError` (anti false police dispatch). Translation `panic_cooldown` available in 7 languages.
- **`_security_event_lock` previously declared but unused** is now actually held around the `_skip_state_change_event` flag flips in both SSE and SQS managers — concurrent security events can no longer race the cache-bypass / skip flag.
- **SSE deduplication key now includes `event_code`** (parity with SQS timestamp-based key) so back-to-back events of the same tag with different codes are no longer silently dropped.
- **`userId` no longer leaks into INFO logs**: `sse_url` is masked via `urlsplit` in `sse_client.py`, `api.py` and `__init__.py`; login and refresh logs print only the first 8 characters of `user_id`.
- **SSE callback tasks are drained at stop**: `AjaxSSEClient.stop()` now `gather()`s `_pending_callback_tasks` before closing the session, so they cannot keep writing to the coordinator after `async_shutdown`.
- **Alarm persistent-notification id is now stable** (`f"ajax_alarm_{space.id}_{event_code}"`) instead of `time.time()` per millisecond — a burst of alarms updates the same notification instead of spamming the dashboard.

### Performance
- **~40-50% fewer API calls in proxy/SSE mode.**
  - Cache `async_get_space` (5 s TTL) — coalesces `video_edges` + `smart_locks` fetches inside the same coordinator tick (was hitting `/spaces/{id}` twice per cycle).
  - Skip `video_edges` and `smart_locks` light fetch on 2 cycles out of 3 when SSE/SQS is active (state is event-driven anyway).
  - Skip `groups` fetch on light cycles when SSE/SQS is active (group arm/disarm is pushed in real time and forces a metadata refresh).
- `TCPConnector` limit reduced 20 → 5 (single-tenant proxy; 5 in-flight is plenty for one coordinator and avoids bursting Julien's shared proxy).
- `@functools.lru_cache(maxsize=4096)` on `parse_event_code` — finite key space (~200 codes × 7 languages), called on every SSE/SQS event.

### Changed
- **ONVIF strategy: connect directly to every camera and doorbell, never via the NVR.** Comment in `onvif_manager.async_start()` explains why (channel-mapping unreliability).
- **10 of 11 `tamper` declarations migrated to `self._tamper_binary_sensor()`** in `devices/{transmitter,smoke_detector,life_quality,manual_call_point,motion_detector,waterstop,flood_detector,hub,door_contact}.py`. `siren.py` kept inline because it needs the `is not None` guard on `attributes['tampered']` (helper unconditionally adds the sensor with default `False`).
- **6 `problem` declarations migrated to `self._problem_binary_sensor()`** in `devices/{hub,light,socket,waterstop,lightswitch,dimmer}.py`.
- **Remove dead `LightHandler`** (`devices/light.py` deleted, import + export cleaned from `devices/__init__.py`). The HA light platform instantiates `AjaxDimmerLight` directly — the handler had been unused since 0.25.x.
- **`services.yaml`: integration-level services now expose `fields.config_entry_id`** (Quality Scale Silver requirement) for `get_raw_devices`, `refresh_metadata`, `get_nvr_recordings`, `get_smart_locks`. Translations added in 7 languages.
- **`event.py`: `via_device` set on event sub-entities** so they appear under their parent space in the device hierarchy (Gold).
- **Logbook: new `ajax_camera_detection` bus event** fired by both ONVIF and SSE/SQS managers, with a localised describer that prints `<Camera> a détecté un mouvement / une personne / un véhicule / un animal / un franchissement de ligne` (7 languages) instead of HA's generic `a détecté un événement` fallback.

## [0.26.3] - 2026-04-18

### Changed
- Migrate 15 device handlers to shared helpers in `devices/base.py` (dimmer, door_contact, flood_detector, hub, life_quality, lightswitch, manual_call_point, motion_detector, siren, smoke_detector, socket, transmitter, waterstop — on top of the 4 already done in 0.26.2). Removes ~500 lines of duplicated battery/signal/tamper/temperature/firmware sensor boilerplate.
- Extract `EventHandlerMixin` in `_event_helpers.py`: SSE and SQS managers now share the same implementation of `_find_video_edge`, `_update_video_detection` and `_reset_doorbell_ring` (-196 lines of duplication).
- `coordinator.py`: cache `space_binding` per hub so `async_get_space_by_hub` is only hit on `full_refresh` (not on every poll tick); motion-reset error path drops the unparsable `motion_detected_at` instead of spamming a WARNING every tick.
- `event_codes.py`: extend `EVENT_MESSAGES` to all 7 supported languages (de/nl/sv/uk added) — 861 messages total, up from fr/en/es only.
- Extend `entity.camera.nvr_channel` / `nvr_channel_sub` translation keys so untitled NVR channels follow the user's HA language (7 languages).

### Fixed
- `camera.py`: guard snapshot cache with an `asyncio.Lock` so two concurrent requests can no longer spawn two FFmpeg processes against the same RTSP stream.
- `__init__.py`: add `async_remove_config_entry_device` so users can delete orphaned Ajax devices (e.g. entities removed by previous releases) from the registry; redact sensitive fields before writing `ajax_raw_devices.json`; escape markdown in persistent-notification source/space names to neutralise `[text](javascript:…)` injection.
- `api.py`: bound aiohttp connector pool (`limit=20`, `limit_per_host=10`) to prevent connector exhaustion under stalls; expose `bypass_cache_next()` as public helper instead of poking `_bypass_cache_once` from the coordinator.
- `fr.json`: use "serrures intelligentes" instead of "smart locks" in `lock_not_supported` for terminology consistency.

## [0.26.2] - 2026-04-18

### Security
- Redact sensitive fields (hub_id, mac, IP, tokens…) in `ajax_raw_devices.json` before writing it to disk
- Escape markdown in user-supplied source/space names rendered in persistent notifications to neutralise `[text](javascript:…)` injection

### Fixed
- Previous optimistic fix on device availability was reading the wrong source (`attributes["online"]` instead of `device.online`), leaving entities stuck "Indisponible" — now reads `device.online`

### Changed
- Factorise `_get_recording_nvr_id` into `AjaxSpace.get_recording_nvr_id()` (removes 3× duplication across camera/sensor/binary_sensor)
- Cache `space_binding` results per hub so `async_get_space_by_hub` is only hit on `full_refresh`, not on every tick
- Expose `AjaxRestApi.bypass_cache_next()` as a public helper (coordinator no longer pokes `_bypass_cache_once` directly)
- Bound aiohttp session connector (`limit=20`, `limit_per_host=10`) to avoid connector exhaustion
- Guard `camera.async_camera_image` with an `asyncio.Lock` so two concurrent requests don't spawn two FFmpeg processes
- Remove ~15 empty `_handle_coordinator_update` overrides that merely re-called `async_write_ha_state()`
- `async_migrate_entry`: explicit while-loop-friendly multi-version pattern
- Smart-lock `Store` schema now versioned via `SMART_LOCK_STORE_VERSION` constant with migration hook ready
- ONVIF object detection now recognises `bicycle`/`motorcycle`/`car`/`truck`/`bus` as vehicles
- Camera "sub stream" entity uses `translation_key` so the label follows the user's HA language
- Logbook messages translated across 7 languages (fr/en/es/de/nl/sv/uk)
- `event_codes.EVENT_TYPES` extended with de/nl/sv/uk translations
- `pytest.ini`: scope `DeprecationWarning` suppression to third-party deps so HA breaking changes stay visible

### Removed
- `issues.critical_firmware_update`, `issues.device_offline`, `issues.firmware_update` from strings/translations — redundant with the `UpdateEntity` platform

## [0.26.1] - 2026-04-18

### Security
- Mask `userId`/`sseUrl` and RTSP credentials in INFO logs (DEBUG only)
- Scrub RTSP URL from FFmpeg error messages in snapshot path
- Expand diagnostics `TO_REDACT` (hub_id, mac, ip, camelCase keys, auth headers)
- Drop response body from 401 refresh-token log

### Fixed
- WallSwitch Jeweller relay state (#120) — keep prior SQS fix
- SSE: persistent 401/403 now surfaces as auth failure with exponential backoff
- SQS: reuse a single `aiobotocore` client per thread, fail-fast on IAM errors, callback timeout kept below visibility to prevent redelivery loops
- Video Edge: strict ISO 8601 regex for uptime, defensive divisions on storage/temperature
- Optimistic light/valve rollback preserves the absence of a previous value
- `life_quality`: fix °C vs 0.1°C unit mismatch in temperature comfort check
- `alarm_control_panel`: return `None` on unknown security state (no longer silently maps to DISARMED)
- `MULTI_TRANSMITTER` / `KEYPAD` mapped to correct handlers
- `event_codes.py`: explicit transition overrides + added KeyPad variant `6A`; `M_22_24` reclassified as `arm_failed`
- `force_arm` / `force_arm_night` services now honor the `entity_id` target (previously fanned out to all hubs)
- `event` platform: unregister entities cleanly on reload to avoid stale dispatch targets
- `sensor` / `binary_sensor`: consult `device.online` attribute (not `attributes` dict) for availability
- Panic button: disabled by default and no longer advertised as `IDENTIFY`
- Doorbell: drop misleading `OCCUPANCY` binary sensor (press handled via event platform)
- ONVIF: `asyncio.Lock` on client map; subscription loss now triggers recreation on the next poll

### Changed
- Raise `ConfigEntryAuthFailed` on authentication errors to trigger the Home Assistant reauth flow
- `HomeAssistantError` across `number`, `select`, `switch`, `lock` now use `translation_domain` + `translation_key`
- Replace hardcoded unit strings with Home Assistant constants (`PERCENTAGE`, `UnitOfTime`, `UnitOfTemperature`, `CONCENTRATION_PARTS_PER_MILLION`, `DEGREE`, `UnitOfInformation`)
- Pass `config_entry` to the `DataUpdateCoordinator` constructor
- `manifest.json`: add `quality_scale: bronze`, remove `aiohttp` (provided by core)
- Extract `_parse_door_state_from_wiring` helper in the coordinator (4× duplication removed)
- Add shared helpers in `devices/base.py` (`_battery_sensor`, `_tamper_binary_sensor`, `_temperature_sensor`, `_signal_strength_percent_sensor`, `_problem_binary_sensor`, `_firmware_version_sensor`)
- Track `call_later` handles and background tasks in SSE / SQS managers for clean teardown
- Translations audit across 7 languages: add missing exceptions (`hub_not_found`, `device_not_found`, `no_api_key`, `lock_not_supported`), `services.get_smart_locks`, `config.step.dhcp_confirm`; remove orphan `options.step.dhcp_confirm`; fix double-escaped newlines in French; expand `icons.json` (event, lock, light, update, valve)
- SQS (relay): write state to `is_on` attribute rather than `state` (#120)

## [0.26.0] - 2026-04-13

### Added
- Smart lock event entity exposing `doorbell_pressed` and `door_left_open` as triggers in automation UI (#88)
- Translations for smart lock events in all 7 languages

### Fixed
- NO_EOL/ONE_EOL door state detection: use OR logic between `externalContactState` and `contactState` to support both static and dynamic firmwares (#103)

## [0.25.1] - 2026-04-11

### Fixed
- SQS connection error with newer botocore: bump `aiobotocore` minimum to `>=2.22.0` (#116)

### Changed
- Pre-commit hooks updated (#115)

## [0.25.0] - 2026-03-29

### Added
- Smart lock doorbell button press event (`M_7E_40`) and door left open warning (`M_7E_37`) for LockBridge L3 (#88)

### Fixed
- AI detection binary sensors (human, vehicle, pet) stuck ON: now cleared when object type disappears or motion ends (#114)

### Changed
- Pre-commit hooks updated (#113)

## [0.24.0] - 2026-03-19

### Added
- ONVIF `Rule` extraction: detection zone name passed as `rule` attribute in camera detection events for zone-based automations

## [0.23.0] - 2026-03-19

### Added
- Camera detection event entities (motion, human, vehicle, pet, line_crossing) for all Video Edge cameras
- Direct ONVIF connection to doorbell for reliable ring detection
- Event platform translations for all 7 languages (en, fr, de, es, nl, sv, uk)

### Changed
- Route doorbell ring events directly to doorbell device instead of NVR channel lookup
- Remove verbose ONVIF and SQS debug logging (only log state changes)

## [0.22.0] - 2026-03-19

### Added
- Video Doorbell support: `DOORBELL` VideoEdge type with event entity for ring detection
- MultiTransmitter `WIRE_INPUT` devices included in fast polling loop (5s when enabled) (#103)
- `EventDeviceClass.BUTTON` and `EventDeviceClass.DOORBELL` for proper event entity naming

### Changed
- Add `from __future__ import annotations` to all Python files
- Add `__slots__` to `AjaxLock` entity class
- Pre-commit hooks updated (#111)

## [0.21.2] - 2026-03-19

### Fixed
- Coordinator crash when `powerConsumedWattsPerHour` or `currentMilliAmpers` is `null` in API response (#112)

## [0.21.1] - 2026-03-19

### Fixed
- Restore NO_EOL `wiringSchemeSpecificDetails.contactState` support for MultiTransmitter wired sensors (#103)

## [0.21.0] - 2026-03-18

### Added
- ONE_EOL wiring scheme support: read door state from `wiringSchemeSpecificDetails.contactDetails.contactState` (#103)

## [0.20.1] - 2026-03-16

### Fixed
- Exclude `externalContactState` and `externalContactTriggered` from device PUT payload to prevent transient door open state when toggling settings (#103)

## [0.20.0] - 2026-03-14

### Added
- Event platform for Button, DoubleButton, SpaceControl, and Doorbell devices with event types: single_press, double_press, long_press, panic, emergency, ring

### Fixed
- Device update API 422 error: keep `deviceTransmissionPowerMode` in PUT payload as required by API (#103)

## [0.19.5] - 2026-03-08

### Fixed
- Transmitter switches (night mode, always active, siren triggers) not persisting: add `api_nested_key: wiredDeviceSettings` so API receives correct nested payload (#103)
- SQS message re-queuing when callback returns false: make unhandled messages visible again instead of deleting (#105)
- SQS callback error handling: restore try/except around `future.result()` to catch timeouts and exceptions (#105)

## [0.19.4] - 2026-03-03

### Fixed
- MultiTransmitter NO_EOL/ONE_EOL wired sensors stuck closed: `wiringSchemeSpecificDetails.contactState` is a static config value that was overwriting the correct `externalContactState` (#103)
- Auth race condition: `token_version` was captured after HTTP response instead of before, causing unnecessary token recovery when concurrent requests refreshed the token (#97)
- Remove unused `repairs.py` that caused HA startup error (`Invalid repairs platform`)

### Changed
- Pre-commit hooks updated: ruff v0.15.4, bandit 1.9.4 (#106)

## [0.19.3] - 2026-02-25

### Fixed
- Transmitter binary sensor fallback to `door_opened` when API returns `externalContactState` instead of `externalContactTriggered` (#103)
- SQS message handling resilience: prune stale messages >300s, don't delete messages for unknown hubs, explicit boolean return contract, poison-pill prevention (#101)

## [0.19.2] - 2026-02-24

### Fixed
- Transmitter (MultiTransmitter wired sensors) contact state stuck at closed: `externalContactTriggered` was only updated in fast polling loop (disabled by default), now extracted in main polling cycle (#103)
- Store `customAlarmType` in camelCase for TransmitterHandler device class compatibility

## [0.19.1] - 2026-02-23

### Fixed
- Proxy auth failures: adaptive token TTL learns actual token lifetime from 401 responses instead of assuming 15-minute Ajax API default (#97)
- Reduce login cooldown from 120s to 30s to minimize downtime when proxy invalidates tokens early (#97)
- Skip refresh endpoint after 3 consecutive failures and go straight to full login (#97)
- Proactive refresh now falls back to login immediately instead of silently ignoring failure (#97)

## [0.19.0] - 2026-02-23

### Changed
- Comprehensive code review and refactoring across 21 files (223 insertions, 308 deletions)
- Centralize device handlers (DEVICE_HANDLERS, DIMMER_RAW_TYPES, get_device_handler) in single location, eliminating duplication across binary_sensor, sensor, switch, and light platforms
- Fix FFmpeg zombie processes: add process.kill() + await process.wait() for proper cleanup
- ONVIF PullPoint auto-reconnect with exponential backoff on subscription failures
- Fix ISO 8601 duration parser to support days component
- Replace string literal "enum" with SensorDeviceClass.ENUM in WaterStop handler
- Use UTC-aware datetime throughout codebase
- Add 8 missing sensitive fields to diagnostics redaction
- Add SSE sock_read timeout (300s) for dead proxy detection
- Reduce SQS thread.join timeout from 25s to 5s for faster shutdown
- Safe sensitivity label accessors in glass break and motion detector handlers
- Replace assert statements with proper error handling in coordinator
- Remove dead code (ALL_EVENTS list, SUPPORTED_LANGUAGES dict)
- Move runtime imports to top-level for faster module loading
- Update pre-commit hooks configuration

## [0.18.4] - 2026-02-16

### Fixed
- SSE client stale token causing repeated 401 auth failures in proxy mode: SSE now fetches the latest session token on each reconnect instead of reusing the token captured at init time (#97)
- Auth recovery in proxy mode (no refresh token): skip refresh and go straight to full login instead of raising an unhandled exception
- Double token refresh race condition in `_request_no_response` by adding token version check

## [0.18.3] - 2026-02-13

### Fixed
- Stop continuous x265 transcoding when nobody is watching: `is_streaming` now returns `False` instead of `available`, preventing HA from maintaining active stream pipelines for idle cameras

## [0.18.2] - 2026-02-13

### Changed
- Disable `use_stream_for_stills` to use single-frame FFmpeg instead of full x265 stream pipeline
- Increase snapshot cache duration from 30s to 60s to reduce FFmpeg calls on multi-camera dashboards
- Use sub stream (640x480) for snapshots instead of main stream (2560x1440) for ~4x faster decode
- Extract `_build_rtsp_url()` method for stream type selection in snapshot and live stream paths

## [0.18.1] - 2026-02-13

### Fixed
- Proactive token refresh: refresh session token before expiry (2 min before 15-min TTL) to prevent 401 cascades with proxies (#97)
- Transient auth error tolerance: tolerate up to 3 consecutive auth failures before triggering reauth flow in HA (#97)

## [0.18.0] - 2026-02-12

### Added
- Handle ArmAttempt SQS event (arm attempt with device malfunction) (#95)

### Fixed
- Authentication 429 rate limiting when refresh token fails repeatedly (#97)
  - Login cooldown (120s) prevents rapid re-authentication
  - Token freshness check avoids redundant refresh from concurrent requests
  - Better error logging for refresh token 401 responses

### Changed
- Bump actions/setup-python from 5 to 6

## [0.17.0] - 2026-02-11

### Added
- Smart lock "last changed by" sensor showing who locked/unlocked the device
- Hub system events handling: firmware update notifications and connectivity status changes (#95)

## [0.16.0] - 2026-02-09

### Added
- Smart lock thumbturn (knob) lock/unlock event codes: M_7E_20, M_7E_27 (#88)

### Changed
- Camera snapshot JPEG quality set to maximum (-q:v 2) for sharper still images

## [0.15.0] - 2026-02-09

### Added
- SpaceControl (remote control) entity support: battery, signal strength, tamper (#93)

## [0.14.9] - 2026-02-08

### Fixed
- SmartLockYale no longer triggers unknown device type warnings every polling cycle (#88)
- Yale cloud lock skip log reduced from INFO to DEBUG to prevent log spam
- SSE/SQS-discovered smart locks now persist across reboots using HA storage (#88)

## [0.14.8] - 2026-02-07

### Fixed
- System state no longer gets stuck at armed_home when full arming after partial arm (#91)
- Added fallback state update when metadata refresh fails after security events

Thanks to @Kolia56 for the contribution (#91)

## [0.14.7] - 2026-02-06

### Fixed
- SSE group event deduplication now includes group ID to prevent multi-zone updates from being rejected as duplicates (#90, #32)
- Reduced debouncer cooldown from 1.5s to 0.5s for faster real-time zone updates
- Reduced SSE sleep delay from 1.0s to 0.3s before group state refresh
- Added proxy cache bypass after SSE security events for fresh group states

Thanks to @Kolia56 for the contribution (#90)

## [0.14.6] - 2026-02-05

### Fixed
- Yale cloud locks now filtered at discovery time (no SSE events, minimal API data)

## [0.14.5] - 2026-02-05

### Added
- New `ajax.get_smart_locks` diagnostic service to fetch smart lock data from API and SSE/SQS-discovered locks

## [0.14.4] - 2026-02-04

### Added
- DHCP discovery now shows dialog to associate discovered hub with existing configuration
- Option to add MAC address to existing config entry without reconfiguring

### Fixed
- Translations properly formatted for all languages

## [0.14.3] - 2026-02-04

### Fixed
- DHCP discovery no longer shows duplicate notifications for already configured hubs
- Hassfest badge link in README now points to correct workflow

## [0.14.2] - 2026-02-04

### Added
- Yale cloud lock detection: after 5 minutes without SSE events, locks are marked unavailable with guidance to use native Yale integration

### Fixed
- CI workflow using incorrect setup-python action version
- Ruff SIM910 lint error in coordinator.py

## [0.14.1] - 2026-02-03

### Fixed
- Smart locks discovered from SSE/SQS events now get entities created dynamically
- Polling cleanup no longer removes SSE/SQS-discovered smart locks missing from API
- Entity deduplication uses HA entity registry instead of fragile in-memory sets

## [0.14.0] - 2026-02-01

### Added
- Smart lock support (LockBridge Jeweller): read-only lock entity with state from SSE/SQS events
- Smart lock door binary sensor (open/close state)
- Smart lock event code mapping for reliable lock/unlock and door state
- Auto-discovery of smart locks from SSE/SQS events
- Smart lock translations in 7 languages (de, en, es, fr, nl, sv, uk)

## [0.13.4] - 2026-01-30

### Fixed
- Fix crash when other integrations register devices with non-standard identifier tuples (#89)

## [0.13.3] - 2026-01-30

### Changed
- Comprehensive code review: fix race conditions, improve thread safety, deduplicate events
- SQS client now uses `threading.Event` for safe thread signaling
- SQS poll errors use exponential backoff (5s→30s max)
- SQS manager deduplicates events within 5s window
- SSE manager cleans dedup dict in-place to avoid reassignment
- Login error handling catches specific exceptions instead of bare `Exception`
- SQS event handler propagates `asyncio.CancelledError`
- Extract `_build_hub_info()` helper to remove device info duplication
- Modernize CI pipeline and add removal documentation

### Fixed
- Race condition on `_bypass_cache_once` flag in API client
- All translation files updated and completed (de, es, fr, nl, sv, uk)
- Remove URL from `proxy_url` description for hassfest compliance

## [0.13.2] - 2026-01-27

### Fixed
- Video edge uptime sensor now properly uses timestamp device class (#85)
- Enum sensor options for translations (#84)
- Auto-remove devices deleted from Ajax account

## [0.13.1] - 2026-01-26

### Added
- LightSwitchDimmer support with brightness control
- Manual Call Point (MCP) fire alarm device support
- `verify_ssl` option for self-signed certificates
- LightSwitch settings entities (touch sensitivity, touch mode, LED, child lock)

### Changed
- Uptime sensor converted to timestamp entity for better UX

### Fixed
- SSE group arm/disarm events not refreshing group states (#32)
- Proxy startup stability and cache debug logs
- LightSwitch multi-gang channel control with additionalParam format

## [0.13.0] - 2026-01-25

### Added
- Proxy cache optimization support:
  - `X-User-Id` header sent on all requests for per-user rate limiting
  - `X-Cache-Control: no-cache` support to bypass proxy cache when needed
  - Reading `X-Suggested-Interval` header to dynamically adjust polling interval
  - Reading `X-Cache-TTL` and `X-Cache` (HIT/MISS) headers from proxy responses
- New coordinator method `async_request_refresh_bypass_cache()` for fresh data after events
- Cache bypass automatically triggered after SSE events (arm/disarm/device changes)
- SSE client now sends `X-User-Id` header for consistent rate limiting

### Changed
- Polling interval now respects proxy suggestions (30s/60s/120s based on load)
- Error recovery in alarm_control_panel uses cache bypass for accurate state

### Technical
- Requires proxy version >= 0.13.0 with cache support for full optimization
- Backward compatible with older proxy versions (headers ignored if not supported)

## [0.12.0] - 2026-01-25

### Added
- X-Client-Version header for proxy mode (required by proxy >= 0.11.2)

### Fixed
- Multi-gang LightSwitch channel detection (#82)

## [0.11.2] - 2026-01-25

### Changed
- Improved code quality from comprehensive review
- Narrowed exception handling to specific API errors
- Added `rooms_map` and `users` fields to AjaxSpace dataclass
- Refactored WaterStop attributes extraction
- Added `entity_category: diagnostic` to firmware sensors

### Fixed
- Resolved all mypy type checking errors
- Fixed version mismatch in startup log
- Fixed timezone-aware timestamps using `datetime.now(UTC)`
- Fixed MAC address validation for RTSP URL building
- Fixed battery sensor condition for mains-powered WallSwitch

## [0.11.1] - 2026-01-23

### Added
- LifeQuality: temperature and humidity problem binary sensors (out of comfort range detection)

### Changed
- Updated supported devices list in README

## [0.11.0] - 2026-01-23

### Added
- Support for new device types:
  - DoorProtectSPlus (door contact with shock/tilt sensors)
  - MotionCamSPhod (motion detector with photo verification)
  - CurtainOutdoorJeweller (outdoor curtain motion detector)
  - GlassProtectS (glass break detector)
  - LightSwitchTwoChannelTwoWay (two-channel light switch)
  - StreetSirenSDoubleDeck (outdoor siren)
  - HomeSirenS (indoor siren)
  - INDOOR video edge camera type (indoor WiFi camera)

### Fixed
- Active connection sensor random order issue (#81): channels now sorted alphabetically

## [0.10.0] - 2026-01-23

### Added
- **Transmitter device handler**: Full support for universal wired sensor modules
  - Dynamic device class based on alarm type (intrusion, opening, fire, flood, gas, CO)
  - External contact binary sensor with configurable mode (NC/NO)
  - Switches: always active, night mode, accelerometer, siren triggers
  - Sensors: contact mode, alarm type, alarm mode, power supply mode, arm/alarm delays
- **LifeQuality device handler**: Air quality sensor support
  - CO2, temperature, humidity sensors
  - CO2 problem binary sensor
  - Indicator light switch
- **Enhanced FireProtect 2 Plus**: Siren trigger switches (smoke, CO, temperature)
- **Enhanced LeaksProtect**: Always active switch
- **Enhanced MotionDetector**: Siren trigger for motion switch
- **Transmitter fast polling**: Real-time updates for wired contact sensors

### Fixed
- Socket power monitoring sensors using correct attribute names
- StreetSiren externally powered sensor attribute lookup
- Consistent use of normalized attribute names across all handlers

## [0.9.0] - 2026-01-23

### Added
- Line crossing detection for Video Edge cameras (ONVIF)

## [0.8.1] - 2026-01-22

### Fixed
- WaterStop valve commands: use `SWITCH_ON`/`SWITCH_OFF` instead of `OPEN`/`CLOSE`

### Changed
- Increase rate limit from 30 to 60 requests per minute

## [0.8.0] - 2026-01-22

### Added
- **WaterStop device support**: Valve control switch for water leak protection
- **SocketOutlet device support**: Smart socket control
- **Siren controls**: Manual siren activation switches for supported devices
- **ONVIF real-time events**: Motion, human, vehicle, pet detection via ONVIF protocol
- **GitHub CI/CD**: HACS validation workflow, issue templates, PR template

### Changed
- **API resilience improvements**:
  - Client-side rate limiting (30 req/60s) with non-blocking wait
  - Retry with exponential backoff for transient errors (5xx, network, timeout)
  - Reuse Home Assistant aiohttp session for connection pooling
- **Coordinator**: Added Debouncer (1.5s) to prevent SQS/SSE event flooding
- **Entity naming**: Use modern `_attr_has_entity_name` pattern
- **Memory optimization**: Added `__slots__` to entity classes

### Fixed
- Invalid fields in hacs.json (removed `iot_class` and `description`)

## [0.7.78] - 2026-01-20

### Added
- **Reconfigure flow**: Update credentials without removing the integration (Gold)
- **Repair issues**: Framework for firmware update and device offline notifications (Gold)

### Fixed
- Typos in diagnostics.py TO_REDACT (`aws_acces_key_id` → `aws_access_key_id`, `que_name` → `queue_name`)

### Changed
- Moved `AjaxConfigEntry` type alias from `__init__.py` to `const.py` (Platinum pattern)

## [0.7.77] - 2026-01-20

### Added
- **NVR multi-channel camera support**: Each NVR channel now creates its own camera entity with correct RTSP URL
- **NVR channel sub streams**: Low bandwidth stream for each channel (disabled by default)
- **AI detection sensors on source camera**: Motion, human, vehicle, pet detections from NVR are now attached to the source camera device with `linked_camera` attribute
- **Diagnostic service**: `ajax.get_nvr_recordings` to test NVR recordings API endpoint

### Fixed
- **Issue #75**: Crash when firmware `updateStatus` is null - now handles null values properly
- **Issue #74**: Notifications appearing even when notification filter set to "None"

### Notes
- NVR recordings API returns 404 - recordings not accessible via Ajax Cloud API
- NVR ONVIF doesn't expose Recording/Replay capabilities - recordings only available via Ajax app

## [0.7.76] - 2026-01-19

### Added
- **Button device sensors**: button_mode, button_brightness, false_press_filter
- **Camera sub stream**: Low bandwidth stream for 3G/4G connections (disabled by default)
- **Socket LED brightness selector**: MIN/MAX brightness configuration
- **FireProtect 2 switches**: CO alarm, high temperature alarm, rapid temperature rise alarm
- **Binary sensors**: high_temperature, rapid_temperature_rise, steam detection

### Changed
- LED brightness selector now hidden when LED indication is disabled
- Switch state changes now notify listeners for immediate UI update
- Updated README with complete device support documentation

## [0.7.75] - 2026-01-19

### Added
- **Video Edge camera entities** with RTSP streaming support
  - TurretCam, TurretCam HL, BulletCam, BulletCam HL, MiniDome, MiniDome HL
  - Ajax-specific RTSP URL format: `/{mac}-{channel}_{stream}`
  - Port 8554 (Ajax default)
- **Firmware update entities** for Video Edge devices and Hub
- **RTSP/ONVIF credentials configuration** in integration options
- LeaksProtect siren on leak switch

### Fixed
- Video Edge iteration bug (was iterating over keys instead of values)

## [0.7.68] - 2026-01-14

### Fixed
- Multi-gang LightSwitch channels - use string format "CHANNEL_1"/"CHANNEL_2" for API commands (#26)

## [0.7.67] - 2026-01-14

### Fixed
- Doorbell device type mapping - add `MotionCamVideoDoorbell` (#50)
- Multi-gang LightSwitch channels - use 0-indexed channels for API commands (#26)
- DoorProtect sensors - use `OPENING` device_class instead of `DOOR` (#45)
- FireProtect CO sensor - use `CO` device_class instead of `GAS` (#53)

### Added
- Dutch translations (nl.json)
- Entity naming - use device name for main entity on device (PR #52)

### Changed
- Bump actions/checkout from 4 to 6
- Update Swedish translations

## [0.7.51] - 2026-01-11

### Fixed
- SSE manager now properly awaits metadata refresh for group events (#32)

## [0.7.50] - 2026-01-11

### Fixed
- Include video_edges data in `ajax.get_raw_devices` service output (#33)

## [0.7.49] - 2026-01-11

### Fixed
- Video edge channels type validation to prevent `'str' object has no attribute 'get'` errors (#26)
- Force full metadata refresh for group arm/disarm events (#32)

### Added
- Space selection for multi-space accounts - choose which spaces to load (#31)

## [0.7.48] - 2026-01-11

### Fixed
- Shock sensitivity mapping values (0=low, 4=normal, 7=high) (#30)
- Video Edge AI detection sensors showing unknown state (#33)
- Polling option changes now apply immediately without reboot

### Added
- Handle `grouparm` and `groupdisarm` events for zone-based arming (#32)
- Debug logging for multi-gang channel switch commands (#26)
- Polling settings in integration options (Options → Polling Settings)
- `ajax.refresh_metadata` service for manual metadata refresh
- Option to enable/disable door sensor fast polling (5s interval)

### Changed
- **API optimization**: Reduced polling from ~7 calls/hub to 2 calls/hub per cycle
- Light polling (every 30-60s): Only hub state + devices
- Full metadata refresh (rooms, users, groups): Hourly instead of every poll
- Polling interval: 30s when disarmed, 60s when armed (SSE/SQS handles real-time)
- Door sensor fast polling disabled by default (can be enabled in options)

## [0.7.47] - 2025-01-10

### Fixed
- Video Edge AI detection sensors not updating during polling (#25)

## [0.7.46] - 2025-01-10

### Added
- Video Edge camera support (Bullet, Turret, MiniDome) (#25)
- AI detection sensors: motion, human, vehicle, pet
- Camera diagnostic sensors: IP address, firmware version

## [0.7.45] - 2025-01-10

### Fixed
- Round sensor values to avoid jitter on last decimal (#29)

### Changed
- Use HA native translations for standard device classes (#27)

## [0.7.43] - 2025-01-08

### Fixed
- Add SSE event deduplication (#28)
- Handle tamperopened SSE event (#28)
- Multi-gang LightSwitch fixes

## [0.7.42] - 2025-01-07

### Fixed
- Use correct API path for cameras endpoint

## [0.7.41] - 2025-01-06

### Added
- Multi-gang LightSwitch support (#26)

## [0.7.40] - 2025-01-05

### Added
- LightSwitch device type mappings (#26)
- Include cameras in get_raw_devices service

## [0.7.38] - 2025-01-04

### Added
- Proxy URL option in integration settings
- Tamper sensor support for TWO_EOL wiring scheme (#23)

## [0.7.36] - 2025-01-03

### Fixed
- SSE/proxy mode improvements (#22)

## [0.7.34] - 2025-01-02

### Fixed
- WireInput device support improvements (#23)

## [0.7.32] - 2025-01-01

### Fixed
- Fibra device support (#23)

## [0.7.29] - 2024-12-30

### Added
- Fibra device type mappings (#23)
- Scenario events handling (#22)

## [0.7.26] - 2024-12-28

### Fixed
- WireInput/MultiTransmitter fixes (#13)

## [0.7.24] - 2024-12-26

### Added
- Fast polling for door sensors (#21)
- Support armwithmalfunctions states (#20)

## [0.7.23] - 2024-12-25

### Added
- Button scenario events (#15)

### Fixed
- Polling and state protection (#17, #18)

## [0.7.18] - 2024-12-22

### Added
- Button device support (#15)

### Fixed
- Night mode detection (#8)
- User area assignments (#11)

## [0.7.12] - 2024-12-20

### Fixed
- Door open/close state (#9)
- Night mode state (#8)

## [0.7.8] - 2024-12-16

### Added
- Group arming support
- English translations

## [0.7.7] - 2024-12-15

### Added
- MultiTransmitter Fibra support

## [0.7.6] - 2024-12-14

### Fixed
- Auth errors and sensors after reboot
- Token refresh for proxy mode

## [0.6.0] - 2024-11-15

### Changed
- Migration to official Ajax REST API
- New AWS SQS real-time events

## [0.5.0] - 2024-10-01

### Added
- EOL sensors via MultiTransmitter
- Real-time streaming improvements

## [0.4.0] - 2024-07-10

### Added
- Automation events
- Group/zone support
- 2FA/TOTP support
