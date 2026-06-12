"""
Frontend Theme Management E2E Tests

Tests ha_manage_theme against a real Home Assistant instance. Fresh test
instances ship without custom themes, so listing asserts structure rather
than content, and the set path exercises the built-in 'default' theme plus
Home Assistant's call-time validation of unknown names.
"""

import logging

import pytest

from ...utilities.assertions import parse_mcp_result, safe_call_tool

logger = logging.getLogger(__name__)


@pytest.mark.themes
class TestManageTheme:
    """E2E coverage for the ha_manage_theme tool."""

    async def test_list_themes(self, mcp_client):
        """action='list' returns names, count, and defaults."""
        result = await mcp_client.call_tool("ha_manage_theme", {"action": "list"})
        data = parse_mcp_result(result)

        assert data.get("success") is True, f"List themes failed: {data}"
        listing = data["data"]
        assert isinstance(listing["themes"], list)
        assert isinstance(listing["count"], int)
        assert listing["count"] == len(listing["themes"])
        assert "default_theme" in listing
        assert "default_dark_theme" in listing

        logger.info(f"Installed themes: {listing}")

    async def test_set_default_theme(self, mcp_client):
        """action='set' with the built-in 'default' theme succeeds and verifies."""
        result = await mcp_client.call_tool(
            "ha_manage_theme", {"action": "set", "theme_name": "default"}
        )
        data = parse_mcp_result(result)

        assert data.get("success") is True, f"Set default theme failed: {data}"
        assert data["data"]["theme"] == "default"
        assert data["data"]["mode"] == "light"
        assert data["data"]["default_theme"] == "default"

    async def test_set_unknown_theme_fails(self, mcp_client):
        """action='set' with a non-installed theme surfaces HA's validation error."""
        data = await safe_call_tool(
            mcp_client,
            "ha_manage_theme",
            {"action": "set", "theme_name": "definitely_not_installed_e2e"},
        )

        assert data.get("success") is False, (
            f"Setting an unknown theme should fail: {data}"
        )

    async def test_set_without_theme_name_fails(self, mcp_client):
        """action='set' without theme_name returns a structured validation error."""
        data = await safe_call_tool(mcp_client, "ha_manage_theme", {"action": "set"})

        assert data.get("success") is False
        error = data.get("error") or {}
        assert error.get("code") == "VALIDATION_MISSING_PARAMETER", (
            f"Expected VALIDATION_MISSING_PARAMETER, got: {data}"
        )
