"""Offer the app's server to Home Assistant's Model Context Protocol integration (#2307).

Runs once per start from ``server_lifespan``, inside the app only. The
Supervisor keys the message by (app, service), so announcing on every start
replaces the URL in place. It is never withdrawn on stop: Core removes the
discovered entry when the message disappears. Failures never stop the server.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx

from ._version import is_running_in_addon
from .client.supervisor_client import make_supervisor_httpx_client

logger = logging.getLogger(__name__)

# First Core release whose Model Context Protocol integration handles app
# discovery (home-assistant/core#180378). Older cores turn the message into a
# discovery card that drops the URL and asks for one by hand.
MCP_DISCOVERY_MIN_CORE = (2026, 10)
# The app's start.py writes the secret path here before it starts the server,
# and serves on this port.
SECRET_PATH_FILE = Path("/data/secret_path.txt")
APP_PORT = 9583


def core_supports_mcp_discovery(version: str) -> bool:
    """Whether a Core version string (``2026.10.0``, ``2026.10.0b1``) is new enough."""
    try:
        year, month = (int(part) for part in version.split(".")[:2])
    except ValueError:
        return False
    return (year, month) >= MCP_DISCOVERY_MIN_CORE


async def _supervisor_data(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
    """GET a Supervisor endpoint and return its ``data`` object."""
    response = await client.get(path)
    response.raise_for_status()
    data = response.json().get("data")
    return data if isinstance(data, dict) else {}


async def announce_mcp_discovery() -> None:
    """Announce ``http://<app hostname>:9583<secret path>`` to the Supervisor."""
    if not is_running_in_addon():
        return
    try:
        secret = await asyncio.to_thread(SECRET_PATH_FILE.read_text, encoding="utf-8")
        secret_path = secret.strip()
        if not secret_path:
            logger.warning("%s is empty; MCP discovery skipped.", SECRET_PATH_FILE)
            return
        async with make_supervisor_httpx_client(timeout=10.0, verify=True) as client:
            version = str((await _supervisor_data(client, "/core/info")).get("version"))
            if not core_supports_mcp_discovery(version):
                logger.info(
                    "Home Assistant %s cannot discover MCP apps (needs 2026.10+); "
                    "restart this app after updating Home Assistant.",
                    version,
                )
                return
            info = await _supervisor_data(client, "/addons/self/info")
            hostname = info.get("hostname")
            if not hostname:
                logger.warning(
                    "Supervisor reported no app hostname; MCP discovery skipped."
                )
                return
            # Starlette redirects a trailing slash, and Core's client does not
            # follow redirects.
            url = f"http://{hostname}:{APP_PORT}{secret_path.rstrip('/')}"
            response = await client.post(
                "/discovery", json={"service": "mcp", "config": {"url": url}}
            )
            response.raise_for_status()
    except (httpx.HTTPError, OSError, ValueError) as err:
        logger.warning("Could not announce this server to Home Assistant: %r", err)
        return
    logger.info(
        "Announced to Home Assistant: Settings > Devices & services offers to "
        "add this server to the Model Context Protocol integration."
    )
