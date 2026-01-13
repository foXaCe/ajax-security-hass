# Ajax Security System Integration for Home Assistant


[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Hassfest](https://github.com/foXaCe/ajax-security-hass/actions/workflows/lint.yml/badge.svg)](https://github.com/foXaCe/ajax-security-hass/actions/workflows/lint.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Community Forum](https://img.shields.io/badge/Home_Assistant-Community-blue?logo=home-assistant)](https://community.home-assistant.io/t/custom-component-ajax-systems/948939/2)
[![Donate](https://img.shields.io/badge/Donate-PayPal-blue.svg)](https://paypal.me/foXaCe66)

**Full-featured** Home Assistant integration for Ajax Security Systems.

---

> [!CAUTION]
> ## Proxy Mode Security
> When using a proxy, the proxy administrator can see all API requests/responses and your credentials hash. This means the proxy can impersonate you with the Ajax API. For a detailed threat model analysis, see [Issue #47](https://github.com/foXaCe/ajax-security-hass/issues/47). For security considerations, see [Security & Privacy](#-security--privacy).

---

> [!NOTE]
> ## About This Integration
> This is **NOT an official Ajax Systems addon**. It is a community-developed integration created in collaboration with Ajax Systems.
>
> **Special thanks to Ajax Systems** for their support and assistance during development. Their help made this integration possible! 🙏

---

> [!WARNING]
> ## No Public Proxy Available
> There is currently **no public proxy server available**. The integration requires either:
> - An **enterprise API key** from Ajax Systems (Direct mode)
> - A **self-hosted proxy server** (Proxy mode)
>
> For discussions and updates, join the community: [HACF Forum](https://forum.hacf.fr/t/alarme-ajax-sur-home-assistant/29511/160)

---

> [!WARNING]
> ## Real-Time Events Only When Armed
> Due to Ajax Systems architecture, **real-time events (SSE/SQS) are only received when the system is armed**. When disarmed, the integration uses polling (30s for sensors, 5s for door sensors). This is an Ajax API limitation.

---

## ⚠️ Project Status & Community

This integration is **actively developed** but I'm just getting started with Ajax security systems. I currently own and test with:
- ✅ **Hub 2 Plus**
- ✅ **MotionCam** (Motion detector with photo capture)
- ✅ **DoorProtect Plus** (Door/window contact sensor)
- ✅ **GlassProtect** (Glass break detector)
- ✅ **HomeSiren** (Indoor siren)

Users tested:
- ✅ **Superior Hub Hybrid 4G**
- ✅ **KeyPad TouchScreen Jeweller** (limited info available from API)
- ✅ **Superior DoorProtect Plus Jeweller**
- ✅ **FireProtect 2 RB (Heat/Smoke Jeweller)**
- ✅ **Superior HomeSiren Jeweller**
- ✅ **ReX 2 Jeweller**
- ✅ **StreetSiren Jeweller**
- ✅ **Superior MotionCam (PhOD) Jeweller**
- ✅ **MultiTransmitter Fibra** (with external contact state)

Since I don't have access to all Ajax devices, **I cannot test every device type**.

**🤝 Community Help Needed**: If you own other Ajax devices and want to help test and improve this integration, your contributions would be greatly appreciated! Together we can make this the best Ajax integration for Home Assistant.

Issues, pull requests, and feedback are welcome!

## ✨ Key Features

### 🔄 Real-Time Synchronization (Optional)
- **Real-time security events** - Arm/disarm, alarms, and intrusions in < 1 second
- **Real-time sensor events** - Motion/door events when system is armed or sensor in "Always Active" mode
- **Multiple connection modes**:
  - **Direct mode** - Direct API connection with optional AWS SQS for real-time events
  - **Proxy mode** - Connection via proxy server with SSE (Server-Sent Events) for real-time events
- **Polling fallback** - 30-second polling for all sensor states, plus 5-second fast polling for door sensors when disarmed or in night mode (for excluded sensors)

### 🛡️ Complete Security Control
- ✅ **Arm** (Away mode)
- ✅ **Disarm**
- ✅ **Night Mode**
- ✅ **Partial Arming** - Group-based arming
- ✅ **Force Arm** - Arm with open sensors/problems
- ✅ **Panic Button** - Trigger emergency alarm from Home Assistant

### 🔔 Notifications
- ✅ **Real-time Notifications** - Arming/disarming events with user name
- ✅ **Persistent Notifications** - Optional Home Assistant notifications
- ✅ **Notification Filters** - None, Alarms only, Security events, or All notifications
- ✅ **Device Events** - Motion detection, door/window opened (when armed or "Always Active")

### 📱 Device Support

| Category | Devices |
|----------|---------|
| **Hubs** | Hub, Hub Plus, Hub 2, Hub 2 Plus, Hub 2 (4G), Hub Hybrid |
| **Motion Detectors** | MotionProtect, MotionProtect Plus, MotionProtect Outdoor, MotionCam, CombiProtect |
| **Door/Window** | DoorProtect, DoorProtect Plus, MultiTransmitter (wired inputs) |
| **Fire Safety** | FireProtect, FireProtect Plus, FireProtect 2 |
| **Flood** | LeaksProtect |
| **Glass Break** | GlassProtect |
| **Sirens** | HomeSiren, StreetSiren, StreetSiren DoubleDeck |
| **Keypads** | KeyPad, KeyPad Plus, KeyPad TouchScreen |
| **Smart Devices** | Socket, WallSwitch, LightSwitch, Relay |
| **Accessories** | SpaceControl, Button, Tag, ReX, ReX 2 |

### 📊 Rich Entity Support
- **Alarm Control Panel** - Full security system control with support for groups/zones
- **Binary Sensors** - Motion, door/window, smoke, flood, glass break, tamper, power status, moisture
- **Sensors** - Battery level, signal strength, temperature, humidity, CO2, device counts, room assignment
- **Switches** - Device settings (always active, night mode, LED indicator, sensitivity, etc.)
- **Button** - Panic button for emergency situations

### 🌍 Multi-Hub & Multi-Language
- Support for multiple Ajax Hubs in one Home Assistant instance
- Fully localized in **English**, **French**, **German**, **Spanish**, **Swedish**, and **Ukrainian**
- All entities properly translated

## 📦 Installation

### Via HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to "Integrations"
3. Click the 3 dots in the top right corner
4. Select "Custom repositories"
5. Add this repository URL: `https://github.com/foXaCe/ajax-security-hass`
6. Category: "Integration"
7. Click "Add"
8. Search for "Ajax Security System"
9. Click "Download"
10. Restart Home Assistant

### Manual Installation

1. Download the latest release
2. Copy the `custom_components/ajax` folder to your Home Assistant `config/custom_components/` directory
3. Restart Home Assistant

## ⚙️ Configuration

### Basic Setup

1. Go to **Settings** → **Devices & Services**
2. Click **"+ Add Integration"**
3. Search for **"Ajax Security System"**
4. Choose your connection mode:

#### Proxy Ajax (Recommended)
Connect via the Ajax community proxy server. Real-time events via SSE (Server-Sent Events).

- **Proxy URL**: URL of the Ajax proxy server
- **Email**: Your Ajax account email
- **Password**: Your Ajax account password

> **Note**: This is the recommended mode for most users. No API key required.

> ⚠️ **User accounts only**: This integration only supports **user accounts**. PRO/Enterprise accounts are not supported.

#### Direct Mode (Enterprise API key only)
Direct connection to the Ajax API. Requires an enterprise API key from Ajax Systems.

- **API Key**: Your enterprise API key from Ajax Systems
- **Email**: Your Ajax account email
- **Password**: Your Ajax account password
- **AWS Credentials** (optional): For real-time event notifications via SQS

> **Note**: Enterprise API keys are only available through Ajax Systems partnership program.

### Optional: Real-Time Events

#### SSE (Proxy Mode)
In proxy mode, real-time events are automatically received via Server-Sent Events (SSE). No additional configuration required.

#### AWS SQS (Direct Mode)
For instant updates (<1 second) in direct mode, configure AWS SQS credentials:
- **AWS Access Key ID**
- **AWS Secret Access Key**
- **Queue Name**

These credentials are provided by Ajax Systems with your enterprise API key.

#### Real-Time Event Limitations

> [!WARNING]
> Due to Ajax Systems architecture, **real-time events (SSE/SQS) are only received when the system is armed**.
>
> When **disarmed**, Ajax does not send real-time events - this is an Ajax API limitation, not the integration.
> - Motion and door events are **not** sent to SQS/SSE
> - The integration uses fast polling (5 seconds) for door sensors
> - Motion sensors fall back to standard polling (30 seconds)
>
> **Exception**: Sensors with **"Always Active"** mode enabled will send real-time events even when disarmed.

### Options (after setup)

Go to the integration options to configure:
- **Persistent Notifications**: Show notifications in Home Assistant UI
- **Notification Filter**: Choose which notifications to display
- **Monitored Spaces**: Select which hubs/spaces to monitor

## 🔒 Security & Privacy

### Credential Storage
- **Local storage only**: Credentials are stored in Home Assistant's encrypted config entry system

### Authentication Process
1. **Password hashing**: Your password is hashed using SHA-256 before transmission
2. **Secure communication**: All API communication uses HTTPS (TLS/SSL)
3. **Session tokens**: Tokens are stored locally and refreshed automatically

### Direct Mode
- **No third parties**: The integration communicates directly with Ajax servers
- End-to-end encrypted communication between your Home Assistant and Ajax

### Proxy Mode
When using a proxy, the proxy administrator can see:
- All API requests and responses (alarm state, arm/disarm actions, device status)
- Your email address

**Important**: This integration does not manage any proxy server. For security and trust considerations, contact the person or organization providing your proxy access.

### What the Developer Cannot Access
- ❌ I (the developer) **cannot access your credentials**
- ❌ No analytics, telemetry, or tracking
- ❌ No data collection of any kind
- ✅ Fully open source - you can audit the code yourself

## 📖 Usage

### Security Control

Use the **Alarm Control Panel** entity to control your security system:

```yaml
# Example automation: Arm when leaving home
automation:
  - alias: "Arm Ajax when leaving"
    trigger:
      - platform: state
        entity_id: person.your_name
        to: "not_home"
    action:
      - service: alarm_control_panel.alarm_arm_away
        target:
          entity_id: alarm_control_panel.ajax_alarm_home
```

### Force Arming

Use force arming to arm the system even with open sensors:

```yaml
# Force arm (away)
service: ajax.force_arm
target:
  entity_id: alarm_control_panel.ajax_alarm_home

# Force arm night mode
service: ajax.force_arm_night
target:
  entity_id: alarm_control_panel.ajax_alarm_home
```

⚠️ **Warning**: Force arming ignores open sensors and system problems. Use with caution.

### Panic Button

```yaml
# Trigger emergency alarm
service: button.press
target:
  entity_id: button.ajax_panic_home
```

⚠️ **Warning**: The panic button triggers a **real emergency alarm**.

### Device Information Report

Generate a diagnostic report to help improve the integration:

```yaml
service: ajax.generate_device_info
```

This creates an anonymized JSON file with device information (no sensitive data included).

## 🔧 Troubleshooting

### Authentication Errors

The integration provides specific error messages to help diagnose login issues:

| Error | Cause | Solution |
|-------|-------|----------|
| **Invalid API key** | API key is missing or incorrect | Check your enterprise API key |
| **Invalid email or password** | Wrong credentials | Verify your Ajax account email and password |
| **Account type not supported** | Using a PRO/Enterprise account | Use a standard user account instead |

### Integration not loading
1. Check Home Assistant logs for errors
2. Verify your Ajax credentials are correct
3. Ensure you have an active internet connection
4. Make sure you're using a **user account** (not PRO/Enterprise)

### Real-time updates not working
1. Verify AWS SQS credentials are configured correctly
2. Check that the queue name matches exactly
3. Review logs for SQS connection errors

### Devices not appearing
1. Wait for initial sync to complete (up to 30 seconds)
2. Check that devices are visible in the Ajax app
3. Try reloading the integration

### Debug Logging

```yaml
logger:
  default: info
  logs:
    custom_components.ajax: debug
```

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

If you have Ajax devices that aren't tested yet, your help would be invaluable in improving device support.

### Development Process

This integration is developed through a **collaborative approach**:
- **Human expertise** - Architecture, security decisions, and code review by [@foXaCe](https://github.com/foXaCe)
- **AI assistance** - Code generation using Claude (Anthropic)
- **Community contributions** - Bug reports, testing, and feature requests

All code is reviewed, tested with real hardware, and open source.

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## ⚠️ Disclaimer

This integration is **not officially affiliated** with Ajax Systems. It is a community project developed with support from Ajax Systems.

Use at your own risk. The developer is not responsible for any issues arising from the use of this integration.
