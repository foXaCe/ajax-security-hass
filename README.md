# Ajax Security System Integration for Home Assistant

<img width="100%" alt="Ajax-HASS" src="https://github.com/user-attachments/assets/7b70b7cb-3cea-4a47-8887-f2e193605dbc" />


[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Sponsor](https://img.shields.io/badge/Sponsor-GitHub-ea4aaa?logo=github)](https://github.com/sponsors/foXaCe)
[![Revolut](https://img.shields.io/badge/Revolut-Donate-0075EB?logo=revolut&logoColor=white)](https://revolut.me/foxace)
[![Community Forum](https://img.shields.io/badge/Home_Assistant-Community-blue?logo=home-assistant)](https://community.home-assistant.io/t/custom-component-ajax-systems/948939/2)

**Full-featured** Home Assistant integration for Ajax Security Systems via the official Cloud API with **real-time synchronization**.

[Version française ci-dessous](#version-française)

## ⚠️ Project Status & Community

This integration is **actively developed** but I'm just getting started with Ajax security systems. I currently own and test with:
- ✅ **Hub 2 Plus**
- ✅ **MotionCam** (Motion detector with photo capture)

Since I don't have access to all Ajax devices yet, **I cannot test every device type**. However, the integration is built on Ajax's official API and should theoretically work with all Ajax devices.

**🤝 Community Help Needed**: If you own other Ajax devices and want to help test and improve this integration, your contributions would be greatly appreciated! Together we can make this the best Ajax integration for Home Assistant.

Issues, pull requests, and feedback are welcome!

## ✨ Key Features

### 🔄 Real-Time Synchronization
- **Instant bidirectional sync** - Changes in Ajax app appear immediately in Home Assistant and vice versa
- **gRPC streaming** - Same technology as the official Ajax mobile app
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

**Theoretically Supported** (via official API, not personally tested):
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

**Note**: The integration uses Ajax's official API and is designed to work with all Ajax devices. If you have devices not listed as tested, they should still work - please report your experience!

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
- **Never leaves your network**: Credentials are only transmitted directly to Ajax's official API servers
- **No third parties**: The integration does not communicate with any third-party servers

### Authentication Process
1. **Password hashing**: Your password is hashed using SHA-256 before being sent to Ajax servers
2. **Secure communication**: All API communication uses gRPC over HTTPS (encrypted TLS/SSL)
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

The integration uses **real-time streaming** for instant updates (< 1 second), with a minimal backup polling every 60 seconds. The polling serves only as a safety fallback in case the streaming connection fails.

**⚠️ Important**: Do not reduce the polling interval below 60 seconds to avoid overloading Ajax's API servers. The streaming already handles all real-time updates.

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
1. Check that streaming tasks are started (see logs)
2. Verify firewall allows gRPC connections (port 443)
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

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## ⚠️ Disclaimer

This integration is **not officially affiliated** with Ajax Systems. It uses the official Ajax Cloud API but is an independent project.

The integration accesses your Ajax account using your credentials. Your credentials are only used to authenticate with Ajax servers and are not stored or transmitted anywhere else.

## 💰 Support the Project

If this integration is useful to you, you can support its development:

💖 **[GitHub Sponsors](https://github.com/sponsors/foXaCe)** - Support via GitHub

💳 **[Revolut](https://revolut.me/foxace)** - Instant payment via Revolut

🪙 **Bitcoin**: `bc1qhe4ge22x0anuyeg0fmts6rdmz3t735dnqwt3p7`

Your contributions help improve this project and add new features. Thank you! 🙏

---

# Version Française

**Intégration complète** Home Assistant pour les systèmes de sécurité Ajax Systems via l'API Cloud officielle avec **synchronisation en temps réel**.

## ⚠️ Statut du Projet & Communauté

Cette intégration est **activement développée** mais je débute avec les systèmes de sécurité Ajax. Je possède actuellement et teste avec :
- ✅ **Hub 2 Plus**
- ✅ **MotionCam** (Détecteur de mouvement avec prise de photo)

N'ayant pas encore accès à tous les appareils Ajax, **je ne peux pas tester tous les types d'appareils**. Cependant, l'intégration est construite sur l'API officielle Ajax et devrait théoriquement fonctionner avec tous les appareils Ajax.

**🤝 Aide de la Communauté Nécessaire** : Si vous possédez d'autres appareils Ajax et souhaitez aider à tester et améliorer cette intégration, vos contributions seraient grandement appréciées ! Ensemble, nous pouvons faire de cette intégration la meilleure pour Home Assistant.

Les issues, pull requests et retours d'expérience sont les bienvenus !

## ✨ Fonctionnalités Principales

### 🔄 Synchronisation Temps Réel
- **Sync bidirectionnelle instantanée** - Les changements dans l'app Ajax apparaissent immédiatement dans Home Assistant et vice versa
- **Streaming gRPC** - Même technologie que l'application mobile Ajax officielle
- **Mises à jour sub-secondes** - Changements d'état reflétés en < 1 seconde

### 🛡️ Contrôle Complet de la Sécurité
- ✅ **Armement** (mode absent)
- ✅ **Désarmement**
- ✅ **Mode Nuit**
- ✅ **Armement Partiel** - Armement par groupe/zone
- ✅ **Armement Forcé** - Armer avec capteurs ouverts/problèmes
- ✅ **Bouton Panique** - Déclencher une alarme d'urgence depuis Home Assistant

### 🔔 Notifications
- ✅ **Notifications Temps Réel** - Événements d'armement/désarmement avec nom d'utilisateur
- ✅ **Notifications Persistantes** - Notifications optionnelles dans Home Assistant
- ✅ **Filtres de Notifications** - Aucune, Alarmes uniquement, Événements de sécurité, ou Toutes les notifications
- ✅ **Événements Dispositifs** - Détection de mouvement, ouverture porte/fenêtre, etc.

### 📱 Support des Appareils

**Appareils Testés** (vérifiés personnellement) :
- ✅ **Hub 2 Plus**
- ✅ **MotionCam** - Détecteur de mouvement avec prise de photo

**Théoriquement Supportés** (via l'API officielle, non testés personnellement) :
- **Autres Hubs** - Hub, Hub Plus, Hub 2, Hub 2 (4G)
- **Détecteurs de Mouvement** - MotionProtect, MotionProtect Plus, MotionProtect Outdoor, CombiProtect
- **Contacts de Porte/Fenêtre** - DoorProtect, DoorProtect Plus
- **Sécurité Incendie** - FireProtect, FireProtect Plus, FireProtect 2
- **Détecteurs d'Inondation** - LeaksProtect
- **Bris de Vitre** - GlassProtect
- **Sirènes** - HomeSiren, StreetSiren, StreetSiren DoubleDeck
- **Claviers** - KeyPad, KeyPad Plus, KeyPad TouchScreen
- **Appareils Intelligents** - Socket, WallSwitch, Relay
- **Autres Appareils** - SpaceControl (télécommande), Button (bouton panique), Tag (badge)

**Note** : L'intégration utilise l'API officielle Ajax et est conçue pour fonctionner avec tous les appareils Ajax. Si vous avez des appareils non listés comme testés, ils devraient quand même fonctionner - merci de partager votre expérience !

### 📊 Entités Riches
- **Panneau de Contrôle d'Alarme** - Contrôle complet du système de sécurité avec support groupes/zones
- **Capteurs Binaires** - Mouvement, porte/fenêtre, fumée, inondation, bris de vitre, sabotage, état alimentation, humidité
- **Capteurs** - Niveau batterie, force signal, température, humidité, CO2, compteurs d'appareils, notifications, statut SIM
- **Bouton** - Bouton panique pour les situations d'urgence
- **Interrupteur** - Prises intelligentes et relais avec contrôle de canal

### 🌍 Multi-Hub & Multilingue
- Support de plusieurs Hubs Ajax dans une instance Home Assistant
- Entièrement localisé en **Français** et **Anglais**
- Toutes les entités correctement traduites

## 📦 Installation

### Via HACS (Recommandé)

1. Ouvrez HACS dans Home Assistant
2. Allez dans "Intégrations"
3. Cliquez sur les 3 points en haut à droite
4. Sélectionnez "Dépôts personnalisés"
5. Ajoutez l'URL de ce dépôt : `https://github.com/foXaCe/ajax-hass`
6. Catégorie : "Integration"
7. Cliquez sur "Ajouter"
8. Recherchez "Ajax Security System"
9. Cliquez sur "Télécharger"
10. Redémarrez Home Assistant

### Installation Manuelle

1. Téléchargez la dernière version
2. Copiez le dossier `custom_components/ajax` dans votre répertoire Home Assistant `config/custom_components/`
3. Redémarrez Home Assistant

## ⚙️ Configuration

1. Allez dans **Paramètres** → **Appareils et Services**
2. Cliquez sur **"+ Ajouter une intégration"**
3. Recherchez **"Ajax Security System"**
4. Entrez vos identifiants de compte Ajax :
   - **Email** : Votre email de compte Ajax
   - **Mot de passe** : Votre mot de passe de compte Ajax
   - **Notifications Persistantes** (optionnel) : Afficher les notifications dans l'interface Home Assistant
   - **Filtre de Notifications** (optionnel) : Choisir quelles notifications afficher :
     - **Aucune** : Pas de notifications
     - **Alarmes uniquement** : Seulement les notifications d'alarme/intrusion
     - **Événements de sécurité** : Alarmes + événements d'armement/désarmement
     - **Toutes les notifications** : Toutes les notifications incluant les événements des appareils
5. Cliquez sur **Soumettre**

![Configuration](config.png)

L'intégration découvrira automatiquement tous vos appareils Ajax et créera des entités pour eux.

## 🔒 Sécurité & Confidentialité

**Vos identifiants sont traités avec le plus grand soin :**

### Stockage des Identifiants
- **Stockage local uniquement** : Votre email et mot de passe sont stockés dans le système de config entry chiffré de Home Assistant (`.storage/core.config_entries`)
- **Ne quitte jamais votre réseau** : Les identifiants sont uniquement transmis directement aux serveurs API officiels Ajax
- **Aucun tiers** : L'intégration ne communique avec aucun serveur tiers

### Processus d'Authentification
1. **Hachage du mot de passe** : Votre mot de passe est haché en SHA-256 avant d'être envoyé aux serveurs Ajax
2. **Communication sécurisée** : Toute communication API utilise gRPC sur HTTPS (TLS/SSL chiffré)
3. **Tokens de session** : Après authentification, les tokens de session sont stockés localement dans le stockage sécurisé de Home Assistant
4. **Pas de journalisation** : Les identifiants ne sont jamais journalisés ou exposés dans les logs de débogage

### Ce que le Développeur ne peut PAS Accéder
- ❌ Je (le développeur) **ne peux pas accéder à vos identifiants**
- ❌ Aucune analyse, télémétrie ou tracking
- ❌ Aucune collecte de données d'aucune sorte
- ✅ Entièrement open source - vous pouvez auditer le code vous-même

### Recommandations de Sécurité
- Utilisez un mot de passe fort et unique pour votre compte Ajax
- ✅ **L'authentification à deux facteurs (2FA) est entièrement supportée** - vous pouvez garder le 2FA activé sur votre compte Ajax pour une sécurité renforcée
- Assurez-vous que votre instance Home Assistant est correctement sécurisée (HTTPS, mots de passe forts, pare-feu)
- Maintenez Home Assistant et cette intégration à jour

Pour une transparence totale, vous pouvez examiner comment les identifiants sont gérés dans le code source :
- Flux de configuration : [`config_flow.py`](https://github.com/foXaCe/ajax-hass/blob/main/custom_components/ajax/config_flow.py)
- Authentification API : [`api.py`](https://github.com/foXaCe/ajax-hass/blob/main/custom_components/ajax/api.py)

## 📖 Utilisation

### Contrôle de Sécurité

Utilisez l'entité **Panneau de Contrôle d'Alarme** pour contrôler votre système de sécurité :

```yaml
# Exemple d'automation : Armer en quittant la maison
automation:
  - alias: "Armer Ajax en partant"
    trigger:
      - platform: state
        entity_id: person.votre_nom
        to: "not_home"
    action:
      - service: alarm_control_panel.alarm_arm_away
        target:
          entity_id: alarm_control_panel.ajax_alarm_maison
```

### Armement Forcé

Utilisez l'armement forcé pour armer le système même avec des capteurs ouverts ou des problèmes :

```yaml
# Exemple : Armement forcé au coucher
automation:
  - alias: "Armement forcé au coucher"
    trigger:
      - platform: time
        at: "23:00:00"
    action:
      - service: ajax.force_arm
        target:
          entity_id: alarm_control_panel.ajax_alarm_maison

# Exemple : Armement forcé en mode nuit
automation:
  - alias: "Armement forcé mode nuit"
    trigger:
      - platform: time
        at: "23:00:00"
    action:
      - service: ajax.force_arm_night
        target:
          entity_id: alarm_control_panel.ajax_alarm_maison
```

⚠️ **Attention** : L'armement forcé ignore les capteurs ouverts et les problèmes système. Utilisez avec précaution.

### Bouton Panique

L'entité bouton panique déclenche une alarme d'urgence :

```yaml
# Exemple : Ajouter le bouton panique au tableau de bord
type: button
tap_action:
  action: call-service
  service: button.press
  target:
    entity_id: button.ajax_panic_maison
name: Urgence
icon: mdi:alarm-light
```

⚠️ **Attention** : Le bouton panique déclenche une **vraie alarme d'urgence**. Ne l'utilisez qu'en cas d'urgence réelle ou pour des tests avec l'accord de votre centre de télésurveillance.

### Rapport d'Information des Appareils

Générez un rapport de diagnostic de vos appareils Ajax pour aider à améliorer l'intégration :

```yaml
# Appelez le service dans Outils de développement > Services
service: ajax.generate_device_info
```

Ce service crée un fichier JSON `ajax_device_info.json` dans votre répertoire de configuration Home Assistant (`/config/`) contenant :
- Types et modèles d'appareils
- Versions firmware et hardware
- Attributs disponibles (batterie, signal, température, etc.)
- Statistiques des appareils

**Confidentialité** : Le rapport **exclut toutes les données sensibles** :
- ❌ Pas de noms d'appareils
- ❌ Pas d'IDs uniques
- ❌ Pas d'adresses MAC
- ❌ Pas d'informations de localisation

Ce rapport anonymisé est parfait pour partager lors d'une demande de support pour de nouveaux types d'appareils !

**Où trouver le fichier :**
- Docker : `/config/ajax_device_info.json`
- Installation standard : `~/.homeassistant/ajax_device_info.json`
- Accès via : Add-on File Editor, Studio Code Server, ou Samba Share

Après avoir exécuté le service, vous recevrez une notification persistante avec l'emplacement du fichier.

### Capteurs et Capteurs Binaires

Tous les appareils Ajax apparaissent comme entités Home Assistant appropriées :

- **Détecteurs de mouvement** → `binary_sensor.ajax_mouvement_*`
- **Contacts porte/fenêtre** → `binary_sensor.ajax_porte_*`
- **Température** → `sensor.ajax_temperature_*`
- **Niveau batterie** → `sensor.ajax_batterie_*`
- etc.

## 🔧 Configuration Avancée

### Intervalle de Mise à Jour

L'intégration utilise le **streaming temps réel** pour des mises à jour instantanées (< 1 seconde), avec un polling de secours minimal toutes les 60 secondes. Le polling sert uniquement de solution de repli au cas où la connexion streaming serait interrompue.

**⚠️ Important** : Ne réduisez pas l'intervalle de polling en dessous de 60 secondes pour éviter de surcharger les serveurs API d'Ajax. Le streaming gère déjà toutes les mises à jour en temps réel.

```python
UPDATE_INTERVAL = 60  # secondes
```

### Journalisation

Pour activer la journalisation de débogage, ajoutez à votre `configuration.yaml` :

```yaml
logger:
  default: info
  logs:
    custom_components.ajax: debug
```

## 🐛 Dépannage

### L'intégration ne se charge pas
1. Vérifiez les journaux Home Assistant pour les erreurs
2. Vérifiez que vos identifiants Ajax sont corrects
3. Assurez-vous d'avoir une connexion internet active

### Les mises à jour temps réel ne fonctionnent pas
1. Vérifiez que les tâches de streaming sont démarrées (voir les journaux)
2. Vérifiez que le pare-feu autorise les connexions gRPC (port 443)
3. Redémarrez l'intégration

### Les appareils n'apparaissent pas
1. Attendez que la synchronisation initiale soit terminée (jusqu'à 30 secondes)
2. Vérifiez que les appareils sont visibles dans l'app Ajax
3. Essayez de recharger l'intégration

### Confidentialité & Sécurité

- ✅ Vos identifiants sont uniquement utilisés pour s'authentifier avec les serveurs Ajax
- ✅ Aucune donnée n'est envoyée à des serveurs tiers
- ✅ Toutes les communications sont chiffrées (TLS/SSL)
- ✅ Les tokens de session sont stockés localement dans le stockage sécurisé de Home Assistant
- ✅ L'intégration est entièrement open source - vous pouvez auditer le code

## 🤝 Contribution

Les contributions sont les bienvenues ! N'hésitez pas à soumettre une Pull Request.

Si vous possédez des appareils Ajax qui n'ont pas encore été testés, votre aide serait inestimable pour améliorer la compatibilité.

## 📄 Licence

Ce projet est sous licence MIT - voir le fichier [LICENSE](LICENSE) pour plus de détails.

## ⚠️ Avertissement

Cette intégration n'est **pas officiellement affiliée** à Ajax Systems. Elle utilise l'API Cloud officielle Ajax mais est un projet indépendant.

L'intégration accède à votre compte Ajax en utilisant vos identifiants. Vos identifiants sont uniquement utilisés pour s'authentifier auprès des serveurs Ajax et ne sont ni stockés ni transmis ailleurs.

## 💰 Soutenir le Projet

Si cette intégration vous est utile, vous pouvez soutenir son développement :

💖 **[GitHub Sponsors](https://github.com/sponsors/foXaCe)** - Soutenir via GitHub

💳 **[Revolut](https://revolut.me/foxace)** - Paiement instantané via Revolut

🪙 **Bitcoin** : `bc1qhe4ge22x0anuyeg0fmts6rdmz3t735dnqwt3p7`

Vos contributions aident à améliorer ce projet et à ajouter de nouvelles fonctionnalités. Merci ! 🙏
