"""Constants for HA MCP Tools integration."""

DOMAIN = "ha_mcp_tools"

# Allowed directories for file operations (relative to config dir)
ALLOWED_READ_DIRS = ["www", "themes", "custom_templates"]
ALLOWED_WRITE_DIRS = ["www", "themes", "custom_templates"]

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
