"""MCP Webhook Proxy - routes MCP requests to the ha-mcp addon via webhook.

This integration is auto-installed by the webhook proxy addon when started.
By default it registers an UNAUTHENTICATED webhook endpoint that proxies
MCP requests to the ha-mcp addon, allowing remote access via any reverse
proxy (Nabu Casa, Cloudflare, DuckDNS, nginx, etc.). The webhook URL itself
is the shared secret in this default mode.

Authentication: when the addon's "Enable OAuth" toggle is on, the addon
writes OAuth client credentials into the config file and this integration
lazy-imports `oauth.py` to register the OAuth 2.1 endpoints + bearer-token
gate. When the toggle is off, no OAuth code is loaded and the proxy behaves
exactly like the original unauthenticated webhook.

Configuration is read from /config/.mcp_proxy_dev_config.json, which is written
by the proxy addon's startup script. No manual configuration is needed — the
addon creates the config entry automatically via the HA API.
"""

import hashlib
import json
import logging
import re
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
import voluptuous as vol
from aiohttp import web
from homeassistant.components.webhook import (
    async_register,
    async_unregister,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

# Tracks whether *this process* raised the logger to INFO for the debug toggle,
# so the off path undoes only our own raise — never a level the user set via
# Home Assistant's `logger:` config. Module-global (not hass.data) because it
# must survive a config-entry reload, during which hass.data[DOMAIN] is gone.
_LOGGER_LEVEL_RAISED = False

DOMAIN = "mcp_proxy_dev"
CONFIG_FILE = Path("/config/.mcp_proxy_dev_config.json")

# OAuth mode markers written into hass.data["oauth_mode"] and read by the
# webhook gate + the mode-aware discovery views. Mirrored as
# auth_native.HA_AUTH_MODE / oauth.MODE_* (a test pins them in agreement).
#   ha_auth — HA core is the authorization server (auth_native.ResourceServer);
#             the add-on only serves the discovery documents + validates bearers.
#   legacy  — this integration's embedded authorization server (oauth.py).
# The two are mutually exclusive: exactly one marker is ever set for an entry.
OAUTH_MODE_HA_AUTH = "ha_auth"
OAUTH_MODE_LEGACY = "legacy"

# Service the add-on calls (via the HA Core API) to raise/clear the
# click-to-restart Repair issues from outside HA — see async_setup.
SERVICE_REFRESH_REPAIRS = "refresh_repairs"
_REFRESH_REPAIRS_SCHEMA = vol.Schema(
    {
        # Kept in sync with repairs.py ISSUE_ID / UPDATE_ISSUE_ID (asserted
        # by the addon test suite); literals here so the schema builds
        # without importing repairs.py on the happy path.
        vol.Required("issue_id"): vol.In(
            ["oauth_restart_required", "update_restart_required"]
        ),
        vol.Required("action"): vol.In(["create", "clear"]),
    }
)
# The add-on's "integration updated, restart HA" notification id — dismissed
# at boot once the new code is actually loaded. Kept in sync with start.py.
UPDATE_NOTIFICATION_ID = "mcp_proxy_dev_update"

# Inbound-request mirror file. When "Log inbound requests" is on we append each
# inbound debug line here in addition to logging it to Home Assistant, so the
# Webhook Proxy addon (a separate process) can tail it and surface the same
# lines in the addon log. Path kept in sync with start.py:INBOUND_LOG_FILE.
INBOUND_LOG_FILE = Path("/config/.mcp_proxy_dev_inbound.log")
# Cap the mirror file so it can't grow without bound; trim to the last half
# when exceeded. 256 KiB keeps plenty of recent history at ~100 bytes/line.
_INBOUND_LOG_CAP = 256 * 1024
# Serializes writes to INBOUND_LOG_FILE: HA dispatches _append_inbound_log to a
# multi-worker executor pool, so concurrent inbound requests could interleave
# the append + the cap's read-modify-write trim without this lock.
_LOG_WRITE_LOCK = threading.Lock()

# Shared HA-instance marker (SAME literal in both flavors — deliberately
# domain-neutral so both read the same key) recording which flavor's DOMAIN
# registered the root OAuth /authorize + /token views. Lets the second flavor
# fail loudly on a cross-flavor collision instead of silently shadowing. NOT
# cleared on unload: HA can't unregister the HTTP views until it restarts, so
# the ownership marker must outlive the config entry too.
OAUTH_ROUTE_OWNER_KEY = "webhook_proxy_oauth_route_owner"

# Fingerprint of the OAuth identity (client id/secret + signing key) currently
# bound to the root /authorize + /token views. Lets a mid-session credential
# regeneration be detected: the bound views can't be re-registered until an HA
# restart, so if the fingerprint changed we must prompt for one instead of
# silently serving mismatched views. Same literal in both flavors.
OAUTH_ROUTE_KEY_FINGERPRINT = "webhook_proxy_oauth_route_key_fingerprint"

# ha-mcp generates a 22-char base64url token after `/private_`. We accept >=16
# as a sanity floor — a truncated/corrupted ha-mcp config yields a shorter
# token, which is the failure mode this length check exists to catch.
_SECRET_PATH_RE = re.compile(r"^/private_[A-Za-z0-9_-]{16,}$")

# Permissive whole-config schema: satisfies hassfest's [CONFIG_SCHEMA] check
# (any integration with async_setup must declare one) while preserving the
# YAML-migration path below. cv.config_entry_only_config_schema is wrong here:
# it raises an ERROR-severity repair issue on any `mcp_proxy_dev:` key, which would
# collide with the legacy `mcp_proxy_dev:` line async_setup imports from.
CONFIG_SCHEMA = vol.Schema(
    {vol.Optional(DOMAIN): vol.Any(None, dict)},
    extra=vol.ALLOW_EXTRA,
)


def _oauth_route_fingerprint(
    client_id: str, client_secret: str, signing_key: bytes
) -> str:
    """Stable fingerprint of the OAuth identity bound to the root views."""
    h = hashlib.sha256()
    h.update(client_id.encode())
    h.update(b"\0")
    h.update(client_secret.encode())
    h.update(b"\0")
    h.update(signing_key)
    return h.hexdigest()


def _validate_target_url(target_url: str) -> tuple[bool, str]:
    """Check that target_url is a well-formed http(s) URL.

    When the path starts with `/private_` we additionally enforce the
    ha-mcp secret-path shape so a truncated token (the issue we're guarding
    against) is rejected. Other paths are accepted as-is — users with a
    custom MCP server pointed at a different path are not constrained.
    """
    parsed = urlparse(target_url)

    if parsed.scheme not in ("http", "https"):
        return False, f"scheme must be http or https, got {parsed.scheme!r}"
    if not parsed.netloc:
        return False, "URL is missing host"
    if parsed.params or parsed.query or parsed.fragment:
        return False, "URL must not contain query, fragment, or path parameters"
    if parsed.path.startswith("/private_") and not _SECRET_PATH_RE.match(parsed.path):
        # Don't echo parsed.path — it contains the (truncated) secret token.
        return False, (
            "secret path is too short or malformed "
            "(expected /private_<token> with token of at least 16 characters)"
        )
    return True, ""


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the MCP Webhook Proxy from configuration.yaml (migration only).

    If the user has an old `mcp_proxy_dev:` entry in configuration.yaml,
    auto-migrate to a config entry so the YAML line can be removed.

    Also runs the boot-time repair-issue check: if a "needs HA restart
    for OAuth" marker file was left behind (by the add-on's fail-closed
    gate or the integration's mid-session OAuth enable), surface it as a
    Repair card with a click-to-restart fix flow. See repairs.py for
    the full lifecycle.

    Registers the `refresh_repairs` service the add-on calls to raise a
    click-to-restart Repair from OUTSIDE Home Assistant the moment a restart
    becomes necessary (integration files updated on disk, or OAuth enabled
    against stale loaded code). Only in-process code can file repair issues,
    so without this service the add-on could only post a persistent
    notification and the Repair card would not appear until the very restart
    it is supposed to prompt.
    """
    if DOMAIN in config:
        _LOGGER.info(
            "MCP Proxy: Found YAML config — migrating to config entry. "
            "You can safely remove 'mcp_proxy_dev:' from configuration.yaml."
        )
        hass.async_create_task(
            hass.config_entries.flow.async_init(DOMAIN, context={"source": "import"})
        )
    if await hass.async_add_executor_job(_marker_present):
        from .repairs import maybe_create_issue

        maybe_create_issue(hass, DOMAIN)

    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_REPAIRS,
        _make_refresh_repairs_handler(hass),
        schema=_REFRESH_REPAIRS_SCHEMA,
    )

    # This code executing at boot means the most recently installed
    # integration files are what's loaded — the restart any earlier
    # "integration updated" notification asked for has happened. Dismissing
    # a notification that doesn't exist is a no-op.
    hass.async_create_task(
        hass.services.async_call(
            "persistent_notification",
            "dismiss",
            {"notification_id": UPDATE_NOTIFICATION_ID},
        )
    )
    return True


def _make_refresh_repairs_handler(
    hass: HomeAssistant,
) -> Callable[[ServiceCall], Coroutine[Any, Any, None]]:
    """Build the refresh_repairs service handler (see async_setup docstring)."""

    async def _handle_refresh_repairs(call: ServiceCall) -> None:
        from .repairs import (
            ISSUE_ID,
            _delete_issue_only,
            create_issue,
            marker_present,
        )

        issue_id: str = call.data["issue_id"]
        if call.data["action"] == "clear":
            _delete_issue_only(hass, DOMAIN, issue_id)
            return
        if issue_id == ISSUE_ID:
            # The marker file is the source of truth for the OAuth repair
            # (it must survive an aborted restart), so file the issue only
            # when the add-on has actually written it.
            if await hass.async_add_executor_job(marker_present):
                create_issue(hass, DOMAIN, issue_id)
            return
        # update_restart_required: non-persistent by design — a successful
        # HA restart loads the new code and drops the issue automatically.
        create_issue(hass, DOMAIN, issue_id)

    return _handle_refresh_repairs


def _marker_present() -> bool:
    # Imported lazily so async_setup doesn't pull in repairs.py module-load
    # cost on the no-marker happy path.
    from .repairs import marker_present

    return marker_present()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MCP Webhook Proxy from a config entry."""
    try:
        proxy_config = await hass.async_add_executor_job(_read_config)
    except (OSError, json.JSONDecodeError) as err:
        _LOGGER.error("MCP Proxy: Failed to read %s: %s", CONFIG_FILE, err)
        raise ConfigEntryError(
            f"Failed to read {CONFIG_FILE}: {err}. Restart the Webhook Proxy "
            "addon to regenerate the config file."
        ) from err

    if proxy_config is None:
        _LOGGER.info(
            "MCP Proxy: No config found at %s. "
            "Start the Webhook Proxy addon to activate.",
            CONFIG_FILE,
        )
        return True

    target_url = proxy_config.get("target_url", "")
    webhook_id = proxy_config.get("webhook_id", "")

    if not target_url or not webhook_id:
        _LOGGER.error("MCP Proxy: Invalid config - missing target_url or webhook_id")
        raise ConfigEntryError(
            "Missing target_url or webhook_id in /config/.mcp_proxy_dev_config.json. "
            "Restart the Webhook Proxy addon to regenerate it."
        )

    # Mask sensitive values in logs to avoid leaking secrets
    if "/private_" in target_url:
        masked_target = target_url.split("/private_")[0] + "/private_********"
    else:
        masked_target = target_url
    masked_wh = webhook_id[:6] + "..." if len(webhook_id) > 6 else "***"

    # Validate target_url shape before registering. Without this, a corrupted
    # URL (e.g. a truncated secret-path) propagates silently and the config
    # entry reports `loaded` while every webhook request returns 404.
    is_valid, reason = _validate_target_url(target_url)
    if not is_valid:
        _LOGGER.error(
            "MCP Proxy: target_url validation failed for %s: %s",
            masked_target,
            reason,
        )
        raise ConfigEntryError(
            f"Invalid target_url ({reason}). Restart the Webhook Proxy addon "
            "to regenerate /config/.mcp_proxy_dev_config.json."
        )

    _LOGGER.info("MCP Proxy: target = %s", masked_target)
    _LOGGER.info("MCP Proxy: webhook endpoint = /api/webhook/%s", masked_wh)

    # Inbound-request debug logging (addon "Log inbound requests" toggle).
    # Custom-component loggers default to WARNING, so when the toggle is on we
    # raise our own logger to INFO so the per-request lines are emitted — but
    # only when the effective level is less verbose, so we never override an
    # explicit DEBUG/INFO the user set via Home Assistant's `logger:` config. We
    # track whether WE raised it and, when the toggle is off, undo only our own
    # raise — never a level the user set themselves.
    global _LOGGER_LEVEL_RAISED
    debug_logging = bool(proxy_config.get("debug_logging", False))
    if debug_logging and _LOGGER.getEffectiveLevel() > logging.INFO:
        _LOGGER.setLevel(logging.INFO)
        _LOGGER_LEVEL_RAISED = True
    elif not debug_logging and _LOGGER_LEVEL_RAISED:
        # Undo only the INFO we raised. (If a user had set an explicit level
        # quieter than INFO — ERROR/CRITICAL — then toggled debug on then off,
        # this resets to NOTSET rather than their original level; restoring that
        # would need durable per-level state, not worth it for a debug aid.)
        _LOGGER.setLevel(logging.NOTSET)
        _LOGGER_LEVEL_RAISED = False
    if debug_logging:
        _LOGGER.info(
            "MCP Proxy: inbound request debug logging is ON — each request to "
            "this webhook will be logged here."
        )

    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=300, sock_connect=10, sock_read=300),
    )

    try:
        async_register(
            hass,
            DOMAIN,
            "MCP Proxy (Dev)",
            webhook_id,
            _handle_webhook,
            allowed_methods=["POST", "GET"],
        )
    except Exception as err:
        _LOGGER.exception(
            "MCP Proxy: failed to register webhook endpoint /api/webhook/%s",
            masked_wh,
        )
        await session.close()
        raise ConfigEntryError(f"Failed to register webhook endpoint: {err}") from err

    hass_data: dict = {
        "target_url": target_url,
        "webhook_id": webhook_id,
        "session": session,
    }
    # Mirror the oauth pattern: only add the key when the feature is on, so the
    # default/OFF path's hass.data shape stays identical to the baseline
    # (target_url, webhook_id, session) — guarded by TestOAuthOffPreservesBehavior.
    if debug_logging:
        hass_data["debug_logging"] = True

    # OAuth is opt-in. When the addon writes an `oauth` section into the
    # config file (only when enable_oauth is on AND both creds are non-empty,
    # validated by start.py), we lazy-import the provider and register its
    # views. When the section is absent, this entire branch is skipped —
    # nothing about hass.data, imports, or registered HTTP views changes
    # from the no-auth baseline. That is the load-bearing guarantee for
    # users who don't opt into OAuth.
    #
    # If the OAuth section IS present but malformed — blank creds, or view
    # registration fails — we fail loudly via ConfigEntryError. The user
    # explicitly opted into auth; silently falling back to no-auth would
    # leave them with an open endpoint they think is locked.
    oauth_restart_needed = False
    oauth_section = proxy_config.get("oauth")
    oauth_mode = oauth_section.get("mode") if isinstance(oauth_section, dict) else None
    if isinstance(oauth_section, dict) and oauth_mode == OAUTH_MODE_HA_AUTH:
        # ── ha_auth: HA core is the authorization server (see auth_native) ──
        # Hard mutual exclusion: a ha_auth section carries NO legacy credentials.
        # If client_id/client_secret keys are present the config is ambiguous
        # (a bad hand-edit or a bug) — refuse to guess which mode was intended
        # rather than risk serving with the wrong auth model.
        if "client_id" in oauth_section or "client_secret" in oauth_section:
            async_unregister(hass, webhook_id)
            await session.close()
            raise ConfigEntryError(
                "Ambiguous OAuth config: the oauth section is mode 'ha_auth' "
                "but also carries legacy client_id/client_secret keys. ha_auth "
                "signs in with your Home Assistant account and takes no add-on "
                "credentials. Restart the Webhook Proxy addon to regenerate "
                "/config/.mcp_proxy_dev_config.json."
            )
        # Log the HA version. No minimum-version gate: everything ha_auth calls
        # at runtime is long-stable HA API — /auth/*, same-origin URL-shaped
        # IndieAuth client_ids, and hass.auth bearer validation. The advertised
        # client_id_metadata_document_supported flag is advertisement-only (HA
        # never fetches a CIMD document; the field just signals capability),
        # and home-assistant/core#153820 is field evidence that claude.ai /
        # ChatGPT custom connectors work against HA's native OAuth, not a
        # runtime code dependency of this add-on — hence nothing to gate on.
        try:
            from homeassistant.const import __version__ as _hass_version
        except ImportError:  # pragma: no cover - defensive
            _hass_version = "unknown"
        _LOGGER.info(
            "MCP Proxy: OAuth mode 'ha_auth' — Home Assistant (version %s) is "
            "the authorization server; this add-on serves only the OAuth "
            "discovery documents and validates bearer tokens via hass.auth. No "
            "HA restart is needed to enable or disable this mode.",
            _hass_version,
        )
        # ha_auth is ALWAYS host-derived: the SAME install must work via the
        # Nabu Casa cloud URL AND any other external URL, so the base URL is
        # built per-request from the host and never pinned. Pass None explicitly
        # and ignore any public_base_url a hand-edited config might carry (the
        # ambiguity guard above only rejects stray client_id/client_secret keys,
        # so a stray public_base_url would otherwise wrongly pin the base URL).
        try:
            from .auth_native import ResourceServer
            from .oauth import register_metadata_views

            resource_server = ResourceServer(hass, webhook_id, None)
            # Registers the seven discovery-document views at most once per HA
            # session (register_metadata_views no-ops when either mode already
            # bound them — see oauth._METADATA_VIEWS_REGISTERED_KEY), so a
            # config-entry reload or a live legacy->ha_auth switch doesn't
            # stack shadowed duplicates. ha_auth binds NO root views (HA core
            # owns /authorize + /token), so there is no owner-key / fingerprint
            # bookkeeping and no restart concept — hence oauth_restart_needed
            # stays False and the marker-CLEAR path runs below.
            register_metadata_views(hass, resource_server)
        except Exception as err:
            _LOGGER.exception(
                "MCP Proxy: failed to initialise ha_auth OAuth (%s)",
                type(err).__name__,
            )
            # OAuth setup failed — unregister the webhook we registered above so
            # we don't leave an unauthenticated endpoint live.
            async_unregister(hass, webhook_id)
            await session.close()
            raise ConfigEntryError(
                f"Failed to enable OAuth on the MCP webhook: {err}. "
                "Auth is not being enforced — refusing to start the "
                "integration so the webhook URL is not silently exposed "
                "without the protection the user requested."
            ) from err
        hass_data["oauth"] = resource_server
        hass_data["oauth_mode"] = OAUTH_MODE_HA_AUTH
    elif isinstance(oauth_section, dict) and oauth_mode not in (
        None,
        OAUTH_MODE_LEGACY,
    ):
        # Unknown mode value — refuse loudly rather than silently guessing.
        async_unregister(hass, webhook_id)
        await session.close()
        raise ConfigEntryError(
            f"Unknown OAuth mode {oauth_mode!r} in "
            "/config/.mcp_proxy_dev_config.json. Valid values are 'ha_auth' and "
            "'legacy'. Restart the Webhook Proxy addon to regenerate the "
            "config file."
        )
    elif isinstance(oauth_section, dict):
        # ── legacy: this integration's embedded authorization server ──────
        # Reached for mode 'legacy' OR an absent mode key (creds-only
        # back-compat with pre-ha_auth config files, which guarantees every
        # existing legacy test keeps passing unchanged). Byte-for-byte the
        # pre-ha_auth behavior below.
        client_id = str(oauth_section.get("client_id", ""))
        client_secret = str(oauth_section.get("client_secret", ""))
        if not client_id or not client_secret:
            # OAuth setup failed — unregister the webhook we registered above so
            # we don't leave an unauthenticated endpoint live.
            async_unregister(hass, webhook_id)
            await session.close()
            raise ConfigEntryError(
                "OAuth was enabled in the addon but client_id and/or "
                "client_secret is blank in /config/.mcp_proxy_dev_config.json. "
                "Restart the Webhook Proxy addon to regenerate the config "
                "file, or turn off Enable OAuth in the addon configuration."
            )
        # Root-route collision guard. The OAuth provider registers /authorize
        # and /token at the HA ROOT (claude.ai builds <host>/authorize from the
        # host root). HA cannot unregister HTTP views until it restarts, and
        # aiohttp lets the first-registered path win — a later duplicate is
        # silently shadowed. So if the OTHER webhook-proxy flavor already owns
        # these routes in this HA instance, registering ours would be shadowed
        # (its provider has a different signing key, so nothing it serves would
        # validate here). Fail LOUDLY instead. This also covers the sibling
        # being *stopped* but its views still bound (see OAUTH_ROUTE_OWNER_KEY).
        route_owner = hass.data.get(OAUTH_ROUTE_OWNER_KEY)
        if route_owner is not None and route_owner != DOMAIN:
            async_unregister(hass, webhook_id)
            await session.close()
            raise ConfigEntryError(
                f"The other Webhook Proxy flavor ('{route_owner}') already owns "
                "the root OAuth /authorize and /token routes in this Home "
                "Assistant instance, and Home Assistant cannot release them "
                "until it restarts. Stop that add-on and RESTART Home Assistant, "
                "then start this one. Only one Webhook Proxy flavor can serve "
                "OAuth at a time."
            )
        public_base_url = proxy_config.get("public_base_url")
        if not isinstance(public_base_url, str) or not public_base_url:
            public_base_url = None
        from .oauth import OAuthProvider, load_or_create_secret

        try:
            # Filesystem I/O — must run off the event loop.
            signing_key = await hass.async_add_executor_job(load_or_create_secret)
            # That executor await is a suspension point: the sibling flavor's
            # concurrently-setting-up entry can register and claim the root
            # routes while this one is suspended, which the pre-await guard
            # above cannot see (TOCTOU). Re-read the owner now — everything
            # from this read to the ownership-marker write below runs
            # synchronously on the event loop, so no further interleave is
            # possible and the claim-or-refuse is atomic.
            route_owner = hass.data.get(OAUTH_ROUTE_OWNER_KEY)
            if route_owner is not None and route_owner != DOMAIN:
                async_unregister(hass, webhook_id)
                await session.close()
                raise ConfigEntryError(
                    f"The other Webhook Proxy flavor ('{route_owner}') claimed "
                    "the root OAuth /authorize and /token routes while this "
                    "entry was setting up, and Home Assistant cannot release "
                    "them until it restarts. Stop that add-on and RESTART Home "
                    "Assistant, then start this one. Only one Webhook Proxy "
                    "flavor can serve OAuth at a time."
                )
            oauth_provider = OAuthProvider(
                hass=hass,
                client_id=client_id,
                client_secret=client_secret,
                webhook_id=webhook_id,
                signing_key=signing_key,
                public_base_url=public_base_url,
            )
            fingerprint = _oauth_route_fingerprint(
                client_id, client_secret, signing_key
            )
            bound_fp = hass.data.get(OAUTH_ROUTE_KEY_FINGERPRINT)
            if route_owner == DOMAIN and bound_fp == fingerprint:
                # Reload of our own entry with the SAME credentials + key: the
                # root views are already bound and current. Reuse them (HA can't
                # re-register mid-session; re-registering would only pile up
                # shadowed duplicates). OAuth is live — no restart.
                _LOGGER.debug(
                    "MCP Proxy: root OAuth views already bound with the current "
                    "credentials this session; reusing them (no restart)."
                )
            elif route_owner == DOMAIN:
                # Reload of our own entry but the credentials/key CHANGED
                # (regenerated) mid-session. HA can't re-bind the root views
                # until a restart, so the live /authorize + /token still use the
                # OLD identity while the webhook now expects the NEW one — clients
                # can't obtain a token the webhook accepts. Surface the restart
                # Repair; leave the stored fingerprint on the OLD (still-bound)
                # value so a boot-time setup re-registers and updates it.
                _LOGGER.warning(
                    "MCP Proxy: OAuth credentials changed but the bound root "
                    "views still use the previous ones — a Home Assistant "
                    "restart is required to activate the new credentials."
                )
                oauth_restart_needed = True
            else:
                # First registration this HA session.
                oauth_provider.register_views()
                hass.data[OAUTH_ROUTE_OWNER_KEY] = DOMAIN
                hass.data[OAUTH_ROUTE_KEY_FINGERPRINT] = fingerprint
                # A first registration happening mid-session isn't live until a
                # full HA restart; flag it. At HA boot it binds cleanly.
                oauth_restart_needed = hass.is_running
        except ConfigEntryError:
            # The post-await collision re-check above already tore down (webhook
            # unregistered, session closed) — re-raise as-is so the generic
            # handler below doesn't re-wrap the message and tear down twice.
            raise
        except Exception as err:
            _LOGGER.exception(
                "MCP Proxy: failed to initialise OAuth provider (%s)",
                type(err).__name__,
            )
            # OAuth setup failed — unregister the webhook we registered above so
            # we don't leave an unauthenticated endpoint live.
            async_unregister(hass, webhook_id)
            await session.close()
            raise ConfigEntryError(
                f"Failed to enable OAuth on the MCP webhook: {err}. "
                "Auth is not being enforced — refusing to start the "
                "integration so the webhook URL is not silently exposed "
                "without the protection the user requested."
            ) from err
        _LOGGER.info(
            "MCP Proxy: OAuth ENABLED (client_id=%s)",
            oauth_provider.client_id_masked(),
        )
        hass_data["oauth"] = oauth_provider
        hass_data["oauth_mode"] = OAUTH_MODE_LEGACY

    hass.data[DOMAIN] = hass_data

    # The integration is set up. If OAuth was (re)configured on a mid-session
    # setup, its root views aren't live until a full HA restart, so surface the
    # HACS-style restart Repair; otherwise (OAuth off, or set up cleanly during
    # HA boot) any prior "needs HA restart for OAuth" marker is now stale, so
    # clear it. Marker writes/cleanup are filesystem I/O and run in the
    # executor; the issue-registry calls are synchronous and safe on the loop.
    from .repairs import _clear_marker, _delete_issue_only, _write_marker, create_issue

    if oauth_restart_needed:
        # OAuth was (re)configured on a mid-session setup — it isn't live until
        # a full HA restart. Surface the HACS-style restart Repair (+ marker so
        # it survives to the next boot). A boot-time setup takes the else branch
        # and clears it once OAuth is genuinely active.
        await hass.async_add_executor_job(_write_marker)
        create_issue(hass, DOMAIN)
    else:
        # OAuth off, or set up during HA boot (views bound cleanly) — no restart
        # needed; clear any stale marker/issue.
        await hass.async_add_executor_job(_clear_marker)
        _delete_issue_only(hass, DOMAIN)

    return True


