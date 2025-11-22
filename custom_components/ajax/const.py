"""Constants for the Ajax integration."""
from enum import Enum

# Integration domain
DOMAIN = "ajax"

# Configuration and defaults
CONF_INTEGRATION_ID = "integration_id"
CONF_API_KEY = "api_key"

# AWS SQS Configuration (optional)
CONF_AWS_ACCESS_KEY = "aws_access_key"
CONF_AWS_SECRET_KEY = "aws_secret_key"
CONF_EVENTS_QUEUE = "events_queue"
CONF_AWS_REGION = "aws_region"

# Options
CONF_PERSISTENT_NOTIFICATION = "persistent_notification"
CONF_NOTIFICATION_FILTER = "notification_filter"

# Notification filter options
NOTIFICATION_FILTER_NONE = "none"
NOTIFICATION_FILTER_ALARMS_ONLY = "alarms_only"
NOTIFICATION_FILTER_SECURITY_EVENTS = "security_events"
NOTIFICATION_FILTER_ALL = "all"

# REST API endpoints (official)
AJAX_REST_API_BASE_URL = "https://api.ajax.systems/api"
AJAX_REST_API_TIMEOUT = 30  # seconds

# AWS SQS defaults
DEFAULT_AWS_REGION = "eu-central-1"  # Frankfurt
DEFAULT_SCAN_INTERVAL = 30  # seconds

# Update intervals (seconds)
UPDATE_INTERVAL = 30  # Poll every 30 seconds (real-time updates via SQS)


# ==============================================================================
# Malfunction and Event Message Mappings
# ==============================================================================


class MalfunctionType(str, Enum):
    """Malfunction types from Ajax API."""

    NO_MALFUNCTION_INFO = "no_malfunction_info"
    CABLE_BREAK_ISSUE = "cable_break_issue"
    VOLTAGE_INSTABILITY = "voltage_instability"
    SIREN_VOLUME_TEST_REQUIRED = "siren_volume_test_required"
    CO_SENSOR_MALFUNCTION = "co_sensor_malfunction"
    CO_SENSOR_LEVEL_EXCEEDED = "co_sensor_level_exceeded"
    SMOKE_DETECTOR_CAMERA_MALFUNCTION = "smoke_detector_camera_malfunction"
    MICROWAVE_SENSOR_CALIBRATION_ERROR = "microwave_sensor_calibration_error"
    ACCELEROMETER_MALFUNCTION = "accelerometer_malfunction"
    BAD_INPUT_RESISTANCE = "bad_input_resistance"
    MODEM_MALFUNCTION = "modem_malfunction"
    WIFI_CONNECTION_FAIL = "wifi_connection_fail"
    BATTERY_MALFUNCTION = "battery_malfunction"
    BATTERY_CHARGE_ERROR = "battery_charge_error"
    SOFTWARE_MALFUNCTION = "software_malfunction"
    FLASH_MALFUNCTION = "flash_malfunction"
    LOW_BATTERY = "low_battery"
    LOST_CONNECTION = "lost_connection"
    TAMPER = "tamper"
    SENSOR_BLOCKED = "sensor_blocked"


