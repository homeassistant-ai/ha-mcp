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
from datetime import timedelta

DOMAIN = "ha_mcp_tools"

# Component version, kept in lockstep with ``manifest.json``'s ``version``.
# ``ha_mcp_tools/info`` reports this so the server can display/debug the running
# component build; ``TestManifestVersionParity`` pins the two together so a
# manifest bump that forgets this constant (or vice-versa) fails in CI. The
# capability negotiation — not this version — gates each WS command (see
# ``websocket_api.CAPABILITIES``).
COMPONENT_VERSION = "1.1.0"

# Config-entry discriminator (``entry.data[CONF_ENTRY_TYPE]``). A missing value
# means "tools" so the pre-existing services entry keeps working across the
# component update with no migration.
CONF_ENTRY_TYPE = "entry_type"
ENTRY_TYPE_TOOLS = "tools"
ENTRY_TYPE_SERVER = "server"
MIN_EMBEDDED_HOME_ASSISTANT_VERSION = "2026.6.0"

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
# Also allows <packages-folder>/*.yaml via pattern matching, where the folder is
# the one the user binds under ``homeassistant: packages:`` (default "packages",
# detected at runtime — see _detect_package_dirs), plus themes/*.yaml.

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

# PyPI distribution names. Stable ships as ``ha-mcp``; the dev channel ships as
# ``ha-mcp-dev`` — published on every master push. BOTH are installed unpinned,
# so every install / reload resolves the newest build of the selected channel
# (the component auto-updates the server rather than pinning a lockstep version
# — see ``UPDATE_CHECK_INTERVAL`` and
# ``EmbeddedServerManager._async_ensure_package``). Both wheels contain the
# *same* ``ha_mcp`` import package (publish-dev.yml only renames the
# distribution), so only one may be installed at a time — see
# EmbeddedServerManager's channel-switch handling.
DIST_NAME_STABLE = "ha-mcp"
DIST_NAME_DEV = "ha-mcp-dev"

# Default pip requirement for the stable channel: the unpinned ``ha-mcp``
# distribution, so each install resolves the newest stable release. The options
# flow's advanced "pip requirement" field overrides this with any pip spec
# (e.g. a version pin or a GitHub tarball URL) for pre-release testing — an
# explicit override also disables automatic updates.
DEFAULT_PIP_SPEC = DIST_NAME_STABLE
DEV_PIP_SPEC = DIST_NAME_DEV

# Release channels (options-flow selector). ``stable`` installs the unpinned
# ``ha-mcp`` and ``dev`` installs the unpinned ``ha-mcp-dev``; both refresh to
# the newest build of that channel on every entry reload / HA restart, and the
# periodic auto-update check reloads the entry when PyPI publishes a newer one.
# An explicit OPT_PIP_SPEC override wins over both and disables auto-update.
CHANNEL_STABLE = "stable"
CHANNEL_DEV = "dev"
DEFAULT_CHANNEL = CHANNEL_STABLE


def dist_for_channel(channel: str) -> str:
    """Map a release channel to its PyPI distribution name.

    The channel <-> distribution correspondence is used by the version
    coordinator, the auto-update notification, and the server manager's pip
    resolution — one shared mapping so a future third channel cannot be added
    to some sites and missed in others (review finding on #1760).
    """
    return DIST_NAME_DEV if channel == CHANNEL_DEV else DIST_NAME_STABLE


def channel_for_dist(dist: str) -> str:
    """Inverse of :func:`dist_for_channel`."""
    return CHANNEL_DEV if dist == DIST_NAME_DEV else CHANNEL_STABLE


# Interval of the ServerVersionCoordinator's PyPI poll (coordinator.py). The
# poll itself ALWAYS runs — it feeds the `update` platform entity, which must
# stay populated even when automatic updates are off (issue #1760). Whether a
# newer build actually triggers a reload/reinstall is decided separately, per
# refresh, in embedded_setup.async_maybe_auto_update (gated on OPT_AUTO_UPDATE
# and on no pip-spec override). Only an explicit pip-spec override skips the
# PyPI fetch — comparing PyPI-latest against an arbitrary pip spec is
# meaningless.
UPDATE_CHECK_INTERVAL = timedelta(hours=6)

# PyPI JSON API for the latest published version of a distribution. ``{dist}``
# is DIST_NAME_STABLE or DIST_NAME_DEV depending on the selected channel.
PYPI_JSON_URL = "https://pypi.org/pypi/{dist}/json"

