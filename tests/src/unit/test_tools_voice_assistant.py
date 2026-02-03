"""Unit tests for voice assistant tools module."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_voice_assistant import (
    register_voice_assistant_tools,
    KNOWN_ASSISTANTS,
)


class TestHaExposeEntity:
    """Test ha_expose_entity tool validation logic."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server."""
        mcp = MagicMock()
        # Store registered tools for testing
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
    def expose_tool(self, mock_mcp, mock_client):
        """Register tools and return the ha_expose_entity function."""
        register_voice_assistant_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_expose_entity"]

    @pytest.fixture
    def list_tool(self, mock_mcp, mock_client):
        """Register tools and return the ha_get_entity_exposure function."""
        register_voice_assistant_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_get_entity_exposure"]

    @pytest.fixture
    def get_exposure_tool(self, mock_mcp, mock_client):
        """Register tools and return the ha_get_entity_exposure function."""
        register_voice_assistant_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_get_entity_exposure"]

    # ==================== Entity ID Validation Tests ====================

    @pytest.mark.asyncio
    async def test_empty_list_entity_ids_returns_error(self, expose_tool):
        """Empty list for entity_ids should return an error."""
        result = await expose_tool(
            entity_ids=[],
            assistants="conversation",
            should_expose=True
        )
        assert result["success"] is False
        assert "entity_ids is required" in result["error"]

    @pytest.mark.asyncio
    async def test_single_entity_id_string(self, mock_mcp, mock_client):
        """Single entity ID as string should work."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True}
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_expose_entity"]

        result = await tool(
            entity_ids="light.living_room",
            assistants="conversation",
            should_expose=True
        )

        assert result["success"] is True
        assert result["entity_ids"] == ["light.living_room"]

    @pytest.mark.asyncio
    async def test_multiple_entity_ids_list(self, mock_mcp, mock_client):
        """Multiple entity IDs as list should work."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True}
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_expose_entity"]

        result = await tool(
            entity_ids=["light.living_room", "light.bedroom"],
            assistants="conversation",
            should_expose=True
        )

        assert result["success"] is True
        assert result["entity_ids"] == ["light.living_room", "light.bedroom"]

    @pytest.mark.asyncio
    async def test_entity_ids_json_array_string(self, mock_mcp, mock_client):
        """Entity IDs as JSON array string should be parsed."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True}
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_expose_entity"]

        result = await tool(
            entity_ids='["light.living_room", "light.bedroom"]',
            assistants="conversation",
            should_expose=True
        )

        assert result["success"] is True
        assert result["entity_ids"] == ["light.living_room", "light.bedroom"]

    # ==================== Assistant Name Validation Tests ====================

    @pytest.mark.asyncio
    async def test_valid_assistant_conversation(self, mock_mcp, mock_client):
        """'conversation' is a valid assistant name."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True}
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_expose_entity"]

        result = await tool(
            entity_ids="light.test",
            assistants="conversation",
            should_expose=True
        )

        assert result["success"] is True
        assert result["assistants"] == ["conversation"]

    @pytest.mark.asyncio
    async def test_valid_assistant_cloud_alexa(self, mock_mcp, mock_client):
        """'cloud.alexa' is a valid assistant name."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True}
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_expose_entity"]

        result = await tool(
            entity_ids="light.test",
            assistants="cloud.alexa",
            should_expose=True
        )

        assert result["success"] is True
        assert result["assistants"] == ["cloud.alexa"]

    @pytest.mark.asyncio
    async def test_valid_assistant_cloud_google_assistant(self, mock_mcp, mock_client):
        """'cloud.google_assistant' is a valid assistant name."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True}
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_expose_entity"]

        result = await tool(
            entity_ids="light.test",
            assistants="cloud.google_assistant",
            should_expose=True
        )

        assert result["success"] is True
        assert result["assistants"] == ["cloud.google_assistant"]

    @pytest.mark.asyncio
    async def test_invalid_assistant_name_rejected(self, expose_tool):
        """Invalid assistant name should return an error."""
        result = await expose_tool(
            entity_ids="light.test",
            assistants="invalid_assistant",
            should_expose=True
        )

        assert result["success"] is False
        assert "Invalid assistant" in result["error"]
        assert "valid_assistants" in result
        assert result["valid_assistants"] == KNOWN_ASSISTANTS

    @pytest.mark.asyncio
    async def test_invalid_assistant_alexa_without_cloud_prefix(self, expose_tool):
        """'alexa' without cloud prefix should be rejected."""
        result = await expose_tool(
            entity_ids="light.test",
            assistants="alexa",
            should_expose=True
        )

        assert result["success"] is False
        assert "Invalid assistant" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_assistant_google_assistant_without_cloud_prefix(
        self, expose_tool
    ):
        """'google_assistant' without cloud prefix should be rejected."""
        result = await expose_tool(
            entity_ids="light.test",
            assistants="google_assistant",
            should_expose=True
        )

        assert result["success"] is False
        assert "Invalid assistant" in result["error"]

    @pytest.mark.asyncio
    async def test_multiple_assistants_list(self, mock_mcp, mock_client):
        """Multiple assistants as list should work."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True}
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_expose_entity"]

        result = await tool(
            entity_ids="light.test",
            assistants=["conversation", "cloud.alexa"],
            should_expose=True
        )

        assert result["success"] is True
        assert set(result["assistants"]) == {"conversation", "cloud.alexa"}

    @pytest.mark.asyncio
    async def test_assistants_json_array_string(self, mock_mcp, mock_client):
        """Assistants as JSON array string should be parsed."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True}
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_expose_entity"]

        result = await tool(
            entity_ids="light.test",
            assistants='["conversation", "cloud.alexa"]',
            should_expose=True
        )

        assert result["success"] is True
        assert result["assistants"] == ["conversation", "cloud.alexa"]

    @pytest.mark.asyncio
    async def test_empty_string_assistant_rejected_as_invalid(self, expose_tool):
        """Empty string assistant should be rejected as invalid."""
        result = await expose_tool(
            entity_ids="light.test",
            assistants="",
            should_expose=True
        )

        assert result["success"] is False
        # Empty string becomes [""], which is invalid
        assert "Invalid assistant" in result["error"]

    @pytest.mark.asyncio
    async def test_empty_list_assistants_returns_error(self, expose_tool):
        """Empty list for assistants should return an error."""
        result = await expose_tool(
            entity_ids="light.test",
            assistants=[],
            should_expose=True
        )

        assert result["success"] is False
        assert "assistants is required" in result["error"]

    @pytest.mark.asyncio
    async def test_mixed_valid_invalid_assistants_rejected(self, expose_tool):
        """Mix of valid and invalid assistants should be rejected."""
        result = await expose_tool(
            entity_ids="light.test",
            assistants=["conversation", "invalid_one"],
            should_expose=True
        )

        assert result["success"] is False
        assert "Invalid assistant" in result["error"]

    # ==================== Should Expose Parameter Validation Tests ====================

    @pytest.mark.asyncio
    async def test_should_expose_true(self, mock_mcp, mock_client):
        """should_expose=True should set exposed to True."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True}
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_expose_entity"]

        result = await tool(
            entity_ids="light.test",
            assistants="conversation",
            should_expose=True
        )

        assert result["success"] is True
        assert result["exposed"] is True

    @pytest.mark.asyncio
    async def test_should_expose_false(self, mock_mcp, mock_client):
        """should_expose=False should set exposed to False."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True}
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_expose_entity"]

        result = await tool(
            entity_ids="light.test",
            assistants="conversation",
            should_expose=False
        )

        assert result["success"] is True
        assert result["exposed"] is False

    @pytest.mark.asyncio
    async def test_should_expose_string_true(self, mock_mcp, mock_client):
        """should_expose='true' (string) should be coerced to True."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True}
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_expose_entity"]

        result = await tool(
            entity_ids="light.test",
            assistants="conversation",
            should_expose="true"
        )

        assert result["success"] is True
        assert result["exposed"] is True

    @pytest.mark.asyncio
    async def test_should_expose_string_false(self, mock_mcp, mock_client):
        """should_expose='false' (string) should be coerced to False."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True}
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_expose_entity"]

        result = await tool(
            entity_ids="light.test",
            assistants="conversation",
            should_expose="false"
        )

        assert result["success"] is True
        assert result["exposed"] is False

    @pytest.mark.asyncio
    async def test_should_expose_invalid_string_returns_error(self, expose_tool):
        """should_expose with invalid string should raise ToolError."""
        with pytest.raises(ToolError) as exc_info:
            await expose_tool(
                entity_ids="light.test",
                assistants="conversation",
                should_expose="maybe"
            )

        # Parse the error message as JSON and verify the error content
        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        error_msg = error_data["error"]["message"] if isinstance(error_data["error"], dict) else error_data["error"]
        assert "should_expose" in error_msg.lower() or "boolean" in error_msg.lower()

    # ==================== API Error Handling Tests ====================

    @pytest.mark.asyncio
    async def test_websocket_error_response(self, mock_mcp, mock_client):
        """WebSocket error response should be handled properly."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": {"message": "Entity not found"}
            }
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_expose_entity"]

        result = await tool(
            entity_ids="light.nonexistent",
            assistants="conversation",
            should_expose=True
        )

        assert result["success"] is False
        assert "Entity not found" in result["error"]

    @pytest.mark.asyncio
    async def test_websocket_exception(self, mock_mcp, mock_client):
        """WebSocket exception should raise ToolError."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=Exception("Connection lost")
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_expose_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(
                entity_ids="light.test",
                assistants="conversation",
                should_expose=True
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "Connection" in error_data["error"]["message"] or "connect" in error_data["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_websocket_error_dict_format(self, mock_mcp, mock_client):
        """WebSocket error as dict should be formatted properly."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": {"code": "not_found", "message": "Unknown entity"}
            }
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_expose_entity"]

        result = await tool(
            entity_ids="light.test",
            assistants="conversation",
            should_expose=True
        )

        assert result["success"] is False
        assert "Unknown entity" in result["error"]

    @pytest.mark.asyncio
    async def test_websocket_error_string_format(self, mock_mcp, mock_client):
        """WebSocket error as string should be handled."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": "Simple error string"
            }
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_expose_entity"]

        result = await tool(
            entity_ids="light.test",
            assistants="conversation",
            should_expose=True
        )

        assert result["success"] is False
        assert "Simple error string" in result["error"]


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
        """WebSocket exception should raise ToolError."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=Exception("Network error")
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity_exposure"]

        with pytest.raises(ToolError) as exc_info:
            await tool()

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        # "Network error" message is mapped to "CONNECTION_FAILED" error
        assert "connect" in error_data["error"]["message"].lower() or "network" in str(exc_info.value).lower()


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
        """WebSocket exception should raise ToolError."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=Exception("Timeout")
        )
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity_exposure"]

        with pytest.raises(ToolError) as exc_info:
            await tool(entity_id="light.living_room")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        # "Timeout" message is mapped to timeout error
        assert "timeout" in str(exc_info.value).lower()


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
    async def test_expose_entity_message_format(self, mock_mcp, mock_client):
        """Expose entity should send correct WebSocket message."""
        register_voice_assistant_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_expose_entity"]

        await tool(
            entity_ids=["light.living_room", "light.bedroom"],
            assistants=["conversation", "cloud.alexa"],
            should_expose=True
        )

        mock_client.send_websocket_message.assert_called_once()
        call_args = mock_client.send_websocket_message.call_args[0][0]

        assert call_args["type"] == "homeassistant/expose_entity"
        assert call_args["entity_ids"] == ["light.living_room", "light.bedroom"]
        assert call_args["assistants"] == ["conversation", "cloud.alexa"]
        assert call_args["should_expose"] is True

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
