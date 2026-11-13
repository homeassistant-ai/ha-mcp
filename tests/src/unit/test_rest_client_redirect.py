"""A Home Assistant URL that a proxy redirects is not a reached Home Assistant.

The client does not follow redirects: a write must not be replayed as a GET,
and the token must not follow a ``Location`` header to another host. A 3xx
used to pass as an empty success, so ``ha_call_service`` reported
"Successfully executed" for a call that never reached Home Assistant. The
proxy below answers like nginx's ``return 301 https://$host$request_uri;`` in
front of the real ``hass``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web
from homeassistant.core import HomeAssistant, ServiceCall

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_service import ServiceTools


@pytest.fixture
def calls(hass: HomeAssistant) -> list[ServiceCall]:
    """``test.record``: a service that records each call Home Assistant runs."""
    received: list[ServiceCall] = []

    async def record(call: ServiceCall) -> None:
        received.append(call)

    hass.services.async_register("test", "record", record)
    return received


@pytest.fixture
async def redirected_client(
    core_client: HomeAssistantClient, aiohttp_server: Any
) -> AsyncIterator[HomeAssistantClient]:
    """The server's client configured with a URL a proxy redirects to Core."""

    async def redirect(request: web.Request) -> web.StreamResponse:
        raise web.HTTPMovedPermanently(core_client.base_url + request.path_qs)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", redirect)
    proxy = await aiohttp_server(app)
    client = HomeAssistantClient(
        base_url=str(proxy.make_url("")).rstrip("/"),
        token=core_client.token,
        verify_ssl=False,
    )
    try:
        yield client
    finally:
        await client.close()


async def test_a_call_to_home_assistants_own_url_runs_the_service(
    core_client: HomeAssistantClient, calls: list[ServiceCall]
) -> None:
    await ServiceTools(core_client, None).ha_call_service(
        domain="test", service="record", wait=False
    )

    assert len(calls) == 1


async def test_a_redirected_call_is_a_connection_failure_not_a_success(
    core_client: HomeAssistantClient,
    redirected_client: HomeAssistantClient,
    calls: list[ServiceCall],
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await ServiceTools(redirected_client, None).ha_call_service(
            domain="test", service="record", wait=False
        )

    error = json.loads(exc_info.value.args[0])["error"]
    assert error["code"] == "CONNECTION_FAILED"
    assert core_client.base_url in error["message"]
    assert calls == []
