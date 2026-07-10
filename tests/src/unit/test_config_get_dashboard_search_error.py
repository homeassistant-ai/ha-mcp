"""Unit tests for ha_config_get_dashboard error handling.

Covers two distinct failure axes:
 - Search mode: structured error responses must not leak internal Python type
   names or tracebacks.
 - List mode (list_only=True): unexpected HA response shapes must emit a
   warning and return an empty list, not a silent degradation.

Replaces test_dashboard_find_card_error.py after ha_dashboard_find_card
was merged into ha_config_get_dashboard (issue #901).
"""

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_config_dashboards import (
    _LAZY_RESOLVE_TRIGGER,
    DashboardConfigTools,
)


class TestConfigGetDashboardSearchErrorHandling:
    """Test ha_config_get_dashboard search mode error path does not leak internals."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            side_effect=RuntimeError("Connection lost")
        )
        return client

    @pytest.fixture
    def get_dashboard_tool(self, mock_client):
        """Return the ha_config_get_dashboard bound method."""
        return DashboardConfigTools(mock_client).ha_config_get_dashboard

    @pytest.mark.asyncio
    async def test_error_does_not_leak_internals(self, get_dashboard_tool):
        """Error response must NOT contain 'error_type' or 'traceback'."""
        with pytest.raises(ToolError) as exc_info:
            await get_dashboard_tool(url_path="lovelace", entity_id="light.test")

        result = json.loads(str(exc_info.value))
        assert result["success"] is False
        assert isinstance(result["error"], dict), (
            "error must be structured dict, not raw string"
        )
        assert "code" in result["error"]
        assert "message" in result["error"]
        assert "error_type" not in result
        assert "traceback" not in result

    @pytest.mark.asyncio
    async def test_error_includes_suggestions(self, get_dashboard_tool):
        """Error response must include dashboard-specific suggestions."""
        with pytest.raises(ToolError) as exc_info:
            await get_dashboard_tool(url_path="lovelace", entity_id="light.test")

        result = json.loads(str(exc_info.value))
        suggestions = result["error"]["suggestions"]
        assert "Check HA connection" in suggestions
        assert (
            "Verify dashboard with ha_config_get_dashboard(list_only=True)"
            in suggestions
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exception_cls,exception_msg,expected_code",
        [
            (ValueError, "invalid dashboard", "VALIDATION_FAILED"),
            (TimeoutError, "timed out", "TIMEOUT_OPERATION"),
            (RuntimeError, "unexpected failure", "INTERNAL_ERROR"),
        ],
    )
    async def test_different_exception_types_produce_correct_error_codes(
        self,
        mock_client,
        get_dashboard_tool,
        exception_cls,
        exception_msg,
        expected_code,
    ):
        """Different exception types should map to appropriate error codes."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=exception_cls(exception_msg)
        )

        with pytest.raises(ToolError) as exc_info:
            await get_dashboard_tool(url_path="lovelace", entity_id="light.test")

        result = json.loads(str(exc_info.value))
        assert result["success"] is False
        assert result["error"]["code"] == expected_code


