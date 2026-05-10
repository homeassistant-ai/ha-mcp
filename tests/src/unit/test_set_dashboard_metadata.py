"""Unit tests for ha_config_set_dashboard metadata-update path."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_config_dashboards import register_config_dashboard_tools


class TestSetDashboardMetadataUpdate:
    """Test the metadata update path introduced by merging ha_config_update_dashboard_metadata."""

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools: dict = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func

            return wrapper

        mcp.tool = tool_decorator
        return mcp

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def set_tool(self, mock_mcp, mock_client):
        register_config_dashboard_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_config_set_dashboard"]

    def _make_dashboard_list(self, url_path: str, dashboard_id: str = "dash-1"):
        """Helper: mock existing dashboards list response."""
        return {"result": [{"url_path": url_path, "id": dashboard_id}]}

    @pytest.mark.asyncio
    async def test_metadata_updated_true_when_title_provided_for_existing(
        self, set_tool, mock_client
    ):
        """metadata_updated=True when title provided for an existing dashboard."""
        mock_client.send_websocket_message.side_effect = [
            self._make_dashboard_list("my-dashboard"),  # lovelace/dashboards/list
            {"success": True},  # lovelace/dashboards/update (metadata)
        ]

        result = await set_tool(url_path="my-dashboard", title="New Title")

        assert result["success"] is True
        assert result["metadata_updated"] is True
        assert result["dashboard_created"] is False

        # Verify the metadata update call was made with correct args
        calls = mock_client.send_websocket_message.call_args_list
        meta_call = calls[1][0][0]
        assert meta_call["type"] == "lovelace/dashboards/update"
        assert meta_call["dashboard_id"] == "dash-1"
        assert meta_call["title"] == "New Title"

    @pytest.mark.asyncio
    async def test_metadata_updated_false_when_no_metadata_params(
        self, set_tool, mock_client
    ):
        """metadata_updated=False when no metadata params given for existing dashboard."""
        mock_client.send_websocket_message.side_effect = [
            self._make_dashboard_list("my-dashboard"),  # lovelace/dashboards/list
        ]

        result = await set_tool(url_path="my-dashboard")

        assert result["success"] is True
        assert result["metadata_updated"] is False
        # Only one WS call (list), no metadata update
        assert mock_client.send_websocket_message.call_count == 1

    @pytest.mark.asyncio
    async def test_metadata_update_multiple_fields(self, set_tool, mock_client):
        """Multiple metadata fields are sent in a single update call."""
        mock_client.send_websocket_message.side_effect = [
            self._make_dashboard_list("my-dashboard"),
            {"success": True},
        ]

        result = await set_tool(
            url_path="my-dashboard",
            title="Updated",
            icon="mdi:home",
            require_admin=True,
            show_in_sidebar=False,
        )

        assert result["success"] is True
        assert result["metadata_updated"] is True

        meta_call = mock_client.send_websocket_message.call_args_list[1][0][0]
        assert meta_call["title"] == "Updated"
        assert meta_call["icon"] == "mdi:home"
        assert meta_call["require_admin"] is True
        assert meta_call["show_in_sidebar"] is False

    @pytest.mark.asyncio
    async def test_metadata_update_fails_returns_error(self, set_tool, mock_client):
        """When the metadata update WS call fails, the tool raises ToolError."""
        mock_client.send_websocket_message.side_effect = [
            self._make_dashboard_list("my-dashboard"),
            {"success": False, "error": {"message": "Permission denied"}},
        ]

        with pytest.raises(ToolError) as exc_info:
            await set_tool(url_path="my-dashboard", title="Unauthorized")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "metadata" in error_data["error"]["message"].lower()
        assert "Permission denied" in error_data["error"]["message"]

    @pytest.mark.asyncio
    async def test_metadata_update_skipped_when_dashboard_id_none(
        self, set_tool, mock_client
    ):
        """When dashboard_id cannot be resolved, metadata update is skipped with a hint."""
        # Lovelace dashboard not in the list (fresh install scenario)
        mock_client.send_websocket_message.return_value = {"result": []}

        result = await set_tool(url_path="lovelace", title="My Home")

        assert result["success"] is True
        assert result["metadata_updated"] is False
        assert "hint" in result
        assert "no storage ID" in result["hint"]

    @pytest.mark.asyncio
    async def test_false_booleans_are_not_filtered_out(self, set_tool, mock_client):
        """False bool values for require_admin/show_in_sidebar must be passed through."""
        mock_client.send_websocket_message.side_effect = [
            self._make_dashboard_list("my-dashboard"),
            {"success": True},
        ]

        await set_tool(
            url_path="my-dashboard",
            require_admin=False,
            show_in_sidebar=False,
        )

        meta_call = mock_client.send_websocket_message.call_args_list[1][0][0]
        assert meta_call["require_admin"] is False
        assert meta_call["show_in_sidebar"] is False


class TestSetDashboardListCallDedup:
    """When the pre-resolver fires (internal-id branch), the existence-check
    site reuses the pre-fetched dashboards list rather than issuing a second
    ``lovelace/dashboards/list`` round-trip.

    The other-branch tests act as regression guards so a future change
    that re-introduces a redundant list call (or accidentally drops the
    one fetch on the canonical-url_path branch) is caught here."""

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools: dict = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func

            return wrapper

        mcp.tool = tool_decorator
        return mcp

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def set_tool(self, mock_mcp, mock_client):
        from ha_mcp.tools.tools_config_dashboards import register_config_dashboard_tools

        register_config_dashboard_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_config_set_dashboard"]

    @staticmethod
    def _list_call_count(mock_client) -> int:
        return sum(
            1
            for c in mock_client.send_websocket_message.call_args_list
            if c.args and c.args[0].get("type") == "lovelace/dashboards/list"
        )

    @pytest.mark.asyncio
    async def test_internal_id_branch_calls_list_only_once(self, set_tool, mock_client):
        """Pre-resolver fires (hyphenless ``my_dash``) and matches; the
        existence-check site MUST reuse that list instead of fetching
        again. Total ``lovelace/dashboards/list`` calls = 1."""
        dashboards_list = {"result": [{"url_path": "my-dash", "id": "my_dash"}]}
        mock_client.send_websocket_message.side_effect = [
            dashboards_list,  # pre-resolver fetch
            {"success": True},  # metadata update
        ]

        result = await set_tool(url_path="my_dash", title="Renamed")

        assert self._list_call_count(mock_client) == 1, (
            "internal-id branch must reuse the pre-resolver's dashboards list"
        )
        assert result["success"] is True
        # Pre-resolver rewrote my_dash -> my-dash; surface marker stays.
        assert result.get("resolved_from") == "my_dash"
        # Metadata update did fire on the canonical url_path.
        meta_call = mock_client.send_websocket_message.call_args_list[1].args[0]
        assert meta_call["type"] == "lovelace/dashboards/update"
        assert meta_call["dashboard_id"] == "my_dash"

    @pytest.mark.asyncio
    async def test_canonical_url_path_branch_still_calls_list_once(
        self, set_tool, mock_client
    ):
        """Already-canonical ``my-dash`` (hyphen present) skips the
        pre-resolver; the existence-check site still fetches once.
        Regression guard: total list calls = 1, not 0 (pre-resolver
        didn't fire) and not 2 (no redundant fetch)."""
        mock_client.send_websocket_message.side_effect = [
            {"result": [{"url_path": "my-dash", "id": "my_dash"}]},
            {"success": True},
        ]

        result = await set_tool(url_path="my-dash", title="Renamed")

        assert self._list_call_count(mock_client) == 1
        assert result["success"] is True
        assert result.get("resolved_from") is None

    @pytest.mark.asyncio
    async def test_internal_id_no_match_falls_through_to_hyphen_check(
        self, set_tool, mock_client
    ):
        """Hyphenless identifier with no matching dashboard: pre-resolver
        fetches the list, finds no match, ``url_path`` stays
        unchanged, then fails the hyphen-validation check before any
        existence-check fetch can fire. Total list calls = 1."""
        mock_client.send_websocket_message.side_effect = [
            {"result": [{"url_path": "other-dash", "id": "other_dash"}]},
        ]

        with pytest.raises(ToolError) as exc_info:
            await set_tool(url_path="ghost", title="X")

        body = json.loads(str(exc_info.value))
        assert "url_path must contain a hyphen" in body["error"]["message"]
        assert self._list_call_count(mock_client) == 1

    @pytest.mark.asyncio
    async def test_canonical_url_path_branch_warns_on_unexpected_shape(
        self, set_tool, mock_client, caplog
    ):
        """Existence-check fallback fetch logs a warning on unexpected
        response shapes, mirroring ``_resolve_dashboard``'s same-arm
        behaviour. Without this parity, an HA-side shape change would go
        silent on the canonical-url_path branch — the bug reports would
        be wedged on ``dashboard_exists = False`` with no operator
        signal."""
        import logging

        mock_client.send_websocket_message.side_effect = [
            "unexpected string",  # response shape failure
            {"success": True},  # create-dashboard call (still proceeds)
        ]

        with caplog.at_level(
            logging.WARNING, logger="ha_mcp.tools.tools_config_dashboards"
        ):
            await set_tool(url_path="my-dash", title="New")

        assert any(
            "unexpected shape" in rec.message and "type=str" in rec.message
            for rec in caplog.records
        ), (
            f"expected an 'unexpected shape' warning naming the response "
            f"type; got {[rec.message for rec in caplog.records]}"
        )
