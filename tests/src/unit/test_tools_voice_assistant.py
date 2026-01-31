"""Unit tests for voice assistant tools module."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from ha_mcp.tools.tools_voice_assistant import (
    register_voice_assistant_tools,
    KNOWN_ASSISTANTS,
)


class TestHaListExposedEntities:
    """Test ha_get_entity_exposure tool validation logic."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server."""
        mcp = MagicMock()
        self.registered_tools = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func
            return wrapper

        mcp.tool = tool_decorator
        return mcp

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def list_tool(self, mock_mcp, mock_client):
        """Register tools and return the ha_get_entity_exposure function."""
        register_voice_assistant_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_get_entity_exposure"]

    @pytest.mark.asyncio
    async def test_list_all_entities_success(self, mock_mcp, mock_client):
        """List all exposed entities should succeed."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "exposed_entities": {
                        "light.living_room": {"conversation": True},
                        "light.bedroom": {"cloud.alexa": True},
                    }
                }
            }
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity_exposure"]

        result = await tool()

        assert result["success"] is True
        assert result["count"] == 2
        assert "exposed_entities" in result
        assert "summary" in result

    @pytest.mark.asyncio
    async def test_filter_by_valid_assistant(self, mock_mcp, mock_client):
        """Filter by valid assistant should work."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "exposed_entities": {
                        "light.living_room": {"conversation": True},
                        "light.bedroom": {"cloud.alexa": True},
                    }
                }
            }
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity_exposure"]

        result = await tool(assistant="conversation")

        assert result["success"] is True
        assert result["filters_applied"]["assistant"] == "conversation"
        # Only entities exposed to conversation should be in filtered results
        assert "light.living_room" in result["exposed_entities"]
        assert "light.bedroom" not in result["exposed_entities"]

    @pytest.mark.asyncio
    async def test_filter_by_invalid_assistant_rejected(self, list_tool):
        """Filter by invalid assistant should be rejected."""
        result = await list_tool(assistant="invalid_assistant")

        assert result["success"] is False
        assert "Invalid assistant" in result["error"]
        assert "valid_assistants" in result
        assert result["valid_assistants"] == KNOWN_ASSISTANTS

    @pytest.mark.asyncio
    async def test_filter_by_entity_id(self, mock_mcp, mock_client):
        """Filter by specific entity_id should work."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "exposed_entities": {
                        "light.living_room": {"conversation": True},
                        "light.bedroom": {"cloud.alexa": True},
                    }
                }
            }
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity_exposure"]

        result = await tool(entity_id="light.living_room")

        assert result["success"] is True
        assert result["entity_id"] == "light.living_room"
        # When entity_id is provided, returns exposed_to dict showing status per assistant
        assert result["exposed_to"]["conversation"] is True
        assert result["is_exposed_anywhere"] is True
        assert result["has_custom_settings"] is True

    @pytest.mark.asyncio
    async def test_filter_by_nonexistent_entity_id(self, mock_mcp, mock_client):
        """Filter by nonexistent entity_id should return defaults."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "exposed_entities": {
                        "light.living_room": {"conversation": True},
                    }
                }
            }
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity_exposure"]

        result = await tool(entity_id="light.nonexistent")

        assert result["success"] is True
        assert result["entity_id"] == "light.nonexistent"
        assert result["is_exposed_anywhere"] is False
        assert result["has_custom_settings"] is False
        # Note field should be present when entity has no custom settings
        assert result["note"] is not None

    @pytest.mark.asyncio
    async def test_summary_counts_per_assistant(self, mock_mcp, mock_client):
        """Summary should count entities per assistant."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "exposed_entities": {
                        "light.living_room": {"conversation": True, "cloud.alexa": True},
                        "light.bedroom": {"conversation": True},
                        "light.kitchen": {"cloud.google_assistant": True},
                    }
                }
            }
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity_exposure"]

        result = await tool()

        assert result["success"] is True
        assert result["summary"]["conversation"] == 2
        assert result["summary"]["cloud.alexa"] == 1
        assert result["summary"]["cloud.google_assistant"] == 1

    @pytest.mark.asyncio
    async def test_websocket_error_response(self, mock_mcp, mock_client):
        """WebSocket error response should be handled."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": {"message": "Service unavailable"}
            }
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity_exposure"]

        result = await tool()

        assert result["success"] is False
        assert "Service unavailable" in result["error"]

    @pytest.mark.asyncio
    async def test_websocket_exception(self, mock_mcp, mock_client):
        """WebSocket exception should be caught."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=Exception("Network error")
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity_exposure"]

        result = await tool()

        assert result["success"] is False
        assert "Network error" in result["error"]


class TestHaGetEntityExposure:
    """Test ha_get_entity_exposure tool validation logic."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server."""
        mcp = MagicMock()
        self.registered_tools = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func
            return wrapper

        mcp.tool = tool_decorator
        return mcp

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def get_exposure_tool(self, mock_mcp, mock_client):
        """Register tools and return the ha_get_entity_exposure function."""
        register_voice_assistant_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_get_entity_exposure"]

    @pytest.mark.asyncio
    async def test_get_exposure_with_custom_settings(self, mock_mcp, mock_client):
        """Entity with custom settings should show exposure status."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "exposed_entities": {
                        "light.living_room": {
                            "conversation": True,
                            "cloud.alexa": False,
                        },
                    }
                }
            }
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity_exposure"]

        result = await tool(entity_id="light.living_room")

        assert result["success"] is True
        assert result["entity_id"] == "light.living_room"
        assert result["exposed_to"]["conversation"] is True
        assert result["exposed_to"]["cloud.alexa"] is False
        assert result["exposed_to"]["cloud.google_assistant"] is False
        assert result["is_exposed_anywhere"] is True
        assert result["has_custom_settings"] is True

    @pytest.mark.asyncio
    async def test_get_exposure_without_custom_settings(self, mock_mcp, mock_client):
        """Entity without custom settings should show defaults."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "exposed_entities": {}
                }
            }
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity_exposure"]

        result = await tool(entity_id="light.living_room")

        assert result["success"] is True
        assert result["entity_id"] == "light.living_room"
        assert result["is_exposed_anywhere"] is False
        assert result["has_custom_settings"] is False
        assert result["note"] is not None  # Should have note about default settings

    @pytest.mark.asyncio
    async def test_get_exposure_all_assistants(self, mock_mcp, mock_client):
        """Entity exposed to all assistants should show all True."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "exposed_entities": {
                        "light.living_room": {
                            "conversation": True,
                            "cloud.alexa": True,
                            "cloud.google_assistant": True,
                        },
                    }
                }
            }
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity_exposure"]

        result = await tool(entity_id="light.living_room")

        assert result["success"] is True
        assert result["exposed_to"]["conversation"] is True
        assert result["exposed_to"]["cloud.alexa"] is True
        assert result["exposed_to"]["cloud.google_assistant"] is True
        assert result["is_exposed_anywhere"] is True

    @pytest.mark.asyncio
    async def test_get_exposure_no_assistants(self, mock_mcp, mock_client):
        """Entity hidden from all assistants should show all False."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "exposed_entities": {
                        "light.living_room": {
                            "conversation": False,
                            "cloud.alexa": False,
                            "cloud.google_assistant": False,
                        },
                    }
                }
            }
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity_exposure"]

        result = await tool(entity_id="light.living_room")

        assert result["success"] is True
        assert result["is_exposed_anywhere"] is False

    @pytest.mark.asyncio
    async def test_websocket_error_response(self, mock_mcp, mock_client):
        """WebSocket error should be handled."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": {"message": "Access denied"}
            }
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity_exposure"]

        result = await tool(entity_id="light.living_room")

        assert result["success"] is False
        assert "Access denied" in result["error"]
        assert result["entity_id"] == "light.living_room"

    @pytest.mark.asyncio
    async def test_websocket_exception(self, mock_mcp, mock_client):
        """WebSocket exception should be caught."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=Exception("Timeout")
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity_exposure"]

        result = await tool(entity_id="light.living_room")

        assert result["success"] is False
        assert "Timeout" in result["error"]
        assert result["entity_id"] == "light.living_room"


