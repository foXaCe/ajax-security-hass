# Changelog

All notable changes to this project will be documented in this file.

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
