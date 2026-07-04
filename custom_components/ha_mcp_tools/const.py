"""Constants for HA MCP Tools integration."""

import re

DOMAIN = "ha_mcp_tools"

# Allowed directories for file operations (relative to config dir)
ALLOWED_READ_DIRS = ["www", "themes", "custom_templates", "dashboards"]
ALLOWED_WRITE_DIRS = ["www", "themes", "custom_templates", "dashboards"]

# NON-OVERRIDABLE deny floor for the user-configurable extra read/write
# directories (issue #1567). The custom allowlist is applied ON TOP of the
# built-in ALLOWED_*_DIRS, but a custom directory can NEVER grant access to
# these. The floor is re-checked before any allow decision on every read,
# write, list, and delete, so neither a stored entry nor an in-flight one can
# punch through it.
#
# .storage holds HA's auth database (refresh/access tokens), hashed passwords,
# and every integration's cleartext credentials (core.config_entries,
# application_credentials, cloud) — including this component's OWN caller
# token (.storage/ha_mcp_tools_auth). Letting a custom dir reach it would both
# leak secrets and hand out the key to this component's own auth gate.
DENY_PATH_SEGMENTS = frozenset({".storage"})

# secrets.yaml is reachable ONLY as the canonical config-root file, where the
# read handler masks its values. Any OTHER secrets.yaml surfaced via a custom
# dir would be returned UNMASKED (masking keys off the literal root path), so
# the floor blocks the basename everywhere except that one canonical location.
DENY_READ_BASENAMES = frozenset({"secrets.yaml"})

# HAOS sibling-volume mounts (issue #1586). These live OUTSIDE the config dir,
# so the config-relative custom-directory allowlist (issue #1567) cannot reach
# them — its normalizer rejects every absolute path. A user may instead add one
# of these fixed absolute roots — or a subdirectory of one — to the custom
# directory list; access is then enforced against the volume root exactly as a
# config-relative entry is enforced against the config dir (issue #1586).
#
# The component runs inside HA Core, so a volume is reachable only if the HA
# Core container actually mounts it (the standard HAOS/Supervised mounts are
# config/share/media/ssl/backup). An unmounted or non-existent root simply
# yields a "not found" at use time — adding it is harmless. As with the
# config-relative list, a configured volume grants BOTH read and write, and the
# non-overridable deny floor (.storage / secrets.yaml) still applies.
ALLOWED_VOLUME_ROOTS = ("/share", "/media", "/ssl", "/backup")

# Files allowed for managed YAML editing
ALLOWED_YAML_CONFIG_FILES = ["configuration.yaml"]
# Also allows packages/*.yaml via pattern matching

