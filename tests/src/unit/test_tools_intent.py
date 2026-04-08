"""Unit tests for ha_call_service intent routing (issue #899)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_service import register_service_tools


class TestHaCallServiceIntentRouting:
    """Test the intent= parameter on ha_call_service."""

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self._tools = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self._tools[func.__name__] = func
                return func
            return wrapper

        mcp.tool = tool_decorator
        return mcp

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.call_service = AsyncMock()
        client.call_intent = AsyncMock()
        client.get_entity_state = AsyncMock(return_value={"state": "on"})
        return client

    @pytest.fixture
    def mock_device_tools(self):
        dt = MagicMock()
        dt.get_device_operation_status = AsyncMock()
        dt.get_bulk_operation_status = AsyncMock()
        dt.bulk_device_control = AsyncMock()
        return dt

    @pytest.fixture
    def call_service_tool(self, mock_mcp, mock_client, mock_device_tools):
        register_service_tools(mock_mcp, mock_client, device_tools=mock_device_tools)
        return self._tools["ha_call_service"]

    # --- Intent routing tests ---

    @pytest.mark.asyncio
    async def test_intent_media_search_routes_to_call_intent(
        self, call_service_tool, mock_client
    ):
        """ha_call_service with intent= must call client.call_intent, not call_service."""
        mock_client.call_intent = AsyncMock(return_value={
            "response": {
                "speech": {"plain": {"speech": "Playing jazz music"}},
                "response_type": "action_done",
            },
            "conversation_id": None,
        })

        result = await call_service_tool(
            intent="HassMediaSearch",
            data={"media_type": "music", "search_term": "jazz"},
        )

        mock_client.call_intent.assert_called_once_with(
            "HassMediaSearch", {"media_type": "music", "search_term": "jazz"}
        )
        mock_client.call_service.assert_not_called()
        assert result["success"] is True
        assert result["intent"] == "HassMediaSearch"
        assert result["speech"] == "Playing jazz music"
        assert result["response_type"] == "action_done"

    @pytest.mark.asyncio
    async def test_intent_media_pause_no_data(self, call_service_tool, mock_client):
        """Intent without data must call call_intent with None data."""
        mock_client.call_intent = AsyncMock(return_value={
            "response": {
                "speech": {"plain": {"speech": "Paused"}},
                "response_type": "action_done",
            },
            "conversation_id": None,
        })

        result = await call_service_tool(intent="HassMediaPause")

        mock_client.call_intent.assert_called_once_with("HassMediaPause", None)
        assert result["success"] is True
        assert result["intent"] == "HassMediaPause"

    @pytest.mark.asyncio
    async def test_normal_service_call_unaffected(self, call_service_tool, mock_client):
        """Without intent=, ha_call_service must use the normal service path."""
        mock_client.call_service = AsyncMock(return_value=[])

        result = await call_service_tool(
            domain="light", service="turn_on", entity_id="light.living_room"
        )

        mock_client.call_service.assert_called_once()
        mock_client.call_intent.assert_not_called()
        assert result["success"] is True
        assert result["domain"] == "light"

    @pytest.mark.asyncio
    async def test_intent_invalid_json_data_raises_tool_error(
        self, call_service_tool, mock_client
    ):
        """Invalid JSON in data= with intent= must raise ToolError, not crash."""
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            await call_service_tool(
                intent="HassMediaSearch",
                data="{not valid json",
            )

        mock_client.call_intent.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_domain_and_service_raises_tool_error(
        self, call_service_tool, mock_client
    ):
        """Calling without intent= and without domain/service must raise ToolError."""
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            await call_service_tool()

        mock_client.call_service.assert_not_called()
        mock_client.call_intent.assert_not_called()

    @pytest.mark.asyncio
    async def test_intent_with_json_string_data(self, call_service_tool, mock_client):
        """data= as a JSON string must be parsed and forwarded correctly."""
        mock_client.call_intent = AsyncMock(return_value={
            "response": {
                "speech": {"plain": {"speech": "Searching for jazz"}},
                "response_type": "action_done",
            },
            "conversation_id": None,
        })

        result = await call_service_tool(
            intent="HassMediaSearch",
            data='{"media_type": "music", "search_term": "jazz"}',
        )

        mock_client.call_intent.assert_called_once_with(
            "HassMediaSearch", {"media_type": "music", "search_term": "jazz"}
        )
        assert result["success"] is True

