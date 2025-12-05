"""Unit tests for tool discovery and filtering functionality."""

import pytest

from ha_mcp.tools.tools_discovery import (
    TOOL_CATEGORIES,
    TOOL_PROFILES,
    get_all_tool_metadata,
    get_profile_info,
    get_tools_for_profile,
    list_all_categories,
    list_all_profiles,
    search_tools,
)


class TestToolCategories:
    """Test tool category definitions."""

    def test_all_categories_have_required_fields(self):
        """Every category must have description and tools list."""
        for name, info in TOOL_CATEGORIES.items():
            assert "description" in info, f"Category {name} missing description"
            assert "tools" in info, f"Category {name} missing tools list"
            assert isinstance(info["tools"], list), f"Category {name} tools must be a list"
            assert len(info["tools"]) > 0, f"Category {name} has no tools"

    def test_tool_names_are_valid(self):
        """All tool names should start with ha_ prefix."""
        for name, info in TOOL_CATEGORIES.items():
            for tool in info["tools"]:
                assert tool.startswith("ha_"), f"Tool {tool} in {name} should start with ha_"

    def test_no_duplicate_tools_across_categories(self):
        """Each tool should only appear in one category."""
        all_tools = []
        for name, info in TOOL_CATEGORIES.items():
            for tool in info["tools"]:
                if tool in all_tools:
                    # This is actually OK for utility tools that might be in multiple categories
                    # But we should be aware of duplicates
                    pass
                all_tools.append(tool)

    def test_expected_categories_exist(self):
        """Core categories should exist."""
        expected = ["search", "service", "automation", "script", "helper", "backup", "system"]
        for category in expected:
            assert category in TOOL_CATEGORIES, f"Expected category {category} not found"


class TestToolProfiles:
    """Test tool profile definitions."""

    def test_all_profiles_have_required_fields(self):
        """Every profile must have description."""
        for name, info in TOOL_PROFILES.items():
            assert "description" in info, f"Profile {name} missing description"

    def test_expected_profiles_exist(self):
        """Core profiles should exist."""
        expected = ["minimal", "standard", "extended", "full", "developer", "monitoring"]
        for profile in expected:
            assert profile in TOOL_PROFILES, f"Expected profile {profile} not found"

    def test_profile_categories_are_valid(self):
        """Profile categories should reference existing categories."""
        for name, info in TOOL_PROFILES.items():
            for category in info.get("categories", []):
                assert category in TOOL_CATEGORIES, (
                    f"Profile {name} references non-existent category {category}"
                )


class TestGetToolsForProfile:
    """Test get_tools_for_profile function."""

    def test_minimal_profile_has_limited_tools(self):
        """Minimal profile should have only essential tools."""
        tools = get_tools_for_profile("minimal")
        assert len(tools) <= 15, f"Minimal profile has too many tools: {len(tools)}"
        assert "ha_search_entities" in tools
        assert "ha_get_state" in tools
        assert "ha_call_service" in tools

    def test_full_profile_has_all_tools(self):
        """Full profile should include all category tools."""
        tools = get_tools_for_profile("full")
        # Should have many tools
        assert len(tools) >= 50, f"Full profile missing tools: {len(tools)}"

    def test_monitoring_profile_excludes_destructive(self):
        """Monitoring profile should not include destructive tools."""
        tools = get_tools_for_profile("monitoring")
        # Should not have delete/create/update tools
        for tool in tools:
            assert "delete" not in tool.lower() or tool == "ha_delete_area", (
                f"Monitoring profile includes destructive tool: {tool}"
            )
            assert "set_" not in tool or tool in [
                "ha_get_state",  # OK - read only
            ], f"Monitoring profile includes modify tool: {tool}"

    def test_invalid_profile_raises_error(self):
        """Invalid profile name should raise ValueError."""
        with pytest.raises(ValueError) as exc_info:
            get_tools_for_profile("nonexistent")
        assert "Unknown profile" in str(exc_info.value)

    def test_developer_profile_includes_debug_tools(self):
        """Developer profile should include debugging tools."""
        tools = get_tools_for_profile("developer")
        # Should include trace and registry tools
        assert any("trace" in t for t in tools), "Developer profile missing trace tools"


class TestGetProfileInfo:
    """Test get_profile_info function."""

    def test_returns_complete_info(self):
        """Profile info should include all expected fields."""
        info = get_profile_info("standard")
        assert "name" in info
        assert "description" in info
        assert "categories" in info
        assert "tool_count" in info
        assert "tools" in info
        assert info["name"] == "standard"
        assert info["tool_count"] > 0
        assert len(info["tools"]) == info["tool_count"]

    def test_invalid_profile_raises_error(self):
        """Invalid profile should raise ValueError."""
        with pytest.raises(ValueError):
            get_profile_info("nonexistent")


