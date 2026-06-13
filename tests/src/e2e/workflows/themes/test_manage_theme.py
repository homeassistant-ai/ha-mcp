"""
Frontend Theme Management E2E Tests

Tests ha_manage_theme against a real Home Assistant instance. Test instances
load two seeded themes (Test Theme A, Test Theme B) from
tests/initial_test_state/themes/, providing real-name coverage of the
_update_hass_theme code path that built-in 'default'/'none' bypass.
"""

import logging

import pytest

from ...utilities.assertions import (
    extract_error_message,
    parse_mcp_result,
    safe_call_tool,
)

logger = logging.getLogger(__name__)


@pytest.mark.themes
class TestManageTheme:
    """E2E coverage for the ha_manage_theme tool."""

    async def test_list_themes(self, mcp_client):
        """action='list' returns names, count, and defaults, including seeded themes."""
        result = await mcp_client.call_tool("ha_manage_theme", {"action": "list"})
        data = parse_mcp_result(result)

        assert data.get("success") is True, f"List themes failed: {data}"
        listing = data["data"]
        assert isinstance(listing["themes"], list)
        assert isinstance(listing["count"], int)
        assert listing["count"] == len(listing["themes"])
        assert "default_theme" in listing
        assert "default_dark_theme" in listing

        # Seeded themes from tests/initial_test_state/themes/ must be present.
        theme_names = set(listing["themes"])
        assert "Test Theme A" in theme_names, f"Missing Test Theme A: {listing}"
        assert "Test Theme B" in theme_names, f"Missing Test Theme B: {listing}"
        # ``>= 2`` rather than ``== 2``: the seeded-name asserts above already
        # guarantee both fixtures are present, and an exact count couples this
        # module to the shared per-worker container's global theme state (the
        # config_set_yaml lifecycle test adds/removes a theme on the same
        # worker, so a failed cleanup there would otherwise break this read).
        assert listing["count"] >= 2, (
            f"Expected at least the 2 seeded themes, got {listing['count']}: {listing}"
        )

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

    async def test_set_dark_mode_theme(self, mcp_client):
        """action='set' with mode='dark' updates only the dark-mode default."""
        # Capture the light default so independence can be asserted below.
        before = parse_mcp_result(
            await mcp_client.call_tool("ha_manage_theme", {"action": "list"})
        )
        light_default_before = before["data"]["default_theme"]

        result = await mcp_client.call_tool(
            "ha_manage_theme",
            {"action": "set", "theme_name": "default", "mode": "dark"},
        )
        data = parse_mcp_result(result)

        assert data.get("success") is True, f"Set dark theme failed: {data}"
        assert data["data"]["mode"] == "dark"
        assert data["data"]["default_dark_theme"] == "default"
        assert data["data"]["default_theme"] == light_default_before, (
            "Setting the dark theme must not change the light default"
        )

        # Cleanup: 'none' clears the dark-mode override again.
        cleanup = parse_mcp_result(
            await mcp_client.call_tool(
                "ha_manage_theme",
                {"action": "set", "theme_name": "none", "mode": "dark"},
            )
        )
        assert cleanup.get("success") is True
        assert cleanup["data"]["default_dark_theme"] is None

    async def test_switch_between_real_themes(self, mcp_client):
        """Switch between installed themes (A→B) and verify light/dark independence.

        Exercises the _update_hass_theme(name, ...) code path that built-in
        'default'/'none' bypass via HA's VALUE_NO_THEME/DEFAULT_THEME branch.
        """
        try:
            # Set Test Theme A as the light default.
            set_a = parse_mcp_result(
                await mcp_client.call_tool(
                    "ha_manage_theme",
                    {"action": "set", "theme_name": "Test Theme A"},
                )
            )
            assert set_a.get("success") is True, f"Set Test Theme A failed: {set_a}"
            assert set_a["data"]["theme"] == "Test Theme A"
            assert set_a["data"]["mode"] == "light"
            assert set_a["data"]["default_theme"] == "Test Theme A"

            # Re-list to confirm the default_theme persisted.
            list_after_a = parse_mcp_result(
                await mcp_client.call_tool("ha_manage_theme", {"action": "list"})
            )
            assert list_after_a["data"]["default_theme"] == "Test Theme A", (
                f"default_theme should be Test Theme A after set: {list_after_a}"
            )

            # Switch to Test Theme B for light mode.
            set_b = parse_mcp_result(
                await mcp_client.call_tool(
                    "ha_manage_theme",
                    {"action": "set", "theme_name": "Test Theme B"},
                )
            )
            assert set_b.get("success") is True, f"Set Test Theme B failed: {set_b}"
            assert set_b["data"]["theme"] == "Test Theme B"
            assert set_b["data"]["default_theme"] == "Test Theme B"

            # Verify the flip stuck.
            list_after_b = parse_mcp_result(
                await mcp_client.call_tool("ha_manage_theme", {"action": "list"})
            )
            assert list_after_b["data"]["default_theme"] == "Test Theme B", (
                f"default_theme should have flipped to Test Theme B: {list_after_b}"
            )

            # Set Test Theme B for dark mode; light default should stay untouched.
            set_b_dark = parse_mcp_result(
                await mcp_client.call_tool(
                    "ha_manage_theme",
                    {"action": "set", "theme_name": "Test Theme B", "mode": "dark"},
                )
            )
            assert set_b_dark.get("success") is True, (
                f"Set Test Theme B dark failed: {set_b_dark}"
            )
            assert set_b_dark["data"]["mode"] == "dark"
            assert set_b_dark["data"]["default_dark_theme"] == "Test Theme B"
            assert set_b_dark["data"]["default_theme"] == "Test Theme B", (
                "Setting dark theme with a real name must not change light default"
            )

        finally:
            # Cleanup: restore container to clean state for sibling tests.
            # Reset light mode to 'default' and clear dark override with 'none'.
            await mcp_client.call_tool(
                "ha_manage_theme",
                {"action": "set", "theme_name": "default"},
            )
            await mcp_client.call_tool(
                "ha_manage_theme",
                {"action": "set", "theme_name": "none", "mode": "dark"},
            )
            logger.info("Cleaned up theme state (reset to default/none)")

    async def test_set_unknown_theme_fails(self, mcp_client):
        """action='set' with a non-installed theme surfaces HA's validation error."""
        unknown = "definitely_not_installed_e2e"
        data = await safe_call_tool(
            mcp_client,
            "ha_manage_theme",
            {"action": "set", "theme_name": unknown},
        )

        assert data.get("success") is False, (
            f"Setting an unknown theme should fail: {data}"
        )
        error_msg = extract_error_message(data)
        assert unknown in error_msg, (
            f"Error message should name the unknown theme: {data}"
        )

    async def test_set_without_theme_name_fails(self, mcp_client):
        """action='set' without theme_name returns a structured validation error."""
        data = await safe_call_tool(mcp_client, "ha_manage_theme", {"action": "set"})

        assert data.get("success") is False
        error = data.get("error") or {}
        assert error.get("code") == "VALIDATION_MISSING_PARAMETER", (
            f"Expected VALIDATION_MISSING_PARAMETER, got: {data}"
        )
