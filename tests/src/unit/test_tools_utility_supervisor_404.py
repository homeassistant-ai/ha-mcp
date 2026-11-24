"""What ``ha_get_logs(source='system_service')`` suggests for a 404.

A 404 means different things depending on who answered. Inside the app
(add-on) the request went straight to Supervisor, so the service is not
exposed there. Off-host it went to Core: with Core's ``hassio`` proxy loaded,
Supervisor answered behind it; without it, the install is not managed by
Supervisor; and when Core cannot say which (the supervision probe failed),
the tool must not claim the instance is unmanaged.

The supervised test runs Core's own ``hassio`` integration and records the
request its proxy sent upstream.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.mcp.types import TextContent
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_logs import register_logs_tools

from ._gateway import Gateway
from ._gateway import gateway as gateway
from ._gateway import gateway_client as gateway_client
from ._supervisor_logs_core import Supervisor
from ._supervisor_logs_core import core_proxy as core_proxy

NOT_EXPOSED = "not expose it on this HA OS version"
UNMANAGED = "this instance is not managed by Supervisor"
UNCONFIRMED = "could not be confirmed"


@pytest.fixture
async def supervisor(aiohttp_client: Any) -> str:
    """A Supervisor that serves no log route: every request answers 404."""
    server = await aiohttp_client(web.Application())
    return f"{server.host}:{server.port}"


async def _core_service_404_suggestions(client: HomeAssistantClient) -> list[str]:
    mcp = FastMCP("system-service-404")
    register_logs_tools(mcp, client)
    async with Client(mcp) as mcp_client:
        result = await mcp_client.call_tool(
            "ha_get_logs",
            {"source": "system_service", "slug": "core"},
            raise_on_error=False,
        )
    assert result.is_error
    content = result.content[0]
    assert isinstance(content, TextContent)
    payload = json.loads(content.text)
    assert payload["error"]["code"] == "RESOURCE_NOT_FOUND"
    suggestions: list[str] = payload["error"]["suggestions"]
    return suggestions


async def test_app_404_names_the_os_version_without_probing_core(
    supervisor: str,
    gateway: Gateway,
    gateway_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside the app Supervisor answered itself; Core is not asked."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
    monkeypatch.setenv("SUPERVISOR_BASE_URL", f"http://{supervisor}")

    suggestions = await _core_service_404_suggestions(gateway_client)

    assert any(NOT_EXPOSED in s for s in suggestions)
    assert "GET /api/config" not in gateway.requests


async def test_supervised_404_names_the_os_version(
    core_proxy: Supervisor, core_client: HomeAssistantClient
) -> None:
    core_proxy.reply(status=404)

    suggestions = await _core_service_404_suggestions(core_client)

    assert any(NOT_EXPOSED in s for s in suggestions)
    assert not any(UNMANAGED in s for s in suggestions)
    assert any(request.path.startswith("/core/logs") for request in core_proxy.requests)


async def test_unsupervised_404_names_the_install_type(
    gateway: Gateway, gateway_client: HomeAssistantClient
) -> None:
    """Core has no ``hassio``, so its probe confirms the install is unmanaged."""
    suggestions = await _core_service_404_suggestions(gateway_client)

    assert any(UNMANAGED in s for s in suggestions)
    assert "GET /api/config" in gateway.requests


async def test_failed_probe_404_does_not_claim_the_install_is_unmanaged(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    # Core's /api/config fails, so the probe cannot tell either way.
    with patch.object(
        hass.config, "as_dict", side_effect=HomeAssistantError("config unavailable")
    ):
        suggestions = await _core_service_404_suggestions(core_client)

    assert any(UNCONFIRMED in s for s in suggestions)
    assert not any(UNMANAGED in s for s in suggestions)
