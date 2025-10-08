"""
Domain-specific handlers for Home Assistant entities.

This module provides domain-specific configuration and logic for handling
different types of Home Assistant entities (lights, climate, covers, etc.).
"""

from typing import Any

# Domain-specific handler configurations
DOMAIN_HANDLERS = {
    "light": {
        "valid_actions": ["on", "off", "toggle", "set", "adjust"],
        "parameters": [
            "brightness",
            "color_temp",
            "rgb_color",
            "effect",
            "kelvin",
            "hs_color",
        ],
        "quick_actions": ["toggle", "dim", "brighten"],
        "state_attributes": ["brightness", "color_temp", "rgb_color"],
        "supports_dimming": True,
        "supports_color": True,
    },
    "climate": {
        "valid_actions": ["on", "off", "set", "heat", "cool", "auto", "heat_cool"],
        "parameters": [
            "temperature",
            "target_temp_high",
            "target_temp_low",
            "hvac_mode",
            "fan_mode",
        ],
        "quick_actions": ["warmer", "cooler", "auto_mode"],
        "state_attributes": ["current_temperature", "target_temperature", "hvac_mode"],
        "supports_temperature": True,
        "supports_hvac_modes": True,
    },
    "cover": {
        "valid_actions": ["open", "close", "stop", "toggle", "set"],
        "parameters": ["position", "tilt_position"],
        "quick_actions": ["open", "close", "toggle"],
        "state_attributes": ["current_position", "current_tilt_position"],
        "supports_position": True,
        "supports_tilt": True,
    },
    "switch": {
        "valid_actions": ["on", "off", "toggle"],
        "parameters": [],
        "quick_actions": ["toggle"],
        "state_attributes": ["state"],
        "supports_dimming": False,
        "supports_color": False,
    },
    "media_player": {
        "valid_actions": ["play", "pause", "stop", "toggle", "set", "next", "previous"],
        "parameters": [
            "volume_level",
            "media_content_id",
            "media_content_type",
            "source",
        ],
        "quick_actions": ["play_pause", "volume_up", "volume_down"],
        "state_attributes": ["volume_level", "media_title", "source"],
        "supports_volume": True,
        "supports_media": True,
    },
    "fan": {
        "valid_actions": ["on", "off", "toggle", "set"],
        "parameters": ["speed", "percentage", "preset_mode", "direction"],
        "quick_actions": ["toggle", "speed_up", "speed_down"],
        "state_attributes": ["percentage", "preset_mode"],
        "supports_speed": True,
        "supports_direction": True,
    },
    "vacuum": {
        "valid_actions": ["start", "stop", "pause", "return_to_base", "clean_spot"],
        "parameters": ["fan_speed", "spot_area"],
        "quick_actions": ["start_cleaning", "return_home"],
        "state_attributes": ["battery_level", "status"],
        "supports_zones": True,
        "supports_mapping": True,
    },
    "lock": {
        "valid_actions": ["lock", "unlock", "open"],
        "parameters": ["code"],
        "quick_actions": ["toggle_lock"],
        "state_attributes": ["locked"],
        "supports_codes": True,
        "security_sensitive": True,
    },
    "alarm_control_panel": {
        "valid_actions": ["arm_home", "arm_away", "arm_night", "disarm"],
        "parameters": ["code"],
        "quick_actions": ["arm", "disarm"],
        "state_attributes": ["state"],
        "security_sensitive": True,
        "requires_code": True,
    },
    "water_heater": {
        "valid_actions": ["on", "off", "set"],
        "parameters": ["temperature", "operation_mode"],
        "quick_actions": ["toggle", "temperature_up", "temperature_down"],
        "state_attributes": ["current_temperature", "target_temperature"],
        "supports_temperature": True,
        "supports_modes": True,
    },
    "humidifier": {
        "valid_actions": ["on", "off", "toggle", "set"],
        "parameters": ["humidity", "mode"],
        "quick_actions": ["toggle", "humidity_up", "humidity_down"],
        "state_attributes": ["current_humidity", "target_humidity"],
        "supports_humidity": True,
        "supports_modes": True,
    },
    "camera": {
        "valid_actions": ["snapshot", "record", "stream"],
        "parameters": ["filename", "duration"],
        "quick_actions": ["take_snapshot"],
        "state_attributes": ["entity_picture", "access_token"],
        "supports_streaming": True,
        "supports_recording": True,
    },
    "scene": {
        "valid_actions": ["turn_on", "activate"],
        "parameters": [],
        "quick_actions": ["activate"],
        "state_attributes": ["state"],
        "is_stateless": True,
        "action_only": True,
    },
    "script": {
        "valid_actions": ["turn_on", "turn_off", "toggle"],
        "parameters": [],
        "quick_actions": ["run", "stop"],
        "state_attributes": ["state"],
        "supports_execution": True,
        "can_be_stopped": True,
    },
    "automation": {
        "valid_actions": ["turn_on", "turn_off", "toggle", "trigger"],
        "parameters": [],
        "quick_actions": ["enable", "disable", "trigger"],
        "state_attributes": ["state", "last_triggered"],
        "supports_enable_disable": True,
        "supports_manual_trigger": True,
    },
    # Read-only domains (sensors, etc.)
    "sensor": {
        "valid_actions": [],
        "parameters": [],
        "quick_actions": [],
        "state_attributes": ["state", "unit_of_measurement"],
        "read_only": True,
        "provides_data": True,
    },
    "binary_sensor": {
        "valid_actions": [],
        "parameters": [],
        "quick_actions": [],
        "state_attributes": ["state", "device_class"],
        "read_only": True,
        "provides_data": True,
    },
    "device_tracker": {
        "valid_actions": [],
        "parameters": [],
        "quick_actions": [],
        "state_attributes": ["state", "latitude", "longitude"],
        "read_only": True,
        "provides_location": True,
    },
}


