"""Unit tests for voice assistant tools module."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_voice_assistant import VoiceAssistantTools


class TestHaListExposedEntities:
    """Test ha_get_entity_exposure tool validation logic."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def tools(self, mock_client):
        """Create VoiceAssistantTools instance."""
        return VoiceAssistantTools(mock_client)

    @pytest.mark.asyncio
    async def test_list_all_entities_success(self, mock_client):
        """List all exposed entities should succeed."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "exposed_entities": {
                        "light.living_room": {"conversation": True},
                        "light.bedroom": {"cloud.alexa": True},
                    }
                },
            }
        )
        tools = VoiceAssistantTools(mock_client)
        result = await tools.ha_get_entity_exposure()

        assert result["success"] is True
        assert result["count"] == 2
        assert "exposed_entities" in result
        assert "summary" in result

    @pytest.mark.asyncio
    async def test_filter_by_valid_assistant(self, mock_client):
        """Filter by valid assistant should work."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "exposed_entities": {
                        "light.living_room": {"conversation": True},
                        "light.bedroom": {"cloud.alexa": True},
                    }
                },
            }
        )
        tools = VoiceAssistantTools(mock_client)
        result = await tools.ha_get_entity_exposure(assistant="conversation")

        assert result["success"] is True
        assert result["filters_applied"]["assistant"] == "conversation"
        assert "light.living_room" in result["exposed_entities"]
        assert "light.bedroom" not in result["exposed_entities"]

    @pytest.mark.asyncio
    async def test_filter_by_invalid_assistant_rejected(self, mock_client):
        """Core judges the filter's assistant; its refusal is the tool's error."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": "Command failed: value must be one of ['cloud.alexa']",
                "error_code": "invalid_format",
            }
        )
        tools = VoiceAssistantTools(mock_client)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity_exposure(assistant="invalid_assistant")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "must be one of" in error_data["error"]["message"]
        # Only Core's check ran; no list was read for an unknown assistant.
        mock_client.send_websocket_message.assert_awaited_once()
        message = mock_client.send_websocket_message.call_args[0][0]
        assert message == {
            "type": "homeassistant/expose_new_entities/get",
            "assistant": "invalid_assistant",
        }

    @pytest.mark.asyncio
    async def test_filter_by_entity_id(self, mock_client):
        """Filter by specific entity_id should work."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "exposed_entities": {
                        "light.living_room": {"conversation": True},
                        "light.bedroom": {"cloud.alexa": True},
                    }
                },
            }
        )
        tools = VoiceAssistantTools(mock_client)
        result = await tools.ha_get_entity_exposure(entity_id="light.living_room")

        assert result["success"] is True
        assert result["entity_id"] == "light.living_room"
        assert result["exposed_to"] == {"conversation": True}
        assert result["is_exposed_anywhere"] is True

    @pytest.mark.asyncio
    async def test_filter_by_nonexistent_entity_id(self, mock_client):
        """Filter by nonexistent entity_id should return defaults."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "exposed_entities": {
                        "light.living_room": {"conversation": True},
                    }
                },
            }
        )
        tools = VoiceAssistantTools(mock_client)
        result = await tools.ha_get_entity_exposure(entity_id="light.nonexistent")

        assert result["success"] is True
        assert result["entity_id"] == "light.nonexistent"
        assert result["exposed_to"] == {}
        assert result["is_exposed_anywhere"] is False

    @pytest.mark.asyncio
    async def test_summary_counts_per_assistant(self, mock_client):
        """Summary should count entities per assistant."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "exposed_entities": {
                        "light.living_room": {
                            "conversation": True,
                            "cloud.alexa": True,
                        },
                        "light.bedroom": {"conversation": True},
                        "light.kitchen": {"cloud.google_assistant": True},
                    }
                },
            }
        )
        tools = VoiceAssistantTools(mock_client)
        result = await tools.ha_get_entity_exposure()

        assert result["success"] is True
        assert result["summary"]["conversation"] == 2
        assert result["summary"]["cloud.alexa"] == 1
        assert result["summary"]["cloud.google_assistant"] == 1

    @pytest.mark.asyncio
    async def test_websocket_error_response(self, mock_client):
        """WebSocket error response should be handled."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": {"message": "Service unavailable"}}
        )
        tools = VoiceAssistantTools(mock_client)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity_exposure()

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "Service unavailable" in error_data["error"]["message"]

    @pytest.mark.asyncio
    async def test_websocket_exception(self, mock_client):
        """WebSocket exception should be caught."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=Exception("Network error")
        )
        tools = VoiceAssistantTools(mock_client)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity_exposure()

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "Network error" in error_data["error"]["details"]


