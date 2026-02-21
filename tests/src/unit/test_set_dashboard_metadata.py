"""Unit tests for ha_config_set_dashboard metadata-update path."""

from unittest.mock import AsyncMock, MagicMock

import pytest

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
        """When the metadata update WS call fails, the tool returns an error."""
        mock_client.send_websocket_message.side_effect = [
            self._make_dashboard_list("my-dashboard"),
            {"success": False, "error": {"message": "Permission denied"}},
        ]

        result = await set_tool(url_path="my-dashboard", title="Unauthorized")

        assert result["success"] is False
        assert "metadata" in result["error"].lower()
        assert "Permission denied" in result["error"]

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
