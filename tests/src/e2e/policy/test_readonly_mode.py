"""Real e2e tests for Read Only Mode (#1569).

Boots a fresh in-process ha-mcp server with ``READ_ONLY_MODE=true``
against the testcontainer HA (function-scoped — the session-scoped
``mcp_client`` boots without the flag) and verifies the full contract:

- write-capable tools disappear from ``tools/list``;
- pure read tools keep working;
- exempt mixed read/write tools stay listed, their read actions work,
  and their write actions return the structured READ_ONLY_MODE error;
- direct calls to hidden write tools return the same error;
- with tool search enabled, proxy-dispatched writes are blocked too;
- ``ha_get_overview`` reports the mode to the LLM;
- the real catalog satisfies the invariant that every mandatory tool is
  either read-safe or exempt (the unit suite can only check this
  against fakes).

Cannot run on Termux (no Docker for testcontainers); CI-only verification.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_constants import TEST_TOKEN

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.read_only import READ_ONLY_EXEMPT_TOOLS, is_read_safe
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.settings_ui import MANDATORY_TOOLS
from ha_mcp.utils.data_paths import get_data_dir

from ..utilities.assertions import parse_mcp_result, tool_error_to_result


async def _expect_read_only_blocked(
    client: Client, tool: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Call ``tool`` and return the parsed READ_ONLY_MODE error body.

    Accept both the raised-ToolError and isError-result transports (see
    test_approval_flow._expect_blocked for the rationale).
    """
    try:
        result = await client.call_tool(tool, args)
    except ToolError as exc:
        body = tool_error_to_result(exc)
    else:
        body = parse_mcp_result(result)
    assert body.get("error", {}).get("code") == "READ_ONLY_MODE", body
    return body


async def _build_readonly_server(
    container_info, monkeypatch, tmp_path, *, extra_env: dict[str, str] | None = None
):
    if container_info.get("backend") == "haos_inaddon":
        pytest.skip(
            "Inaddon backend uses the addon's own MCP endpoint; this test "
            "needs an in-process server with READ_ONLY_MODE=true."
        )

    monkeypatch.setenv("READ_ONLY_MODE", "true")
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    get_data_dir.cache_clear()

    # Reset cached settings so the new server picks up the env vars.
    import ha_mcp.config

    monkeypatch.setattr(ha_mcp.config, "_settings", None)

    base_url = container_info["base_url"]
    token = container_info.get("token", TEST_TOKEN)
    ha_client = HomeAssistantClient(base_url=base_url, token=token)
    server = HomeAssistantSmartMCPServer(client=ha_client)
    return server, ha_client


@pytest.fixture
async def readonly_mcp(ha_container_with_fresh_config, monkeypatch, tmp_path):
    """Read-only server with the default full catalog (tool search off)."""
    server, ha_client = await _build_readonly_server(
        ha_container_with_fresh_config, monkeypatch, tmp_path
    )
    client = Client(server.mcp)
    async with client:
        yield client, server
    await ha_client.close()
    get_data_dir.cache_clear()


@pytest.fixture
async def readonly_toolsearch_mcp(
    ha_container_with_fresh_config, monkeypatch, tmp_path
):
    """Read-only server with tool search on — exercises proxy dispatch."""
    server, ha_client = await _build_readonly_server(
        ha_container_with_fresh_config,
        monkeypatch,
        tmp_path,
        extra_env={"ENABLE_TOOL_SEARCH": "true"},
    )
    client = Client(server.mcp)
    async with client:
        yield client, server
    await ha_client.close()
    get_data_dir.cache_clear()


@pytest.mark.asyncio
async def test_write_tools_hidden_exempt_and_read_tools_listed(readonly_mcp):
    client, _server = readonly_mcp
    tools = await client.list_tools()
    names = {t.name for t in tools}

    # Pure write tools are gone from the catalog.
    for write_tool in ("ha_config_set_scene", "ha_restart", "ha_call_service"):
        assert write_tool not in names, f"{write_tool} should be hidden"

    # Pure read tools and always-registered exempt mixed tools remain.
    for kept in (
        "ha_get_state",
        "ha_search",
        "ha_manage_backup",
        "ha_manage_pipeline",
        "ha_manage_energy_prefs",
    ):
        assert kept in names, f"{kept} should stay listed"

    # Everything still listed is either annotated read-only or exempt.
    for tool in tools:
        assert is_read_safe(tool) or tool.name in READ_ONLY_EXEMPT_TOOLS, (
            f"{tool.name} is write-capable but survived the catalog filter"
        )


