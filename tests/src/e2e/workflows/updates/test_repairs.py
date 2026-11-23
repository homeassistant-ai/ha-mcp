"""Repairs issues through ha_manage_updates (ignore_repair, unignore_repair, fix_repair).

kitchen_sink (loaded by the E2E configuration) files fixable Repairs issues
when it sets up; the fix-flow walk uses one of them. The ignore payload itself
is pinned by tests/src/unit/test_updates_repairs.py, the fix flow's branches
by tests/src/unit/test_updates_repair_fix.py.
"""

from uuid import uuid4

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool


@pytest.mark.updates
@pytest.mark.parametrize("action", ["ignore_repair", "unignore_repair"])
async def test_unknown_repair_is_reported_per_item(mcp_client, action):
    """Home Assistant says it does not hold the issue, with its own code."""
    mcp = MCPAssertions(mcp_client)
    issue_id = f"missing_{uuid4().hex[:12]}"

    result = await mcp.call_tool_failure(
        "ha_manage_updates",
        {"action": action, "repairs": [{"domain": "sun", "issue_id": issue_id}]},
    )

    assert result["requested"] == 1
    assert result["failed"] == 1
    assert result["results"][0]["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert result["results"][0]["ha_error_code"] == "unknown_issue"


@pytest.mark.updates
async def test_repair_action_requires_repairs(mcp_client):
    mcp = MCPAssertions(mcp_client)
    await mcp.call_tool_failure(
        "ha_manage_updates",
        {"action": "ignore_repair"},
        expected_error="requires repairs",
    )


@pytest.mark.updates
async def test_a_fix_flow_walk_fixes_a_kitchen_sink_issue(mcp_client):
    """The first call returns Home Assistant's confirm step with its texts,
    the answer fixes the issue, and Home Assistant then holds no such issue
    to fix again."""
    mcp = MCPAssertions(mcp_client)
    repairs = [{"domain": "kitchen_sink", "issue_id": "out_of_blinker_fluid"}]
    flow_id = None
    try:
        step = await mcp.call_tool_success(
            "ha_manage_updates", {"action": "fix_repair", "repairs": repairs}
        )
        flow_id = step["flow_id"]
        assert step["flow_type"] == "form"
        assert step["step_id"] == "confirm"
        assert step["title"]
        assert step["description"]

        fixed = await mcp.call_tool_success(
            "ha_manage_updates",
            {
                "action": "fix_repair",
                "repairs": repairs,
                "flow_id": flow_id,
                "config": {},
            },
        )
        flow_id = None
        assert fixed["fixed"] is True

        again = await mcp.call_tool_failure(
            "ha_manage_updates", {"action": "fix_repair", "repairs": repairs}
        )
        assert again["status_code"] == 400
    finally:
        if flow_id is not None:
            await safe_call_tool(
                mcp_client,
                "ha_manage_updates",
                {
                    "action": "fix_repair",
                    "repairs": repairs,
                    "flow_id": flow_id,
                    "abort": True,
                },
            )
