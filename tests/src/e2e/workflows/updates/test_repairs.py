"""Repairs issues through ha_manage_updates(action='ignore_repair' / 'unignore_repair').

The test Home Assistant has no integration that files a Repairs issue on
demand (see test_call_service_ws_command.py), so the live coverage here is the
issue-list lookup and the per-item not-found verdict. The ignore payload
itself is pinned by tests/src/unit/test_updates_repairs.py.
"""

from uuid import uuid4

import pytest

from ...utilities.assertions import MCPAssertions


@pytest.mark.updates
@pytest.mark.parametrize("action", ["ignore_repair", "unignore_repair"])
async def test_unknown_repair_is_reported_not_found(mcp_client, action):
    mcp = MCPAssertions(mcp_client)
    issue_id = f"missing_{uuid4().hex[:12]}"

    result = await mcp.call_tool_failure(
        "ha_manage_updates",
        {"action": action, "repairs": [{"domain": "sun", "issue_id": issue_id}]},
    )

    assert result["requested"] == 1
    assert result["failed"] == 1
    assert result["results"][0]["error"]["code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.updates
async def test_repair_action_requires_repairs(mcp_client):
    mcp = MCPAssertions(mcp_client)
    await mcp.call_tool_failure(
        "ha_manage_updates",
        {"action": "ignore_repair"},
        expected_error="requires repairs",
    )