@pytest.mark.asyncio
async def test_real_catalog_mandatory_tools_stay_available(readonly_mcp):
    """Every MANDATORY tool must be read-safe or exempt — otherwise
    read-only mode would block a tool the settings UI refuses to
    disable. Run against the REAL registered catalog so a future
    mandatory write tool fails here at PR time."""
    _client, server = readonly_mcp
    catalog = await server.mcp.local_provider._list_tools()
    by_name = {t.name: t for t in catalog}
    for name in MANDATORY_TOOLS:
        tool = by_name.get(name)
        assert tool is not None, f"mandatory tool {name} not registered"
        assert is_read_safe(tool) or name in READ_ONLY_EXEMPT_TOOLS, (
            f"mandatory tool {name} is write-capable but not exempt — "
            "read-only mode would dead-end it"
        )
    # Exempt names must reference real tools (typo guard). Feature-gated
    # tools are only registered when their flag is on.
    feature_gated = {"ha_manage_custom_tool"}
    for name in READ_ONLY_EXEMPT_TOOLS:
        if name in feature_gated:
            continue
        assert name in by_name, f"exempt tool {name} not found in catalog"


@pytest.mark.asyncio
async def test_read_tools_still_work(readonly_mcp):
    client, _server = readonly_mcp
    result = await client.call_tool("ha_search", {"query": "light"})
    body = parse_mcp_result(result)
    assert body.get("success") is True, body


@pytest.mark.asyncio
async def test_overview_reports_read_only_mode(readonly_mcp):
    client, _server = readonly_mcp
    result = await client.call_tool("ha_get_overview", {})
    body = parse_mcp_result(result)
    assert body.get("read_only_mode") is True, body
    assert "Read Only Mode is ON" in body.get("read_only_mode_hint", ""), body


@pytest.mark.asyncio
async def test_direct_write_tool_call_blocked(readonly_mcp):
    client, _server = readonly_mcp
    body = await _expect_read_only_blocked(
        client,
        "ha_call_service",
        {"domain": "light", "service": "turn_on", "entity_id": "light.bed_light"},
    )
    assert body["tool_name"] == "ha_call_service"
    # The error must point the user at the toggle.
    assert "Read Only Mode" in body["error"]["message"]


@pytest.mark.asyncio
async def test_exempt_tool_read_action_works(readonly_mcp):
    client, _server = readonly_mcp
    result = await client.call_tool(
        "ha_manage_backup", {"scope": "edits", "action": "list"}
    )
    body = parse_mcp_result(result)
    assert body.get("success") is True, body


@pytest.mark.asyncio
async def test_exempt_tool_write_action_blocked(readonly_mcp):
    client, _server = readonly_mcp
    body = await _expect_read_only_blocked(
        client, "ha_manage_backup", {"scope": "snapshot", "action": "create"}
    )
    assert body["blocked_operation"], body
    # The error teaches what remains available on this tool.
    assert "list" in body["error"]["message"], body


@pytest.mark.asyncio
async def test_proxy_dispatched_write_blocked_with_tool_search(readonly_toolsearch_mcp):
    """ha_call_write_tool re-dispatches through the middleware chain, so
    the inner call must hit the read-only blocker even though the proxy
    itself passes through."""
    client, _server = readonly_toolsearch_mcp
    try:
        result = await client.call_tool(
            "ha_call_write_tool",
            {
                "name": "ha_set_zone",
                "arguments": {
                    "name": "ro_test_zone",
                    "latitude": 1.0,
                    "longitude": 1.0,
                },
            },
        )
    except ToolError as exc:
        body = tool_error_to_result(exc)
    else:
        body = parse_mcp_result(result)
    assert body.get("error", {}).get("code") == "READ_ONLY_MODE", body
