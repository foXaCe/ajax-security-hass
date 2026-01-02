"""Ajax event codes mapping.

Event code format: M_XX_YY
- M = signal type (fixed)
- XX = device type (see DEVICE_TYPES)
- YY = event signal

Placeholders in messages:
- %1$s = source object name (device name)
- %2$s = room name
- %3$s = hub name

Supported languages: fr, en, es
"""

from __future__ import annotations

from typing import Any

# Event types (eventTypeV2 field in SQS events)
# Format: event_type -> {lang: description}
EVENT_TYPES: dict[str, dict[str, str]] = {
    "ALARM": {
        "fr": "Alarme (intrusion, incendie, inondation, perte de communication)",
        "en": "Alarm (intrusion, fire, flood, loss of communication)",
        "es": "Alarma (intrusión, incendio, inundación, pérdida de comunicación)",
    },
    "ALARM_RECOVERED": {
        "fr": "Alarme récupérée",
        "en": "Alarm recovered",
        "es": "Alarma recuperada",
    },
    "MALFUNCTION": {
        "fr": "Dysfonctionnement (brouillage, hors ligne, batterie faible, sabotage)",
        "en": "Malfunction (jamming, offline, low battery, tamper)",
        "es": "Mal funcionamiento (interferencia, fuera de línea, batería baja, sabotaje)",
    },
    "FUNCTION_RECOVERED": {
        "fr": "Dysfonctionnement récupéré",
        "en": "Malfunction recovered",
        "es": "Mal funcionamiento recuperado",
    },
    "SECURITY": {
        "fr": "Sécurité (armement/désarmement, mode nuit)",
        "en": "Security (arm/disarm, night mode)",
        "es": "Seguridad (armado/desarmado, modo nocturno)",
    },
    "USER": {
        "fr": "Utilisateur (ajouté/supprimé)",
        "en": "User (added/removed)",
        "es": "Usuario (añadido/eliminado)",
    },
    "LIFECYCLE": {
        "fr": "Cycle de vie (appareil/pièce ajouté/supprimé)",
        "en": "Lifecycle (device/room added/removed)",
        "es": "Ciclo de vida (dispositivo/habitación añadido/eliminado)",
    },
    "SMART_HOME_ACTUATOR": {
        "fr": "Actionneur domotique (relais on/off)",
        "en": "Smart home actuator (relay on/off)",
        "es": "Actuador domótico (relé on/off)",
    },
    "SMART_HOME_ALARM": {
        "fr": "Alarme domotique (qualité d'air hors limites)",
        "en": "Smart home alarm (air quality out of range)",
        "es": "Alarma domótica (calidad del aire fuera de rango)",
    },
    "SMART_HOME_ALARM_RECOVERED": {
        "fr": "Alarme domotique récupérée",
        "en": "Smart home alarm recovered",
        "es": "Alarma domótica recuperada",
    },
    "SMART_HOME_EVENT": {
        "fr": "Événement domotique",
        "en": "Smart home event",
        "es": "Evento domótico",
    },
    "ALARM_WARNING": {
        "fr": "Avertissement d'alarme (zones croisées)",
        "en": "Alarm warning (cross zone devices)",
        "es": "Advertencia de alarma (dispositivos de zona cruzada)",
    },
}

# Device type codes (XX in M_XX_YY)
DEVICE_TYPES = {
    "01": "DoorProtect",
    "02": "MotionProtect",
    "03": "FireProtect",
    "04": "GlassProtect",
    "05": "LeaksProtect",
    "06": "MotionProtect Curtain",
    "07": "ReX",
    "08": "CombiProtect",
    "09": "FireProtect Plus",
    "0A": "KeyPad",
    "0B": "SpaceControl",
    "0C": "Button",
    "0D": "MotionCam",
    "0E": "MotionProtect Plus",
    "0F": "DoorProtect Plus",
    "11": "Transmitter",
    "12": "Relay",
    "13": "MotionProtect Outdoor",
    "14": "StreetSiren",
    "15": "HomeSiren",
    "18": "MotionCam Outdoor",
    "19": "Keypad Plus",
    "1A": "DualCurtain Outdoor",
    "1B": "StreetSiren DoubleDeck",
    "1D": "MultiTransmitter",
    "1E": "Socket",
    "1F": "WallSwitch",
    "21": "Hub",
    "22": "User",
    "23": "Group",
    "24": "Room",
    "25": "Camera",
    "26": "Transmitter (wired)",
    "28": "KeyPad TouchScreen",
    "29": "Tag",
    "2A": "Scenario",
    "2E": "Access Card",
    "30": "Access Code",
    "33": "Arm Switch",
    "41": "KeyPad Fibra",
    "42": "DoubleButton",
    "43": "KeyPad Combi",
    "44": "LightSwitch",
    "45": "vhfBridge",
    "46": "ReX 2",
    "47": "LifeQuality",
    "48": "WaterStop",
    "4C": "Socket (type G/B)",
    "4D": "FireProtect 2",
    "4E": "Curtain Outdoor",
    "4F": "ManualCallPoint",
    "61": "DoorProtect Fibra",
    "62": "MotionProtect Fibra",
    "64": "GlassProtect Fibra",
    "68": "CombiProtect Fibra",
    "6D": "MotionCam Fibra",
    "6E": "MotionProtect Plus Fibra",
    "6F": "DoorProtect Plus Fibra",
    "71": "Transmitter Fibra",
    "72": "MultiRelay Fibra",
    "74": "StreetSiren Fibra",
    "75": "HomeSiren Fibra",
    "76": "LineProtect Fibra",
    "7C": "MultiTransmitter Fibra",
    "ABS": "Common (all devices)",
    "FCP": "Fibra Common",
}

