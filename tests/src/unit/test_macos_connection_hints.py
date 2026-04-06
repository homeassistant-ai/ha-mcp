"""Unit tests for macOS-specific connection error hints.

Verifies that macOS users get platform-specific troubleshooting suggestions
when connection errors occur (Local Network Privacy, SSH tunnel, http vs https).
"""

from unittest.mock import patch

from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.errors import ErrorCode
from ha_mcp.tools.helpers import exception_to_structured_error


MACOS_HINT_FRAGMENT = "Local Network"


class TestMacOSConnectionHints:
    """Tests for macOS-specific hints appended to connection errors."""

    def test_hints_appear_on_darwin_for_connection_failed(self):
        """macOS hints should appear for CONNECTION_FAILED on darwin."""
        error = HomeAssistantConnectionError("All connection attempts failed")
        with patch("ha_mcp.tools.helpers.sys") as mock_sys:
            mock_sys.platform = "darwin"
            result = exception_to_structured_error(error, raise_error=False)

        suggestions = result["error"].get("suggestions", [])
        assert any(MACOS_HINT_FRAGMENT in s for s in suggestions)

    def test_hints_appear_on_darwin_for_connection_timeout(self):
        """macOS hints should appear for CONNECTION_TIMEOUT on darwin."""
        error = HomeAssistantConnectionError("Request timeout: timed out")
        with patch("ha_mcp.tools.helpers.sys") as mock_sys:
            mock_sys.platform = "darwin"
            result = exception_to_structured_error(error, raise_error=False)

        suggestions = result["error"].get("suggestions", [])
        assert any(MACOS_HINT_FRAGMENT in s for s in suggestions)

    def test_hints_absent_on_linux(self):
        """macOS hints should NOT appear on non-darwin platforms."""
        error = HomeAssistantConnectionError("All connection attempts failed")
        with patch("ha_mcp.tools.helpers.sys") as mock_sys:
            mock_sys.platform = "linux"
            result = exception_to_structured_error(error, raise_error=False)

        suggestions = result["error"].get("suggestions", [])
        assert not any(MACOS_HINT_FRAGMENT in s for s in suggestions)

    def test_hints_absent_for_non_connection_errors(self):
        """macOS hints should NOT appear for non-connection errors."""
        error = ValueError("invalid parameter")
        with patch("ha_mcp.tools.helpers.sys") as mock_sys:
            mock_sys.platform = "darwin"
            result = exception_to_structured_error(error, raise_error=False)

        suggestions = result["error"].get("suggestions", [])
        assert not any(MACOS_HINT_FRAGMENT in s for s in suggestions)

    def test_caller_suggestions_override_defaults_but_hints_survive(self):
        """Caller-provided suggestions should replace defaults, but macOS hints still append."""
        error = HomeAssistantConnectionError("All connection attempts failed")
        caller_suggestions = ["Custom suggestion A", "Custom suggestion B"]
        with patch("ha_mcp.tools.helpers.sys") as mock_sys:
            mock_sys.platform = "darwin"
            result = exception_to_structured_error(
                error, raise_error=False, suggestions=caller_suggestions
            )

        suggestions = result["error"]["suggestions"]
        # Caller suggestions should be present
        assert "Custom suggestion A" in suggestions
        assert "Custom suggestion B" in suggestions
        # macOS hints should also be present
        assert any(MACOS_HINT_FRAGMENT in s for s in suggestions)

    def test_caller_suggestions_still_override_on_non_darwin(self):
        """On non-darwin, caller suggestions should replace defaults (original behavior)."""
        error = HomeAssistantConnectionError("All connection attempts failed")
        caller_suggestions = ["Custom suggestion"]
        with patch("ha_mcp.tools.helpers.sys") as mock_sys:
            mock_sys.platform = "linux"
            result = exception_to_structured_error(
                error, raise_error=False, suggestions=caller_suggestions
            )

        assert result["error"]["suggestions"] == ["Custom suggestion"]

    def test_string_matched_connection_errors_get_hints(self):
        """Generic exceptions classified as connection errors via string matching
        should also get macOS hints."""
        error = Exception("connection refused to host")
        with patch("ha_mcp.tools.helpers.sys") as mock_sys:
            mock_sys.platform = "darwin"
            result = exception_to_structured_error(error, raise_error=False)

        assert result["error"]["code"] == ErrorCode.CONNECTION_FAILED
        suggestions = result["error"].get("suggestions", [])
        assert any(MACOS_HINT_FRAGMENT in s for s in suggestions)

    def test_hints_include_ssh_tunnel_suggestion(self):
        """macOS hints should include the SSH tunnel workaround."""
        error = HomeAssistantConnectionError("All connection attempts failed")
        with patch("ha_mcp.tools.helpers.sys") as mock_sys:
            mock_sys.platform = "darwin"
            result = exception_to_structured_error(error, raise_error=False)

        suggestions = result["error"].get("suggestions", [])
        assert any("SSH tunnel" in s for s in suggestions)

    def test_hints_include_http_vs_https_suggestion(self):
        """macOS hints should include http vs https guidance."""
        error = HomeAssistantConnectionError("All connection attempts failed")
        with patch("ha_mcp.tools.helpers.sys") as mock_sys:
            mock_sys.platform = "darwin"
            result = exception_to_structured_error(error, raise_error=False)

        suggestions = result["error"].get("suggestions", [])
        assert any("http://" in s for s in suggestions)