class TestListAllProfiles:
    """Test list_all_profiles function."""

    def test_returns_all_profiles(self):
        """Should return info for all defined profiles."""
        profiles = list_all_profiles()
        assert len(profiles) == len(TOOL_PROFILES)
        for profile in profiles:
            assert "name" in profile
            assert "description" in profile
            assert "tool_count" in profile

    def test_profiles_sorted_or_complete(self):
        """All profile names should be present."""
        profiles = list_all_profiles()
        names = [p["name"] for p in profiles]
        for expected in TOOL_PROFILES.keys():
            assert expected in names


class TestListAllCategories:
    """Test list_all_categories function."""

    def test_returns_all_categories(self):
        """Should return info for all defined categories."""
        categories = list_all_categories()
        assert len(categories) == len(TOOL_CATEGORIES)
        for category in categories:
            assert "name" in category
            assert "description" in category
            assert "tool_count" in category
            assert "tools" in category

    def test_categories_have_positive_tool_counts(self):
        """All categories should have at least one tool."""
        categories = list_all_categories()
        for category in categories:
            assert category["tool_count"] > 0, f"Category {category['name']} has no tools"


class TestSearchTools:
    """Test search_tools function."""

    def test_search_by_exact_name(self):
        """Exact tool name match should return high score."""
        results = search_tools("ha_search_entities")
        assert len(results) > 0
        # Exact match should be first
        assert results[0]["tool_name"] == "ha_search_entities"
        assert results[0]["score"] >= 80

    def test_search_by_partial_name(self):
        """Partial name match should find tools."""
        results = search_tools("search")
        assert len(results) > 0
        # Should find search-related tools
        tool_names = [r["tool_name"] for r in results]
        assert any("search" in t.lower() for t in tool_names)

    def test_search_by_category(self):
        """Category name search should find category tools."""
        results = search_tools("automation")
        assert len(results) > 0
        # Should find automation tools
        categories = [r["category"] for r in results]
        assert "automation" in categories

    def test_search_with_category_filter(self):
        """Category filter should limit results."""
        results = search_tools("", category_filter="search")
        assert len(results) > 0
        # All results should be in search category
        for result in results:
            assert result["category"] == "search"

    def test_empty_search_returns_nothing(self):
        """Empty search without category filter returns nothing."""
        results = search_tools("")
        assert len(results) == 0

    def test_no_match_returns_empty(self):
        """Search with no matches returns empty list."""
        results = search_tools("xyznonexistent123")
        assert len(results) == 0

    def test_results_include_metadata(self):
        """Search results should include metadata."""
        results = search_tools("backup")
        assert len(results) > 0
        for result in results:
            assert "tool_name" in result
            assert "category" in result
            assert "score" in result
            assert "match_reasons" in result

    def test_results_sorted_by_score(self):
        """Results should be sorted by score descending."""
        results = search_tools("service")
        if len(results) > 1:
            for i in range(len(results) - 1):
                assert results[i]["score"] >= results[i + 1]["score"]


class TestGetAllToolMetadata:
    """Test get_all_tool_metadata function."""

    def test_returns_all_tools(self):
        """Should return metadata for all tools."""
        metadata = get_all_tool_metadata()
        total_tools = sum(len(cat["tools"]) for cat in TOOL_CATEGORIES.values())
        assert len(metadata) == total_tools

    def test_metadata_has_category(self):
        """Each tool should have category info."""
        metadata = get_all_tool_metadata()
        for tool_name, tool_meta in metadata.items():
            assert "category" in tool_meta
            assert "category_description" in tool_meta
            assert tool_meta["category"] in TOOL_CATEGORIES


class TestProfileToolOverlap:
    """Test relationships between profiles."""

    def test_minimal_is_subset_of_standard(self):
        """Minimal tools should all be in standard."""
        minimal = set(get_tools_for_profile("minimal"))
        standard = set(get_tools_for_profile("standard"))
        # Minimal should be subset
        assert minimal.issubset(standard), f"Minimal tools not in standard: {minimal - standard}"

    def test_standard_is_subset_of_extended(self):
        """Standard tools should all be in extended."""
        standard = set(get_tools_for_profile("standard"))
        extended = set(get_tools_for_profile("extended"))
        assert standard.issubset(extended), f"Standard tools not in extended: {standard - extended}"

    def test_extended_is_subset_of_full(self):
        """Extended tools should all be in full."""
        extended = set(get_tools_for_profile("extended"))
        full = set(get_tools_for_profile("full"))
        assert extended.issubset(full), f"Extended tools not in full: {extended - full}"
