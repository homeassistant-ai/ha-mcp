"""E2E tests for tool discovery functionality via MCP protocol."""

import logging

import pytest

from ..utilities.assertions import parse_mcp_result

logger = logging.getLogger(__name__)


class TestSearchToolsE2E:
    """E2E tests for ha_search_tools MCP tool."""

    @pytest.mark.asyncio
    async def test_search_tools_with_query(self, mcp_client):
        """Test searching tools by query returns results."""
        result = await mcp_client.call_tool(
            "ha_search_tools",
            {"query": "automation"}
        )
        data = parse_mcp_result(result)

        assert data.get("success") is True, f"Search failed: {data}"
        assert "results" in data or "categories" in data

        if "results" in data:
            results = data["results"]
            assert len(results) > 0, "Expected automation tools in results"
            # Check that automation tools are found
            tool_names = [r["tool_name"] for r in results]
            assert any("automation" in t for t in tool_names), (
                f"Expected automation tools, got: {tool_names}"
            )

    @pytest.mark.asyncio
    async def test_search_tools_empty_query_shows_categories(self, mcp_client):
        """Test empty query shows categories overview."""
        result = await mcp_client.call_tool(
            "ha_search_tools",
            {"query": ""}
        )
        data = parse_mcp_result(result)

        assert data.get("success") is True
        # Empty query should show categories
        if "mode" in data:
            assert data["mode"] == "categories_overview"
        assert "categories" in data
        assert len(data["categories"]) > 0

    @pytest.mark.asyncio
    async def test_search_tools_with_category_filter(self, mcp_client):
        """Test filtering by category."""
        result = await mcp_client.call_tool(
            "ha_search_tools",
            {"query": "", "category": "search"}
        )
        data = parse_mcp_result(result)

        assert data.get("success") is True
        results = data.get("results", [])
        # All results should be in search category
        for r in results:
            assert r["category"] == "search", f"Expected search category, got {r['category']}"

    @pytest.mark.asyncio
    async def test_search_tools_no_match(self, mcp_client):
        """Test search with no matches returns suggestions."""
        result = await mcp_client.call_tool(
            "ha_search_tools",
            {"query": "xyznonexistent123abc"}
        )
        data = parse_mcp_result(result)

        assert data.get("success") is True
        assert data["total_matches"] == 0
        assert "suggestions" in data

    @pytest.mark.asyncio
    async def test_search_tools_by_action(self, mcp_client):
        """Test searching for action words like delete, create."""
        result = await mcp_client.call_tool(
            "ha_search_tools",
            {"query": "delete"}
        )
        data = parse_mcp_result(result)

        assert data.get("success") is True
        if data["total_matches"] > 0:
            results = data["results"]
            # Should find delete tools
            tool_names = [r["tool_name"] for r in results]
            assert any("delete" in t for t in tool_names)


class TestListToolProfilesE2E:
    """E2E tests for ha_list_tool_profiles MCP tool."""

    @pytest.mark.asyncio
    async def test_list_profiles_returns_all(self, mcp_client):
        """Test listing all profiles."""
        result = await mcp_client.call_tool(
            "ha_list_tool_profiles",
            {}
        )
        data = parse_mcp_result(result)

        assert data.get("success") is True
        assert "profiles" in data
        profiles = data["profiles"]
        assert len(profiles) >= 5, "Expected at least 5 profiles"

        # Check expected profiles exist
        profile_names = [p["name"] for p in profiles]
        assert "minimal" in profile_names
        assert "standard" in profile_names
        assert "full" in profile_names

    @pytest.mark.asyncio
    async def test_profiles_have_metadata(self, mcp_client):
        """Test each profile has required metadata."""
        result = await mcp_client.call_tool(
            "ha_list_tool_profiles",
            {}
        )
        data = parse_mcp_result(result)

        for profile in data["profiles"]:
            assert "name" in profile
            assert "description" in profile
            assert "tool_count" in profile
            assert profile["tool_count"] > 0


