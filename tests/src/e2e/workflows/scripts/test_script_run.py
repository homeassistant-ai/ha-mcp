"""Script start/stop through ha_config_set_script(run=...)."""

from uuid import uuid4

import pytest

from ...utilities.assertions import MCPAssertions
from ...utilities.wait_helpers import wait_for_entity_state


@pytest.mark.script
async def test_run_start_then_stop(mcp_client, cleanup_tracker):
    """run='start' runs the script and run='stop' stops that running execution."""
    mcp = MCPAssertions(mcp_client)
    script_id = f"run_control_{uuid4().hex[:12]}"
    cleanup_tracker.track("script", f"script.{script_id}")
    await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": script_id,
            "config": {
                "alias": f"Run control {script_id}",
                "sequence": [{"delay": {"seconds": 60}}],
                "mode": "single",
            },
        },
    )

    started = await mcp.call_tool_success(
        "ha_config_set_script",
        {"script_id": script_id, "run": "start", "variables": {"note": "e2e"}},
    )
    entity_id = started["entity_id"]
    assert started["action"] == "start"
    assert await wait_for_entity_state(mcp_client, entity_id, "on")

    stopped = await mcp.call_tool_success(
        "ha_config_set_script", {"script_id": script_id, "run": "stop"}
    )
    assert stopped["action"] == "stop"
    assert await wait_for_entity_state(mcp_client, entity_id, "off")


@pytest.mark.script
async def test_run_is_refused_alongside_a_config_write(mcp_client):
    """A run cannot be ordered after the script reload a write triggers."""
    mcp = MCPAssertions(mcp_client)
    script_id = f"run_control_{uuid4().hex[:12]}"

    await mcp.call_tool_failure(
        "ha_config_set_script",
        {
            "script_id": script_id,
            "config": {"sequence": [{"delay": {"seconds": 1}}]},
            "run": "start",
        },
        expected_error="cannot be combined",
    )
    await mcp.call_tool_failure(
        "ha_config_get_script",
        {"script_id": script_id},
        expected_error="not found",
    )
