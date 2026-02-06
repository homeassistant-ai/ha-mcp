"""MCP Webhook Proxy - routes MCP requests to the ha-mcp addon via webhook.

This integration is auto-installed by the ha-mcp addon when Nabu Casa remote
access is enabled. It registers an unauthenticated webhook endpoint that
proxies MCP requests to the addon, bypassing the ingress auth requirement.

Configuration is read from /config/.mcp_proxy_config.json, which is written
by the addon's startup script. No user configuration is needed in
configuration.yaml beyond the bare `mcp_proxy:` entry.
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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "mcp_proxy"
CONFIG_FILE = Path("/config/.mcp_proxy_config.json")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the MCP Webhook Proxy integration."""
    # Read config written by the ha-mcp addon
    proxy_config = await hass.async_add_executor_job(_read_config)
    if proxy_config is None:
        _LOGGER.info(
            "MCP Proxy: No config found at %s. "
            "Enable 'nabu_casa_remote' in the ha-mcp addon to activate.",
            CONFIG_FILE,
        )
        return True

    target_url = proxy_config.get("target_url", "")
    webhook_id = proxy_config.get("webhook_id", "")

    if not target_url or not webhook_id:
        _LOGGER.error("MCP Proxy: Invalid config - missing target_url or webhook_id")
        return False

    _LOGGER.info("MCP Proxy: target = %s", target_url)
    _LOGGER.info("MCP Proxy: webhook endpoint = /api/webhook/%s", webhook_id)

    hass.data[DOMAIN] = {
        "target_url": target_url,
        "webhook_id": webhook_id,
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
        if key.lower() in ("host", "content-length", "transfer-encoding", "connection"):
            continue
        forward_headers[key] = value

    try:
        timeout = aiohttp.ClientTimeout(total=300, sock_read=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
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
                    # Non-streaming response
                    resp_headers["Content-Type"] = content_type
                    resp_body = await upstream_resp.read()
                    return web.Response(
                        status=upstream_resp.status,
                        body=resp_body,
                        headers=resp_headers,
                    )

    except aiohttp.ClientError as err:
        _LOGGER.error("MCP Proxy: upstream request failed: %s", err)
        return web.Response(status=502, text=f"MCP Proxy: upstream error: {err}")
    except Exception as err:
        _LOGGER.exception("MCP Proxy: unexpected error: %s", err)
        return web.Response(status=500, text=f"MCP Proxy: internal error: {err}")


async def async_unload(hass: HomeAssistant) -> bool:
    """Unload the MCP Webhook Proxy."""
    data = hass.data.pop(DOMAIN, {})
    webhook_id = data.get("webhook_id")
    if webhook_id:
        async_unregister(hass, webhook_id)
    return True