def _read_config() -> dict | None:
    """Read proxy config from JSON file (blocking I/O).

    Returns None only when the file does not exist (fresh install). Read or
    parse errors propagate as OSError/JSONDecodeError so the caller can
    distinguish "no config yet" from "config is corrupted".
    """
    if not CONFIG_FILE.exists():
        return None
    data: dict | None = json.loads(CONFIG_FILE.read_text())
    return data


def _append_inbound_log(line: str) -> None:
    """Append one inbound-debug line to the mirror file the addon tails.

    Capped: when the file grows past ``_INBOUND_LOG_CAP`` it is trimmed to its
    last half (dropping the now-partial first line) so it can't grow without
    bound. Best-effort — swallows its own ``OSError`` (e.g. a read-only
    ``/config``) so a mirror failure never surfaces as an unretrieved executor
    exception. Blocking filesystem I/O — call via ``hass.async_add_executor_job``.
    """
    try:
        with _LOG_WRITE_LOCK:
            with INBOUND_LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if INBOUND_LOG_FILE.stat().st_size > _INBOUND_LOG_CAP:
                data = INBOUND_LOG_FILE.read_bytes()[-(_INBOUND_LOG_CAP // 2) :]
                nl = data.find(b"\n")
                if nl != -1:
                    data = data[nl + 1 :]
                INBOUND_LOG_FILE.write_bytes(data)
    except OSError as e:
        _LOGGER.debug("MCP Proxy: inbound mirror write failed: %s", e)


async def _debug_log(hass: HomeAssistant, message: str) -> None:
    """Log an inbound-request debug line to Home Assistant AND mirror it to the
    addon log file (``INBOUND_LOG_FILE``) so it surfaces in the Webhook Proxy
    addon log too, not only in Settings -> System -> Logs.

    The mirror write is dispatched to the executor fire-and-forget: it runs off
    the event loop and we deliberately don't await it, so an opt-in debug log
    never adds latency to (or fails) the proxied request. ``_append_inbound_log``
    swallows its own ``OSError`` (the only realistic failure here, since the
    message is controlled ASCII), so the unawaited future does not carry an
    exception in practice.
    """
    _LOGGER.info("%s", message)
    hass.async_add_executor_job(_append_inbound_log, message)


async def _handle_webhook(
    hass: HomeAssistant, webhook_id: str, request: web.Request
) -> web.StreamResponse:
    """Forward the MCP request to the addon and stream the response back."""
    data = hass.data[DOMAIN]
    target_url = data["target_url"]

    # Inbound-request debug logging (opt-in). Logged BEFORE the OAuth gate so
    # the unauthenticated discovery probe (which gets a 401) is captured too —
    # that probe arriving is the proof a client actually reached the server.
    debug = data.get("debug_logging")
    if debug:
        wh = data["webhook_id"]
        masked_path = f"/api/webhook/{wh[:6]}..." if len(wh) > 6 else "/api/webhook/***"
        # request.remote is the client IP validated by HA's trusted-proxy layer
        # (it resolves X-Forwarded-For when the proxy is trusted). Reading the
        # raw X-Forwarded-For header here would let an untrusted client spoof
        # the logged source.
        source = request.remote or "unknown"
        has_auth = "present" if request.headers.get("Authorization") else "absent"
        await _debug_log(
            hass,
            f"MCP Proxy [inbound]: {request.method} {masked_path} from {source} "
            f"(Authorization header: {has_auth})",
        )

    # OAuth gate. When OAuth isn't configured, `oauth_provider` is None and
    # this branch is a single attribute lookup with zero behavior change vs
    # the original handler. When it is configured, the bearer check dispatches
    # by mode: ha_auth validates against HA core via the ResourceServer (async),
    # legacy validates this integration's own signed bearer (sync). Either way
    # an invalid/missing bearer yields the SAME 401 discovery challenge.
    oauth_provider = data.get("oauth")
    if oauth_provider is not None:
        if data.get("oauth_mode") == OAUTH_MODE_HA_AUTH:
            authorized = await oauth_provider.validate_request(request)
        else:
            authorized = oauth_provider.validate_bearer(request)
        if not authorized:
            if debug:
                await _debug_log(
                    hass,
                    "MCP Proxy [inbound]: -> 401 Unauthorized (no/invalid OAuth "
                    "bearer; expected for the initial discovery probe)",
                )
            from .oauth import build_unauthorized_response

            return build_unauthorized_response(request, oauth_provider)

    body = await request.read()

    # Forward headers, excluding hop-by-hop headers
    forward_headers = {}
    for key, value in request.headers.items():
        if key.lower() in (
            "host",
            "content-length",
            "transfer-encoding",
            "connection",
            "cookie",
            "authorization",
        ):
            continue
        forward_headers[key] = value

    # Allowed Content-Types for MCP responses (prevents XSS via HTML injection)
    allowed_content_types = ("application/json", "text/event-stream")
    session = data["session"]

    try:
        async with session.request(
            method=request.method,
            url=target_url,
            headers=forward_headers,
            data=body if body else None,
        ) as upstream_resp:
            content_type = upstream_resp.headers.get("Content-Type", "")

            if debug:
                await _debug_log(
                    hass,
                    f"MCP Proxy [inbound]: -> upstream responded "
                    f"{upstream_resp.status} ({content_type or 'no content-type'})",
                )

            # Common headers for both streaming and non-streaming
            resp_headers = {
                "Cache-Control": "no-cache, no-transform",
                "Content-Encoding": "identity",
            }
            mcp_session = upstream_resp.headers.get("Mcp-Session-Id")
            if mcp_session:
                resp_headers["Mcp-Session-Id"] = mcp_session

            if "text/event-stream" in content_type:
                # SSE streaming response - prevent HA compression middleware
                # from breaking it (supervisor#6470)
                resp_headers["Content-Type"] = "text/event-stream"
                resp_headers["X-Accel-Buffering"] = "no"

                response = web.StreamResponse(
                    status=upstream_resp.status,
                    headers=resp_headers,
                )
                await response.prepare(request)
                async for chunk in upstream_resp.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
            else:
                # Restrict Content-Type to allowed MCP types
                if not any(ct in content_type for ct in allowed_content_types):
                    content_type = "application/json"
                resp_headers["Content-Type"] = content_type
                resp_body = await upstream_resp.read()
                return web.Response(
                    status=upstream_resp.status,
                    body=resp_body,
                    headers=resp_headers,
                )

    except aiohttp.ClientError as err:
        _LOGGER.error("MCP Proxy: upstream request failed: %s", err)
        return web.Response(status=502, text="MCP Proxy: upstream unavailable")
    except Exception as err:
        _LOGGER.exception("MCP Proxy: unexpected error: %s", err)
        return web.Response(status=500, text="MCP Proxy: internal error")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the MCP Webhook Proxy config entry."""
    data = hass.data.pop(DOMAIN, {})
    webhook_id = data.get("webhook_id")
    if webhook_id:
        async_unregister(hass, webhook_id)
    session = data.get("session")
    if session:
        await session.close()
    return True
