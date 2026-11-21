"""The entity dialog's main-entity switch through ha_set_entity (issue #2611).

On an entity with an integration name, HA stores the switch as an
entity-registry ``name`` of ``""`` and then shows the device name as its
friendly name. ``ha_set_entity(name="")`` keeps its revert-to-default meaning;
``use_device_name`` carries the new state.
"""

from typing import Any

import pytest

from ha_mcp._vendor.fastmcp import Client

from ...utilities.assertions import (
    MCPAssertions,
    parse_mcp_result,
)
from ...utilities.cleanup import CleanupSteps

# default_config's sun integration: has_entity_name with a "Sun" device, so the
# friendly name visibly changes between "Sun Next dawn" and "Sun".
ENTITY = "sensor.sun_next_dawn"


async def _entry(mcp_client: Client) -> dict[str, Any]:
    data = parse_mcp_result(
        await mcp_client.call_tool("ha_get_entity", {"entity_id": ENTITY})
    )
    assert data.get("success"), data
    return data["entity_entry"]


async def _friendly_name(mcp_client: Client) -> str | None:
    # ha_get_state answers with the record under "data" and no success key.
    data = parse_mcp_result(
        await mcp_client.call_tool("ha_get_state", {"entity_id": ENTITY})
    )
    record = data.get("data") or data
    assert "attributes" in record, data
    return record["attributes"].get("friendly_name")


@pytest.mark.registry
class TestUseDeviceName:
    async def test_switch_on_and_off(self, mcp_client: Client) -> None:
        """The switch stores ``""`` and the friendly name becomes the device
        name; off restores the integration default."""
        before = await _entry(mcp_client)
        default_friendly = await _friendly_name(mcp_client)
        try:
            on = parse_mcp_result(
                await mcp_client.call_tool(
                    "ha_set_entity", {"entity_id": ENTITY, "use_device_name": True}
                )
            )
            assert on.get("success"), on
            entry = await _entry(mcp_client)
            assert entry["name"] == "", entry
            assert await _friendly_name(mcp_client) == "Sun"

            off = parse_mcp_result(
                await mcp_client.call_tool(
                    "ha_set_entity", {"entity_id": ENTITY, "use_device_name": False}
                )
            )
            assert off.get("success"), off
            entry = await _entry(mcp_client)
            assert entry["name"] is None, entry
            assert await _friendly_name(mcp_client) == default_friendly
        finally:
            with CleanupSteps() as cleanup, cleanup.step():
                # name='' means revert, so an originally empty override is restored
                # through the switch itself.
                restore: dict[str, Any] = (
                    {"use_device_name": True}
                    if before.get("name") == ""
                    else {"name": before.get("name") or ""}
                )
                await MCPAssertions(mcp_client).call_tool_success(
                    "ha_set_entity", {"entity_id": ENTITY, **restore}
                )

    async def test_rejected_together_with_name(self, mcp_client: Client) -> None:
        mcp = MCPAssertions(mcp_client)
        data = await mcp.call_tool_failure(
            "ha_set_entity",
            {"entity_id": ENTITY, "name": "Dawn", "use_device_name": True},
        )
        assert data["error"]["code"] == "VALIDATION_INVALID_PARAMETER", data
        assert (await _entry(mcp_client))["name"] is None
