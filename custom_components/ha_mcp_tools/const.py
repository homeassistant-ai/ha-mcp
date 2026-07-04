"""Constants for the HA-MCP custom component.

The integration serves two config-entry types under one domain
(:data:`DOMAIN`), discriminated by ``entry.data[CONF_ENTRY_TYPE]``:

* ``tools`` — the privileged file / YAML services (the original component).
  Pre-existing entries carry no ``entry_type`` key, so a missing value is
  treated as ``tools`` (no migration needed).
* ``server`` — the in-process ha-mcp FastMCP server (issue #1527), exposed
  through a Home Assistant webhook.

The two halves keep their constants in separate blocks below; the ``server``
block was folded in from the former standalone ``ha_mcp_server`` integration.
"""

import re

DOMAIN = "ha_mcp_tools"

# Config-entry discriminator (``entry.data[CONF_ENTRY_TYPE]``). A missing value
# means "tools" so the pre-existing services entry keeps working across the
# component update with no migration.
CONF_ENTRY_TYPE = "entry_type"
ENTRY_TYPE_TOOLS = "tools"
ENTRY_TYPE_SERVER = "server"

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
# In-process MCP server entry (issue #1527)
#
# Folded in from the former standalone ``ha_mcp_server`` integration. The
# "server" config-entry type runs the full ha-mcp FastMCP server in-process
# inside Home Assistant (a dedicated thread with its own asyncio loop) and
# exposes it remotely through a Home Assistant webhook, exactly like the
# webhook-proxy add-on. Creating the entry starts the server; disabling or
# removing the entry stops it. Everything below is namespaced under the shared
# ``DOMAIN`` (distinct hass.data sub-keys, distinct entry unique_id).
# ---------------------------------------------------------------------------

# The pinned ha-mcp release installed at runtime via
# homeassistant.requirements.async_process_requirements. Kept in lockstep with
# pyproject.toml's project.version. The options flow's advanced "pip requirement"
# field overrides this with any pip spec (e.g. a GitHub tarball URL) for
# pre-release testing.
PINNED_HA_MCP_VERSION = "7.9.0"

# PyPI distribution names. Stable ships as ``ha-mcp`` (pinned above); the dev
# channel ships as ``ha-mcp-dev`` — published on every master push, unpinned so
# the newest dev build resolves at install time. Both wheels contain the *same*
# ``ha_mcp`` import package (publish-dev.yml only renames the distribution), so
# only one may be installed at a time — see EmbeddedServerManager's channel-
# switch handling.
DIST_NAME_STABLE = "ha-mcp"
DIST_NAME_DEV = "ha-mcp-dev"

DEFAULT_PIP_SPEC = f"{DIST_NAME_STABLE}=={PINNED_HA_MCP_VERSION}"
DEV_PIP_SPEC = DIST_NAME_DEV

# Release channels (options-flow selector). ``stable`` installs the pinned
# DEFAULT_PIP_SPEC; ``dev`` installs the latest ha-mcp-dev, refreshed on every
# entry reload / HA restart. An explicit OPT_PIP_SPEC override wins over both.
CHANNEL_STABLE = "stable"
CHANNEL_DEV = "dev"
DEFAULT_CHANNEL = CHANNEL_STABLE

# Options-flow keys (stored in entry.options).
OPT_CHANNEL = "channel"
OPT_SERVER_PORT = "server_port"
OPT_BIND_HOST = "bind_host"
OPT_WEBHOOK_AUTH = "webhook_auth"
OPT_PIP_SPEC = "pip_spec"
OPT_SERVER_URL = "server_url"

# entry.data keys (persisted ids + secrets; entry.data is fine for secrets).
DATA_WEBHOOK_ID = "webhook_id"
DATA_SECRET_PATH = "secret_path"
DATA_SERVER_USER_ID = "server_user_id"
DATA_REFRESH_TOKEN_ID = "refresh_token_id"
DATA_ACCESS_TOKEN = "access_token"
# Last pip spec that was successfully installed. Lets a changed spec (the
# pre-release test channel) force an actual reinstall on the next start instead
# of hitting the requirements manager's is-installed shortcut.
DATA_LAST_PIP_SPEC = "last_pip_spec"

# hass.data[DOMAIN] sub-keys for the server runtime. Distinct from the tools
# entry's sub-keys ("caller_token" / "allowed_paths") so both entry types can
# share hass.data[DOMAIN] without collision.
DATA_MANAGER = "manager"
DATA_WEBHOOK = "webhook"
DATA_BRINGUP_TASK = "bringup_task"
# Snapshot of entry.options taken at setup so the update listener reloads only
# on a genuine options change — the background bring-up persists ids/token/pip
# spec to entry.data, and those writes must not trigger a self-reload.
DATA_LAST_OPTIONS = "last_options"

# Webhook auth modes (mirrors the webhook-proxy add-on's default posture).
WEBHOOK_AUTH_NONE = "none"  # secret webhook URL is the shared secret (default)
WEBHOOK_AUTH_HA = "ha_auth"  # HA-native bearer (HA core is the OAuth AS)

# Default bind host + port. 9584 (not the add-on's 9583) so this in-process
# server and an add-on install can coexist on the same box.
DEFAULT_SERVER_PORT = 9584
DEFAULT_BIND_HOST = "127.0.0.1"
BIND_HOST_ALL = "0.0.0.0"

# Loopback base URL the server uses to reach HA core (REST + WS).
DEFAULT_LOOPBACK_URL = "http://127.0.0.1:8123"

# Persistent data dir for the in-process server, under the HA config dir so it
# survives restarts and is isolated from an add-on's /data. Generic ".ha_mcp"
# to match the merged integration's naming (unreleased server entry, so no
# migration from the former ".ha_mcp_server").
SERVER_CONFIG_SUBDIR = ".ha_mcp"

# Client name recorded on the provisioned long-lived access token, and the name
# of the local admin user the server logs in as. Stable so a reused token is
# recognizable in Settings -> People -> <user> -> tokens. "HA-MCP" phrasing (not
# "Home Assistant MCP Server") to avoid confusion with HA's official MCP Server
# integration.
SERVER_TOKEN_CLIENT_NAME = "HA-MCP in-process server"
SERVER_USER_NAME = "HA-MCP in-process server"

# RFC 8414 / RFC 9728 discovery documents for ha_auth mode are served under this
# namespace (mirrors the webhook-proxy add-on's /api/mcp_proxy/oauth base).
OAUTH_BASE = "/api/ha_mcp_tools/oauth"

# Repair-issue ids surfaced when server bring-up fails.
ISSUE_PACKAGE_FAILED = "server_package_install_failed"
ISSUE_START_FAILED = "server_start_failed"
