"""
E2E: an integration entry's own WebSocket commands, the API its panel uses.

The test container has no KNX entry, so these run against the first entry
that is not ha-mcp's own: ha_get_integration lists only that domain's
commands, and ha_set_integration refuses a command outside the entry's
prefix before anything is sent. Sending a KNX command needs a KNX entry and
is covered by the unit tests.
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
        entry = await _first_entry(mcp)
        result = await mcp.call_tool_success(
            "ha_get_integration",
            {"entry_id": entry["entry_id"], "include_ws_commands": True},
        )
    data = result.get("data", result)
    prefix = f"{entry['domain']}/"
    assert isinstance(data["ws_commands"], list), data.get("warnings")
    for row in data["ws_commands"]:
        assert row["command"].startswith(prefix), row
        assert "description_complete" in row, row
    logger.info("%s lists %d ws_commands", entry["domain"], len(data["ws_commands"]))


@pytest.mark.asyncio
async def test_ws_command_outside_the_entrys_prefix_is_refused(mcp_client):
    async with MCPAssertions(mcp_client) as mcp:
        entry = await _first_entry(mcp)
        await mcp.call_tool_failure(
            "ha_set_integration",
            {"entry_id": entry["entry_id"], "ws_command": "auth/current_user"},
            expected_error=f"own '{entry['domain']}/' commands",
        )