# The component manifest as it existed at a server release's git tag. Its
# ``version`` is the component version that SHIPPED with that server build, so
# a value newer than the running component means the release changed the
# component too — the pre-install auto-update gate in embedded_setup holds the
# server update until HACS delivers the component (issues #1783/#1785).
# Tag-timing caveat: stable ``vX.Y.Z`` tags exist before the PyPI publish
# (semantic-release pushes the tag first), but a dev ``vX.Y.Z.devN`` tag is
# only created when its draft GitHub release is published — AFTER the binary
# builds, minutes after PyPI already has the version. During that dev window
# this URL 404s and the gate deliberately fails open (the registry's
# skip-on-failure is the backstop on that channel).
COMPONENT_MANIFEST_AT_TAG_URL = (
    "https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/"
    "v{version}/custom_components/ha_mcp_tools/manifest.json"
)

# Options-flow keys (stored in entry.options).
OPT_CHANNEL = "channel"
# Automatic server-version updates toggle (default on). When on, the channel is
# unpinned and auto-updates (force-install on reload/restart + a reload when the
# periodic check sees a newer build). When off, the server stays on the version
# currently installed: _resolve_pip_spec pins the channel's dist to that version
# — but the periodic PyPI check KEEPS running so the update entity still shows
# newer builds; its Install button is the manual path (issue #1760). Governs the
# ha-mcp server package only — component updates still come through HACS. An
# explicit OPT_PIP_SPEC override wins over both and skips the check entirely.
OPT_AUTO_UPDATE = "auto_update"
DEFAULT_AUTO_UPDATE = True
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
# suppressed; the connect URLs still reach the admin-only Home Assistant log.
OPT_ENABLE_STARTUP_NOTIFICATION = "enable_startup_notification"
# When False, the admin-only "HA-MCP" sidebar settings panel is not registered;
# the server's options stay reachable on the entry's Configure screen.
OPT_ENABLE_SIDEBAR_PANEL = "enable_sidebar_panel"

# entry.data keys (persisted ids + secrets; entry.data is fine for secrets).
DATA_WEBHOOK_ID = "webhook_id"
DATA_SECRET_PATH = "secret_path"
# Legacy OAuth mode credentials, minted by embedded_entry._ensure_secrets and
# consumed by oauth_legacy.LegacyOAuthProvider. DATA_OAUTH_SIGNING_KEY is a hex
# string (entry.data must be JSON-serializable, so raw bytes aren't stored
# directly) — the provider converts it with bytes.fromhex(). The signed token
# payload carries the client_id (not the secret), so rotating the client_id
# revokes every outstanding token (see LegacyOAuthProvider._validate_token).
# Because validation never involves the client_secret, a secret-only override
# change instead rotates the signing key, evicting outstanding tokens at the
# restart that activates the new credentials (see
# embedded_entry._ensure_legacy_oauth_secrets). Until that restart the bound
# views keep serving the OLD identity, so the startup log withholds rotated
# credentials (embedded_setup._surface_connect_urls).
DATA_OAUTH_CLIENT_ID = "oauth_client_id"
DATA_OAUTH_CLIENT_SECRET = "oauth_client_secret"
DATA_OAUTH_SIGNING_KEY = "oauth_signing_key"
DATA_SERVER_USER_ID = "server_user_id"
DATA_REFRESH_TOKEN_ID = "refresh_token_id"
DATA_ACCESS_TOKEN = "access_token"
# Last pip spec that was successfully installed. Lets a changed spec (the
# pre-release test channel) force an actual reinstall on the next start instead
# of hitting the requirements manager's is-installed shortcut.
DATA_LAST_PIP_SPEC = "last_pip_spec"
# One-shot marker set by the update entity's Install button (issue #1760):
# with auto-update off, EmbeddedServerManager._resolve_pip_spec pins the
# channel to the CURRENTLY installed version, so a bare reload would just
# reinstall the same build. This pins the next install to a specific version
# regardless of auto_update; embedded_server clears it when it CONSUMES it
# (before the install attempt) — one marker buys exactly one attempt, so a
# failing pinned version can never re-pin later reloads (review finding).
DATA_PENDING_INSTALL_VERSION = "pending_install_version"

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
# The ServerVersionCoordinator instance backing the `update` platform entity
# (issue #1760) — stored so the platform's async_setup_entry can retrieve it.
DATA_UPDATE_COORDINATOR = "update_coordinator"
# Set by async_maybe_auto_update right before it reloads the entry for an
# automatic update ({"old": <version>}): the "server updated" notification must
# only fire once the reloaded entry's bring-up actually installed and started
# the new build — the reload call returns as soon as entry SETUP finishes,
# while the pip install still runs in the background and can fail (review
# finding on #1760). Bring-up pops it: notification on success, silent drop on
# failure (the package/start repair issues cover that path).
DATA_PENDING_UPDATE_NOTIFY = "pending_update_notify"
# Unregister callback for the conversation-agent LLM API (#1745), stored by
# the bring-up success path and invoked (idempotently) by teardown.
DATA_LLM_API_UNSUB = "llm_api_unsub"

