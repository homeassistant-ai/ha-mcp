"""``core_client``: the server's Home Assistant client on a real ``hass``.

Kept out of ``conftest.py``, which predates the private-access check and is
listed for it, so every rule in ``tests/AGENTS.md`` applies to these fixtures.
``conftest.py`` loads this module as a pytest plugin.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from ha_mcp.client.rest_client import HomeAssistantClient


def free_port() -> int:
    """A loopback port nothing listens on once the probe lets it go."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def refused_url() -> str:
    """A loopback URL whose port nothing listens on: the connect is refused."""
    return f"http://127.0.0.1:{free_port()}"


@pytest.fixture
async def core_client(
    hass: HomeAssistant,
    aiohttp_client: Any,
    hass_access_token: str,
    socket_enabled: None,
) -> AsyncIterator[HomeAssistantClient]:
    """The server's own Home Assistant client, connected to the real ``hass``.

    A tool test passes this where a tool takes its client, so every REST and
    WebSocket reply is Core's own. The caller is an admin. The component's
    commands are not registered, as on an install without the component;
    ``component_core_client`` adds them.
    """
    from homeassistant.setup import async_setup_component

    from ha_mcp.client.rest_client import HomeAssistantClient
    from ha_mcp.client.websocket_client import websocket_manager

    assert await async_setup_component(hass, "api", {})
    assert await async_setup_component(hass, "websocket_api", {})
    server = await aiohttp_client(hass.http.app)
    client = HomeAssistantClient(
        base_url=str(server.make_url("")).rstrip("/"),
        token=hass_access_token,
        verify_ssl=False,
    )
    try:
        yield client
    finally:
        # Nested, so a failed disconnect cannot leave the HTTP client open.
        try:
            await websocket_manager.disconnect()
        finally:
            await client.close()


@pytest.fixture
async def read_only_client(
    core_client: HomeAssistantClient, hass_read_only_access_token: str
) -> AsyncIterator[HomeAssistantClient]:
    """The server's client on Core, with a non-admin user's token."""
    from ha_mcp.client.rest_client import HomeAssistantClient

    client = HomeAssistantClient(
        base_url=core_client.base_url,
        token=hass_read_only_access_token,
        verify_ssl=False,
    )
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
async def component_core_client(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """``core_client`` on a ``hass`` that also answers ``ha_mcp_tools/*``."""
    from custom_components.ha_mcp_tools import websocket_api

    websocket_api.async_register_commands(hass)
    return core_client
