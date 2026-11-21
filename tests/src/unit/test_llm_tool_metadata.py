"""Unit tests for the Core tool metadata the LLM API's tools declare (#1745).

Home Assistant reads tool titles, safety hints and ``llm.ToolResult``; the
tests compare against Core's own ``llm`` types. The tools come from a real MCP
server on loopback (``_llm_api_server``).
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from mcp import types

from custom_components.ha_mcp_tools.const import (
    DOMAIN,
    EXPOSURE_FULL,
    EXPOSURE_TOOL_SEARCH,
)

from ._llm_api_server import (
    ServedTool,
    api_instance,
    call,
    mirrored_tool,
    serve,
    tool_named,
    use_sdk_v2,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")


def _annotated(**hints: bool) -> ServedTool:
    return ServedTool(
        "ha_get_state", annotations={"title": "Get Entity State", **hints}
    )


class TestCoreToolMetadata:
    """Home Assistant reads tool titles, safety hints and ToolResult."""

    @pytest.mark.parametrize("sdk", ["v1", "v2"])
    async def test_mirrored_tool_carries_the_servers_metadata(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, sdk: str
    ) -> None:
        if sdk == "v2":
            use_sdk_v2(monkeypatch)
        hints = {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        async with serve([_annotated(**hints)]) as server:
            instance = await api_instance(hass, server.port, EXPOSURE_FULL)

        (tool,) = instance.tools
        assert tool.title == "Get Entity State"
        assert tool.integration == DOMAIN
        assert tool.annotations == llm.ToolAnnotations(
            read_only=True, destructive=False, idempotent=True, open_world=False
        )

    async def test_an_undeclared_hint_keeps_cores_default(
        self, hass: HomeAssistant
    ) -> None:
        async with serve([_annotated(readOnlyHint=True)]) as server:
            instance = await api_instance(hass, server.port, EXPOSURE_FULL)

        assert instance.tools[0].annotations == llm.ToolAnnotations(read_only=True)

    @pytest.mark.parametrize("is_error", [True, False])
    async def test_call_returns_a_tool_result_flagging_errors(
        self, hass: HomeAssistant, is_error: bool
    ) -> None:
        result = types.CallToolResult(
            content=[types.TextContent(type="text", text="x")], isError=is_error
        )
        async with serve([ServedTool("ha_search", result=result)]) as server:
            answer = await call(hass, mirrored_tool(server.port), {})

        assert answer.error is is_error
        assert answer.data["isError"] is is_error

    async def test_meta_tools_declare_their_own_safety(
        self, hass: HomeAssistant
    ) -> None:
        async with serve([ServedTool("ha_get_state")]) as server:
            instance = await api_instance(hass, server.port, EXPOSURE_TOOL_SEARCH)
            search = tool_named(instance, "ha_search_tools")
            dispatcher = tool_named(instance, "ha_call_tool")
            unknown = await call(hass, dispatcher, {"name": "ha_nope"})

        assert search.annotations == llm.ToolAnnotations(
            read_only=True, destructive=False, idempotent=True, open_world=False
        )
        # The dispatcher can run any exposed tool: Core's least-safe default.
        assert "annotations" not in vars(dispatcher)
        assert {search.integration, dispatcher.integration} == {DOMAIN}
        assert unknown.error is True
