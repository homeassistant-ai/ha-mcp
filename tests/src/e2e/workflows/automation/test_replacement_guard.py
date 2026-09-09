"""Prevent accidental replacement while preserving intentional edits (#2407)."""

from uuid import uuid4

import pytest

from ...utilities.assertions import MCPAssertions


@pytest.mark.automation
async def test_existing_automation_alias_change_requires_read(
    mcp_client, cleanup_tracker
):
    """A blocked replacement leaves the first config intact; a fresh hash allows it."""
    mcp = MCPAssertions(mcp_client)
    identifier = f"issue_2407_{uuid4().hex}"
    config = {
        "alias": f"Issue 2407 A {identifier}",
        "initial_state": False,
        "triggers": [{"trigger": "event", "event_type": identifier}],
        "actions": [{"stop": "Throwaway A"}],
    }
    # Custom-ID creation is an existing capability and must remain available.
    cleanup_tracker.track("automation", identifier)
    await mcp.call_tool_success(
        "ha_config_set_automation", {"identifier": identifier, "config": config}
    )
    before = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": identifier}
    )
    replacement = {
        **config,
        "alias": f"Issue 2407 B {identifier}",
        "actions": [{"stop": "Throwaway B"}],
    }
    blocked = await mcp.call_tool_failure(
        "ha_config_set_automation",
        {"identifier": identifier, "config": replacement},
        expected_error="already exists",
    )
    assert blocked["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    after = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": identifier}
    )
    assert after["config"] == before["config"]
    assert after["config_hash"] == before["config_hash"]

    # Intentionally renaming/replacing the target remains possible after reading it.
    await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "identifier": identifier,
            "config": replacement,
            "config_hash": after["config_hash"],
        },
    )
    renamed = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": identifier}
    )
    assert renamed["config"]["id"] == identifier
    assert renamed["config"]["alias"] == replacement["alias"]
    assert renamed["config"]["actions"] == replacement["actions"]