# Translations for event messages
# Format: action_key -> {lang: message}
EVENT_MESSAGES: dict[str, dict[str, str]] = {
    # Security
    "armed": {"fr": "Armé", "en": "Armed", "es": "Armado"},
    "disarmed": {"fr": "Désarmé", "en": "Disarmed", "es": "Desarmado"},
    "night_mode": {
        "fr": "Mode nuit activé",
        "en": "Night mode activated",
        "es": "Modo nocturno activado",
    },
    "night_mode_off": {
        "fr": "Mode nuit désactivé",
        "en": "Night mode deactivated",
        "es": "Modo nocturno desactivado",
    },
    "armed_auto": {
        "fr": "Armé automatiquement",
        "en": "Armed automatically",
        "es": "Armado automáticamente",
    },
    "disarmed_auto": {
        "fr": "Désarmé automatiquement",
        "en": "Disarmed automatically",
        "es": "Desarmado automáticamente",
    },
    "night_mode_auto": {
        "fr": "Mode nuit activé automatiquement",
        "en": "Night mode activated automatically",
        "es": "Modo nocturno activado automáticamente",
    },
    # Door
    "door_opened": {
        "fr": "Ouverture détectée",
        "en": "Opening detected",
        "es": "Apertura detectada",
    },
    "door_closed": {"fr": "Fermé", "en": "Closed", "es": "Cerrado"},
    "ext_contact_opened": {
        "fr": "Contact externe ouvert",
        "en": "External contact open",
        "es": "Contacto externo abierto",
    },
    "ext_contact_closed": {
        "fr": "Contact externe fermé",
        "en": "External contact closed",
        "es": "Contacto externo cerrado",
    },
    "shock_detected": {
        "fr": "Choc détecté",
        "en": "Shock detected",
        "es": "Choque detectado",
    },
    "tilt_detected": {
        "fr": "Inclinaison détectée",
        "en": "Tilt detected",
        "es": "Inclinación detectada",
    },
    "magnetic_masking": {
        "fr": "Masquage magnétique détecté",
        "en": "Magnetic masking detected",
        "es": "Enmascaramiento magnético detectado",
    },
    "magnetic_masking_cleared": {
        "fr": "Plus de masquage magnétique",
        "en": "No magnetic masking",
        "es": "Sin enmascaramiento magnético",
    },
    # Motion
    "motion_detected": {
        "fr": "Mouvement détecté",
        "en": "Motion detected",
        "es": "Movimiento detectado",
    },
    "motion_cleared": {
        "fr": "Plus de mouvement",
        "en": "No motion",
        "es": "Sin movimiento",
    },
    # Glass
    "glass_break": {
        "fr": "Bris de glace détecté",
        "en": "Glass break detected",
        "es": "Rotura de vidrio detectada",
    },
    "glass_break_low": {
        "fr": "Bris de glace détecté (basse fréquence)",
        "en": "Glass break detected (low frequency)",
        "es": "Rotura de vidrio detectada (baja frecuencia)",
    },
    # Smoke/Fire
    "smoke_detected": {
        "fr": "Fumée détectée",
        "en": "Smoke detected",
        "es": "Humo detectado",
    },
    "smoke_cleared": {"fr": "Plus de fumée", "en": "No smoke", "es": "Sin humo"},
    "temp_high": {
        "fr": "Température élevée",
        "en": "High temperature",
        "es": "Temperatura alta",
    },
    "temp_normal": {
        "fr": "Température normale",
        "en": "Normal temperature",
        "es": "Temperatura normal",
    },
    "temp_low": {
        "fr": "Température basse",
        "en": "Low temperature",
        "es": "Temperatura baja",
    },
    "temp_comfort": {
        "fr": "Température de confort",
        "en": "Comfort temperature",
        "es": "Temperatura de confort",
    },
    "rapid_temp_rise": {
        "fr": "Hausse rapide de température",
        "en": "Rapid temperature rise",
        "es": "Aumento rápido de temperatura",
    },
    "rapid_temp_stopped": {
        "fr": "Hausse de température stoppée",
        "en": "Temperature rise stopped",
        "es": "Aumento de temperatura detenido",
    },
    "co_detected": {
        "fr": "Monoxyde de carbone détecté",
        "en": "Carbon monoxide detected",
        "es": "Monóxido de carbono detectado",
    },
    "co_cleared": {
        "fr": "Niveau de CO normal",
        "en": "CO level normal",
        "es": "Nivel de CO normal",
    },
    "fire_alarm": {
        "fr": "Alarme incendie",
        "en": "Fire alarm",
        "es": "Alarma de incendio",
    },
    "hardware_failure": {
        "fr": "Défaillance matérielle",
        "en": "Hardware failure",
        "es": "Fallo de hardware",
    },
    "hardware_ok": {"fr": "Matériel OK", "en": "Hardware OK", "es": "Hardware OK"},
    "chamber_dirty": {
        "fr": "Chambre de fumée sale",
        "en": "Smoke chamber dirty",
        "es": "Cámara de humo sucia",
    },
    "chamber_clean": {
        "fr": "Chambre de fumée propre",
        "en": "Smoke chamber clean",
        "es": "Cámara de humo limpia",
    },
    # Flood
    "leak_detected": {
        "fr": "Fuite d'eau détectée",
        "en": "Water leak detected",
        "es": "Fuga de agua detectada",
    },
    "leak_cleared": {
        "fr": "Plus de fuite d'eau",
        "en": "No water leak",
        "es": "Sin fuga de agua",
    },
    "gas_leak": {
        "fr": "Fuite de gaz détectée",
        "en": "Gas leak detected",
        "es": "Fuga de gas detectada",
    },
    "gas_ok": {
        "fr": "Niveau de gaz normal",
        "en": "Gas level normal",
        "es": "Nivel de gas normal",
    },
    # Panic/Alarm
    "panic": {
        "fr": "Bouton panique pressé",
        "en": "Panic button pressed",
        "es": "Botón de pánico presionado",
    },
    "alarm": {
        "fr": "Alarme détectée",
        "en": "Alarm detected",
        "es": "Alarma detectada",
    },
    "alarm_recovered": {
        "fr": "Alarme récupérée",
        "en": "Alarm recovered",
        "es": "Alarma recuperada",
    },
    "auxiliary_alarm": {
        "fr": "Alarme auxiliaire",
        "en": "Auxiliary alarm",
        "es": "Alarma auxiliar",
    },
    "alarm_muted": {
        "fr": "Alarme temporairement coupée",
        "en": "Alarm temporarily muted",
        "es": "Alarma temporalmente silenciada",
    },
    "unauthorized_access": {
        "fr": "Tentative d'accès non autorisé",
        "en": "Unauthorized access attempt",
        "es": "Intento de acceso no autorizado",
    },
    "brute_force": {
        "fr": "Tentative de forçage du code",
        "en": "Code brute force attempt",
        "es": "Intento de fuerza bruta del código",
    },
    # Relay/Switch
    "switched_on": {"fr": "Allumé", "en": "Switched on", "es": "Encendido"},
    "switched_off": {"fr": "Éteint", "en": "Switched off", "es": "Apagado"},
    "light_on": {"fr": "Lumière allumée", "en": "Light on", "es": "Luz encendida"},
    "light_off": {"fr": "Lumière éteinte", "en": "Light off", "es": "Luz apagada"},
    "light_on_touch": {
        "fr": "Lumière allumée (bouton)",
        "en": "Light on (button)",
        "es": "Luz encendida (botón)",
    },
    "light_off_touch": {
        "fr": "Lumière éteinte (bouton)",
        "en": "Light off (button)",
        "es": "Luz apagada (botón)",
    },
    "light_on_scenario": {
        "fr": "Lumière allumée (scénario)",
        "en": "Light on (scenario)",
        "es": "Luz encendida (escenario)",
    },
    "light_off_scenario": {
        "fr": "Lumière éteinte (scénario)",
        "en": "Light off (scenario)",
        "es": "Luz apagada (escenario)",
    },
    "light_on_arm": {
        "fr": "Lumière allumée (armement)",
        "en": "Light on (arming)",
        "es": "Luz encendida (armado)",
    },
    "light_on_disarm": {
        "fr": "Lumière allumée (désarmement)",
        "en": "Light on (disarming)",
        "es": "Luz encendida (desarmado)",
    },
    "light_off_timer": {
        "fr": "Lumière éteinte (minuterie)",
        "en": "Light off (timer)",
        "es": "Luz apagada (temporizador)",
    },
    "water_on": {"fr": "Eau ouverte", "en": "Water on", "es": "Agua abierta"},
    "water_off": {"fr": "Eau coupée", "en": "Water off", "es": "Agua cerrada"},
    "valve_stuck": {
        "fr": "Vanne bloquée",
        "en": "Valve stuck",
        "es": "Válvula atascada",
    },
    # Button
    "single_press": {
        "fr": "Appui simple",
        "en": "Single press",
        "es": "Pulsación simple",
    },
    "double_press": {
        "fr": "Double appui",
        "en": "Double press",
        "es": "Doble pulsación",
    },
    "long_press": {
        "fr": "Appui long",
        "en": "Long press",
        "es": "Pulsación larga",
    },
    "emergency": {
        "fr": "Bouton d'urgence",
        "en": "Emergency button",
        "es": "Botón de emergencia",
    },
    "siren_activated": {
        "fr": "Sirène activée",
        "en": "Siren activated",
        "es": "Sirena activada",
    },
    # Device status
    "device_online": {
        "fr": "Appareil en ligne",
        "en": "Device online",
        "es": "Dispositivo en línea",
    },
    "device_offline": {
        "fr": "Appareil hors ligne",
        "en": "Device offline",
        "es": "Dispositivo fuera de línea",
    },
    "hub_online": {"fr": "Hub en ligne", "en": "Hub online", "es": "Hub en línea"},
    "hub_offline": {
        "fr": "Hub hors ligne",
        "en": "Hub offline",
        "es": "Hub fuera de línea",
    },
    "low_battery": {"fr": "Batterie faible", "en": "Low battery", "es": "Batería baja"},
    "battery_ok": {"fr": "Batterie OK", "en": "Battery OK", "es": "Batería OK"},
    "power_disconnected": {
        "fr": "Alimentation déconnectée",
        "en": "Power disconnected",
        "es": "Alimentación desconectada",
    },
    "power_restored": {
        "fr": "Alimentation restaurée",
        "en": "Power restored",
        "es": "Alimentación restaurada",
    },
    "overheat": {"fr": "Surchauffe", "en": "Overheat", "es": "Sobrecalentamiento"},
    "temp_ok": {"fr": "Température OK", "en": "Temperature OK", "es": "Temperatura OK"},
    # Tamper
    "tamper_open": {"fr": "Capot ouvert", "en": "Lid open", "es": "Tapa abierta"},
    "tamper_closed": {"fr": "Capot fermé", "en": "Lid closed", "es": "Tapa cerrada"},
    # Hub
    "gsm_poor": {
        "fr": "Signal GSM faible",
        "en": "Poor GSM signal",
        "es": "Señal GSM débil",
    },
    "gsm_ok": {"fr": "Signal GSM OK", "en": "GSM signal OK", "es": "Señal GSM OK"},
    "interference_high": {
        "fr": "Interférences radio élevées",
        "en": "High radio interference",
        "es": "Alta interferencia de radio",
    },
    "interference_ok": {
        "fr": "Interférences radio OK",
        "en": "Radio interference OK",
        "es": "Interferencia de radio OK",
    },
    "hub_off": {"fr": "Hub éteint", "en": "Hub off", "es": "Hub apagado"},
    "hub_on": {"fr": "Hub allumé", "en": "Hub on", "es": "Hub encendido"},
    "ethernet_lost": {
        "fr": "Connexion Ethernet perdue",
        "en": "Ethernet connection lost",
        "es": "Conexión Ethernet perdida",
    },
    "ethernet_restored": {
        "fr": "Connexion Ethernet restaurée",
        "en": "Ethernet connection restored",
        "es": "Conexión Ethernet restaurada",
    },
    "cellular_lost": {
        "fr": "Connexion cellulaire perdue",
        "en": "Cellular connection lost",
        "es": "Conexión celular perdida",
    },
    "cellular_restored": {
        "fr": "Connexion cellulaire restaurée",
        "en": "Cellular connection restored",
        "es": "Conexión celular restaurada",
    },
    "wifi_lost": {
        "fr": "Connexion Wi-Fi perdue",
        "en": "Wi-Fi connection lost",
        "es": "Conexión Wi-Fi perdida",
    },
    "wifi_restored": {
        "fr": "Connexion Wi-Fi restaurée",
        "en": "Wi-Fi connection restored",
        "es": "Conexión Wi-Fi restaurada",
    },
    "firmware_updating": {
        "fr": "Mise à jour firmware en cours",
        "en": "Firmware updating",
        "es": "Actualizando firmware",
    },
    "firmware_updated": {
        "fr": "Firmware mis à jour",
        "en": "Firmware updated",
        "es": "Firmware actualizado",
    },
    "malfunction": {
        "fr": "Dysfonctionnement détecté",
        "en": "Malfunction detected",
        "es": "Mal funcionamiento detectado",
    },
    "sync_failure": {
        "fr": "Échec de synchronisation",
        "en": "Sync failure",
        "es": "Error de sincronización",
    },
    "sync_ok": {"fr": "Synchronisation OK", "en": "Sync OK", "es": "Sincronización OK"},
    "device_added": {
        "fr": "Appareil ajouté",
        "en": "Device added",
        "es": "Dispositivo añadido",
    },
    "device_removed": {
        "fr": "Appareil supprimé",
        "en": "Device removed",
        "es": "Dispositivo eliminado",
    },
    "device_deactivated": {
        "fr": "Appareil désactivé",
        "en": "Device deactivated",
        "es": "Dispositivo desactivado",
    },
    "device_activated": {
        "fr": "Appareil activé",
        "en": "Device activated",
        "es": "Dispositivo activado",
    },
    "device_moved": {
        "fr": "Appareil déplacé",
        "en": "Device moved",
        "es": "Dispositivo movido",
    },
    # User/Scenarios
    "user_added": {
        "fr": "Nouvel utilisateur ajouté",
        "en": "New user added",
        "es": "Nuevo usuario añadido",
    },
    "user_removed": {
        "fr": "Utilisateur supprimé",
        "en": "User removed",
        "es": "Usuario eliminado",
    },
    "scenario_added": {
        "fr": "Nouveau scénario ajouté",
        "en": "New scenario added",
        "es": "Nuevo escenario añadido",
    },
    "scenario_removed": {
        "fr": "Scénario supprimé",
        "en": "Scenario removed",
        "es": "Escenario eliminado",
    },
    "chime_on": {
        "fr": "Carillon activé",
        "en": "Chime activated",
        "es": "Timbre activado",
    },
    "chime_off": {
        "fr": "Carillon désactivé",
        "en": "Chime deactivated",
        "es": "Timbre desactivado",
    },
    # MotionCam
    "wings_lost": {
        "fr": "Connexion Wings perdue",
        "en": "Wings connection lost",
        "es": "Conexión Wings perdida",
    },
    "wings_restored": {
        "fr": "Connexion Wings restaurée",
        "en": "Wings connection restored",
        "es": "Conexión Wings restaurada",
    },
    "photo_received": {
        "fr": "Photo reçue",
        "en": "Photo received",
        "es": "Foto recibida",
    },
    "photo_failed": {
        "fr": "Échec réception photo",
        "en": "Photo reception failed",
        "es": "Error al recibir foto",
    },
    "photo_alarm": {
        "fr": "Photo d'alarme reçue",
        "en": "Alarm photo received",
        "es": "Foto de alarma recibida",
    },
    # LifeQuality
    "humidity_high": {
        "fr": "Humidité élevée",
        "en": "High humidity",
        "es": "Humedad alta",
    },
    "humidity_low": {
        "fr": "Humidité basse",
        "en": "Low humidity",
        "es": "Humedad baja",
    },
    "humidity_comfort": {
        "fr": "Humidité de confort",
        "en": "Comfort humidity",
        "es": "Humedad de confort",
    },
    "co2_high": {"fr": "CO₂ élevé", "en": "High CO₂", "es": "CO₂ alto"},
    "co2_normal": {"fr": "CO₂ normal", "en": "Normal CO₂", "es": "CO₂ normal"},
}

