"""Constants for HA MCP Tools integration."""

import re

DOMAIN = "ha_mcp_tools"

# Allowed directories for file operations (relative to config dir)
ALLOWED_READ_DIRS = ["www", "themes", "custom_templates", "dashboards"]
ALLOWED_WRITE_DIRS = ["www", "themes", "custom_templates", "dashboards"]

# Files allowed for managed YAML editing
ALLOWED_YAML_CONFIG_FILES = ["configuration.yaml"]
# Also allows packages/*.yaml via pattern matching

# Top-level YAML keys allowed for editing.
# ONLY keys that have no UI/API alternative belong here.
# Keys manageable via ha_config_set_helper (input_*, counter, timer, schedule)
# or ha_config_set_automation/script/scene are intentionally excluded.
ALLOWED_YAML_KEYS = frozenset(
    {
        "template",
        "sensor",
        "binary_sensor",
        "command_line",
        "rest",
        "mqtt",
        "shell_command",
        "switch",
        "light",
        "fan",
        "cover",
        "climate",
        "notify",
        "group",
        "utility_meter",
    }
)

# Post-edit action required for each YAML key.
# Only template, mqtt, and group have YAML reload services in HA core
# (verified against homeassistant/components/homeassistant/__init__.py,
# async_handle_reload_all). All others require a full HA restart.
YAML_KEY_POST_ACTIONS: dict[str, dict[str, str]] = {
    "template": {
        "post_action": "reload_available",
        "reload_service": "homeassistant.reload_custom_templates",
    },
    "mqtt": {
        "post_action": "reload_available",
        "reload_service": "mqtt.reload",
    },
    "group": {
        "post_action": "reload_available",
        "reload_service": "group.reload",
    },
}
# Default for keys not in YAML_KEY_POST_ACTIONS:
YAML_KEY_DEFAULT_POST_ACTION = {"post_action": "restart_required"}

# YAML-mode dashboard url_path validation (issue #1034).
# Pattern: lowercase letters/digits, hyphen-separated, must contain at least
# one hyphen (HA's lovelace dashboard rule). No leading/trailing/double hyphens.
DASHBOARD_URL_PATH_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+")

# url_paths reserved by HA core dashboards/routes — must not be registered as
# YAML-mode dashboards or they will shadow / collide with built-ins.
RESERVED_DASHBOARD_URL_PATHS = frozenset(
    {
        "lovelace",
        "overview",
        "map",
        "logbook",
        "history",
        "energy",
        "developer-tools",
        "config",
        "profile",
        "media-browser",
        "todo",
        "calendar",
    }
)