class TestGetToolProfileE2E:
    """E2E tests for ha_get_tool_profile MCP tool."""

    @pytest.mark.asyncio
    async def test_get_minimal_profile(self, mcp_client):
        """Test getting minimal profile details."""
        result = await mcp_client.call_tool(
            "ha_get_tool_profile",
            {"profile_name": "minimal"}
        )
        data = parse_mcp_result(result)

        assert data.get("success") is True
        assert "profile" in data
        assert data["profile"]["name"] == "minimal"
        assert "all_tools" in data
        # Minimal should have limited tools
        assert len(data["all_tools"]) <= 15

    @pytest.mark.asyncio
    async def test_get_full_profile(self, mcp_client):
        """Test getting full profile includes all tools."""
        result = await mcp_client.call_tool(
            "ha_get_tool_profile",
            {"profile_name": "full"}
        )
        data = parse_mcp_result(result)

        assert data.get("success") is True
        assert data["profile"]["name"] == "full"
        # Full should have many tools
        assert len(data["all_tools"]) >= 50

    @pytest.mark.asyncio
    async def test_get_invalid_profile(self, mcp_client):
        """Test getting invalid profile returns error."""
        result = await mcp_client.call_tool(
            "ha_get_tool_profile",
            {"profile_name": "nonexistent"}
        )
        data = parse_mcp_result(result)

        assert data.get("success") is False
        # Should suggest available profiles
        if "available_profiles" in data:
            assert "full" in data["available_profiles"]

    @pytest.mark.asyncio
    async def test_profile_has_tools_by_category(self, mcp_client):
        """Test profile response includes tools organized by category."""
        result = await mcp_client.call_tool(
            "ha_get_tool_profile",
            {"profile_name": "standard"}
        )
        data = parse_mcp_result(result)

        assert data.get("success") is True
        assert "tools_by_category" in data
        tools_by_cat = data["tools_by_category"]
        assert isinstance(tools_by_cat, dict)
        # Should have at least some categories
        assert len(tools_by_cat) > 0


class TestListToolCategoriesE2E:
    """E2E tests for ha_list_tool_categories MCP tool."""

    @pytest.mark.asyncio
    async def test_list_categories(self, mcp_client):
        """Test listing all categories."""
        result = await mcp_client.call_tool(
            "ha_list_tool_categories",
            {}
        )
        data = parse_mcp_result(result)

        assert data.get("success") is True
        assert "categories" in data
        categories = data["categories"]
        assert len(categories) >= 10, "Expected at least 10 categories"

    @pytest.mark.asyncio
    async def test_categories_have_tools(self, mcp_client):
        """Test each category has tool information."""
        result = await mcp_client.call_tool(
            "ha_list_tool_categories",
            {}
        )
        data = parse_mcp_result(result)

        for category in data["categories"]:
            assert "name" in category
            assert "description" in category
            assert "tool_count" in category
            assert "tools" in category
            assert category["tool_count"] > 0
            assert len(category["tools"]) == category["tool_count"]

    @pytest.mark.asyncio
    async def test_total_tools_count(self, mcp_client):
        """Test total tools count is accurate."""
        result = await mcp_client.call_tool(
            "ha_list_tool_categories",
            {}
        )
        data = parse_mcp_result(result)

        assert "total_tools" in data
        total = data["total_tools"]
        assert total >= 50, f"Expected at least 50 total tools, got {total}"

        # Verify sum matches
        categories = data["categories"]
        calculated_total = sum(c["tool_count"] for c in categories)
        assert calculated_total == total


class TestToolDiscoveryIntegration:
    """Integration tests combining discovery tools."""

    @pytest.mark.asyncio
    async def test_search_then_get_profile(self, mcp_client):
        """Test workflow: search for tools, then get profile info."""
        # First search for automation tools
        search_result = await mcp_client.call_tool(
            "ha_search_tools",
            {"query": "automation"}
        )
        search_data = parse_mcp_result(search_result)
        assert search_data.get("success") is True

        # Then get the developer profile (should include automation)
        profile_result = await mcp_client.call_tool(
            "ha_get_tool_profile",
            {"profile_name": "developer"}
        )
        profile_data = parse_mcp_result(profile_result)
        assert profile_data.get("success") is True

        # Verify automation tools are in developer profile
        dev_tools = profile_data["all_tools"]
        assert any("automation" in t for t in dev_tools)

    @pytest.mark.asyncio
    async def test_list_categories_match_search(self, mcp_client):
        """Test categories from list match search results."""
        # Get all categories
        cat_result = await mcp_client.call_tool(
            "ha_list_tool_categories",
            {}
        )
        cat_data = parse_mcp_result(cat_result)
        categories = [c["name"] for c in cat_data["categories"]]

        # Search with category filter for each
        for category_name in categories[:3]:  # Test first 3
            search_result = await mcp_client.call_tool(
                "ha_search_tools",
                {"query": "", "category": category_name}
            )
            search_data = parse_mcp_result(search_result)
            assert search_data.get("success") is True
            # Results should all be in that category
            for r in search_data.get("results", []):
                assert r["category"] == category_name
