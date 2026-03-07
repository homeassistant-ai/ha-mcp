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
from pathlib import Path

import aiohttp
from aiohttp import web
from homeassistant.components.webhook import (
    async_register,
    async_unregister,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "mcp_proxy"
CONFIG_FILE = Path("/config/.mcp_proxy_config.json")


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
    proxy_config = await hass.async_add_executor_job(_read_config)
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
        return False

    # Mask sensitive values in logs to avoid leaking secrets
    if "/private_" in target_url:
        masked_target = target_url.split("/private_")[0] + "/private_********"
    else:
        masked_target = target_url
    masked_wh = webhook_id[:6] + "..." if len(webhook_id) > 6 else "***"
    _LOGGER.info("MCP Proxy: target = %s", masked_target)
    _LOGGER.info("MCP Proxy: webhook endpoint = /api/webhook/%s", masked_wh)

    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=300, sock_connect=10, sock_read=300),
    )
    hass.data[DOMAIN] = {
        "target_url": target_url,
        "webhook_id": webhook_id,
        "session": session,
    }

    async_register(
        hass,
        DOMAIN,
        "MCP Proxy",
        webhook_id,
        _handle_webhook,
        allowed_methods=["POST", "GET"],
    )

    return True


def _read_config() -> dict | None:
    """Read proxy config from JSON file (blocking I/O)."""
    if not CONFIG_FILE.exists():
        return None
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        _LOGGER.error("MCP Proxy: Failed to read %s: %s", CONFIG_FILE, e)
        return None


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
