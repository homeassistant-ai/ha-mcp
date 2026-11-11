"""
E2E: ha_search finds an entity by the name Home Assistant shows for it.

Since Home Assistant 2026.11 that name is composed by Core from the parent
device, device and entity names, and the friendly_name state attribute is the
legacy name (#2965). The test container's kitchen_sink power strip has outlets
as child devices, whose sensors carry a composed name that differs from their
friendly_name on a fresh install.
"""

import pytest

from ..utilities.assertions import MCPAssertions


@pytest.mark.asyncio
@pytest.mark.requires_component_surface
async def test_search_finds_an_entity_by_the_name_home_assistant_shows(mcp_client):
    async with MCPAssertions(mcp_client) as mcp:
        listed = await mcp.call_tool_success(
            "ha_search",
            {
                "domain_filter": "sensor",
                "limit": 200,
                "result_fields": ["entity_id", "name", "friendly_name"],
            },
        )
        differing = [
            row for row in listed["entities"] if row["name"] != row["friendly_name"]
        ]
        assert differing, (
            "no sensor in the test container has a composed name that differs "
            "from its friendly_name"
        )
        target = differing[0]

        found = await mcp.call_tool_success("ha_search", {"query": target["name"]})

        assert target["entity_id"] in [row["entity_id"] for row in found["entities"]]
