"""
E2E test: ha_config_list_helpers reports a helper's stored config (#2901).

input_select.set_options changes a helper's options until Home Assistant
restarts without changing its stored config. An agent that reads the listing
as the helper's config must see the stored options.
"""

import uuid

import pytest

from ...utilities.assertions import assert_mcp_success, safe_call_tool
from ...utilities.wait_helpers import wait_for_entity_registration


@pytest.mark.asyncio
@pytest.mark.config
async def test_listing_shows_stored_options_after_runtime_set_options(mcp_client):
    """After set_options, the listing still shows the options the helper stores."""
    name = f"E2E Stored Select {uuid.uuid4().hex[:8]}"
    created = assert_mcp_success(
        await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_select",
                "name": name,
                "config": {"options": ["stored_a", "stored_b"], "initial": "stored_b"},
            },
        ),
        "create input_select",
    )
    entity_id = created["entity_id"]
    try:
        assert await wait_for_entity_registration(mcp_client, entity_id)
        assert_mcp_success(
            await mcp_client.call_tool(
                "ha_call_service",
                {
                    "domain": "input_select",
                    "service": "set_options",
                    "entity_id": entity_id,
                    "data": {"options": ["runtime_x", "runtime_y"]},
                },
            ),
            "set runtime options",
        )

        listed = assert_mcp_success(
            await mcp_client.call_tool(
                "ha_config_list_helpers",
                {"helper_type": "input_select", "limit": 500},
            ),
            "list input_select",
        )
        (record,) = [h for h in listed["helpers"] if h.get("entity_id") == entity_id]
        assert record["options"] == ["stored_a", "stored_b"]
        assert record["initial"] == "stored_b"
        assert "editable" not in record
    finally:
        await safe_call_tool(
            mcp_client,
            "ha_remove_helpers_integrations",
            {"helper_type": "input_select", "target": entity_id, "confirm": True},
        )
