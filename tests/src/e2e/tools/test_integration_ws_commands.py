"""
E2E: an integration entry's own WebSocket commands, the API its panel uses.

The test container has no KNX entry. The listing runs against its
shopping_list entry, whose integration registers shopping_list/* commands,
so the list cannot pass empty, and Core's own rejection of a call that does
not match a command's schema runs against the same entry;
ha_set_integration's refusal of a command outside the entry's prefix runs
against the first entry that is not ha-mcp's own. Sending a KNX command
needs a KNX entry and is covered by the unit tests.
"""

import logging
from typing import Any

import pytest

from ..utilities.assertions import MCPAssertions

logger = logging.getLogger(__name__)


async def _first_entry(mcp: MCPAssertions) -> dict[str, Any]:
    listing = await mcp.call_tool_success("ha_get_integration", {})
    entries = [
        entry
        for entry in listing.get("data", listing).get("entries", [])
        if entry.get("domain") != "ha_mcp_tools"
    ]
    if not entries:
        pytest.skip("no config entries besides ha-mcp's own in the test container")
    return entries[0]


@pytest.mark.asyncio
@pytest.mark.requires_component_surface
async def test_include_ws_commands_lists_only_the_entrys_own_commands(mcp_client):
    async with MCPAssertions(mcp_client) as mcp:
        listing = await mcp.call_tool_success(
            "ha_get_integration", {"domain": "shopping_list"}
        )
        entries = listing.get("data", listing).get("entries", [])
        assert entries, "the test container has no shopping_list entry"
        result = await mcp.call_tool_success(
            "ha_get_integration",
            {"entry_id": entries[0]["entry_id"], "include_ws_commands": True},
        )
    data = result.get("data", result)
    commands = data.get("ws_commands")
    assert isinstance(commands, list), data.get("warnings")
    assert "shopping_list/items" in [row["command"] for row in commands], commands
    for row in commands:
        assert row["command"].startswith("shopping_list/"), row
        assert "description_complete" in row, row
    logger.info("shopping_list lists %d ws_commands", len(commands))


@pytest.mark.asyncio
async def test_cores_schema_rejection_of_ws_data_is_returned(mcp_client):
    async with MCPAssertions(mcp_client) as mcp:
        listing = await mcp.call_tool_success(
            "ha_get_integration", {"domain": "shopping_list"}
        )
        entries = listing.get("data", listing).get("entries", [])
        assert entries, "the test container has no shopping_list entry"
        await mcp.call_tool_failure(
            "ha_set_integration",
            {
                "entry_id": entries[0]["entry_id"],
                "ws_command": "shopping_list/items",
                "ws_data": {"bogus": 1},
            },
            expected_error="extra keys not allowed",
        )


@pytest.mark.asyncio
async def test_ws_command_outside_the_entrys_prefix_is_refused(mcp_client):
    async with MCPAssertions(mcp_client) as mcp:
        entry = await _first_entry(mcp)
        await mcp.call_tool_failure(
            "ha_set_integration",
            {"entry_id": entry["entry_id"], "ws_command": "auth/current_user"},
            expected_error=f"own '{entry['domain']}/' commands",
        )
