"""Real e2e tests for manage-tool routing through the search call proxies (#2358).

Boots a fresh in-process ha-mcp server with ``ENABLE_TOOL_SEARCH=true``
against the testcontainer HA (function-scoped — the session-scoped
``mcp_client`` boots without the flag) and verifies, against the real
registered catalog, that a ``manage`` tool is reachable from every proxy:

- search results list every proxy a manage tool can be reached through;
- its read actions run through ``ha_call_read_tool``;
- its write actions are refused there and redirected to the write proxy;
- ``ha_call_delete_tool`` runs the whole tool, reads included.

The unit suite covers the dispatch closure with a synthetic catalog; this
file pins that the real tools carry the annotations and read-only
predicates the routing relies on. Requires Docker (testcontainers).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_constants import TEST_TOKEN

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.utils.data_paths import get_data_dir

from ..utilities.assertions import (
    MCPAssertions,
    parse_mcp_result,
    tool_error_to_result,
)


async def _call(client: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call ``tool`` and return the parsed body from either transport.

    Accept both the raised-ToolError and isError-result transports (see
    test_approval_flow._expect_blocked for the rationale).
    """
    try:
        result = await client.call_tool(tool, args)
    except ToolError as exc:
        return tool_error_to_result(exc)
    return parse_mcp_result(result)


def _expect_wrong_proxy(body: dict[str, Any], *, correct_proxy: str) -> None:
    error = body.get("error") or {}
    assert error.get("code") == "VALIDATION_INVALID_PARAMETER", body
    assert body.get("correct_proxy") == correct_proxy, body


@pytest.fixture
async def toolsearch_mcp(ha_container_with_fresh_config, monkeypatch, tmp_path):
    """In-process server with tool search on and no other mode flag."""
    if ha_container_with_fresh_config.get("backend") == "haos_inaddon":
        pytest.skip(
            "Inaddon backend uses the addon's own MCP endpoint; this test "
            "needs an in-process server with ENABLE_TOOL_SEARCH=true."
        )

    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "true")
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    get_data_dir.cache_clear()

    # Reset cached settings so the new server picks up the env vars.
    import ha_mcp.config

    monkeypatch.setattr(ha_mcp.config, "_settings", None)

    ha_client = HomeAssistantClient(
        base_url=ha_container_with_fresh_config["base_url"],
        token=ha_container_with_fresh_config.get("token", TEST_TOKEN),
    )
    server = HomeAssistantSmartMCPServer(client=ha_client)
    client = Client(server.mcp)
    async with client:
        yield client
    await ha_client.close()
    get_data_dir.cache_clear()


@pytest.mark.asyncio
async def test_search_result_lists_every_proxy_for_a_manage_tool(toolsearch_mcp):
    """The energy tool has approved read actions, so its hint names all
    three proxies with the read route first."""
    body = parse_mcp_result(
        await toolsearch_mcp.call_tool(
            "ha_search_tools", {"query": "energy dashboard preferences"}
        )
    )
    entries = body if isinstance(body, list) else []
    if isinstance(body, dict):
        for key in ("tools", "results", "matches"):
            entries.extend(body.get(key) or [])
    hints = {
        entry["name"]: entry.get("execute_via", "")
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    assert "ha_manage_energy_prefs" in hints, sorted(hints)

    hint = hints["ha_manage_energy_prefs"]
    assert hint.startswith("Read actions: "), hint
    assert 'ha_call_read_tool(name="ha_manage_energy_prefs"' in hint
    assert 'ha_call_write_tool(name="ha_manage_energy_prefs"' in hint
    assert 'ha_call_delete_tool(name="ha_manage_energy_prefs"' in hint


@pytest.mark.asyncio
async def test_read_proxy_runs_a_manage_tools_read_action(toolsearch_mcp):
    body = await _call(
        toolsearch_mcp,
        "ha_call_read_tool",
        {"name": "ha_manage_energy_prefs", "arguments": {"mode": "get"}},
    )
    assert body.get("success") is True, body


@pytest.mark.asyncio
async def test_read_proxy_refuses_a_manage_tools_write_action(toolsearch_mcp):
    """The read proxy is the hard boundary: a write action never reaches
    the tool, so the tool's own argument validation never runs here."""
    async with MCPAssertions(toolsearch_mcp) as mcp:
        body = await mcp.call_tool_failure(
            "ha_call_read_tool",
            {
                "name": "ha_manage_energy_prefs",
                "arguments": {"mode": "set", "config": {}},
            },
        )
    _expect_wrong_proxy(body, correct_proxy="ha_call_write_tool")


@pytest.mark.asyncio
async def test_delete_proxy_runs_a_manage_tools_write_action(toolsearch_mcp):
    """Restoring the built-in theme is a real write with no lasting effect
    on a fresh container, so it proves the delete proxy dispatched the call."""
    body = await _call(
        toolsearch_mcp,
        "ha_call_delete_tool",
        {
            "name": "ha_manage_theme",
            "arguments": {"action": "set", "theme_name": "default"},
        },
    )
    assert body.get("success") is True, body


@pytest.mark.asyncio
async def test_delete_proxy_runs_a_manage_tools_read_action(toolsearch_mcp):
    """No per-call check on the destructive proxies: a read goes through."""
    body = await _call(
        toolsearch_mcp,
        "ha_call_delete_tool",
        {"name": "ha_manage_energy_prefs", "arguments": {"mode": "get"}},
    )
    assert body.get("success") is True, body
