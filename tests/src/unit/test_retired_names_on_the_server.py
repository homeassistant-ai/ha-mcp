"""A retired call on the server itself, connected to a real ``hass``.

The alias middleware and the search proxy each take the server's client to
have Core judge what a retired call's current tool drops. Their own tests
build them with a client of their own; this one calls through the server.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

import ha_mcp.server as server_module
from ha_mcp._vendor.fastmcp import Client
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import reset_global_settings

from ._tool_errors import error_of


@asynccontextmanager
async def _no_lifespan(_server: Any) -> AsyncIterator[dict[str, Any]]:
    yield {}


@pytest.fixture
async def server(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[server_module.HomeAssistantSmartMCPServer]:
    """The search-catalog server on ``core_client``; the lifespan is replaced
    so its startup checks stay out of the call."""
    for domain in ("config", "homeassistant"):
        assert await async_setup_component(hass, domain, {})
    er.async_get(hass).async_get_or_create(
        "light", "demo", "lamp", suggested_object_id="lamp"
    )
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "true")
    monkeypatch.setenv("PINNED_TOOLS", "")
    monkeypatch.setenv("DISABLED_TOOLS", "")
    monkeypatch.setattr(server_module, "server_lifespan", _no_lifespan)
    reset_global_settings()
    try:
        yield server_module.HomeAssistantSmartMCPServer(client=core_client)
    finally:
        reset_global_settings()


_ARGUMENTS = {"entity_id": "light.lamp", "assistant": "bogus"}


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        pytest.param("ha_get_entity_exposure", _ARGUMENTS, id="by_name"),
        pytest.param(
            "ha_call_read_tool",
            {"name": "ha_get_entity_exposure", "arguments": _ARGUMENTS},
            id="proxy",
        ),
    ],
)
async def test_the_server_has_core_judge_a_retired_assistant(
    server: server_module.HomeAssistantSmartMCPServer,
    tool: str,
    arguments: dict[str, Any],
) -> None:
    async with Client(server.mcp) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(tool, arguments)

    assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