# Human-readable malfunction messages
MALFUNCTION_MESSAGES = {
    MalfunctionType.CABLE_BREAK_ISSUE: {
        "en": "Cable break detected",
        "fr": "Rupture de câble détectée",
    },
    MalfunctionType.VOLTAGE_INSTABILITY: {
        "en": "Voltage instability",
        "fr": "Instabilité de tension",
    },
    MalfunctionType.SIREN_VOLUME_TEST_REQUIRED: {
        "en": "Siren volume test required",
        "fr": "Test du volume de la sirène requis",
    },
    MalfunctionType.CO_SENSOR_MALFUNCTION: {
        "en": "CO sensor malfunction",
        "fr": "Dysfonctionnement du détecteur de CO",
    },
    MalfunctionType.CO_SENSOR_LEVEL_EXCEEDED: {
        "en": "CO level exceeded",
        "fr": "Niveau de CO dépassé",
    },
    MalfunctionType.SMOKE_DETECTOR_CAMERA_MALFUNCTION: {
        "en": "Smoke detector camera malfunction",
        "fr": "Dysfonctionnement de la caméra du détecteur de fumée",
    },
    MalfunctionType.MICROWAVE_SENSOR_CALIBRATION_ERROR: {
        "en": "Microwave sensor calibration error",
        "fr": "Erreur de calibration du capteur micro-ondes",
    },
    MalfunctionType.ACCELEROMETER_MALFUNCTION: {
        "en": "Accelerometer malfunction",
        "fr": "Dysfonctionnement de l'accéléromètre",
    },
    MalfunctionType.BAD_INPUT_RESISTANCE: {
        "en": "Bad input resistance",
        "fr": "Mauvaise résistance d'entrée",
    },
    MalfunctionType.MODEM_MALFUNCTION: {
        "en": "Modem malfunction",
        "fr": "Dysfonctionnement du modem",
    },
    MalfunctionType.WIFI_CONNECTION_FAIL: {
        "en": "Wi-Fi connection failed",
        "fr": "Échec de connexion Wi-Fi",
    },
    MalfunctionType.BATTERY_MALFUNCTION: {
        "en": "Battery malfunction",
        "fr": "Dysfonctionnement de la batterie",
    },
    MalfunctionType.BATTERY_CHARGE_ERROR: {
        "en": "Battery charge error",
        "fr": "Erreur de charge de la batterie",
    },
    MalfunctionType.SOFTWARE_MALFUNCTION: {
        "en": "Software malfunction",
        "fr": "Dysfonctionnement logiciel",
    },
    MalfunctionType.FLASH_MALFUNCTION: {
        "en": "Flash memory malfunction",
        "fr": "Dysfonctionnement de la mémoire flash",
    },
    MalfunctionType.LOW_BATTERY: {
        "en": "Low battery",
        "fr": "Batterie faible",
    },
    MalfunctionType.LOST_CONNECTION: {
        "en": "Connection lost",
        "fr": "Connexion perdue",
    },
    MalfunctionType.TAMPER: {
        "en": "Tamper detected",
        "fr": "Sabotage détecté",
    },
    MalfunctionType.SENSOR_BLOCKED: {
        "en": "Sensor blocked",
        "fr": "Capteur bloqué",
    },
}


