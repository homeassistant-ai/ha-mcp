"""Scene activation through ha_config_set_scene(activate=True)."""

from uuid import uuid4

import pytest

from ...utilities.assertions import MCPAssertions


@pytest.mark.cleanup
async def test_activate_with_a_write_and_alone(mcp_client, cleanup_tracker):
    """activate follows the scene reload of a write, and works on its own."""
    mcp = MCPAssertions(mcp_client)
    scene_id = f"activate_{uuid4().hex[:12]}"
    cleanup_tracker.track("scene", f"scene.{scene_id}")
    config = {
        "name": f"E2E activate {scene_id}",
        "entities": {"light.bed_light": {"state": "on", "brightness": 120}},
    }

    created = await mcp.call_tool_success(
        "ha_config_set_scene",
        {"scene_id": scene_id, "config": config, "activate": True},
    )
    assert created["activated"] is True
    assert not any("scene reload" in w for w in created.get("warnings", []))

    activated = await mcp.call_tool_success(
        "ha_config_set_scene", {"scene_id": scene_id, "activate": True}
    )
    assert activated["action"] == "activate"
    assert activated["activated"] is True
    state = await mcp.call_tool_success(
        "ha_get_state", {"entity_id": activated["entity_id"], "fields": ["state"]}
    )
    # A scene's state is the timestamp of its last activation.
    assert state["data"]["state"] not in ("unknown", "unavailable")


async def test_activate_unknown_scene_is_not_found(mcp_client):
    """A missing scene is refused instead of a silent scene.turn_on no-op."""
    mcp = MCPAssertions(mcp_client)
    await mcp.call_tool_failure(
        "ha_config_set_scene",
        {"scene_id": f"scene.missing_{uuid4().hex[:12]}", "activate": True},
        expected_error="not found",
    )
