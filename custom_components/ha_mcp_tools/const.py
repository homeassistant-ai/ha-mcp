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

# Component version: the server version this release shares, stamped together
# with ``manifest.json``'s ``version`` and its ``ha-mcp`` pin by semantic-release
# (``version_variables`` in pyproject.toml). ``ha_mcp_tools/info`` reports it so
# the server can display/debug the running component build. Capability
# negotiation — not this version — gates each WS command (see
# ``websocket_api.constants.CAPABILITIES``).
COMPONENT_VERSION = "8.6.0"

# Config-entry discriminator (``entry.data[CONF_ENTRY_TYPE]``). A missing value
# means "tools" so the pre-existing services entry keeps working across the
# component update with no migration.
CONF_ENTRY_TYPE = "entry_type"
ENTRY_TYPE_TOOLS = "tools"
ENTRY_TYPE_SERVER = "server"

# Titles shown for each entry in the integration tile's entry list. Public so
# __init__'s setup migration can retitle pre-#1853 tools entries still
# carrying the legacy default (a user-customized title is left alone).
TOOLS_ENTRY_TITLE = "HA-MCP File & YAML Tools"
TOOLS_ENTRY_LEGACY_TITLE = "HA MCP Tools"
SERVER_ENTRY_TITLE = "HA-MCP Server"
# Kept level with the HACS floor in hacs.json: from component 2.1.3 the manifest
# declares voluptuous-openapi, which Core releases before 2026.7 pin to an older
# version so the requirement cannot resolve there, and
# that requirement gates the whole integration before any entry's config flow
# runs, so a lower runtime floor here would promise what the load cannot keep.
MIN_EMBEDDED_HOME_ASSISTANT_VERSION = "2026.8.0"

# Allowed directories for file operations (relative to config dir).
# "blueprints" is read-only BY DEFAULT — in ALLOWED_READ_DIRS but not
# ALLOWED_WRITE_DIRS, so ha_write_file / ha_delete_file reject it (raw blueprint
# reads are safe: community YAML, no secrets — issue #1965). This is the default
# allowlist, not an absolute guarantee: an admin who adds "blueprints" as a
# custom extra directory (issue #1567, see _current_extra_dirs) grants it
# read+write, since extra_dirs are honored on the write path too. Blueprint
# writes should instead go through ha_manage_blueprints(action="import") (which
# invokes the blueprint/save WS command internally). Prefer
# ha_manage_blueprints(action="get") for the parsed body; raw read is the escape
# hatch for the exact on-disk text.
ALLOWED_READ_DIRS = ["www", "themes", "custom_templates", "dashboards", "blueprints"]
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

# Basenames the floor denies wherever they appear. Despite the historical
# name, this set is enforced on every read, write and delete, and the file
# lister drops an entry carrying one of these names from its results, so a
# name here is neither openable nor enumerable through the component.
#
# secrets.yaml is reachable ONLY as the canonical config-root file, where the
# read handler masks its values. Any OTHER secrets.yaml surfaced via a custom
# dir would be returned UNMASKED (masking keys off the literal root path), so
# the floor blocks the basename everywhere except that one canonical location.
#
# approval_pin.json holds the digest of the PIN that authorises an approve or
# deny arriving on the event bus (issue #2502). It lives in ha-mcp's own data
# directory, which on an embedded install sits under the configuration
# directory, and an extra file path covering that directory grants read AND
# write: a tool could copy the digest to attack it offline, or simply replace
# it with the digest of a PIN of its own and decide its own approvals. Denying
# the basename leaves the rest of the data directory reachable, which is what
# the rest of it is for.
DENY_READ_BASENAMES = frozenset({"secrets.yaml", "approval_pin.json"})

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
# Also allows <packages-folder>/*.yaml via pattern matching, where the folder is
# the one the user binds under ``homeassistant: packages:`` (default "packages",
# detected at runtime — see _detect_package_dirs), plus themes/*.yaml.

