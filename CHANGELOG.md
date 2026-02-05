# Changelog

All notable changes to this project will be documented in this file.

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
