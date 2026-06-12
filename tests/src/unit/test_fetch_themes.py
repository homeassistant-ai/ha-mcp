"""Unit tests for _fetch_themes in tools_system module."""

from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools.tools_system import SystemTools
from ha_mcp.tools.util_helpers import summarize_theme_listing


def _ws_client_with_themes(themes_dict, default_theme=None, default_dark_theme=None):
    """Mock ws_client whose send_command('frontend/get_themes') returns themes."""
    ws = AsyncMock()
    ws.send_command.return_value = {
        "success": True,
        "result": {
            "themes": themes_dict,
            "default_theme": default_theme or "default",
            "default_dark_theme": default_dark_theme,
        },
    }
    return ws


class TestSummarizeThemeListing:
    """Unit coverage for the shared summarize_theme_listing helper."""

    def test_null_themes_value_is_treated_as_empty(self):
        """A degraded frontend may return themes: null - no AttributeError."""
        result = summarize_theme_listing({"themes": None, "default_theme": "default"})

        assert result["themes"] == []
        assert result["count"] == 0
        assert result["default_theme"] == "default"
        assert result["default_dark_theme"] is None


class TestFetchThemes:
    """Unit coverage for the _fetch_themes helper."""

    @pytest.mark.asyncio
    async def test_happy_path_with_themes(self):
        """_fetch_themes returns sorted theme names plus defaults."""
        ws = _ws_client_with_themes(
            {
                "default": {},
                "dark_blue": {"primary-color": "#1a237e"},
                "light_green": {"primary-color": "#2e7d32"},
            },
            default_theme="default",
            default_dark_theme="dark_blue",
        )

        result = await SystemTools._fetch_themes(ws)

        assert result["count"] == 3
        assert result["themes"] == ["dark_blue", "default", "light_green"]
        assert result["default_theme"] == "default"
        assert result["default_dark_theme"] == "dark_blue"

    @pytest.mark.asyncio
    async def test_recovery_mode_shape_missing_default_dark_theme(self):
        """In recovery/safe mode, core returns no default_dark_theme key."""
        ws = AsyncMock()
        ws.send_command.return_value = {
            "success": True,
            "result": {
                "themes": {},
                "default_theme": "default",
            },
        }

        result = await SystemTools._fetch_themes(ws)

        assert result["count"] == 0
        assert result["themes"] == []
        assert result["default_theme"] == "default"
        assert result["default_dark_theme"] is None

    @pytest.mark.asyncio
    async def test_ws_failure_path(self):
        """WS send_command returning success=False surfaces error."""
        ws = AsyncMock()
        ws.send_command.return_value = {
            "success": False,
            "error": {"code": "unknown_error", "message": "frontend not ready"},
        }

        result = await SystemTools._fetch_themes(ws)

        assert result["count"] == 0
        assert result["themes"] == []
        assert "frontend not ready" in result["error"]

    @pytest.mark.asyncio
    async def test_ws_exception_captured_as_error(self):
        """Exceptions during the WS call are caught and surfaced via error."""
        ws = AsyncMock()
        ws.send_command.side_effect = RuntimeError("ws disconnect")

        result = await SystemTools._fetch_themes(ws)

        assert result["count"] == 0
        assert "ws disconnect" in result["error"]

    @pytest.mark.asyncio
    async def test_valid_include_accepted(self):
        """include='themes' is in VALID_INCLUDES and does not trigger warning."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from ha_mcp.tools.tools_system import SystemTools

        client = MagicMock()
        tools = SystemTools(client)
        mock_themes = AsyncMock(return_value={"themes": [], "count": 0})

        ws_client = MagicMock()
        ws_client.disconnect = AsyncMock()
        baseline = {"success": True, "health_info": {}}

        with (
            patch.object(
                SystemTools,
                "_fetch_health_info",
                new=AsyncMock(return_value=(ws_client, baseline)),
            ),
            patch.object(SystemTools, "_fetch_themes", new=mock_themes),
        ):
            result = await tools.ha_get_system_health(include="themes")

        assert "themes" in result
        assert result.get("warnings") is None or not any(
            "Unknown include" in w for w in result.get("warnings", [])
        )
        mock_themes.assert_awaited_once()
