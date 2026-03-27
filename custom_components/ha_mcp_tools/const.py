"""Constants for HA MCP Tools integration."""

DOMAIN = "ha_mcp_tools"

# Allowed directories for file operations (relative to config dir)
ALLOWED_READ_DIRS = ["www", "themes", "custom_templates"]
ALLOWED_WRITE_DIRS = ["www", "themes", "custom_templates"]

# Files allowed for managed YAML editing
ALLOWED_YAML_CONFIG_FILES = ["configuration.yaml"]
# Also allows packages/*.yaml via pattern matching

# Top-level YAML keys allowed for editing.
# These are YAML-only features that typically lack a full UI/API path.
ALLOWED_YAML_KEYS = frozenset(
    {
        "template",
        "sensor",
        "binary_sensor",
        "input_boolean",
        "input_number",
        "input_text",
        "input_select",
        "input_datetime",
        "input_button",
        "counter",
        "timer",
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
        "schedule",
    }
)

# Top-level keys explicitly blocked from editing — core HA config.
BLOCKED_YAML_KEYS = frozenset(
    {
        "homeassistant",
        "default_config",
        "http",
        "api",
        "auth",
        "cloud",
        "frontend",
        "recorder",
        "logger",
        "history",
        "logbook",
        "system_log",
        "automation",
        "script",
        "scene",
    }
)
