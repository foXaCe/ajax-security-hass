# Ajax Security System Integration for Home Assistant

![Header](ajax-header-400x400.png)

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Sponsor](https://img.shields.io/badge/Sponsor-GitHub-ea4aaa?logo=github)](https://github.com/sponsors/foXaCe)
[![Revolut](https://img.shields.io/badge/Revolut-Donate-0075EB?logo=revolut&logoColor=white)](https://revolut.me/foxace)
[![PayPal](https://img.shields.io/badge/PayPal-Donate-00457C?logo=paypal&logoColor=white)](https://paypal.me/foXaCe66)
[![Community Forum](https://img.shields.io/badge/Home_Assistant-Community-blue?logo=home-assistant)](https://community.home-assistant.io/t/custom-component-ajax-systems/948939/2)

**Full-featured** Home Assistant integration for Ajax Security Systems**.

[Version française ci-dessous](#version-française)

## ⚠️ Project Status & Community

This integration is **actively developed** but I'm just getting started with Ajax security systems. I currently own and test with:
- ✅ **Hub 2 Plus**
- ✅ **MotionCam** (Motion detector with photo capture)

Users tested:
- ✅ **Superior Hub Hybrid 4G**
- ✅ **KeyPad TouchScreen Jeweller** (not much info from it)
- ✅ **Superior DoorProtect Plus Jeweller**
- ✅ **FireProtect 2 RB (Heat/Smoke Jeweller)**
- ✅ **Superior HomeSiren Jeweller**
- ✅ **ReX 2 Jeweller**
- ✅ **StreetSiren Jeweller**
- ✅ **Superior MotionCam (PhOD) Jeweller**

Since I don't have access to all Ajax devices yet, **I cannot test every device type**.

**🤝 Community Help Needed**: If you own other Ajax devices and want to help test and improve this integration, your contributions would be greatly appreciated! Together we can make this the best Ajax integration for Home Assistant.

Issues, pull requests, and feedback are welcome!

## ✨ Key Features

### 🔄 Real-Time Synchronization
- **Instant bidirectional sync** - Changes in Ajax app appear immediately in Home Assistant and vice versa
- **Sub-second updates** - State changes reflected in < 1 second

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
- ✅ **Device Events** - Motion detection, door/window opened, etc.

### 📱 Device Support

**Tested Devices** (personally verified):
- ✅ **Hub 2 Plus**
- ✅ **MotionCam** - Motion detector with photo capture

**Theoretically Supported**
- **Other Hubs** - Hub, Hub Plus, Hub 2, Hub 2 (4G)
- **Motion Detectors** - MotionProtect, MotionProtect Plus, MotionProtect Outdoor, CombiProtect
- **Door/Window Contacts** - DoorProtect, DoorProtect Plus
- **Fire Safety** - FireProtect, FireProtect Plus, FireProtect 2
- **Flood Detectors** - LeaksProtect
- **Glass Break** - GlassProtect
- **Sirens** - HomeSiren, StreetSiren, StreetSiren DoubleDeck
- **Keypads** - KeyPad, KeyPad Plus, KeyPad TouchScreen
- **Smart Devices** - Socket, WallSwitch, Relay
- **Other Devices** - SpaceControl (key fob), Button (panic button), Tag (keyring)

### 📊 Rich Entity Support
- **Alarm Control Panel** - Full security system control with support for groups/zones
- **Binary Sensors** - Motion, door/window, smoke, flood, glass break, tamper, power status, moisture
- **Sensors** - Battery level, signal strength, temperature, humidity, CO2, device counts, notifications, SIM status
- **Button** - Panic button for emergency situations
- **Switch** - Smart sockets and relays with channel control

### 🌍 Multi-Hub & Multi-Language
- Support for multiple Ajax Hubs in one Home Assistant instance
- Fully localized in **French** and **English**
- All entities properly translated

## 📦 Installation

### Via HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to "Integrations"
3. Click the 3 dots in the top right corner
4. Select "Custom repositories"
5. Add this repository URL: `https://github.com/foXaCe/ajax-hass`
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

1. Go to **Settings** → **Devices & Services**
2. Click **"+ Add Integration"**
3. Search for **"Ajax Security System"**
4. Enter your Ajax account credentials:
   - **Email**: Your Ajax account email
   - **Password**: Your Ajax account password
   - **Persistent Notifications** (optional): Show notifications in Home Assistant UI
   - **Notification Filter** (optional): Choose which notifications to display:
     - **None**: No notifications
     - **Alarms only**: Only alarm/intrusion notifications
     - **Security events**: Alarms + arming/disarming events
     - **All notifications**: All notifications including device events
5. Click **Submit**

![Configuration](config.png)

The integration will automatically discover all your Ajax devices and create entities for them.

## 🔒 Security & Privacy

**Your credentials are handled with the utmost care:**

### Credential Storage
- **Local storage only**: Your email and password are stored in Home Assistant's encrypted config entry system (`.storage/core.config_entries`)
- **No third parties**: The integration does not communicate with any third-party servers

### Authentication Process
1. **Password hashing**: Your password is hashed using SHA-256 before being sent to Ajax servers
2. **Secure communication**: All API communication uses HTTPS (encrypted TLS/SSL)
3. **Session tokens**: After authentication, session tokens are stored locally in Home Assistant's secure storage
4. **No logging**: Credentials are never logged or exposed in debug logs

### What the Developer Cannot Access
- ❌ I (the developer) **cannot access your credentials**
- ❌ No analytics, telemetry, or tracking
- ❌ No data collection of any kind
- ✅ Fully open source - you can audit the code yourself

### Security Recommendations
- Use a strong, unique password for your Ajax account
- ✅ **Two-factor authentication (2FA) is fully supported** - you can keep 2FA enabled on your Ajax account for enhanced security
- Ensure your Home Assistant instance is properly secured (HTTPS, strong passwords, firewall)
- Keep Home Assistant and this integration up to date

For complete transparency, you can review how credentials are handled in the source code:
- Configuration flow: [`config_flow.py`](https://github.com/foXaCe/ajax-hass/blob/main/custom_components/ajax/config_flow.py)
- API authentication: [`api.py`](https://github.com/foXaCe/ajax-hass/blob/main/custom_components/ajax/api.py)

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

Use force arming to arm the system even with open sensors or problems:

```yaml
# Example: Force arm at night
automation:
  - alias: "Force arm at bedtime"
    trigger:
      - platform: time
        at: "23:00:00"
    action:
      - service: ajax.force_arm
        target:
          entity_id: alarm_control_panel.ajax_alarm_home

# Example: Force arm in night mode
automation:
  - alias: "Force arm night mode"
    trigger:
      - platform: time
        at: "23:00:00"
    action:
      - service: ajax.force_arm_night
        target:
          entity_id: alarm_control_panel.ajax_alarm_home
```

⚠️ **Warning**: Force arming ignores open sensors and system problems. Use with caution.

### Panic Button

The panic button entity triggers an emergency alarm:

```yaml
# Example: Add panic button to dashboard
type: button
tap_action:
  action: call-service
  service: button.press
  target:
    entity_id: button.ajax_panic_home
name: Emergency
icon: mdi:alarm-light
```

⚠️ **Warning**: The panic button triggers a **real emergency alarm**. Only use it in genuine emergencies or for testing with your monitoring center's knowledge.

### Device Information Report

Generate a diagnostic report of your Ajax devices to help improve the integration:

```yaml
# Call the service in Developer Tools > Services
service: ajax.generate_device_info
```

This service creates a JSON file `ajax_device_info.json` in your Home Assistant config directory (`/config/`) containing:
- Device types and models
- Firmware and hardware versions
- Available attributes (battery, signal, temperature, etc.)
- Device statistics

**Privacy**: The report **excludes all sensitive data**:
- ❌ No device names
- ❌ No unique IDs
- ❌ No MAC addresses
- ❌ No location information

This anonymized report is perfect for sharing when requesting support for new device types!

**Where to find the file:**
- Docker: `/config/ajax_device_info.json`
- Standard install: `~/.homeassistant/ajax_device_info.json`
- Access via: File Editor add-on, Studio Code Server, or Samba Share

After running the service, you'll receive a persistent notification with the file location.

### Sensors & Binary Sensors

All Ajax devices appear as appropriate Home Assistant entities:

- **Motion detectors** → `binary_sensor.ajax_motion_*`
- **Door/window contacts** → `binary_sensor.ajax_door_*`
- **Temperature** → `sensor.ajax_temperature_*`
- **Battery level** → `sensor.ajax_battery_*`
- etc.

## 🔧 Advanced Configuration

### Update Interval

The integration uses **real-time updates** for instant synchronization (< 1 second), with a minimal backup polling every 60 seconds. The polling serves only as a safety fallback.

**⚠️ Important**: Do not reduce the polling interval below 60 seconds to avoid overloading Ajax's API servers.

```python
UPDATE_INTERVAL = 60  # seconds
```

### Logging

To enable debug logging, add to your `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.ajax: debug
```

## 🐛 Troubleshooting

### Integration not loading
1. Check Home Assistant logs for errors
2. Verify your Ajax credentials are correct
3. Ensure you have an active internet connection

### Real-time updates not working
1. Check Home Assistant logs for errors
2. Verify your internet connection is stable
3. Restart the integration

### Devices not appearing
1. Wait for initial sync to complete (up to 30 seconds)
2. Check that devices are visible in the Ajax app
3. Try reloading the integration

### Privacy & Security

- ✅ Your credentials are only used to authenticate with Ajax servers
- ✅ No data is sent to any third-party servers
- ✅ All communication is encrypted (TLS/SSL)
- ✅ Session tokens are stored locally in Home Assistant's secure storage
- ✅ The integration is fully open source - you can audit the code

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

If you have Ajax devices that aren't tested yet, your help would be invaluable in improving device support.

### 🤖 Development Process & AI Transparency

This integration is developed through a **collaborative approach** combining:

- **Human expertise** - Core architecture, security decisions, and code review by [@foXaCe](https://github.com/foXaCe)
- **AI assistance** - Code generation and optimization using Claude (Anthropic) and Cursor AI
- **Community contributions** - Bug reports, feature requests, and testing from users

**Why AI?** AI tools accelerate development and help implement features faster, but every line of code is:
- ✅ Reviewed and validated by human developers
- ✅ Tested with real Ajax hardware
- ✅ Open source and auditable
- ✅ Subject to community scrutiny

**Security note**: All security-critical code (authentication, encryption, credential handling) is carefully reviewed and follows Home Assistant best practices.

We believe in **full transparency** about our development process. If you have concerns or questions, please open an issue!

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## ⚠️ Disclaimer

This integration is **not officially affiliated** with Ajax Systems.
