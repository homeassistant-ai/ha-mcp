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
            "color_temp_kelvin",
            "rgb_color",
            "effect",
            "hs_color",
        ],
        "quick_actions": ["toggle", "dim", "brighten"],
        "state_attributes": [
            "brightness",
            "color_temp_kelvin",
            "min_color_temp_kelvin",
            "max_color_temp_kelvin",
            "rgb_color",
        ],
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
        # HA removed the legacy `speed` param / `fan.set_speed` service in the
        # 2021-2022 percentage migration (absent in 2026.6). Use percentage
        # (fan.set_percentage) and preset_mode (fan.set_preset_mode).
        "parameters": ["percentage", "preset_mode", "direction"],
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
        # in_zones (all containing zones, smallest first) and tracking_type
        # ("connection"/"location") were added in HA 2026.7; latitude/longitude
        # may be absent when a person/tracker is located by a presence scanner.
        "state_attributes": [
            "state",
            "latitude",
            "longitude",
            "in_zones",
            "tracking_type",
        ],
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

    domain = entity_id.split(".", maxsplit=1)[0]
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