# Mapping of raw event type strings to user-friendly messages
EVENT_TYPE_MAPPING = {
    # Raw event types as they appear in the API
    "motion_detected": {"en": "Motion detected", "fr": "Mouvement détecté"},
    "door_opened": {"en": "Door opened", "fr": "Porte ouverte"},
    "door_closed": {"en": "Door closed", "fr": "Porte fermée"},
    "window_opened": {"en": "Window opened", "fr": "Fenêtre ouverte"},
    "window_closed": {"en": "Window closed", "fr": "Fenêtre fermée"},
    "smoke_detected": {"en": "Smoke detected", "fr": "Fumée détectée"},
    "leak_detected": {"en": "Water leak detected", "fr": "Fuite d'eau détectée"},
    "flood_detected": {"en": "Flood detected", "fr": "Inondation détectée"},
    "glass_break": {"en": "Glass break detected", "fr": "Bris de vitre détecté"},
    "glass_break_detected": {"en": "Glass break detected", "fr": "Bris de vitre détecté"},

    # Alarm events
    "alarm_triggered": {"en": "Alarm triggered", "fr": "Alarme déclenchée"},
    "intrusion_alarm": {"en": "Intrusion detected", "fr": "Intrusion détectée"},
    "intrusion": {"en": "Intrusion detected", "fr": "Intrusion détectée"},
    "fire_alarm": {"en": "Fire alarm", "fr": "Alarme incendie"},
    "fire": {"en": "Fire alarm", "fr": "Alarme incendie"},
    "co_alarm": {"en": "Carbon monoxide detected", "fr": "Monoxyde de carbone détecté"},
    "flood_alarm": {"en": "Flood alarm", "fr": "Alarme inondation"},
    "panic_button": {"en": "Panic button pressed", "fr": "Bouton panique activé"},
    "panic": {"en": "Panic button pressed", "fr": "Bouton panique activé"},
    "tamper_alarm": {"en": "Tamper alarm", "fr": "Alarme sabotage"},
    "tamper": {"en": "Tamper detected", "fr": "Sabotage détecté"},
    "tampered": {"en": "Tamper detected", "fr": "Sabotage détecté"},

    # Arming/Disarming
    "armed": {"en": "System armed", "fr": "Système armé"},
    "disarmed": {"en": "System disarmed", "fr": "Système désarmé"},
    "night_mode_on": {"en": "Night mode activated", "fr": "Mode nuit activé"},
    "night_mode_off": {"en": "Night mode deactivated", "fr": "Mode nuit désactivé"},
    "armed_with_malfunctions": {"en": "Armed with problems", "fr": "Armé avec des problèmes"},
    "night_mode_with_malfunctions": {"en": "Night mode with problems", "fr": "Mode nuit avec des problèmes"},

    # Device status
    "device_online": {"en": "Device connected", "fr": "Appareil connecté"},
    "device_offline": {"en": "Device disconnected", "fr": "Appareil déconnecté"},
    "online": {"en": "Connected", "fr": "Connecté"},
    "offline": {"en": "Disconnected", "fr": "Déconnecté"},
    "low_battery": {"en": "Low battery", "fr": "Batterie faible"},
    "battery_ok": {"en": "Battery OK", "fr": "Batterie OK"},
    "battery_low": {"en": "Low battery", "fr": "Batterie faible"},
    "malfunction_detected": {"en": "Problem detected", "fr": "Problème détecté"},
    "malfunction_resolved": {"en": "Problem resolved", "fr": "Problème résolu"},
    "malfunction": {"en": "Problem detected", "fr": "Problème détecté"},
    "tamper_detected": {"en": "Tamper detected", "fr": "Sabotage détecté"},
    "tamper_resolved": {"en": "Tamper resolved", "fr": "Sabotage résolu"},

    # System events
    "hub_online": {"en": "Hub connected", "fr": "Hub connecté"},
    "hub_offline": {"en": "Hub disconnected", "fr": "Hub déconnecté"},
    "power_outage": {"en": "Power outage", "fr": "Coupure de courant"},
    "power_restored": {"en": "Power restored", "fr": "Alimentation rétablie"},
    "external_power_on": {"en": "External power connected", "fr": "Alimentation externe connectée"},
    "external_power_off": {"en": "External power disconnected", "fr": "Alimentation externe déconnectée"},

    # User actions
    "user_armed": {"en": "Armed by user", "fr": "Armé par l'utilisateur"},
    "user_disarmed": {"en": "Disarmed by user", "fr": "Désarmé par l'utilisateur"},
    "device_bypassed": {"en": "Device bypassed", "fr": "Appareil contourné"},
    "device_unbypassed": {"en": "Device active", "fr": "Appareil actif"},
    "bypassed": {"en": "Bypassed", "fr": "Contourné"},

    # Groups
    "group_armed": {"en": "Group armed", "fr": "Groupe armé"},
    "group_disarmed": {"en": "Group disarmed", "fr": "Groupe désarmé"},
    "group_armed_with_malfunctions": {"en": "Group armed with problems", "fr": "Groupe armé avec des problèmes"},
}


def get_malfunction_message(malfunction_type: str, language: str = "en") -> str:
    """Get human-readable malfunction message.

    Args:
        malfunction_type: The malfunction type key
        language: Language code (en or fr)

    Returns:
        Human-readable message
    """
    try:
        malfunction_enum = MalfunctionType(malfunction_type.lower())
        return MALFUNCTION_MESSAGES.get(malfunction_enum, {}).get(
            language, malfunction_type.replace("_", " ").title()
        )
    except (ValueError, KeyError):
        return malfunction_type.replace("_", " ").title()


def get_event_message(
    event_type: str,
    language: str = "en",
    device_name: str | None = None,
    room_name: str | None = None,
) -> str:
    """Get human-readable event message.

    Args:
        event_type: The event type key (raw from API)
        language: Language code (en or fr)
        device_name: Optional device name to include
        room_name: Optional room name to include

    Returns:
        Human-readable message formatted like the Ajax app
    """
    # Normalize event type
    event_type_normalized = event_type.lower().replace(" ", "_")

    # Get base message from mapping
    event_data = EVENT_TYPE_MAPPING.get(event_type_normalized)
    if event_data:
        base_message = event_data.get(language, event_type.replace("_", " ").title())
    else:
        base_message = event_type.replace("_", " ").title()

    # Build full message like Ajax app does it
    # Format: "Device name (Room name) - Message"
    parts = []

    if device_name:
        if room_name:
            if language == "fr":
                parts.append(f"{device_name} ({room_name})")
            else:
                parts.append(f"{device_name} ({room_name})")
        else:
            parts.append(device_name)

    if parts:
        return f"{parts[0]} - {base_message}"

    return base_message