def get_domain_handler(entity_id: str) -> dict[str, Any]:
    """Get domain-specific configuration for an entity.

    Args:
        entity_id: Full entity ID (e.g., 'light.living_room')

    Returns:
        Domain handler configuration dictionary
    """
    if "." not in entity_id:
        # Fallback for invalid entity ID format
        return get_default_handler()

    domain = entity_id.split(".")[0]
    return DOMAIN_HANDLERS.get(domain, get_default_handler())


def get_default_handler() -> dict[str, Any]:
    """Get default handler for unknown domains.

    Returns:
        Default handler configuration
    """
    return {
        "valid_actions": ["on", "off", "toggle"],
        "parameters": [],
        "quick_actions": ["toggle"],
        "state_attributes": ["state"],
        "supports_basic_control": True,
        "unknown_domain": True,
    }


def get_domain_capabilities(domain: str) -> dict[str, Any]:
    """Get capabilities for a specific domain.

    Args:
        domain: Domain name (e.g., 'light', 'climate')

    Returns:
        Dictionary of domain capabilities
    """
    handler = DOMAIN_HANDLERS.get(domain, get_default_handler())

    capabilities = {
        "domain": domain,
        "controllable": len(handler.get("valid_actions", [])) > 0,
        "read_only": handler.get("read_only", False),
        "security_sensitive": handler.get("security_sensitive", False),
        "requires_parameters": len(handler.get("parameters", [])) > 0,
        "has_quick_actions": len(handler.get("quick_actions", [])) > 0,
    }

    # Add specific capability flags
    for capability in [
        "supports_dimming",
        "supports_color",
        "supports_temperature",
        "supports_position",
        "supports_volume",
        "supports_speed",
    ]:
        if capability in handler:
            capabilities[capability] = handler[capability]

    return capabilities


def get_all_controllable_domains() -> list[str]:
    """Get list of all controllable domains.

    Returns:
        List of domain names that support control actions
    """
    controllable = []
    for domain, handler in DOMAIN_HANDLERS.items():
        if handler.get("valid_actions") and not handler.get("read_only", False):
            controllable.append(domain)

    return sorted(controllable)


def get_all_sensor_domains() -> list[str]:
    """Get list of all sensor/read-only domains.

    Returns:
        List of domain names that provide data but don't support control
    """
    sensors = []
    for domain, handler in DOMAIN_HANDLERS.items():
        if handler.get("read_only", False) or handler.get("provides_data", False):
            sensors.append(domain)

    return sorted(sensors)


def validate_action_for_domain(domain: str, action: str) -> tuple[bool, str]:
    """Validate if an action is supported for a domain.

    Args:
        domain: Domain name
        action: Action to validate

    Returns:
        Tuple of (is_valid, error_message_if_invalid)
    """
    handler = DOMAIN_HANDLERS.get(domain, get_default_handler())
    valid_actions = handler.get("valid_actions", [])

    if not valid_actions:
        return False, f"Domain '{domain}' does not support any control actions"

    if action not in valid_actions:
        return (
            False,
            f"Action '{action}' not supported for domain '{domain}'. Valid actions: {', '.join(valid_actions)}",
        )

    return True, ""


def get_suggested_parameters(domain: str, action: str) -> list[str]:
    """Get suggested parameters for a domain/action combination.

    Args:
        domain: Domain name
        action: Action being performed

    Returns:
        List of suggested parameter names
    """
    handler = DOMAIN_HANDLERS.get(domain, get_default_handler())
    all_params = handler.get("parameters", [])

    # Action-specific parameter suggestions
    action_params = {
        "light": {
            "set": ["brightness", "color_temp", "rgb_color"],
            "on": ["brightness", "color_temp"],
            "adjust": ["brightness"],
        },
        "climate": {
            "set": ["temperature"],
            "heat": ["temperature"],
            "cool": ["temperature"],
        },
        "cover": {"set": ["position"], "open": [], "close": []},
        "media_player": {
            "set": ["volume_level"],
            "play": ["media_content_id", "media_content_type"],
        },
    }

    if domain in action_params and action in action_params[domain]:
        return action_params[domain][action]

    return list(all_params)


def is_security_sensitive_domain(domain: str) -> bool:
    """Check if a domain is security sensitive.

    Args:
        domain: Domain name to check

    Returns:
        True if domain requires extra security considerations
    """
    handler = DOMAIN_HANDLERS.get(domain, {})
    return bool(handler.get("security_sensitive", False))


def get_domain_description(domain: str) -> str:
    """Get human-readable description of a domain.

    Args:
        domain: Domain name

    Returns:
        Human-readable description
    """
    descriptions = {
        "light": "Lighting devices (bulbs, strips, switches)",
        "climate": "Climate control (thermostats, HVAC systems)",
        "cover": "Window coverings (blinds, curtains, garage doors)",
        "switch": "Simple on/off switches and outlets",
        "media_player": "Audio/video players and streaming devices",
        "fan": "Fans and ventilation devices",
        "vacuum": "Robotic vacuums and cleaning devices",
        "lock": "Smart locks and door controls",
        "alarm_control_panel": "Security system panels and alarms",
        "water_heater": "Water heating systems",
        "humidifier": "Humidity control devices",
        "camera": "Security cameras and video devices",
        "scene": "Predefined device state combinations",
        "script": "Custom automation scripts",
        "automation": "Automated rules and triggers",
        "sensor": "Environmental and status sensors",
        "binary_sensor": "On/off status sensors",
        "device_tracker": "Location tracking devices",
    }

    return descriptions.get(domain, f"Unknown domain: {domain}")