class TestGetDashboardListOnlyUnexpectedShape:
    """list_only=True emits a warning (not a failure) on unexpected HA response shape.

    fetch_dashboards_list logs at WARNING and returns None; the ``or []``
    fallback means the tool still returns a valid success response with an
    empty dashboards list. This test pins the behavior introduced when the
    inline fetch was extracted to the shared helper so that a silent ``[]``
    return can no longer mask a future HA shape change at the
    ``ha_config_get_dashboard`` list path (``list_only=True`` branch).
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def get_dashboard_tool(self, mock_client):
        return DashboardConfigTools(mock_client).ha_config_get_dashboard

    @pytest.mark.asyncio
    async def test_unexpected_shape_logs_warning_and_returns_empty_list(
        self, get_dashboard_tool, mock_client, caplog
    ):
        mock_client.send_websocket_message.return_value = "unexpected string"

        with caplog.at_level(
            logging.WARNING, logger="ha_mcp.tools.tools_config_dashboards"
        ):
            result = await get_dashboard_tool(list_only=True)

        assert result["success"] is True
        assert result["dashboards"] == []
        assert result["count"] == 0
        assert any(
            "unexpected shape" in rec.message and "type=str" in rec.message
            for rec in caplog.records
        ), (
            f"expected an 'unexpected shape' warning naming the response "
            f"type; got {[rec.message for rec in caplog.records]}"
        )


class TestFindCardDisclosureWarnings:
    """Response-level ``warnings[]`` for the find-card disclosure layer.

    Pins the issue #1599 round-2 item-1 behaviour at the tool boundary: the
    warning keys off the *presence* of a non-traversed child-bearing shape
    (collected during the walk), not off a 0-match — so a matching un-walkable
    container no longer suppresses it and a true negative no longer cries wolf.
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def get_dashboard_tool(self, mock_client):
        return DashboardConfigTools(mock_client).ha_config_get_dashboard

    async def _search(self, tool, mock_client, config, **criteria):
        # url_path="default" → effective_url_path is None, so no lazy-resolve
        # retry: the single websocket reply carries the config under "result".
        mock_client.send_websocket_message.return_value = {"result": config}
        return await tool(url_path="default", **criteria)

    @pytest.mark.asyncio
    async def test_warns_on_uncoverable_shape_even_when_matched(
        self, get_dashboard_tool, mock_client
    ):
        """A picture-elements card that itself matches still triggers the warning
        — the suppressible-warning bug is closed."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "picture-elements",
                            "image": "/local/x.png",
                            "elements": [{"type": "state-badge", "entity": "light.pe"}],
                        }
                    ]
                }
            ]
        }
        result = await self._search(
            get_dashboard_tool,
            mock_client,
            config,
            card_type="picture-elements",
        )
        assert result["match_count"] == 1  # the container matched
        warnings = result.get("warnings", [])
        assert any("elements" in w for w in warnings), warnings
        assert any(".views[0].cards[0].elements" in w for w in warnings), warnings

    @pytest.mark.asyncio
    async def test_silent_on_true_negative_without_shape(
        self, get_dashboard_tool, mock_client
    ):
        """A 0-match over a fully-coverable dashboard emits no disclosure warning
        — the cry-wolf bug is closed."""
        config = {"views": [{"cards": [{"type": "tile", "entity": "light.plain"}]}]}
        result = await self._search(
            get_dashboard_tool, mock_client, config, card_type="nonexistent"
        )
        assert result["match_count"] == 0
        assert "warnings" not in result

    @pytest.mark.asyncio
    async def test_silent_on_empty_dashboard(self, get_dashboard_tool, mock_client):
        """An empty dashboard (no cards) emits no disclosure warning."""
        config = {"views": [{"cards": []}]}
        result = await self._search(
            get_dashboard_tool, mock_client, config, card_type="tile"
        )
        assert result["match_count"] == 0
        assert "warnings" not in result

    @pytest.mark.asyncio
    async def test_response_warnings_is_top_level_list_of_str(
        self, get_dashboard_tool, mock_client
    ):
        """When present, ``warnings`` is a top-level ``list[str]`` (return
        contract), never nested under ``data`` nor a singular string."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "picture-elements",
                            "elements": [{"type": "state-badge"}],
                        }
                    ]
                }
            ]
        }
        result = await self._search(
            get_dashboard_tool, mock_client, config, card_type="tile"
        )
        assert isinstance(result["warnings"], list)
        assert all(isinstance(w, str) for w in result["warnings"])


class TestResolvedUrlPathInErrorContext:
    """An unexpected exception raised after lazy-resolve must report the
    resolved url_path in error context, not the caller's original
    (pre-resolve) identifier.

    Regression test for the C901 decomposition of ha_config_get_dashboard:
    splitting the mode dispatch into _get_dashboard_search_mode /
    _get_dashboard_get_mode moved the url_path reassignment into a helper's
    local scope, so the outer method's except-block context silently fell
    back to reporting the unresolved identifier.
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def get_dashboard_tool(self, mock_client):
        return DashboardConfigTools(mock_client).ha_config_get_dashboard

    @pytest.mark.asyncio
    async def test_search_mode_reports_resolved_url_path(
        self, get_dashboard_tool, mock_client, monkeypatch
    ):
        mock_client.send_websocket_message.side_effect = [
            {
                "success": False,
                "error": {"message": f"{_LAZY_RESOLVE_TRIGGER}: internal-id"},
            },
            {"result": [{"id": "internal-id", "url_path": "resolved-path"}]},
            {"result": {"views": [{"cards": []}]}},
        ]
        monkeypatch.setattr(
            DashboardConfigTools,
            "_build_search_result",
            MagicMock(side_effect=RuntimeError("boom")),
        )

        with pytest.raises(ToolError) as exc_info:
            await get_dashboard_tool(url_path="internal-id", card_type="tile")

        result = json.loads(str(exc_info.value))
        assert result["url_path"] == "resolved-path"

    @pytest.mark.asyncio
    async def test_get_mode_reports_resolved_url_path(
        self, get_dashboard_tool, mock_client, monkeypatch
    ):
        mock_client.send_websocket_message.side_effect = [
            {
                "success": False,
                "error": {"message": f"{_LAZY_RESOLVE_TRIGGER}: internal-id"},
            },
            {"result": [{"id": "internal-id", "url_path": "resolved-path"}]},
            {"result": {"views": []}},
        ]
        monkeypatch.setattr(
            "ha_mcp.tools.tools_config_dashboards.compute_config_hash",
            MagicMock(side_effect=RuntimeError("boom")),
        )

        with pytest.raises(ToolError) as exc_info:
            await get_dashboard_tool(url_path="internal-id")

        result = json.loads(str(exc_info.value))
        assert result["url_path"] == "resolved-path"
