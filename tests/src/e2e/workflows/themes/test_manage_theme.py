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
    MCPAssertions,
    extract_error_message,
    parse_mcp_result,
    safe_call_tool,
)
from ...utilities.cleanup import CleanupSteps

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
        # Test Theme B declares a dark mode; Test Theme A declares none.
        assert listing["modes"].get("Test Theme B") == ["dark"], listing
        assert "Test Theme A" not in listing["modes"], listing

        logger.info(f"Installed themes: {listing}")

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
            assert set_b["data"]["default_theme"] == "Test Theme B"

            # Verify the flip stuck.
            list_after_b = parse_mcp_result(
                await mcp_client.call_tool("ha_manage_theme", {"action": "list"})
            )
            assert list_after_b["data"]["default_theme"] == "Test Theme B", (
                f"default_theme should have flipped to Test Theme B: {list_after_b}"
            )

            # Set Test Theme A for dark mode; light default should stay Test Theme B.
            set_a_dark = parse_mcp_result(
                await mcp_client.call_tool(
                    "ha_manage_theme",
                    {"action": "set", "theme_name_dark": "Test Theme A"},
                )
            )
            assert set_a_dark.get("success") is True, (
                f"Set Test Theme A dark failed: {set_a_dark}"
            )
            assert set_a_dark["data"]["default_dark_theme"] == "Test Theme A"
            assert set_a_dark["data"]["default_theme"] == "Test Theme B", (
                "Setting dark theme with a real name must not change light default"
            )

            # An unknown theme comes back as Core's refusal and changes nothing.
            refused = await MCPAssertions(mcp_client).call_tool_failure(
                "ha_manage_theme",
                {"action": "set", "theme_name": "No Such Theme"},
                expected_error="No Such Theme",
            )
            assert refused["error"]["code"] == "VALIDATION_INVALID_PARAMETER", refused
            assert refused["ha_error_code"] == "invalid_format", refused
            list_after_refusal = parse_mcp_result(
                await mcp_client.call_tool("ha_manage_theme", {"action": "list"})
            )
            assert list_after_refusal["data"]["default_theme"] == "Test Theme B"
            assert list_after_refusal["data"]["default_dark_theme"] == "Test Theme A"

        finally:
            # Cleanup: restore container to clean state for sibling tests.
            # Reset light mode to 'default' and clear dark override with 'none'.
            with CleanupSteps() as cleanup:
                with cleanup.step():
                    await MCPAssertions(mcp_client).call_tool_success(
                        "ha_manage_theme", {"action": "set", "theme_name": "default"}
                    )
                with cleanup.step():
                    await MCPAssertions(mcp_client).call_tool_success(
                        "ha_manage_theme",
                        {"action": "set", "theme_name_dark": "none"},
                    )

    async def test_set_engine_theme_without_value_fails(self, mcp_client):
        """action='set_engine_theme' without value returns a validation error.

        This check runs before any engine resolution, so it is deterministic
        whether or not a screenshot engine is configured for the run.
        """
        data = await safe_call_tool(
            mcp_client, "ha_manage_theme", {"action": "set_engine_theme"}
        )

        assert data.get("success") is False
        error = data.get("error") or {}
        assert error.get("code") == "VALIDATION_MISSING_PARAMETER", (
            f"Expected VALIDATION_MISSING_PARAMETER, got: {data}"
        )

    async def test_get_engine_theme_fails_actionably_without_an_engine(
        self, mcp_client
    ):
        """The engine-account read never crashes: it succeeds or explains.

        The e2e instance has no screenshot engine, so this exercises the
        failure contract — a structured error naming why the engine account
        could not be reached, rather than a raw exception or a silent read of
        ha-mcp's OWN profile (which is the bug the identity guard prevents).
        """
        data = await safe_call_tool(
            mcp_client, "ha_manage_theme", {"action": "get_engine_theme"}
        )

        if data.get("success") is True:
            # An engine IS configured for this run: the value must be the
            # user-data shape (dict) or None when never configured.
            theme = data["data"]["theme"]
            assert theme is None or isinstance(theme, dict), (
                f"Unexpected engine theme payload: {data}"
            )
            return

        assert data.get("success") is False, (
            f"Expected an explicit failure, got: {data}"
        )
        # Assert the structured-error contract rather than matching wording:
        # the message varies with WHY the engine is unavailable (not
        # configured, not installed, not started, unidentifiable account).
        error = data.get("error") or {}
        assert error.get("code"), f"Failure must carry an error code: {data}"
        assert extract_error_message(data), f"Failure must carry a message: {data}"
        assert error.get("suggestions"), (
            f"Engine failures must be actionable, not bare: {data}"
        )