class TestHaGetEntityExposure:
    """Test ha_get_entity_exposure tool validation logic."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_get_exposure_of_exposed_entity(self, mock_client):
        """An exposed entity reports the assistants it is exposed to."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "exposed_entities": {
                        "light.living_room": {"conversation": True},
                    }
                },
            }
        )
        tools = VoiceAssistantTools(mock_client)
        result = await tools.ha_get_entity_exposure(entity_id="light.living_room")

        assert result["success"] is True
        assert result["entity_id"] == "light.living_room"
        # Core's row as served: only the assistants the entity is exposed to.
        assert result["exposed_to"] == {"conversation": True}
        assert result["is_exposed_anywhere"] is True

    @pytest.mark.asyncio
    async def test_get_exposure_of_entity_exposed_nowhere(self, mock_client):
        """An entity absent from Core's map is exposed nowhere."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": {"exposed_entities": {}}}
        )
        tools = VoiceAssistantTools(mock_client)
        result = await tools.ha_get_entity_exposure(entity_id="light.living_room")

        assert result["success"] is True
        assert result["entity_id"] == "light.living_room"
        assert result["exposed_to"] == {}
        assert result["is_exposed_anywhere"] is False

    @pytest.mark.asyncio
    async def test_get_exposure_all_assistants(self, mock_client):
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
                },
            }
        )
        tools = VoiceAssistantTools(mock_client)
        result = await tools.ha_get_entity_exposure(entity_id="light.living_room")

        assert result["success"] is True
        assert result["exposed_to"]["conversation"] is True
        assert result["exposed_to"]["cloud.alexa"] is True
        assert result["exposed_to"]["cloud.google_assistant"] is True
        assert result["is_exposed_anywhere"] is True

    @pytest.mark.asyncio
    async def test_websocket_error_response(self, mock_client):
        """WebSocket error should be handled."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": {"message": "Access denied"}}
        )
        tools = VoiceAssistantTools(mock_client)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity_exposure(entity_id="light.living_room")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "Access denied" in error_data["error"]["message"]
        assert error_data["entity_id"] == "light.living_room"

    @pytest.mark.asyncio
    async def test_websocket_exception(self, mock_client):
        """WebSocket exception should be caught."""
        mock_client.send_websocket_message = AsyncMock(side_effect=Exception("Timeout"))
        tools = VoiceAssistantTools(mock_client)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_entity_exposure(entity_id="light.living_room")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "Timeout" in error_data["error"]["details"]
        assert error_data["entity_id"] == "light.living_room"


class TestWebSocketMessageFormat:
    """Test that WebSocket messages are formatted correctly."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(return_value={"success": True})
        return client

    @pytest.mark.asyncio
    async def test_list_entities_message_format(self, mock_client):
        """List entities should send correct WebSocket message."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": {"exposed_entities": {}}}
        )
        tools = VoiceAssistantTools(mock_client)
        await tools.ha_get_entity_exposure()

        mock_client.send_websocket_message.assert_called_once()
        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["type"] == "homeassistant/expose_entity/list"

    @pytest.mark.asyncio
    async def test_get_exposure_message_format(self, mock_client):
        """Get exposure should send correct WebSocket message."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": {"exposed_entities": {}}}
        )
        tools = VoiceAssistantTools(mock_client)
        await tools.ha_get_entity_exposure(entity_id="light.living_room")

        mock_client.send_websocket_message.assert_called_once()
        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["type"] == "homeassistant/expose_entity/list"