# Event codes mapping
# Format: "M_XX_YY": ("action_key", is_alarm)
EVENT_CODES: dict[str, tuple[str, bool]] = {
    # ============== Hub Events (21) ==============
    "M_21_00": ("power_disconnected", False),
    "M_21_01": ("power_restored", False),
    "M_21_02": ("low_battery", False),
    "M_21_03": ("battery_ok", False),
    "M_21_04": ("tamper_open", True),
    "M_21_05": ("tamper_closed", False),
    "M_21_06": ("gsm_poor", False),
    "M_21_07": ("gsm_ok", False),
    "M_21_08": ("interference_high", False),
    "M_21_09": ("interference_ok", False),
    "M_21_0A": ("hub_offline", True),
    "M_21_0B": ("hub_online", False),
    "M_21_0C": ("hub_off", False),
    "M_21_0D": ("hub_on", False),
    "M_21_10": ("firmware_updating", False),
    "M_21_11": ("firmware_updated", False),
    "M_21_12": ("malfunction", True),
    "M_21_20": ("ethernet_lost", False),
    "M_21_21": ("ethernet_restored", False),
    "M_21_22": ("cellular_lost", False),
    "M_21_23": ("cellular_restored", False),
    "M_21_24": ("wifi_lost", False),
    "M_21_25": ("wifi_restored", False),
    # ============== DoorProtect Events (01, 61, 0F, 6F) ==============
    "M_01_20": ("door_opened", True),
    "M_01_21": ("door_closed", False),
    "M_01_22": ("ext_contact_opened", True),
    "M_01_23": ("ext_contact_closed", False),
    "M_61_20": ("door_opened", True),
    "M_61_21": ("door_closed", False),
    "M_61_22": ("ext_contact_opened", True),
    "M_61_23": ("ext_contact_closed", False),
    "M_0F_20": ("door_opened", True),
    "M_0F_21": ("door_closed", False),
    "M_0F_22": ("ext_contact_opened", True),
    "M_0F_23": ("ext_contact_closed", False),
    "M_0F_30": ("shock_detected", True),
    "M_0F_31": ("tilt_detected", True),
    "M_6F_20": ("door_opened", True),
    "M_6F_21": ("door_closed", False),
    "M_6F_22": ("ext_contact_opened", True),
    "M_6F_23": ("ext_contact_closed", False),
    "M_6F_30": ("shock_detected", True),
    "M_6F_31": ("tilt_detected", True),
    "M_6F_36": ("magnetic_masking", True),
    "M_6F_37": ("magnetic_masking_cleared", False),
    # ============== MotionProtect Events (02, 62, 06, 0E, 6E) ==============
    "M_02_20": ("motion_detected", True),
    "M_02_21": ("motion_cleared", False),
    "M_06_20": ("motion_detected", True),
    "M_62_20": ("motion_detected", True),
    "M_0E_20": ("motion_detected", True),
    "M_0E_21": ("motion_cleared", False),
    "M_6E_20": ("motion_detected", True),
    # ============== MotionCam Events (0D, 18, 6D) ==============
    "M_0D_20": ("motion_detected", True),
    "M_0D_30": ("wings_lost", False),
    "M_0D_31": ("wings_restored", False),
    "M_0D_32": ("photo_received", False),
    "M_0D_33": ("photo_failed", False),
    "M_0D_34": ("photo_alarm", True),
    "M_18_20": ("motion_detected", True),
    "M_6D_20": ("motion_detected", True),
    # ============== GlassProtect Events (04, 64) ==============
    "M_04_20": ("glass_break", True),
    "M_04_21": ("glass_break_low", True),
    "M_04_22": ("ext_contact_opened", True),
    "M_04_23": ("ext_contact_closed", False),
    "M_64_20": ("glass_break", True),
    # ============== LeaksProtect Events (05) ==============
    "M_05_20": ("leak_detected", True),
    "M_05_21": ("leak_cleared", False),
    "M_05_22": ("device_moved", False),
    # ============== FireProtect Events (03, 09, 4D) ==============
    "M_03_20": ("smoke_detected", True),
    "M_03_21": ("smoke_cleared", False),
    "M_03_22": ("temp_high", True),
    "M_03_23": ("temp_normal", False),
    "M_03_24": ("hardware_failure", True),
    "M_03_25": ("hardware_ok", False),
    "M_03_26": ("chamber_dirty", False),
    "M_03_27": ("chamber_clean", False),
    "M_03_28": ("low_battery", False),
    "M_03_29": ("battery_ok", False),
    "M_03_2A": ("rapid_temp_rise", True),
    "M_03_2B": ("rapid_temp_stopped", False),
    "M_03_2E": ("smoke_detected", True),
    "M_03_2F": ("smoke_cleared", False),
    "M_09_20": ("smoke_detected", True),
    "M_09_21": ("smoke_cleared", False),
    "M_09_22": ("temp_high", True),
    "M_09_23": ("temp_normal", False),
    "M_09_30": ("co_detected", True),
    "M_09_31": ("co_cleared", False),
    "M_09_36": ("co_detected", True),
    "M_09_37": ("co_cleared", False),
    "M_4D_20": ("smoke_detected", True),
    "M_4D_21": ("smoke_cleared", False),
    "M_4D_30": ("co_detected", True),
    "M_4D_31": ("co_cleared", False),
    # ============== CombiProtect Events (08, 68) ==============
    "M_08_20": ("motion_detected", True),
    "M_08_21": ("glass_break", True),
    "M_68_20": ("motion_detected", True),
    "M_68_21": ("glass_break", True),
    # ============== Keypad Events (0A, 0B, 19, 28, 29, 41, 43, 6A) ==============
    "M_0A_20": ("disarmed", False),
    "M_0A_21": ("armed", False),
    "M_0A_22": ("night_mode", False),
    "M_0A_23": ("panic", True),
    "M_0A_30": ("unauthorized_access", True),
    "M_0B_20": ("disarmed", False),
    "M_0B_21": ("armed", False),
    "M_0B_22": ("night_mode", False),
    "M_0B_23": ("panic", True),
    "M_19_23": ("panic", True),
    "M_28_23": ("panic", True),
    "M_28_30": ("brute_force", True),
    "M_29_20": ("disarmed", False),
    "M_29_21": ("armed", False),
    "M_41_23": ("panic", True),
    "M_43_23": ("panic", True),
    "M_6A_23": ("panic", True),
    # ============== User Events (22) ==============
    "M_22_00": ("disarmed", False),
    "M_22_01": ("armed", False),
    "M_22_02": ("night_mode", False),
    "M_22_03": ("panic", True),
    "M_22_07": ("user_added", False),
    "M_22_08": ("user_removed", False),
    "M_22_20": ("panic", True),
    "M_22_24": ("armed", False),  # Unsuccessful arming attempt
    "M_22_26": ("armed", False),  # Armed with malfunctions
    "M_22_28": ("night_mode_off", False),
    "M_22_36": ("alarm_muted", False),
    "M_22_38": ("chime_on", False),
    "M_22_39": ("chime_off", False),
    # ============== Button Events (0C, 42) ==============
    "M_0C_20": ("panic", True),
    "M_0C_30": ("fire_alarm", True),
    "M_0C_31": ("auxiliary_alarm", True),
    "M_42_20": ("panic", True),
    "M_42_21": ("panic", True),
    # ============== Transmitter Events (11, 1D, 26, 71, 7C) ==============
    "M_11_20": ("alarm", True),
    "M_11_21": ("alarm_recovered", False),
    "M_11_30": ("fire_alarm", True),
    "M_11_31": ("smoke_cleared", False),
    "M_11_36": ("panic", True),
    "M_11_39": ("gas_leak", True),
    "M_11_3A": ("gas_ok", False),
    "M_1D_20": ("alarm", True),
    "M_1D_43": ("leak_detected", True),
    "M_1D_44": ("leak_cleared", False),
    "M_26_20": ("alarm", True),
    "M_26_21": ("alarm_recovered", False),
    "M_71_20": ("alarm", True),
    "M_71_21": ("alarm_recovered", False),
    "M_7C_20": ("alarm", True),
    "M_7C_43": ("leak_detected", True),
    "M_7C_44": ("leak_cleared", False),
    # ============== Relay/Socket Events (12, 1E, 1F, 4C, 72) ==============
    "M_1E_20": ("overheat", True),
    "M_1E_21": ("temp_ok", False),
    "M_1E_22": ("switched_on", False),
    "M_1E_23": ("switched_off", False),
    "M_1E_24": ("switched_off", False),  # Failed to switch off
    "M_1E_25": ("overheat", True),  # Overcurrent
    "M_1E_28": ("overheat", True),  # Overvoltage
    "M_1E_29": ("overheat", True),  # Low voltage
    "M_1E_2A": ("temp_ok", False),  # Voltage OK
    "M_1E_2D": ("switched_on", False),  # Activated
    "M_1F_22": ("switched_on", False),
    "M_1F_23": ("switched_off", False),
    "M_1F_2D": ("switched_on", False),  # Activated
    "M_12_22": ("switched_on", False),
    "M_12_23": ("switched_off", False),
    "M_12_2D": ("siren_activated", True),
    "M_4C_22": ("switched_on", False),
    "M_4C_23": ("switched_off", False),
    "M_72_2D": ("switched_on", False),
    "M_72_2F": ("switched_on", False),  # Scenario
    "M_72_30": ("switched_on", False),  # Arming
    "M_72_31": ("switched_on", False),  # Disarming
    "M_72_33": ("switched_off", False),
    "M_72_35": ("switched_off", False),  # Scenario
    "M_72_36": ("switched_off", False),  # Timer
    # ============== LightSwitch Events (44) ==============
    "M_44_20": ("overheat", True),
    "M_44_21": ("temp_ok", False),
    "M_44_2D": ("light_on", False),
    "M_44_2E": ("light_on_touch", False),
    "M_44_2F": ("light_on_scenario", False),
    "M_44_30": ("light_on_arm", False),
    "M_44_31": ("light_on_disarm", False),
    "M_44_33": ("light_off", False),
    "M_44_34": ("light_off_touch", False),
    "M_44_35": ("light_off_scenario", False),
    "M_44_36": ("light_off_timer", False),
    # ============== Siren Events (14, 15, 1B, 74, 75) ==============
    "M_14_20": ("device_moved", False),
    "M_15_20": ("device_moved", False),
    "M_1B_20": ("device_moved", False),
    "M_74_20": ("device_moved", False),
    "M_75_20": ("device_moved", False),
    # ============== Scenario Events (2A) ==============
    "M_2A_08": ("scenario_added", False),
    "M_2A_09": ("scenario_removed", False),
    "M_2A_20": ("disarmed_auto", False),
    "M_2A_21": ("armed_auto", False),
    "M_2A_22": ("night_mode_auto", False),
    "M_2A_26": ("armed_auto", False),  # Armed with malfunctions
    "M_2A_28": ("night_mode_off", False),
    # ============== LifeQuality Events (47) ==============
    "M_47_20": ("temp_high", False),
    "M_47_21": ("temp_low", False),
    "M_47_22": ("temp_comfort", False),
    "M_47_23": ("humidity_high", False),
    "M_47_24": ("humidity_low", False),
    "M_47_25": ("humidity_comfort", False),
    "M_47_26": ("co2_high", True),
    "M_47_27": ("co2_normal", False),
    "M_47_28": ("device_moved", False),
    # ============== WaterStop Events (48) ==============
    "M_48_20": ("power_disconnected", False),
    "M_48_21": ("power_restored", False),
    "M_48_22": ("temp_high", False),
    "M_48_23": ("temp_normal", False),
    "M_48_24": ("valve_stuck", True),
    "M_48_33": ("water_on", False),
    "M_48_36": ("water_on", False),
    "M_48_38": ("water_off", False),
    "M_48_41": ("water_off", False),
    # ============== Common Device Events (ABS) ==============
    "M_ABS_00": ("tamper_open", True),
    "M_ABS_01": ("tamper_closed", False),
    "M_ABS_02": ("battery_ok", False),
    "M_ABS_03": ("low_battery", False),
    "M_ABS_04": ("device_offline", True),
    "M_ABS_05": ("device_online", False),
    "M_ABS_06": ("sync_failure", False),
    "M_ABS_07": ("sync_ok", False),
    "M_ABS_08": ("device_added", False),
    "M_ABS_09": ("device_removed", False),
    "M_ABS_0A": ("device_deactivated", False),
    "M_ABS_0B": ("device_activated", False),
    "M_ABS_0E": ("device_deactivated", False),  # Max alarms
    "M_ABS_0F": ("device_deactivated", False),  # Restoration timer
    "M_ABS_12": ("malfunction", True),
    # ============== Fibra Common Events (FCP) ==============
    "M_FCP_00": ("tamper_open", True),
    "M_FCP_01": ("tamper_closed", False),
    "M_FCP_02": ("power_restored", False),
    "M_FCP_03": ("low_battery", False),  # Insufficient power
    "M_FCP_04": ("device_offline", True),
    "M_FCP_05": ("device_online", False),
    "M_FCP_06": ("sync_failure", False),
    "M_FCP_07": ("sync_ok", False),
    "M_FCP_08": ("device_added", False),
    "M_FCP_09": ("device_removed", False),
    "M_FCP_0A": ("device_deactivated", False),
    "M_FCP_0B": ("device_activated", False),
    "M_FCP_0E": ("device_deactivated", False),
    "M_FCP_0F": ("device_deactivated", False),
    "M_FCP_12": ("malfunction", True),
}

