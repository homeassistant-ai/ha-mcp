"""Constants for the Home Assistant MCP Server integration (issue #1527).

This integration runs the full ha-mcp FastMCP server in-process inside Home
Assistant (a dedicated thread with its own asyncio loop) and exposes it remotely
through a Home Assistant webhook, exactly like the webhook-proxy add-on. Creating
the config entry starts the server; disabling or removing the entry stops it.

It is a separate install method that lives alongside the optional
``ha_mcp_tools`` component (which provides the privileged file/YAML services the
server's file tools use) — the two are independent integrations.
"""

DOMAIN = "ha_mcp_server"

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

# hass.data[DOMAIN] sub-keys for the runtime.
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

# Persistent data dir for the server, under the HA config dir so it survives
# restarts and is isolated from an add-on's /data.
SERVER_CONFIG_SUBDIR = ".ha_mcp_server"

# Client name recorded on the provisioned long-lived access token. Stable so a
# reused token is recognizable in Settings -> People -> <user> -> tokens.
SERVER_TOKEN_CLIENT_NAME = "Home Assistant MCP Server"
SERVER_USER_NAME = "Home Assistant MCP Server"

# RFC 8414 / RFC 9728 discovery documents for ha_auth mode are served under this
# namespace (mirrors the webhook-proxy add-on's /api/mcp_proxy/oauth base).
OAUTH_BASE = "/api/ha_mcp_server/oauth"

# Repair-issue ids surfaced when server bring-up fails.
ISSUE_PACKAGE_FAILED = "server_package_install_failed"
ISSUE_START_FAILED = "server_start_failed"
