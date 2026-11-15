"""A mirrored tool call over loopback MCP and how its failures map (#1745).

The agent sees a ``HomeAssistantError`` for any failure to reach the server; a
genuine bug must surface as itself instead of being relabeled as one.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from mcp import McpError, types

from custom_components.ha_mcp_tools import llm_api

from ._llm_api_server import (
    ServedTool,
    call,
    mirrored_tool,
    payload,
    serve,
    unused_port,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")


async def test_a_call_passes_its_arguments_and_returns_the_servers_result(
    hass: HomeAssistant,
) -> None:
    tools = [
        ServedTool(
            result=types.CallToolResult(
                content=[types.TextContent(type="text", text="found it")]
            )
        )
    ]
    async with serve(tools) as srv:
        result = await call(hass, mirrored_tool(srv.port), {"query": "kitchen light"})

    assert srv.calls == [("ha_search", {"query": "kitchen light"})]
    assert result.error is False
    assert payload(result)["content"] == [{"type": "text", "text": "found it"}]


async def test_a_refused_connection_raises_homeassistanterror(
    hass: HomeAssistant,
) -> None:
    with pytest.raises(HomeAssistantError, match="ha_search"):
        await call(hass, mirrored_tool(unused_port()), {})


async def test_a_protocol_error_from_the_server_raises_homeassistanterror(
    hass: HomeAssistant,
) -> None:
    """JSON-RPC errors surface as McpError, which Core's own mcp integration
    maps the same way."""
    error = McpError(types.ErrorData(code=-32000, message="boom"))
    async with serve(list_error=error) as srv:
        with pytest.raises(HomeAssistantError, match="ha_search"):
            await call(hass, mirrored_tool(srv.port), {})

    assert srv.calls == []


async def test_a_malformed_answer_propagates_as_the_bug_it_is(
    hass: HomeAssistant,
) -> None:
    """The SDK's task groups wrap a genuine bug; mapping it to a friendly
    transport message would hide its traceback (#1782)."""
    async with serve(empty_list_answer=True) as srv:
        with pytest.raises(ExceptionGroup):
            await call(hass, mirrored_tool(srv.port), {})


async def test_a_slow_tool_call_times_out_as_homeassistanterror(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_api, "CALL_TOOL_TIMEOUT_SECONDS", 0.05)
    async with serve() as srv:
        srv.call_delay = 1.0
        with pytest.raises(HomeAssistantError, match="ha_search"):
            await call(hass, mirrored_tool(srv.port), {})