# Action to category mapping (for determining how to handle the event)
ACTION_CATEGORIES = {
    # Security state changes
    "armed": "security",
    "disarmed": "security",
    "night_mode": "security",
    "night_mode_off": "security",
    "night_mode_auto": "security",
    "armed_auto": "security",
    "disarmed_auto": "security",
    # Door events
    "door_opened": "door",
    "door_closed": "door",
    "ext_contact_opened": "door",
    "ext_contact_closed": "door",
    # Motion events
    "motion_detected": "motion",
    "motion_cleared": "motion",
    # Smoke/Fire events
    "smoke_detected": "smoke",
    "smoke_cleared": "smoke",
    "temp_high": "smoke",
    "temp_normal": "smoke",
    "rapid_temp_rise": "smoke",
    "co_detected": "smoke",
    "co_cleared": "smoke",
    "fire_alarm": "smoke",
    # Flood events
    "leak_detected": "flood",
    "leak_cleared": "flood",
    # Glass break events
    "glass_break": "glass",
    "glass_break_low": "glass",
    # Relay/Switch events
    "switched_on": "relay",
    "switched_off": "relay",
    "light_on": "relay",
    "light_off": "relay",
    "light_on_touch": "relay",
    "light_off_touch": "relay",
    "light_on_scenario": "relay",
    "light_off_scenario": "relay",
    "light_on_arm": "relay",
    "light_on_disarm": "relay",
    "light_off_timer": "relay",
    "water_on": "relay",
    "water_off": "relay",
    # Tamper events
    "tamper_open": "tamper",
    "tamper_closed": "tamper",
    # Device status events
    "device_online": "status",
    "device_offline": "status",
    "hub_online": "status",
    "hub_offline": "status",
    "low_battery": "status",
    "battery_ok": "status",
    "power_disconnected": "status",
    "power_restored": "status",
    # Panic events
    "panic": "panic",
    "alarm": "alarm",
    "alarm_recovered": "alarm",
}


