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

Configuration is read from /config/.mcp_proxy_config.json, which is written
by the proxy addon's startup script. No manual configuration is needed — the
addon creates the config entry automatically via the HA API.
"""

import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from homeassistant.components.webhook import (
    async_register,
    async_unregister,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "mcp_proxy"
CONFIG_FILE = Path("/config/.mcp_proxy_config.json")

# ha-mcp generates a 22-char base64url token after `/private_`. We accept >=16
# as a sanity floor — a truncated/corrupted ha-mcp config yields a shorter
# token, which is the failure mode this length check exists to catch.
_SECRET_PATH_RE = re.compile(r"^/private_[A-Za-z0-9_-]{16,}$")


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

    If the user has an old `mcp_proxy:` entry in configuration.yaml,
    auto-migrate to a config entry so the YAML line can be removed.

    Also runs the boot-time repair-issue check: if the addon left a
    "needs HA restart for OAuth" marker file behind, surface it as a
    Repair card with a click-to-restart fix flow. See repairs.py for
    the full lifecycle.
    """
    if DOMAIN in config:
        _LOGGER.info(
            "MCP Proxy: Found YAML config — migrating to config entry. "
            "You can safely remove 'mcp_proxy:' from configuration.yaml."
        )
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "import"}
            )
        )
    if await hass.async_add_executor_job(_marker_present):
        from .repairs import maybe_create_issue
        maybe_create_issue(hass, DOMAIN)
    return True


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
            "Missing target_url or webhook_id in /config/.mcp_proxy_config.json. "
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
            "to regenerate /config/.mcp_proxy_config.json."
        )

    _LOGGER.info("MCP Proxy: target = %s", masked_target)
    _LOGGER.info("MCP Proxy: webhook endpoint = /api/webhook/%s", masked_wh)

    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=300, sock_connect=10, sock_read=300),
    )

    try:
        async_register(
            hass,
            DOMAIN,
            "MCP Proxy",
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
        raise ConfigEntryError(
            f"Failed to register webhook endpoint: {err}"
        ) from err

    hass_data: dict = {
        "target_url": target_url,
        "webhook_id": webhook_id,
        "session": session,
    }

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
    oauth_section = proxy_config.get("oauth")
    if isinstance(oauth_section, dict):
        client_id = str(oauth_section.get("client_id", ""))
        client_secret = str(oauth_section.get("client_secret", ""))
        if not client_id or not client_secret:
            await session.close()
            raise ConfigEntryError(
                "OAuth was enabled in the addon but client_id and/or "
                "client_secret is blank in /config/.mcp_proxy_config.json. "
                "Restart the Webhook Proxy addon to regenerate the config "
                "file, or turn off Enable OAuth in the addon configuration."
            )
        public_base_url = proxy_config.get("public_base_url")
        if not isinstance(public_base_url, str) or not public_base_url:
            public_base_url = None
        from .oauth import OAuthProvider, load_or_create_secret
        try:
            # Filesystem I/O — must run off the event loop.
            signing_key = await hass.async_add_executor_job(
                load_or_create_secret
            )
            oauth_provider = OAuthProvider(
                hass=hass,
                client_id=client_id,
                client_secret=client_secret,
                webhook_id=webhook_id,
                signing_key=signing_key,
                public_base_url=public_base_url,
            )
            oauth_provider.register_views()
        except Exception as err:
            _LOGGER.exception(
                "MCP Proxy: failed to initialise OAuth provider (%s)",
                type(err).__name__,
            )
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

    hass.data[DOMAIN] = hass_data

    # If we got here, the integration is set up and (if OAuth is configured)
    # the OAuth provider's views are registered. Either way, any prior
    # "needs HA restart for OAuth" marker is now stale — clear it so the
    # Repair card disappears. Marker cleanup is filesystem I/O so it runs
    # in the executor; the issue-registry call is synchronous and safe on
    # the event loop.
    from .repairs import _clear_marker, _delete_issue_only
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
    return json.loads(CONFIG_FILE.read_text())


async def _handle_webhook(
    hass: HomeAssistant, webhook_id: str, request: web.Request
) -> web.StreamResponse:
    """Forward the MCP request to the addon and stream the response back."""
    data = hass.data[DOMAIN]
    target_url = data["target_url"]

    # OAuth gate. When OAuth isn't configured, `oauth_provider` is None and
    # this branch is a single attribute lookup with zero behavior change vs
    # the original handler.
    oauth_provider = data.get("oauth")
    if oauth_provider is not None and not oauth_provider.validate_bearer(request):
        from .oauth import build_unauthorized_response
        return build_unauthorized_response(request, oauth_provider)

    body = await request.read()

    # Forward headers, excluding hop-by-hop headers
    forward_headers = {}
    for key, value in request.headers.items():
        if key.lower() in (
            "host", "content-length", "transfer-encoding", "connection",
            "cookie", "authorization",
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