# Top-level YAML keys allowed for editing in any allowed file
# (configuration.yaml or packages/*.yaml).
# ONLY keys that have no UI/API alternative belong here.
# Keys manageable via ha_config_set_helper (input_*, counter, timer, schedule)
# are intentionally excluded. automation/script/scene live in
# PACKAGES_ONLY_YAML_KEYS below — they have storage-mode equivalents
# (ha_config_set_automation/script/scene) but are still exposed in
# packages/*.yaml for the YAML-packages workflow.
ALLOWED_YAML_KEYS = frozenset(
    {
        "template",
        "sensor",
        "binary_sensor",
        "command_line",
        "rest",
        "knx",
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

# Top-level YAML keys allowed ONLY inside packages/*.yaml files, never in
# configuration.yaml. Storage-mode UI/API equivalents already exist
# (ha_config_set_automation/script/scene), so these are exposed here only
# for the YAML-packages workflow used by git-managed configs — where users
# expect to keep automations/scripts/scenes alongside templates and other
# YAML-defined items. Writes to configuration.yaml for these keys remain
# rejected so storage-mode and YAML-mode collections don't collide.
PACKAGES_ONLY_YAML_KEYS = frozenset(
    {
        "automation",
        "script",
        "scene",
    }
)

# Post-edit action required for each YAML key.
# template, mqtt, group, automation, script, and scene have first-party
# reload services in HA core. All others require a full HA restart.
# ``TestPostActionTableContract`` pins the in-repo shape; the HA-core
# side of the contract is a write-time snapshot, not a continuous check.
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
    "automation": {
        "post_action": "reload_available",
        "reload_service": "automation.reload",
    },
    "script": {
        "post_action": "reload_available",
        "reload_service": "script.reload",
    },
    "scene": {
        "post_action": "reload_available",
        "reload_service": "scene.reload",
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

# ---------------------------------------------------------------------------
# In-process embedded MCP server (issue #1527)
# ---------------------------------------------------------------------------
#
# Opt-in: the component can run the full ha-mcp FastMCP server inside the Home
# Assistant process (a dedicated thread with its own asyncio loop) and expose it
# remotely through an HA webhook, exactly like the webhook-proxy add-on. None of
# this activates unless the user enables it in the options flow, so upgrading the
# component is a no-op for existing installs.

# The pinned ha-mcp release installed at runtime via
# homeassistant.requirements.async_process_requirements. Kept in lockstep with
# pyproject.toml's project.version. The options flow's advanced "pip requirement"
# field overrides this with any pip spec (e.g. a GitHub tarball URL) for
# pre-release testing.
PINNED_HA_MCP_VERSION = "7.9.0"
DEFAULT_PIP_SPEC = f"ha-mcp=={PINNED_HA_MCP_VERSION}"

# Options-flow keys (stored in entry.options).
OPT_EMBEDDED_ENABLED = "embedded_server_enabled"
OPT_SERVER_PORT = "server_port"
OPT_BIND_HOST = "bind_host"
OPT_WEBHOOK_AUTH = "webhook_auth"
OPT_PIP_SPEC = "pip_spec"
OPT_SERVER_URL = "server_url"

# entry.data keys (persisted ids + secrets; entry.data is fine for secrets).
DATA_WEBHOOK_ID = "embedded_webhook_id"
DATA_SECRET_PATH = "embedded_secret_path"
DATA_SERVER_USER_ID = "embedded_server_user_id"
DATA_REFRESH_TOKEN_ID = "embedded_refresh_token_id"
DATA_ACCESS_TOKEN = "embedded_access_token"

# hass.data[DOMAIN] sub-keys for the embedded runtime.
DATA_EMBEDDED_MANAGER = "embedded_manager"
DATA_EMBEDDED_WEBHOOK = "embedded_webhook"

# Webhook auth modes (mirrors the webhook-proxy add-on's default posture).
WEBHOOK_AUTH_NONE = "none"  # secret webhook URL is the shared secret (default)
WEBHOOK_AUTH_HA = "ha_auth"  # HA-native bearer (HA core is the OAuth AS)

# Default bind host + port. 9584 (not the add-on's 9583) so an embedded server
# and an add-on install can coexist on the same box.
DEFAULT_SERVER_PORT = 9584
DEFAULT_BIND_HOST = "127.0.0.1"
BIND_HOST_ALL = "0.0.0.0"

# Loopback base URL the embedded server uses to reach HA core (REST + WS).
DEFAULT_LOOPBACK_URL = "http://127.0.0.1:8123"

# Persistent data dir for the embedded server, under the HA config dir so it
# survives restarts and is isolated from an add-on's /data.
EMBEDDED_CONFIG_SUBDIR = ".ha_mcp_embedded"

# Client name recorded on the provisioned long-lived access token. Stable so a
# reused token is recognizable in Settings -> People -> <user> -> tokens.
EMBEDDED_TOKEN_CLIENT_NAME = "HA MCP Embedded Server"
EMBEDDED_SERVER_USER_NAME = "HA MCP Embedded Server"

# RFC 8414 / RFC 9728 discovery documents for ha_auth mode are served under this
# namespace (mirrors the webhook-proxy add-on's /api/mcp_proxy/oauth base).
OAUTH_BASE = "/api/ha_mcp_tools/oauth"

# Repair-issue ids surfaced when embedded setup fails but services keep working.
ISSUE_EMBEDDED_PACKAGE_FAILED = "embedded_package_install_failed"
ISSUE_EMBEDDED_START_FAILED = "embedded_server_start_failed"