# Default language fallback order
DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("fr", "en", "es")


def get_event_message(action_key: str, language: str = DEFAULT_LANGUAGE) -> str:
    """Get the translated message for an action key.

    Args:
        action_key: Action key like "door_opened", "motion_detected"
        language: Language code (fr, en, es)

    Returns:
        Translated message or the action_key if not found
    """
    if action_key not in EVENT_MESSAGES:
        # Return a formatted version of the action key
        return action_key.replace("_", " ").title()

    messages = EVENT_MESSAGES[action_key]

    # Try requested language, fall back to English, then any available
    if language in messages:
        return messages[language]
    if DEFAULT_LANGUAGE in messages:
        return messages[DEFAULT_LANGUAGE]
    # Return first available
    return next(iter(messages.values()), action_key)


def get_event_type_description(
    event_type: str, language: str = DEFAULT_LANGUAGE
) -> str:
    """Get the translated description for an event type (eventTypeV2).

    Args:
        event_type: Event type like "ALARM", "SECURITY", "MALFUNCTION"
        language: Language code (fr, en, es)

    Returns:
        Translated description or the event_type if not found
    """
    if event_type not in EVENT_TYPES:
        return event_type.replace("_", " ").title()

    descriptions = EVENT_TYPES[event_type]

    if language in descriptions:
        return descriptions[language]
    if DEFAULT_LANGUAGE in descriptions:
        return descriptions[DEFAULT_LANGUAGE]
    return next(iter(descriptions.values()), event_type)


