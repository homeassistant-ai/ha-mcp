"""Unit tests for the Core tool metadata the LLM API's tools declare (#1745).

Home Assistant reads tool titles, safety hints and ``llm.ToolResult``; the
tests compare against Core's own ``llm`` types.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from custom_components.ha_mcp_tools import llm_api
from custom_components.ha_mcp_tools.const import (
    DOMAIN,
    EXPOSURE_TOOL_SEARCH,
)

from ._llm_api_helpers import (
    fake_session,
    llm_context,
    make_api,
    tool_entry,
)


def _annotated_entry(sdk: str, **hints: bool) -> SimpleNamespace:
    """A stamped tool entry whose annotations come from a real SDK model."""
    if sdk == "v1":
        from mcp import types as sdk_types

        annotations = sdk_types.ToolAnnotations(title="Get Entity State", **hints)
    else:
        from ha_mcp._vendor.mcp import types as vendored_types

        annotations = vendored_types.ToolAnnotations.model_validate(
            {"title": "Get Entity State", **hints}
        )
    entry = tool_entry("ha_get_state")
    entry.annotations = annotations
    return entry


class TestCoreToolMetadata:
    """Home Assistant 2026.10 reads tool titles, safety hints and ToolResult."""

    @pytest.mark.parametrize("sdk", ["v1", "v2"])
    async def test_mirrored_tool_carries_the_servers_metadata(
        self,
        hass: HomeAssistant,
        monkeypatch: pytest.MonkeyPatch,
        sdk: str,
    ) -> None:
        hints = {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        fake_session(monkeypatch, tools=[_annotated_entry(sdk, **hints)])

        instance = await make_api(hass).async_get_api_instance(llm_context())

        (tool,) = instance.tools
        assert tool.title == "Get Entity State"
        assert tool.integration == DOMAIN
        assert tool.annotations == llm.ToolAnnotations(
            read_only=True, destructive=False, idempotent=True, open_world=False
        )

    async def test_an_undeclared_hint_keeps_cores_default(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_session(monkeypatch, tools=[_annotated_entry("v2", readOnlyHint=True)])

        instance = await make_api(hass).async_get_api_instance(llm_context())

        assert instance.tools[0].annotations == llm.ToolAnnotations(read_only=True)

    @pytest.mark.parametrize("is_error", [True, False])
    async def test_call_returns_a_tool_result_flagging_errors(
        self,
        hass: HomeAssistant,
        monkeypatch: pytest.MonkeyPatch,
        is_error: bool,
    ) -> None:
        from ha_mcp._vendor.mcp.types import CallToolResult, TextContent

        fake_session(
            monkeypatch,
            call_result=CallToolResult(
                content=[TextContent(type="text", text="x")], is_error=is_error
            ),
        )
        tool = llm_api.HaMcpTool(
            "ha_search", "Search", {}, "http://127.0.0.1:9584/private_x"
        )

        result = await tool.async_call(
            hass,
            llm_api.llm.ToolInput("ha_search", {}),
            llm_context(),
        )

        assert isinstance(result, llm.ToolResult)
        assert result.error is is_error
        assert result.data["isError"] is is_error

    async def test_meta_tools_declare_their_own_safety(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_session(monkeypatch, tools=[tool_entry("ha_get_state")])
        instance = await make_api(
            hass, mode=EXPOSURE_TOOL_SEARCH
        ).async_get_api_instance(llm_context())
        search, call = instance.tools

        assert search.annotations == llm.ToolAnnotations(
            read_only=True, destructive=False, idempotent=True, open_world=False
        )
        # The dispatcher can run any exposed tool: Core's least-safe default.
        assert "annotations" not in vars(call)
        assert {search.integration, call.integration} == {DOMAIN}

        unknown = await call.async_call(
            hass,
            llm_api.llm.ToolInput("ha_call_tool", {"name": "ha_nope"}),
            llm_context(),
        )
        assert unknown.error is True

    async def test_older_core_gets_plain_results_and_no_annotations(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delattr(llm_api.llm, "ToolResult", raising=False)
        monkeypatch.delattr(llm_api.llm, "ToolAnnotations", raising=False)
        fake_session(monkeypatch, tools=[_annotated_entry("v2", readOnlyHint=True)])

        instance = await make_api(hass).async_get_api_instance(llm_context())

        assert "annotations" not in vars(instance.tools[0])
        assert instance.tools[0].title == "Get Entity State"
