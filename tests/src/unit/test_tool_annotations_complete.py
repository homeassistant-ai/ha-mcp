"""Every advertised tool declares all four MCP hints and a hand-written title.

Home Assistant 2026.10+ copies these into its LLM tool metadata and fills an
omitted hint with its least-safe default, so a read-only tool that leaves out
``destructiveHint`` is shown to Home Assistant as destructive.
"""

from typing import Any

import pytest

from ha_mcp._vendor.fastmcp import Client
from ha_mcp._vendor.fastmcp.tools.base import _default_title

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

_ALL_FLAGS = (
    "ENABLE_BETA_FEATURES",
    "ENABLE_CODE_MODE",
    "ENABLE_SECURITY_POLICY_TOOL",
    "ENABLE_YAML_CONFIG_EDITING",
    "HAMCP_ENABLE_DASHBOARD_SCREENSHOT",
    "HAMCP_ENABLE_DEV_MODE",
    "HAMCP_ENABLE_FILESYSTEM_TOOLS",
)


async def _list_tools(
    monkeypatch: pytest.MonkeyPatch, flags: tuple[str, ...]
) -> list[Any]:
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "unused")
    for flag in flags:
        monkeypatch.setenv(flag, "true")
    from ha_mcp.server import HomeAssistantSmartMCPServer

    async with Client(HomeAssistantSmartMCPServer().mcp) as client:
        return await client.list_tools()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "flags",
    [(), _ALL_FLAGS, ("ENABLE_TOOL_SEARCH",)],
    ids=["defaults", "all-flags", "tool-search"],
)
async def test_every_tool_declares_all_hints_and_a_title(
    monkeypatch: pytest.MonkeyPatch, flags: tuple[str, ...]
) -> None:
    tools = await _list_tools(monkeypatch, flags)
    assert tools
    missing = {}
    for tool in tools:
        declared = (
            tool.annotations.model_dump(by_alias=True, exclude_none=True)
            if tool.annotations
            else {}
        )
        gaps = [hint for hint in HINTS if hint not in declared]
        if not tool.title or tool.title == _default_title(tool.name):
            gaps.append("title")
        if gaps:
            missing[tool.name] = gaps
    assert not missing, f"tools with undeclared metadata: {missing}"


@pytest.mark.anyio
async def test_read_only_tools_are_not_advertised_as_destructive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = await _list_tools(monkeypatch, _ALL_FLAGS)
    contradictory = [
        tool.name
        for tool in tools
        if tool.annotations
        and tool.annotations.read_only_hint
        and tool.annotations.destructive_hint is not False
    ]
    assert not contradictory