def parse_event_code(
    event_code: str, language: str = DEFAULT_LANGUAGE
) -> dict[str, Any] | None:
    """Parse an event code and return event details.

    Args:
        event_code: Event code like "M_01_20"
        language: Language code for message translation (fr, en, es)

    Returns:
        Dict with action, message, category, is_alarm, device_type or None if not found
    """
    if not event_code:
        return None

    # Normalize the code (uppercase)
    code = event_code.upper()

    # Look up in our mapping
    if code not in EVENT_CODES:
        return None

    action_key, is_alarm = EVENT_CODES[code]
    category = ACTION_CATEGORIES.get(action_key, "unknown")
    message = get_event_message(action_key, language)
    device_type = get_device_type_name(code)

    return {
        "action": action_key,
        "message": message,
        "category": category,
        "is_alarm": is_alarm,
        "device_type": device_type,
        "event_code": code,
    }


def get_device_type_name(event_code: str) -> str | None:
    """Get the device type name from an event code.

    Args:
        event_code: Event code like "M_01_20"

    Returns:
        Device type name like "DoorProtect" or None
    """
    if not event_code or not event_code.startswith("M_"):
        return None

    parts = event_code.split("_")
    if len(parts) >= 2:
        device_code = parts[1].upper()
        return DEVICE_TYPES.get(device_code)

    return None


def format_event_message(
    event_code: str,
    device_name: str = "",
    room_name: str = "",
    hub_name: str = "",
    language: str = DEFAULT_LANGUAGE,
) -> str:
    """Format an event message with device/room/hub names.

    Uses the Ajax placeholder format:
    - %1$s = device name
    - %2$s = room name
    - %3$s = hub name

    Args:
        event_code: Event code like "M_01_20"
        device_name: Name of the device
        room_name: Name of the room
        hub_name: Name of the hub
        language: Language code (fr, en, es)

    Returns:
        Formatted message string
    """
    parsed = parse_event_code(event_code, language)
    if not parsed:
        return f"Unknown event: {event_code}"

    message = parsed["message"]

    # Build full message with context
    parts = []

    if hub_name:
        parts.append(hub_name)

    parts.append(message)

    if device_name:
        parts.append(device_name)
        if room_name:
            # Use localized "in" word
            in_word = {"fr": "dans", "en": "in", "es": "en"}.get(language, "in")
            parts.append(f"{in_word} {room_name}")

    return ": ".join(parts[:2]) if len(parts) > 1 else parts[0] if parts else message