# Top-level YAML keys allowed for editing in any allowed file
# (configuration.yaml or packages/*.yaml).
# The bar is "YAML is a legitimate way to manage this key", not "this key
# has no UI alternative": template, utility_meter and group do have helper
# equivalents and stay allowed for git-managed YAML configs (the caller
# attaches a routing warning instead – see _HELPER_EQUIVALENT_KEYS in
# src/ha_mcp/tools/tools_yaml_config.py).
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
        # recorder is YAML-only (no UI or storage-mode helper): purge_keep_days,
        # include/exclude, commit_interval. Its surface is smaller than keys
        # already here — it only controls what HA records and for how long, with
        # no code-execution path like command_line/shell_command/rest (#1852).
        "recorder",
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

# Top-level YAML keys an operator can never unlock (#1887).
# The operator-configurable extra-key list (ha-mcp's "extra YAML write
# keys" setting) is additive on top of ALLOWED_YAML_KEYS, so this floor
# is what keeps that setting from reaching HA's own trust boundary. It is
# checked before every single-key allowlist branch and is deliberately NOT
# operator-extendable – otherwise the same trust question just reopens
# one level up. Scope note: it guards the per-key merge path only.
# ``action="replace_file"`` returns before key validation runs at all, so a
# whole-file rewrite of configuration.yaml can still contain these keys –
# pre-existing behaviour, and the reason this is a floor under the extra-key
# setting rather than a general "these keys are unwritable" guarantee.
#
# The bar is not "powerful": command_line, shell_command and rest are
# already allowed above, so command execution and outbound HTTP are
# accepted surface. The bar is "redefines authentication, escalates the
# write surface itself, or can lock the user out" – unrecoverable in a
# way a broken sensor is not. Verified against home-assistant/core:
#   homeassistant: CORE_CONFIG_SCHEMA (homeassistant/core_config.py) takes
#     auth_providers / auth_mfa_modules (how the instance authenticates)
#     and packages (which folder is loaded as packages – a write here
#     would redirect the very surface this feature is bounded by).
#   http: takes trusted_proxies + use_x_forwarded_for (a spoofable
#     X-Forwarded-For becomes an auth bypass), cors_allowed_origins, and
#     ip_ban_enabled / login_attempts_threshold (brute-force protection).
#   frontend: takes extra_module_url, JavaScript modules loaded into the
#     authenticated dashboard – a stored-XSS foothold with access to the
#     instance and its tokens.
#   lovelace: takes resources (url + type: module), loaded whenever
#     resource_mode resolves to yaml. That is the same JS-into-an-
#     authenticated-dashboard primitive as frontend: extra_module_url, so
#     denying one while allowing the other would be a floor contradicting
#     its own rationale. Only the bare key is denied; the validated
#     lovelace.dashboards.<url_path> shape is a different branch and stays
#     available for YAML-mode dashboard management.
# auth and api are absent on purpose: both have an empty CONFIG_SCHEMA in
# core, so there is no sub-key to restrict.
YAML_KEY_DENYLIST = frozenset(
    {
        "homeassistant",
        "http",
        "frontend",
        "lovelace",
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
# HA-MCP Server entry (issue #1527)
#
# Folded in from the former standalone ``ha_mcp_server`` integration. The
# "server" config-entry type runs the full ha-mcp FastMCP server in-process
# inside Home Assistant (a dedicated thread with its own asyncio loop) and
# exposes it remotely through a Home Assistant webhook, exactly like the
# webhook-proxy add-on. Creating the entry starts the server; disabling or
# removing the entry stops it. Everything below is namespaced under the shared
# ``DOMAIN`` (distinct hass.data sub-keys, distinct entry unique_id).
# ---------------------------------------------------------------------------

# PyPI distribution names. The server ships as ``ha-mcp``; master builds are
# also published as ``ha-mcp-dev``. Both wheels contain the *same* ``ha_mcp``
# import package (publish-dev.yml only renames the distribution), so only one
# may be installed at a time — see EmbeddedServerManager's conflicting-dist
# handling.
DIST_NAME_STABLE = "ha-mcp"
DIST_NAME_DEV = "ha-mcp-dev"
KNOWN_SERVER_DISTS = (DIST_NAME_STABLE, DIST_NAME_DEV)

# The server requirement when the component manifest carries no ``ha-mcp``
# pin, which only a hand-edited manifest lacks: semantic-release stamps every
# release's pin and scripts/stamp_component_version.py every pre-release's.
# Typing it into the options flow's pip-spec field means "no override".
DEFAULT_PIP_SPEC = DIST_NAME_STABLE

# Release channels the server used to be selected by (``channel`` option).
# The paired manifest pin replaced them; kept only so ha_mcp_tools/
# server_entry_update still accepts what older servers send.
CHANNEL_STABLE = "stable"
CHANNEL_DEV = "dev"

# Options-flow keys (stored in entry.options).
# Retired: entries saved before the paired-release change may still carry
# ``channel`` and ``auto_update``. Nothing acts on them; only the server_entry
# / server_entry_update WS commands still echo ``channel`` for older servers.
OPT_CHANNEL = "channel"
OPT_SERVER_PORT = "server_port"
OPT_BIND_HOST = "bind_host"
OPT_WEBHOOK_AUTH = "webhook_auth"
# Legacy OAuth mode (self-hosted authorization server, static client_id/secret
# for Google Gemini Spark) credential management — mirrors the
# OPT_WEBHOOK_ID_OVERRIDE / OPT_REGENERATE_SECRETS shape below. Empty override
# fields mean "keep the current value"; OPT_OAUTH_REGENERATE is one-shot.
# _override suffix distinguishes these OPTIONS keys from the DATA_OAUTH_*
# entry.data keys (which store the resolved values under the un-suffixed
# names) — mirrors OPT_WEBHOOK_ID_OVERRIDE vs DATA_WEBHOOK_ID.
OPT_OAUTH_CLIENT_ID = "oauth_client_id_override"
OPT_OAUTH_CLIENT_SECRET = "oauth_client_secret_override"
OPT_OAUTH_REGENERATE = "oauth_regenerate"
OPT_PIP_SPEC = "pip_spec"
OPT_SERVER_URL = "server_url"
# Connect-URL surface + secret management (owner request, parity with the
# webhook-proxy app's external-URL option and the add-on's secret-path
# override). All optional; empty string = automatic/keep-current.
OPT_EXTERNAL_URL = "external_url"
OPT_WEBHOOK_ID_OVERRIDE = "webhook_id_override"
OPT_SECRET_PATH_OVERRIDE = "secret_path_override"
OPT_REGENERATE_SECRETS = "regenerate_secrets"
# Local-only mode (owner request): when False, the HA webhook is never
# registered, so nothing - including Nabu Casa remote UI - can reach the
# server through Home Assistant; only the direct server port (+ the
# admin-only sidebar panel, which proxies over loopback) remains.
OPT_ENABLE_WEBHOOK = "enable_webhook"
# Conversation-agent LLM API (#1745): when False, the toolset is not
# registered as a Home Assistant LLM API, so it never appears in any
# conversation agent's "Control Home Assistant" selector. On by default —
# registering the API only makes it selectable; nothing is exposed until a
# user picks it on an agent.
OPT_ENABLE_LLM_API = "enable_llm_api"
DEFAULT_ENABLE_LLM_API = True
# Which exposure shape(s) the LLM API offers to conversation agents:
# ``tool_search`` (default) registers a compact API — pinned tools plus
# search/execute meta-tools — the shape context-limited models need; ``full``
# registers the whole exposed catalog as one API; ``both`` registers the two
# side by side so the choice is made per agent in HA's own selector.
OPT_LLM_API_EXPOSURE = "llm_api_exposure"
EXPOSURE_TOOL_SEARCH = "tool_search"
EXPOSURE_FULL = "full"
EXPOSURE_BOTH = "both"
DEFAULT_LLM_API_EXPOSURE = EXPOSURE_TOOL_SEARCH
# When False, the persistent notification created on every server bring-up is
# suppressed; the connect URLs stay on the entry's Configure screen.
OPT_ENABLE_STARTUP_NOTIFICATION = "enable_startup_notification"
# When False, the admin-only "HA-MCP" sidebar settings panel is not registered;
# the server's options stay reachable on the entry's Configure screen.
OPT_ENABLE_SIDEBAR_PANEL = "enable_sidebar_panel"
# One-shot: an administrator's long-lived access token to switch the server
# to (#2427). Validated by the options flow, moved into entry.data on the next
# setup (embedded_entry._ensure_secrets) and cleared; never shown back.
OPT_ADMIN_TOKEN_REPLACEMENT = "admin_token_replacement"
# The callback URLs the none-mode auto-approve /authorize may redirect to
# (#2427). Absent = DEFAULT_OAUTH_REDIRECT_ALLOWLIST; a saved list, even an
# empty one, replaces it. Read per request, so a change needs no reload.
OPT_OAUTH_REDIRECT_ALLOWLIST = "oauth_redirect_allowlist"
DEFAULT_OAUTH_REDIRECT_ALLOWLIST: tuple[str, ...] = (
    "https://claude.ai/api/mcp/auth_callback",
)
# Bounds every writer of the list applies (Configure, the panel's WS command).
MAX_OAUTH_CALLBACKS = 50
MAX_OAUTH_CALLBACK_LENGTH = 2048

# entry.data keys (persisted ids + secrets; entry.data is fine for secrets).
DATA_WEBHOOK_ID = "webhook_id"
DATA_SECRET_PATH = "secret_path"
# Set by the package repair's fix flow: the next start reinstalls the server
# package even though its version is satisfied, before anything imports it. A
# dependency another integration downgraded only resolves again on a
# reinstall, and the broken module stays loaded until Home Assistant restarts.
DATA_REINSTALL_REQUESTED = "reinstall_requested"
# Legacy OAuth mode credentials, minted by embedded_entry._ensure_secrets and
# consumed by oauth_legacy.LegacyOAuthProvider. DATA_OAUTH_SIGNING_KEY is a hex
# string (entry.data must be JSON-serializable, so raw bytes aren't stored
# directly) — the provider converts it with bytes.fromhex(). The signed token
# payload carries the client_id (not the secret), so rotating the client_id
# revokes every outstanding token at the restart that rebinds the views (see
# LegacyOAuthProvider._validate_token).
# Because validation never involves the client_secret, a secret-only override
# change instead rotates the signing key, evicting outstanding tokens at the
# restart that activates the new credentials (see
# embedded_entry._ensure_legacy_oauth_secrets). Until that restart the bound
# views keep serving the OLD identity, so the startup log withholds rotated
# credentials (embedded_setup._surface_connect_urls).
DATA_OAUTH_CLIENT_ID = "oauth_client_id"
DATA_OAUTH_CLIENT_SECRET = "oauth_client_secret"
DATA_OAUTH_SIGNING_KEY = "oauth_signing_key"
# Hex-encoded per-entry HMAC key signing stateless DCR client_ids (RFC 7591
# compat endpoint). Generated at setup when absent, so entries created before
# 2.0.0 gain one on their first reload after upgrade.
DATA_DCR_SIGNING_KEY = "dcr_signing_key"
DATA_SERVER_USER_ID = "server_user_id"
DATA_REFRESH_TOKEN_ID = "refresh_token_id"
# A long-lived access token of an administrator, supplied by the user (#2427).
# The server runs with it; entries from older releases may instead carry the
# provisioned DATA_SERVER_USER_ID / DATA_REFRESH_TOKEN_ID pair.
DATA_ADMIN_TOKEN = "admin_token"
DATA_ACCESS_TOKEN = "access_token"
# Last pip spec that was successfully installed. Lets a changed spec (a
# pip-spec override) force an actual reinstall on the next start instead
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
# Unregister callback for the conversation-agent LLM API (#1745), stored by
# the bring-up success path and invoked (idempotently) by teardown.
DATA_LLM_API_UNSUB = "llm_api_unsub"

# Webhook auth modes (mirrors the webhook-proxy add-on's default posture).
WEBHOOK_AUTH_NONE = "none"  # secret webhook URL is the shared secret (default)
WEBHOOK_AUTH_HA = "ha_auth"  # HA-native bearer (HA core is the OAuth AS)
# Self-hosted OAuth 2.1 authorization server with a static client_id/secret,
# ported from the webhook-proxy add-on's "legacy" mode. Originally needed
# because HA core's /auth/authorize did not fetch Client ID Metadata Documents
# for cross-origin redirect_uris before 2026.9 (home-assistant/core#176282,
# fixed by #176286), which Google Gemini Spark's custom connected apps require;
# kept as the pasted-credential fallback for clients that want one.
WEBHOOK_AUTH_LEGACY = "legacy"

# Default bind host + port. 9584 (not the add-on's 9583) so this in-process
# server and an add-on install can coexist on the same box.
DEFAULT_SERVER_PORT = 9584
# Fallback for entries created before setup asked for network access: they
# kept the add-on's LAN-reachable port. New entries save loopback explicitly
# (#2427).
DEFAULT_BIND_HOST = "0.0.0.0"
BIND_HOST_ALL = "0.0.0.0"
BIND_HOST_LOOPBACK = "127.0.0.1"

# Loopback base URL the server uses to reach HA core (REST + WS).
DEFAULT_LOOPBACK_URL = "http://127.0.0.1:8123"

# Persistent data dir for the in-process server, under the HA config dir so it
# survives restarts and is isolated from an add-on's /data. Generic ".ha_mcp"
# to match the merged integration's naming (unreleased server entry, so no
# migration from the former ".ha_mcp_server").
SERVER_CONFIG_SUBDIR = ".ha_mcp"

# RFC 8414 / RFC 9728 discovery documents for ha_auth mode are served under this
# namespace (mirrors the webhook-proxy add-on's /api/mcp_proxy/oauth base).
OAUTH_BASE = "/api/ha_mcp_tools/oauth"

# Docs section explaining how the server is updated (it arrives with each
# component release; the pip-spec override is the testing escape hatch),
# linked as learn_more_url from the component-outdated repair issue. The
# anchor is the GitHub slug of the "Server updates" heading in
# docs/in-process-server.md; hassfest forbids literal URLs inside strings.json.
SERVER_UPDATES_DOCS_URL = (
    "https://github.com/homeassistant-ai/ha-mcp/blob/master/docs/"
    "in-process-server.md#server-updates"
)

# Usage guide for the conversation-agent LLM API option (#1745). Injected into
# the options form as a description placeholder — hassfest forbids literal
# URLs inside strings.json.
LLM_API_DOCS_URL = (
    "https://github.com/homeassistant-ai/ha-mcp/blob/master/docs/"
    "in-process-server.md"
    "#chat-with-the-toolset-from-home-assistant-conversation-agents--voice"
)

# Repair-issue ids surfaced when server bring-up fails.
ISSUE_PACKAGE_FAILED = "server_package_install_failed"
ISSUE_START_FAILED = "server_start_failed"
# Fixable repair issue: the server has no usable Home Assistant credential
# (#2427). Its fix flow asks for an administrator's long-lived access token.
ISSUE_TOKEN_NEEDED = "server_token_needed"
# Repair issue surfaced when the installed ha-mcp server requires a newer
# custom component than the one running. Each component release pins the
# server it was built with, so this only fires for a pip-spec override that
# installs a newer server; it points the user at the HACS component update
# (non-blocking).
ISSUE_COMPONENT_OUTDATED = "component_outdated"
# Repair issue surfaced when the legacy OAuth mode's root /authorize + /token
# views are out of sync with the CONFIGURED webhook_auth mode — either just
# enabled (views not yet bound with the current credentials) or just disabled
# (views still bound and serving the old identity). aiohttp can neither bind
# nor unbind an HTTP view without a full Home Assistant restart, so both
# transitions need one; see oauth_legacy.bind_legacy_views.
ISSUE_LEGACY_OAUTH_RESTART = "legacy_oauth_restart"

# How long the in-process server's listener keeps an idle keep-alive connection
# (uvicorn's default). The webhook relay drops its pooled connections sooner, or
# a request sent as the listener closes one fails with a reset (mcp_webhook).
SERVER_KEEPALIVE_SECONDS = 5
