"""Discover a seeded scene and hand its storage key to the full-config reader."""

from ...utilities.assertions import MCPAssertions


async def test_scene_discovery_to_get_preserves_storage_key(mcp_client):
    async with MCPAssertions(mcp_client) as mcp:
        listing = await mcp.call_tool_success(
            "ha_config_get_scene", {"query": "E2E Test Seed Scene", "limit": 5}
        )
        assert listing["count"] == 1
        row = listing["scenes"][0]
        assert row["entity_id"].startswith("scene.")
        assert row["scene_id"] == "e2e_test_scene_seed"
        assert row["config_available"] is True
        assert "config" not in row

        result = await mcp.call_tool_success(
            "ha_config_get_scene", {"scene_id": row["scene_id"]}
        )
        assert result["config"]["entities"]["light.bed_light"]["state"] == "off"
        assert result["config_hash"]

        matches = await mcp.call_tool_success(
            "ha_config_get_scene",
            {"query": "off", "search_in_config": True, "limit": 100},
        )
        assert any(
            item["scene_id"] == row["scene_id"] and item["match_in_config"]
            for item in matches["scenes"]
        )