class TestKnownAssistants:
    """Test KNOWN_ASSISTANTS constant."""

    def test_known_assistants_includes_conversation(self):
        """KNOWN_ASSISTANTS should include 'conversation'."""
        assert "conversation" in KNOWN_ASSISTANTS

    def test_known_assistants_includes_cloud_alexa(self):
        """KNOWN_ASSISTANTS should include 'cloud.alexa'."""
        assert "cloud.alexa" in KNOWN_ASSISTANTS

    def test_known_assistants_includes_cloud_google_assistant(self):
        """KNOWN_ASSISTANTS should include 'cloud.google_assistant'."""
        assert "cloud.google_assistant" in KNOWN_ASSISTANTS

    def test_known_assistants_count(self):
        """KNOWN_ASSISTANTS should have exactly 3 entries."""
        assert len(KNOWN_ASSISTANTS) == 3


class TestWebSocketMessageFormat:
    """Test that WebSocket messages are formatted correctly."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server."""
        mcp = MagicMock()
        self.registered_tools = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func
            return wrapper

        mcp.tool = tool_decorator
        return mcp

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(return_value={"success": True})
        return client

    @pytest.mark.asyncio
    async def test_list_entities_message_format(self, mock_mcp, mock_client):
        """List entities should send correct WebSocket message."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {"exposed_entities": {}}
            }
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity_exposure"]

        await tool()

        mock_client.send_websocket_message.assert_called_once()
        call_args = mock_client.send_websocket_message.call_args[0][0]

        assert call_args["type"] == "homeassistant/expose_entity/list"

    @pytest.mark.asyncio
    async def test_get_exposure_message_format(self, mock_mcp, mock_client):
        """Get exposure should send correct WebSocket message."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {"exposed_entities": {}}
            }
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity_exposure"]

        await tool(entity_id="light.living_room")

        mock_client.send_websocket_message.assert_called_once()
        call_args = mock_client.send_websocket_message.call_args[0][0]

        assert call_args["type"] == "homeassistant/expose_entity/list"