# Webhook auth modes (mirrors the webhook-proxy add-on's default posture).
WEBHOOK_AUTH_NONE = "none"  # secret webhook URL is the shared secret (default)
WEBHOOK_AUTH_HA = "ha_auth"  # HA-native bearer (HA core is the OAuth AS)
# Self-hosted OAuth 2.1 authorization server with a static client_id/secret,
# ported from the webhook-proxy add-on's "legacy" mode. Needed because HA
# core's /auth/authorize does not yet fetch Client ID Metadata Documents for
# cross-origin redirect_uris (home-assistant/core#176282), which is what
# Google Gemini Spark's custom connected apps require.
WEBHOOK_AUTH_LEGACY = "legacy"

# Default bind host + port. 9584 (not the add-on's 9583) so this in-process
# server and an add-on install can coexist on the same box.
DEFAULT_SERVER_PORT = 9584
# LAN-reachable by default - parity with the add-on, whose port has always
# been directly reachable with the secret path as the credential. Loopback
# is the optional hardening choice, not the default (owner decision).
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

# Client name recorded on the provisioned long-lived access token, and the name
# of the local admin user the server logs in as. Stable so a reused token is
# recognizable in Settings -> People -> <user> -> tokens. "HA-MCP" phrasing (not
# "Home Assistant MCP Server") to avoid confusion with HA's official MCP Server
# integration.
SERVER_TOKEN_CLIENT_NAME = "HA-MCP Server"
SERVER_USER_NAME = "HA-MCP Server"

# RFC 8414 / RFC 9728 discovery documents for ha_auth mode are served under this
# namespace (mirrors the webhook-proxy add-on's /api/mcp_proxy/oauth base).
OAUTH_BASE = "/api/ha_mcp_tools/oauth"

# HACS "add repository" deep link for the custom component. Shared learn_more_url
# for every repair issue that ends with "install/reinstall the component via
# HACS" (the component-outdated issue and the legacy-HACS-source issue below).
HACS_COMPONENT_URL = (
    "https://my.home-assistant.io/redirect/hacs_repository/"
    "?owner=homeassistant-ai&repository=ha-mcp-integration&category=integration"
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
# Repair issue surfaced when the installed ha-mcp server requires a newer
# custom component than the one running. The server package updates
# independently of the HACS component, so the running component can lag what
# the server expects; this points the user at the HACS component update
# (non-blocking).
ISSUE_COMPONENT_OUTDATED = "component_outdated"
# Repair issue surfaced while an automatic server update is HELD because the
# newer server release also shipped a newer custom component than the one
# running (issues #1783/#1785): installing that server under the old component
# is the combination that broke starts. Held is loud (this issue + a warning
# log every check) and escapable — applying the HACS component update (which
# takes an HA restart) unblocks the next check, and the update entity's
# Install button bypasses the hold entirely.
ISSUE_UPDATE_HELD = "server_update_held"
# Repair issue surfaced when HACS is tracking the MAIN ha-mcp server repo for
# this component (the pre-mirror install path — issue #1760). That install
# keeps working (HACS downloads the repo snapshot at the release tag, which
# contains the component), but HACS shows the SERVER's version numbers and
# release notes, not the component's own; HACS has no repository-migration
# mechanism, so this only self-resolves if the user re-adds the dedicated
# mirror (homeassistant-ai/ha-mcp-integration).
ISSUE_LEGACY_HACS_SOURCE = "legacy_hacs_source"
# Repair issue surfaced when the legacy OAuth mode's root /authorize + /token
# views are out of sync with the CONFIGURED webhook_auth mode — either just
# enabled (views not yet bound with the current credentials) or just disabled
# (views still bound and serving the old identity). aiohttp can neither bind
# nor unbind an HTTP view without a full Home Assistant restart, so both
# transitions need one; see oauth_legacy.bind_legacy_views.
ISSUE_LEGACY_OAUTH_RESTART = "legacy_oauth_restart"
