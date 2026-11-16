"""The app (add-on) proxies when Home Assistant is served over TLS.

Core is served with a self-signed certificate (``_tls_core.py``) in front of
the stand-in Supervisor (``_tools_addons_core.py``). With verification off
(``HA_VERIFY_SSL=false``) the proxies must reach the app through it; a
connection to the app's own container stays plain whatever Home Assistant's
scheme is.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import websocket_manager

from ._core_client import free_port
from ._tls_core import tls_core
from ._tools_addons_core import SLUG, Route, Supervisor, manage
from ._tools_addons_core import app_route as app_route
from ._tools_addons_core import core_supervisor as core_supervisor
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app


@pytest.fixture
async def unverified_client(
    hass: HomeAssistant,
    core_supervisor: Supervisor,
    hass_access_token: str,
    tmp_path: Path,
    socket_enabled: None,
) -> AsyncIterator[HomeAssistantClient]:
    """The server's client of a self-signed Core, verification off."""
    async with tls_core(hass, tmp_path) as url:
        client = HomeAssistantClient(
            base_url=url, token=hass_access_token, verify_ssl=False
        )
        try:
            yield client
        finally:
            try:
                await websocket_manager.disconnect()
            finally:
                await client.close()


@pytest.mark.parametrize("websocket", [False, True], ids=["http", "websocket"])
async def test_with_verification_off_a_self_signed_home_assistant_is_proxied(
    core_supervisor: Supervisor,
    unverified_client: HomeAssistantClient,
    websocket: bool,
) -> None:
    core_supervisor.running()
    core_supervisor.target.frames = ["hello"]

    result = await manage(unverified_client, slug=SLUG, path="/x", websocket=websocket)

    assert result["success"] is True
    [seen] = core_supervisor.target.requests
    # Core reports the scheme the request reached it over.
    assert seen.headers["X-Forwarded-Proto"] == "https"


async def test_a_direct_port_websocket_stays_plain_beside_a_tls_home_assistant(
    core_supervisor: Supervisor, unverified_client: HomeAssistantClient
) -> None:
    core_supervisor.running()
    core_supervisor.target.frames = ["hello"]

    result = await manage(
        unverified_client,
        slug=SLUG,
        path="/ws",
        websocket=True,
        port=core_supervisor.target.port,
    )

    assert result["messages"] == ["hello"]


async def test_inside_the_app_an_https_home_assistant_leaves_the_app_websocket_plain(
    app_route: Route,
) -> None:
    """Inside the app the connection goes to the app's container, never Core."""
    app_route.supervisor.running()
    app_route.target.frames = ["hello"]
    client = HomeAssistantClient(
        base_url=f"https://127.0.0.1:{free_port()}", token="unused", verify_ssl=True
    )
    try:
        result = await manage(client, slug=SLUG, path="/ws", websocket=True)
    finally:
        await client.close()

    assert result["messages"] == ["hello"]
