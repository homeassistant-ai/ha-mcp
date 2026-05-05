"""MCP Webhook Proxy - routes MCP requests to the ha-mcp addon via webhook.

This integration is auto-installed by the webhook proxy addon when started.
It registers an unauthenticated webhook endpoint that proxies MCP requests
to the ha-mcp addon, allowing remote access via any reverse proxy (Nabu Casa,
Cloudflare, DuckDNS, nginx, etc.).

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
    return True


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

    hass.data[DOMAIN] = {
        "target_url": target_url,
        "webhook_id": webhook_id,
        "session": session,
    }

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
