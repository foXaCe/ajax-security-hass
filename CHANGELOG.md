# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.32.1] - 2026-06-13

### Fixed
- **Per-group arm indicators could stay wrong until the next hourly refresh (#150).** The per-group arm state lives only on the `/groups` endpoint, and light (state-only) polls skipped that fetch while SSE/SQS was active — so a dropped or stale real-time group event left a group panel showing the wrong state (most visibly: arming a second group, or disarming the last one, stayed visibly wrong). Group-mode hubs now refetch group states on every poll, so the indicator self-heals within one poll cycle; non-group hubs keep skipping the fetch.

## [0.32.0] - 2026-06-01

An overhaul pass: latent bugs fixed, dead code removed, test coverage raised from **32 % to 97 %** (433 → 1821 tests), and entity/device identifiers namespaced per account. mypy `--strict` clean, ruff clean, validated on a live install.

### ⚠️ To be aware of
- **Entity & device identifiers are now namespaced per config entry (schema v1.2 → v1.3).** This makes it possible to run **multiple Ajax accounts** in one Home Assistant without entity/device collisions. Existing setups migrate **automatically** on upgrade: each `unique_id` is renamed *in place*, so your **`entity_id`s, history, dashboards and automations are preserved** — no action required. (Single-account setups see no functional change.)

### Added
- Comprehensive unit-test suite: coverage 32 % → 97 %, with a migration "guarantee" test pinning that the v1.3 migration reproduces the exact runtime `unique_id` format (so the in-place rename can never orphan an entity).

### Fixed
- **Motion detection was never reported in direct (SQS) mode.** The SQS motion handler wrote the attribute `motion` while the motion binary sensor reads `motion_detected` (the key the SSE handler already writes), so a real-time motion event never turned the sensor on when armed in direct mode. It now writes `motion_detected` (+ `motion_detected_at`).
- **Diagnostics download crashed for every proxy / SSE user.** `_connectivity_snapshot` tried to *call* `AjaxSSEClient.is_connected`, which is a `@property` returning a `bool` — `TypeError: 'bool' object is not callable` aborted the whole download. It now reads the property without calling it.
- **CombiProtect glass-break was silent in direct (SQS) mode.** Its glass-break sensor only read the SSE key `glass_break_detected`; SQS writes `glass_alarm`. It now reads both (matching the standalone GlassProtect sensor).
- **Door-sensor fast-polling could silently die.** The background loop iterated `account.spaces` while awaiting an API call; a hub discovered concurrently raised `RuntimeError: dictionary changed size during iteration` and killed the task until reload. The loop now snapshots the spaces first.
- **DoorProtect Plus / GlassProtect config switches bounced back.** The shock-sensor / accelerometer / extra-contact / ignore-simple-impact / blink-while-armed switches set an optimistic state, but the poller overwrote it without checking the optimistic guard. The poller now honours the reservation (parity with the other config switches).
- **Doorbell "Last ring" sensor went `unknown` on every ring.** The `TIMESTAMP` sensor received an ISO **string**; Home Assistant requires a `datetime`. The stored value is now parsed back to a `datetime`.
- **Duplicate hub-battery sensor.** `hub_battery` was defined twice (space-level *and* hub-level), producing a colliding `unique_id`; Home Assistant silently dropped one entity and logged an error on every start. It is now defined once.
- **Manual Call Point colour sensor errored for some colours.** The `ENUM` options listed only red/blue/white/black; a yellow/green/graphite device raised `ValueError`. The full Ajax colour set is now declared (with translations in all 7 languages).
- **Socket external-power monitoring was wired to the wrong key.** The socket sensor gated/read `external_power` (never populated) while the REST poller writes `externally_powered`, and the SSE power event wrote a third, dead key. All three now agree on `externally_powered`.
- **Auth failures on the rooms endpoint bypassed reauth.** `async_get_rooms` errors were swallowed by a broad `except`, so a token expiry surfacing there never counted toward the reauth threshold. `AjaxRestAuthError` now propagates (parity with the hub/users/groups fetches).
- **External Contact entity lagged ~30 s in proxy mode (#151).** `extcontactopened` / `extcontactclosed` real-time events were routed through the door handler and wrote `door_opened`, so the *External Contact* binary sensor only ever updated from the REST poll. They now write `external_contact_opened` (matching the REST poll and the sensor's value_fn) in both the SSE and SQS managers — real-time again.
- **Smoke detector high-temperature alarm ignored real-time SSE.** The `high_temperature` binary sensor only read the REST `temperatureAlarmDetected` key and missed the SSE `temperature_alert` key, so a temperature alarm only showed up at the next poll. It now reads both.
- **Dimmer switches bounced back after toggling.** `AjaxDimmerSettingsSwitch` and `AjaxDimmerBoolSwitch` applied an optimistic state without guarding it, so a poll landing within ~1 s reverted the toggle. The state is now reserved for 15 s and cleared on the error rollback (parity with the other switches).
- **A malformed `model` payload could abort a whole refresh.** Device reconciliation now guards against a non-object `model` field instead of raising `TypeError` and halting the poll cycle.
- **Hardening:** `get_nvr_recordings` no longer dereferences a not-yet-populated coordinator; the API client raises a clear "not logged in" error (instead of building a `user/None/…` URL) on the device/camera endpoints; the SSE "alarm triggered by motion" log now shows the previous state rather than the new one.
- **Deprecation warnings cleared:** `TrackerEntity` is imported from its public path, and the firmware-update entity passes `hw_version` as a string — both silencing Home Assistant 2026.x deprecation notices.
- **Real-time SQS events recovered after a deferred `aiobotocore` install.** When `aiobotocore` was pulled in lazily on first run, the SQS client failed to start and real-time events stayed off until a reload; it now connects automatically once the dependency is available.

### Changed
- **Internal modularisation (no behaviour change, no `unique_id` change — verified against a live install).** The largest modules were split along clean responsibility lines so the codebase is easier to maintain:
  - SSE/SQS event-mapping tables → `event_maps.py` (a single source of truth shared by both managers, removing the `sse_manager → sqs_manager` import).
  - `coordinator.py` → door-sensor fast-polling extracted to `_coordinator_door_poll.py` (`AjaxDoorPollingMixin`).
  - `_coordinator_devices.py` → stateless attribute normalisation + motion-impulse expiry extracted to `_device_normalize.py`.
  - `switch.py` → entity classes split into `_switch_entity.py` (generic) and `_switch_dimmer.py` (LightSwitchDimmer).
  - `config_flow.py` → options flow extracted to `config_flow_options.py`.
  - `__init__.py` → service registration extracted to `_services.py`.
- Test coverage raised **97 % → 98 %** (1833 tests), notably bringing `__init__` (setup entry) and `coordinator` (constructor) to ~100 %.
- French translation polish: corrected an anglicism, added the missing French space before colons in the setup/options dialogs, and hyphenated the French e-mail term.

### Removed
- Dead code: unused API client methods (`async_control_device`, `async_set_light_state`, `async_get_nvr_status`), an unreferenced notification parser, `sse_client.update_session_token`, `onvif_manager.get_client`, and abandoned poll-count instrumentation in the SQS client.

## [0.31.1] - 2026-05-29

A full-codebase review pass: 25 runtime bugs, each adversarially verified before fixing, plus a lifecycle audit. mypy `--strict` clean, ruff clean, **429 tests** (was 411).

### ⚠️ To be aware of
- **Minimum Home Assistant is now 2024.11.** The integration already required `entry.runtime_data` (2024.6) and the modern `OptionsFlow` (2024.11); the declared minimum (`2024.1.0`) was simply wrong and let incompatible installs crash on startup. No working setup on a recent HA is affected.
- **Socket energy/current units corrected.** The energy sensor now reports **kWh** (was mislabelled Wh) and the current sensor reports **A** (was mislabelled mA) — a 1000× display/statistics error. Energy Dashboard history recorded under the old unit will show a scale break at the upgrade.

### Fixed
- **Water-leak and glass-break sensors never fired on a real detection.** The LeaksProtect moisture sensor read a camelCase key (`leakDetected`) that nothing ever wrote, and the dedicated GlassProtect sensor only read a `state` field that is never populated. Both now read the real-time keys actually written by the SSE manager (`leak_detected`, `glass_break_detected`) and the SQS manager (`flood_alarm`, `glass_alarm`); the smoke sensor also honours the SQS `smoke_alarm` key.
- **Socket Energy Dashboard scaling.** Energy/current sensor units now match what the coordinator stores (kWh / A) instead of being off by 1000×.
- **Switch and valve bounced back after toggling.** The single-switch path (Socket/Relay/WallSwitch) and the WaterStop valve applied an optimistic state without protecting it, so a poll landing before the device reported its new state reverted the entity. The optimistic state is now guarded for 15 s and cleared on the error rollback.
- **A transient hub error wiped a space.** A timeout/5xx on the per-hub fetch downgraded an already-known space to "unknown", firing a phantom state-change event and dropping its cameras/locks until the next full refresh. An existing space now keeps its state on a transient error; only a genuinely new space falls back to a placeholder.
- **Token expiry did not trigger re-auth.** `AjaxRestAuthError` from the per-hub `get_hub`/`get_users`/`get_groups`/`get_space_by_hub` calls is now propagated so it counts toward the re-auth threshold instead of being silently degraded.
- **`state=null` made every entity unavailable.** The night-mode parse is now null-safe (`str(state).upper()`), so a hub momentarily reporting a null state no longer raises `AttributeError` and fails the whole refresh.
- **Config-flow session leaks.** Re-auth and reconfigure now close their aiohttp session on every error branch instead of leaking one `ClientSession` per failed attempt.
- **Empty space discovery disabled every hub.** The direct and 2FA setup paths no longer write `enabled_spaces=[]` (which the coordinator reads as "disable all"); an absent value correctly means "all enabled".
- **ONVIF leaks and missing alerts.** A lost subscription is no longer re-created without shutting the old PullPoint manager down (leaking its renew task); the "partial cameras connected" repair issue now triggers correctly; and the ONVIF/RTSP username is no longer written to INFO logs.
- **Panic button cooldown burned on failure.** A failed panic call no longer consumes the 5 s cooldown, so an emergency retry stays possible after a transient API error.
- **Smart-lock doorbell missing from the logbook.** `ajax_smart_lock_doorbell` now has a describer, so a smart-lock doorbell press produces a readable logbook entry like the Video Edge doorbell.
- **SSE/proxy parity.** The SSE security handler now realigns the polling interval on non-full transitions (e.g. night-mode-off) and uses the same notification-action labels as the SQS transport; both managers stop accumulating spent timer handles (slow memory growth on busy camera installs).
- **DoorProtect Plus Fibra tilt** events (`M_6F_31`) are now classified as TRIGGERED, matching the non-Fibra twin; `format_event_message` no longer truncates the device/room context.
- **Migrated unique_id casing.** The v1.1→v1.2 migration now lower-cases the e-mail unique_id to match the config flow, so duplicate detection works for mixed-case e-mails.

## [0.31.0] - 2026-05-29

A full code-review pass over the whole integration: 46 confirmed findings fixed, each adversarially verified, plus four agent-introduced regressions caught and corrected during a diff-verification pass. mypy `--strict` clean (58 files), ruff clean, **342 tests** (was 328).

### ⚠️ Behaviour changes to be aware of
- **WaterStop diagnostic sensor states changed.** `motor_state` now reports `off` / `rotate_to_closing` / `rotate_to_open` (was `off` / `on`) and `external_power` reports `supply` / `no_supply` (was `supply` / `battery` / `unknown`), matching the real Ajax `motorState` / `extPower` API enums. **Automations or history templates keyed on the old `on`, `battery` or `unknown` states will need updating.**
- **SQS alarm-notification default flipped.** An *unset* notification-filter option now defaults to "all" instead of "none". If you never set a filter and currently receive no alarm notifications, you will start receiving them after this upgrade (only an explicit "none" suppresses them).
- **SSE / proxy mode now produces alarm history and persistent notifications.** Motion, smoke/fire, flood and glass-break events — and a door opened while armed — now append to a space's recent-events history and raise a persistent notification, matching SQS behaviour. Proxy-mode users will start seeing these.
- **Devices survive a partial poll.** A device missing from a single non-empty `200` response is no longer deleted immediately; it must be absent for **3 consecutive polls** first. A truncated proxy response no longer wipes devices (stale devices may linger up to ~3 polls longer).
- **Hub "external power" binary sensor** now reads the dedicated `externallyPowered` field instead of inferring from `battery.state != DISCHARGED`; the reported state may change for some hubs and now reports even when no battery data exists.
- **Video Edge, Video Edge sensor and firmware-update entities go *unavailable*** during failed coordinator updates instead of showing stale values.

### Added
- **Live discovery of spaces and groups.** A space or group added in Ajax after setup now creates its alarm-panel entity at runtime via the new `SIGNAL_NEW_SPACE` / `SIGNAL_NEW_GROUP` dispatcher signals — no integration reload required (mirrors the existing new-device path).
- **SSE / proxy-mode alarm history & notifications** for motion, smoke/fire, flood, glass-break and door-while-armed (parity with the SQS transport). Door-while-armed records history without forcing the space to `TRIGGERED`, mirroring SQS.
- **`arm_failed` event message** added to `EVENT_MESSAGES` in fr / en / es / de / nl / sv / uk (used by the unsuccessful-arming event code).
- **"Unknown" label** for the Video Edge storage-status sensor across `strings.json` and all 7 languages, matching the code's fallback value.

### Fixed
- **Cross-hub event suppression (multi-hub).** Replaced the coordinator's single global `_skip_state_change_event` boolean with a per-hub `_skipped_state_change_hubs` set, so a metadata refresh on one hub no longer drops the REST-side arm/disarm state-change event for every *other* hub processed in the same tick.
- **Night-mode unavailability (#149 class).** A transient `async_get_hub` failure inside the per-hub loop left `is_new_space` / `real_space_id` / `space_name` unbound, raising `UnboundLocalError` and flipping the whole refresh to failure (all entities unavailable). The except handler now initialises them so the space degrades gracefully (created as `NONE`, or kept) instead of crashing the tick.
- **Optimistic arm/disarm reverted by a stale poll.** Space-level state updates now honour `has_pending_ha_action` (the same `ha_protected` guard the group path already had), so a REST poll landing inside the ~10 s window no longer snaps the panel back to its previous state after a user command.
- **SSE second-event misattribution.** The SSE manager now *peeks* the pending-HA-action flag (`has_pending_ha_action`) instead of consuming it, so when Ajax emits two state events for one HA command (e.g. `arm` then `armwithmalfunctions`) both stay attributed to Home Assistant rather than the second firing a misleading "armed/disarmed by <user>" notification.
- **Optimistic LED / brightness reverted by polling.** Siren `led_indication` and dimmer `actualBrightnessCh1` are now only overwritten from the API when *not* inside their optimistic-update guard; a poll within the TTL window no longer reverts the user's change. The siren blink-while-armed switch maps its optimistic write to `led_indication` (the attribute it actually reads), and multi-gang LightSwitch tracks the optimistic guard **per channel** so one channel's poll can't overwrite another's in-flight change.
- **Cache bypass missed most of the refresh.** `bypass_cache_next()` set a one-shot boolean consumed by the *first* request of a cycle (the hub list), so device and space getters still served stale data. It now opens a short 2 s window covering the whole refresh cycle, and the space getter (which feeds cameras and smart locks) honours it too — entity state after an SSE/SQS event or user action is now fresh, not TTL-lagged.
- **`Retry-After` HTTP-date crashed the back-off path.** A bare `int(Retry-After)` raised `ValueError` when a server sent an RFC 7231/9110 HTTP-date on a `429`, crashing the retry exactly when asked to back off. A new `_parse_retry_after()` handles both seconds and dates, clamps to non-negative, and falls back to 60 s.
- **Panel state not restored on a failed command.** A failed arm/disarm/night-mode on space and group panels now captures and restores the previous state synchronously (`async_write_ha_state`) before the debounced refresh, instead of showing the wrong optimistic state during the debounce window.
- **Night-mode arm switch never worked on LightSwitch / Transmitter.** Both read the camelCase `nightModeArm`, but the coordinator only stores the snake_case `night_mode_arm` alias — so the switch was never created on LightSwitch and always read `False` on Transmitter. Both now use `night_mode_arm`.
- **Siren alarm-duration showed an invalid state.** `alarmDuration` is reported in **minutes** (not seconds); an earlier `/60` produced an off-list `0`. The select now maps 1:1 and snaps any off-list hub value to the nearest of `1/2/3/5/10/15`.
- **FireProtect2 CO siren-trigger switch snapped back.** The CO switch read either `CO`/`CCO` token but the write only removed `CO`, so turning it off left the other token and the read flipped it back on. It now uses one token (prefer `CO`, else `CCO`) for both directions.
- **Video Edge channel lookup returned the wrong channel.** `_get_channel_by_id` now does two passes (explicit-id match, then positional fallback) so out-of-order channel ids — e.g. after a camera is removed — no longer resolve to a neighbour by index.
- **Video Edge enum sensors emitted raw API values.** Record-mode, record-policy and storage-status now fall back to the in-options `unknown` instead of `value.lower()` for unmapped enums. The boot-time sensor is normalised to whole minutes so it stops jittering by seconds every poll and inflating recorder history.
- **Per-room device counts went stale.** `room.device_ids` is now rebuilt every poll (it was append-only), so a device moved between rooms or deleted no longer inflates the per-room `device_count` shown in panel attributes.
- **Shared door-sensor polling stopped too early.** The shared polling task now only stops when *no* space is disarmed or in night mode, instead of being cancelled when any one space armed — other disarmed spaces keep getting timely door updates.
- **Auth errors now drive reauth.** `AjaxRestAuthError` is re-raised (not swallowed) in the video-edge and smart-lock updaters, so token expiry counts toward the reauth threshold instead of being logged and ignored.
- **Orphaned ONVIF clients.** On a partial ONVIF init failure, already-connected clients are stopped via `async_stop()` before the reference is dropped, preventing orphaned poll tasks / PullPoint subscriptions.
- **Colliding notification IDs.** Arm/disarm persistent-notification ids now include the space and source name, so concurrent events no longer overwrite each other in the notifications panel.
- **Arming-failure event misclassified.** Removed the `M_22_21 → TRIGGERED` override; it now resolves to `RECOVERED` via the odd/even hex heuristic, so an arming failure is no longer surfaced as a one-shot triggered state.
- **ONVIF detection zone name dropped on secondary events.** The rule/zone name is now threaded into `_parse_event` so every emitted detection event (including secondary AI object-detection, motion, line-crossing and doorbell events) carries it, not just the single returned event.
- **Diagnostics SQS connectivity** is derived from the live receiver thread and queue URL (the old `is_connected()` call didn't exist and always read `False`).
- **Diagnostic dumps redacted.** `ajax_nvr_recordings.json` and `ajax_smart_locks.json` now pass through `async_redact_data`, and the diagnostics `TO_REDACT` set additionally covers `address`, `ssid`, `gateway`, `netmask`, `dns` and `networkInterface`.
- **Flood-detector malfunctions sensor** simplified to the normalised int count (the list/string branches were dead code).

### Changed
- **Force-arm services route through the coordinator.** `force_arm` / `force_arm_night` now call `coordinator.async_arm_space` / `async_arm_night_mode(force=True)` (instead of the raw API), apply across **all** matched config entries, and report aggregated per-hub failures (raising `invalid_target` when nothing resolves) — multi-account users can drive them across every hub.
- **Diagnostic services span all entries.** `get_raw_devices`, force-metadata-refresh, NVR-recordings and smart-lock dump handlers now iterate every matched config entry instead of only the first (they simply run for each; no failure aggregation).
- **Night-mode fast-poll filter default** flipped so door/transmitter/wire-input sensors that don't report `night_mode_arm` are now *included* in night-mode fast polling (faster updates for those sensors in night mode).

### Removed
- **`devices/dimmer.py` dead handler methods** (`get_switches` / `get_numbers` / `get_selects`, ~280 lines): dimmer entities are built from static definitions in the platform files gated by `is_dimmer_device()`, so these were never invoked.
- **`AjaxDevice.is_triggered` and `last_notification`**: dead code (trigger state is tracked via per-attribute flags + `last_trigger_time` in the SSE/SQS managers).
- **Per-door-sensor fast-poll mechanism** (`_fast_poll_tasks`, `_async_fast_poll_door_sensor` and its teardown): superseded by the shared continuous door-sensor polling task.

### Tests
- **+14 regression tests (328 → 342).** New coverage for: `Retry-After` date parsing and the cache-bypass window (`test_api_helpers.py`); the night-mode runtime-import and hub-details `UnboundLocalError` paths (`test_coordinator_spaces_nightmode.py`); the transmitter / siren / WaterStop / Video Edge handler fixes (`test_device_handlers.py`); the Video Edge channel id-vs-index lookup (`test_models_extras.py`).
- **New ENUM-options ⇄ translation parity guard** (`test_translations.py`): scans `devices/*.py` and fails if any literal `options` list has a key without a matching translated `state` entry in `strings.json` — the exact class of bug (WaterStop / Video Edge) caught in this pass.

## [0.30.1] - 2026-05-28

### Fixed
- **Integration goes fully unavailable in Night Mode (#149).** A regression introduced by the v0.29.0 coordinator split: `SecurityState` was imported only under `TYPE_CHECKING` in the new `_coordinator_spaces.py`, but the night-mode code path uses it at runtime (`security_state = SecurityState.NIGHT_MODE`). The moment a hub reported night mode, every coordinator refresh raised `NameError: name 'SecurityState' is not defined`, so all Ajax entities became unavailable until night mode was turned off. The "groups" angle in the report was a correlation, not the cause. Affected v0.29.0 and v0.30.0. `SecurityState` is now imported at runtime.

### Changed
- **CI guard against this class of bug:** enabled the ruff `TC004` lint rule, which flags any `TYPE_CHECKING`-only import used in executable code. (`TC001/2/3` — the "move into TYPE_CHECKING" rules — are intentionally left off, since pushing a runtime-needed import into a `TYPE_CHECKING` block is exactly what caused this regression.)

### Tests
- Added `tests/test_coordinator_spaces_nightmode.py` — drives `_async_update_spaces_from_hubs` through the night-mode branch (the exact #149 repro: night mode + groups, plus three other triggers of the same line) so a re-import regression fails loudly, independently of the ruff guard.

## [0.30.0] - 2026-05-28

### Added
- **Quality Scale: Platinum.** The three Platinum rules are now done — `async-dependency`, `inject-websession`, and `strict-typing`. `pyproject.toml` ships `strict = true` and CI's `typing` job blocks merges on any new mypy error. The cleanup pass took 392 → 0 strict errors (134 type-arg, 68 no-untyped-def, 59 no-any-return, 34 attr-defined, 32 no-untyped-call). All `# type: ignore[no-any-return]` are at REST/SQS JSON-payload boundaries — none in business logic.
- **Native translations for the 5 Repairs issues** added in v0.29.0 (`onvif_init_failed`, `onvif_no_cameras`, `onvif_partial_cameras`, `sse_init_failed`, `sqs_init_failed`): de / es / nl / sv / uk now match the FR quality. Placeholders (`{error}`, `{connected}`, `{total}`) preserved.
- **Test suite: 146 → 313 tests** (+114%). New entity tests use the `object.__new__(EntityClass)` pattern to exercise descriptor wiring and state mapping without a full HA fixture: `alarm_control_panel` SecurityState → AlarmControlPanelState routing for all 8 known states, `binary_sensor` / `sensor` / `switch` / `valve` / `lock` / `select` / `number` / `device_tracker` / `button` / `event` is_on / native_value / available / fire contracts. New module tests for `api.py` (URL routing per auth mode, devices cache TTL, rate-limit backoff), `sse_manager` (`_find_device` matching strategies, `is_state_protected` window, `_handle_door_event` mutations), `_coordinator_arm` (HA-action 10-second protection window, per-space lock identity), `_coordinator_events` (every SecurityState → bus event mapping). `diagnostics.py` coverage 42 → 87%.
- **Performance invariants pinned** (`tests/test_perf_invariants.py`): no f-string in `_LOGGER.X(...)` calls, every entity class declares `__slots__`, main update interval stays ≥ 30 s, debouncer cooldown stays in [0.1 s, 2 s]. Verified cold-start setup blocking at 1.5 s (< Platinum 2 s target).
- **mypy CI job** (`.github/workflows/ci.yml`) — runs on every push, fails the job on any new type error.

### Fixed
- **ONVIF Repairs issue reported `2/3 cameras connected` when the user had 2 cameras + 1 NVR.** The denominator counted the NVR, which `AjaxOnvifManager.async_start` deliberately skips (its sourceAliases-based channel→camera mapping is unreliable). The new `target_count` property excludes NVRs from the count so a healthy 2-camera + 1-NVR setup reports `2/2` and the repair issue auto-deletes instead of staying up as a phantom warning. `target_count == 0` (NVR-only setup) now emits an info log rather than the misleading `no_cameras` issue.

### Changed
- **`pyproject.toml [tool.mypy]`** flips to `strict = true` with `implicit_reexport = true` only for the `homeassistant.*` namespace (the HA team does not always update `__all__` when public symbols move).

## [0.29.0] - 2026-05-27

### Added
- **Quality Scale: Gold** (bumped from Bronze → Silver → Gold in this cycle). All Gold rules implemented and acknowledged in `quality_scale.yaml`: `devices` (DeviceInfo with model/sw/hw + `via_device`), `discovery-update-info` (DHCP confirm refreshes entry data), `dynamic-devices`, `stale-devices` (`_async_cleanup_stale_devices` prunes the HA registry on startup), `icon-translations` (77-entry `icons.json` across 12 entity categories).
- **Snapshot URL on detection events.** `ajax_camera_detection` and `ajax_doorbell_ring` bus events now ship `camera_entity_id` and `snapshot_url=/api/camera_proxy/<entity>` so automations can embed snapshots directly in Telegram / `notify` payloads without an extra `camera.snapshot` call. Resolved via `entity_registry` for both standalone cameras and NVR channel 0. Wired in SSE/SQS *and* ONVIF paths.
- **5 new Repairs issues** for soft-failed inits — guided UI message instead of a buried log line: `onvif_init_failed`, `onvif_no_cameras`, `onvif_partial_cameras` (with `{connected}/{total}` placeholders), `sse_init_failed`, `sqs_init_failed`. Each issue auto-deletes when the underlying problem resolves. Full FR translation + EN fallback for de/en/es/nl/sv/uk.
- **Coordinator diagnostics counters.** `stats` dict tracks `events_sse_received`, `events_sqs_received`, `events_onvif_received`, `auth_errors`, `discovery_refreshes` — visible in the integration's diagnostics download.
- **README extensions for Gold docs**: *How It Works (Data Update)*, *Use Cases & Examples* (3 ready-to-paste automations including the new snapshot URL), *Known Limitations* (5-row table).

### Changed
- **`coordinator.py` god-class split into 7 thematic mixins**: 3329 → 803 lines (-76%). The runtime behaviour is unchanged; the file split makes the bootstrap, arm/disarm services, devices reconciliation, event dispatch, ONVIF handling, spaces reconciliation, and state updaters each readable in isolation. Loggers now carry the actual module name (e.g. `custom_components.ajax._coordinator_spaces`) instead of a single 3.3k-line bucket.
- **16 "device not found" warnings demoted to debug** in `sse_manager.py` / `sqs_manager.py`. These fire on event-vs-discovery race conditions (event arrives before / after the device is known) and are not actionable.
- **HACS visibility**: `hacs.json` drops the FR-only country filter (Ajax is sold in 169 countries) and explicitly sets `zip_release: false`. HACS Action validation passes 8/8 checks; ready for HACS default submission.

### Fixed
- **`time` import hoisted to module level** in `models.py` so test patches can target `custom_components.ajax.models.time` consistently.
- **Orphan `@callback` decorators** removed during the coordinator extraction passes.

### Tests
- **75 → 141 tests (+88%)**. `models.py` 79% → 89%, `devices/__init__.py` 84% → 100%, `devices/*.py` handlers avg ~25% → ~40%. New files: `test_device_handlers.py`, `test_coordinator_parsers.py`, `test_models_extras.py`, `test_repair_issues.py`, `test_runtime_data.py`, `test_socket_energy.py`.

## [0.28.2] - 2026-05-22

### Fixed
- **Duplicate `ajax_armed`/`ajax_disarmed` bus events** under a REST/SSE race (#133 follow-up). The `_skip_state_change_event` flag that suppresses the REST poller's duplicate was set only *after* the `asyncio.sleep` preceding the metadata refresh, so a REST poll tick landing in that window fired its own event. The `_security_event_lock` now spans the whole sequence and the flag is set *before* the sleep — a single arm/disarm produces exactly one event again.
- **Quality Scale Silver blockers**: the missing `api_not_initialized` abort string (config flow showed a raw key); `alarm_control_panel` arm/disarm/group actions now raise a typed `HomeAssistantError` with `translation_key` instead of leaking the raw API exception; added `tests/__init__.py` for pytest package discovery.
- **SQS** `CALLBACK_TIMEOUT` raised 10 s → 20 s (still below the 30 s visibility timeout): a security-event callback running `sleep` + metadata refresh under lock contention could exceed 10 s and trigger a needless message requeue.
- **Unknown device in an SSE/SQS event** now triggers a throttled coordinator refresh so the device is discovered (and `SIGNAL_NEW_DEVICE` fires) instead of staying invisible until the next hourly full refresh.

### Changed
- **Security**: a persistent Repairs issue is now raised while `verify_ssl` is disabled (MITM exposure stays visible in the UI, cleared automatically when re-enabled); per-event SSE/SQS log lines carrying the Ajax user's display name (PII) demoted from INFO to DEBUG.
- **Performance**: `GET /hubs/{id}/devices` responses cached 5 s (keyed by `hub_id`+`enrich`) so the periodic update loop and the door-sensor fast-poll loop coalesce into one request when their schedules cross; the door-sensor loop calls `async_set_updated_data` once per pass instead of once per changed space.
- **Refactor**: dynamic entity discovery centralised in a new `_discovery.py` helper — the 11 entity platforms shed ~600 lines of duplicated registry-filter / dispatcher-wiring boilerplate. The last 3 inline entity descriptors (`door_contact` tamper/temperature, `hub` firmware) migrated to the `devices/base.py` helpers.
- Dependency bumps (dependabot): `homeassistant>=2026.5.3`, `aiobotocore>=3.7.0`, `coverage>=7.14.0`, `pytest-homeassistant-custom-component>=0.13.331`; pre-commit hooks autoupdate.

## [0.28.1] - 2026-05-03

### Fixed
- **`ajax_armed` / `ajax_disarmed` / `ajax_armed_night` / `ajax_armed_home` bus events fire even when REST polling consumed the state change first** (#133, thanks @Kolia56). The previous gate `if state_changed:` in SSE/SQS `_handle_security_event` suppressed the fire whenever `old_state == new_state` — typically when REST had already updated the local state via optimistic update — so automations listening to the bus never saw `nightmodeon`/`nightmodeoff` transitions in particular. The coordinator's `_skip_state_change_event` flag already guarantees the REST poller doesn't emit a duplicate, so the gate was redundant *and* harmful.

### Changed
- Cleaned up the now-explanatory comment around the unconditional fire (no behaviour change).

## [0.28.0] - 2026-05-02

### Added
- **Dynamic device discovery via dispatcher signals.** Devices, video edges and smart locks added to the Ajax account between two coordinator refreshes are now picked up live instead of waiting for a full HA restart. New `SIGNAL_NEW_DEVICE` and `SIGNAL_NEW_VIDEO_EDGE` signals (alongside the existing `SIGNAL_NEW_SMART_LOCK`); every entity platform that owns per-device entities (binary_sensor, sensor, event, switch, light, number, select, valve, update, camera) listens to the relevant signal and creates entities through `async_dispatcher_connect`, with entity-registry guard against duplicates.
- **`verify_ssl` togglable from the reconfigure flow.** Users on a self-signed proxy certificate no longer have to delete and recreate the entry to flip the SSL verification flag. Strings + 7 translations updated; reconfigure step exposes the field with a description explaining the self-signed proxy use case.
- **2FA proxy session bootstrap is now complete.** After a successful 2FA verification in proxy mode the API client captures the same auth payload as a normal proxy login: `refreshToken`, `userId`, `sseUrl`, `apiKey`, plus the same TTL bookkeeping. Without this, a 2FA-protected proxy account had no SSE endpoint and no API key after first login, forcing a full reconfigure to recover. Hubs discovery after 2FA now also degrades gracefully when the proxy doesn't expose `/hubs` to a freshly-2FA'd session.
- Translations test suite under `tests/` (with the necessary `.gitignore` carve-out so `tests/test_*.py` is tracked while root-level scratch tests stay ignored). Smoke-tests that `strings.json` and every `translations/<lang>.json` are valid JSON and share the same key tree.

### Changed
- Dependency bumps (dependabot): `aiobotocore>=3.5.0`, `pytest-asyncio>=1.3.0`, `pytest-homeassistant-custom-component>=0.13.326`, `coverage>=7.13.5`, `homeassistant>=2026.4.4`.
- Pre-commit hooks autoupdate.

## [0.27.0] - 2026-04-22

### Added
- **`ajax_armed` / `ajax_disarmed` / `ajax_armed_night` / `ajax_armed_home` / `ajax_security_state_changed` bus events now carry `source_name` and `source_type`** (resolves request from #93). The source_name is the Ajax user/keypad/space-control that triggered the arm/disarm (or `"Home Assistant"` for a local HA action); source_type mirrors the Ajax `sourceObjectType` (`USER`, `KEYPAD`, `SPACE_CONTROL`, `APP`, ...) or `"HA"`. Makes automations such as "disarm by user X → open gate" possible without needing SIA events. The fields are populated by both the SSE and SQS managers — the REST fallback omits them since Ajax REST doesn't expose the actor.
- Logbook describers for the same events now append `par <source>` / `by <source>` (7 languages) when the info is present.

### Changed
- Dependency bumps (dependabot): `aiohttp>=3.13.5`, `pytest>=9.0.3`, `pytest-cov>=7.1.0`, `pytest-mock>=3.15.1`, `homeassistant>=2026.4.3`.
- Pre-commit hooks autoupdate.

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
