"""
Unit tests for Group management tools.

These tests verify the input validation and error handling of the group tools
without requiring a live Home Assistant instance.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_groups import GroupTools


class TestGroupToolsValidation:
    """Test input validation for group tools."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.get_states = AsyncMock(return_value=[])
        # Required by wait_for_entity_registered/removed in tool internals
        client.get_entity_state = AsyncMock(return_value={"state": "on"})
        client.call_service = AsyncMock(return_value=None)
        return client

    @pytest.fixture
    def tools(self, mock_client):
        """Create GroupTools instance."""
        return GroupTools(mock_client)

    async def test_set_group_invalid_object_id_with_dot(self, tools):
        """Test that object_id with dots is rejected."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(
                object_id="group.invalid",
                entities=["light.test"],
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "Invalid object_id" in error_data["error"]["message"]

    async def test_set_group_empty_entities_list(self, tools):
        """Test that empty entities list is rejected."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(
                object_id="test_group",
                entities=[],
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "empty" in error_data["error"]["message"].lower()

    async def test_set_group_empty_add_entities_list(self, tools):
        """Test that empty add_entities list is rejected."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(
                object_id="test_group",
                add_entities=[],
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "empty" in error_data["error"]["message"].lower()

    async def test_set_group_mutually_exclusive_operations(self, tools):
        """Test that mutually exclusive entity operations are rejected."""
        # Test entities + add_entities
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(
                object_id="test_group",
                entities=["light.test"],
                add_entities=["light.another"],
            )
        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "Only one of" in error_data["error"]["message"]

        # Test entities + remove_entities
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(
                object_id="test_group",
                entities=["light.test"],
                remove_entities=["light.old"],
            )
        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "Only one of" in error_data["error"]["message"]

        # Test add_entities + remove_entities
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(
                object_id="test_group",
                add_entities=["light.new"],
                remove_entities=["light.old"],
            )
        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "Only one of" in error_data["error"]["message"]

    async def test_remove_group_invalid_object_id(self, tools):
        """Test that remove_group rejects invalid object_id."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_remove_group(
                object_id="group.invalid",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "Invalid object_id" in error_data["error"]["message"]

    async def test_list_groups_success(self, mock_client):
        """Test successful group listing."""
        # Mock states with groups
        mock_client.get_states = AsyncMock(return_value=[
            {
                "entity_id": "group.living_room",
                "state": "on",
                "attributes": {
                    "friendly_name": "Living Room",
                    "entity_id": ["light.lamp1", "light.lamp2"],
                    "icon": "mdi:sofa",
                    "all": False,
                },
            },
            {
                "entity_id": "light.bed_light",  # Not a group
                "state": "off",
                "attributes": {"friendly_name": "Bed Light"},
            },
        ])

        tools = GroupTools(mock_client)
        result = await tools.ha_config_list_groups()

        assert result["success"] is True
        assert result["count"] == 1
        assert len(result["groups"]) == 1
        assert result["groups"][0]["entity_id"] == "group.living_room"
        assert result["groups"][0]["object_id"] == "living_room"
        assert result["groups"][0]["friendly_name"] == "Living Room"
        assert "light.lamp1" in result["groups"][0]["entity_ids"]

    async def test_set_group_success(self, tools, mock_client):
        """Test successful group creation."""
        result = await tools.ha_config_set_group(
            object_id="test_group",
            name="Test Group",
            entities=["light.lamp1", "light.lamp2"],
            icon="mdi:lightbulb-group",
        )

        assert result["success"] is True
        assert result["entity_id"] == "group.test_group"
        assert result["object_id"] == "test_group"
        assert "name" in result["updated_fields"]
        assert "entities" in result["updated_fields"]
        assert "icon" in result["updated_fields"]

        # Verify service was called
        mock_client.call_service.assert_called_once_with(
            "group", "set",
            {
                "object_id": "test_group",
                "name": "Test Group",
                "icon": "mdi:lightbulb-group",
                "entities": ["light.lamp1", "light.lamp2"],
            }
        )

    async def test_remove_group_success(self, mock_client):
        """Test successful group removal."""
        from ha_mcp.client.rest_client import HomeAssistantAPIError

        # After removal, entity state should return 404
        mock_client.get_entity_state = AsyncMock(
            side_effect=HomeAssistantAPIError("Not found", status_code=404)
        )

        tools = GroupTools(mock_client)
        result = await tools.ha_config_remove_group(
            object_id="test_group",
        )

        assert result["success"] is True
        assert result["entity_id"] == "group.test_group"
        assert result["object_id"] == "test_group"

        # Verify service was called
        mock_client.call_service.assert_called_once_with(
            "group", "remove",
            {"object_id": "test_group"}
        )

    async def test_set_group_all_on_parameter(self, tools, mock_client):
        """Test that all_on parameter is correctly mapped to 'all'."""
        result = await tools.ha_config_set_group(
            object_id="test_group",
            entities=["light.lamp1"],
            all_on=True,
        )

        assert result["success"] is True

        # Verify 'all' was passed to service
        call_args = mock_client.call_service.call_args
        assert call_args[0][2]["all"] is True

    async def test_list_groups_error_handling(self, mock_client):
        """Test error handling in list_groups."""
        # Make get_states raise an exception
        mock_client.get_states = AsyncMock(side_effect=Exception("Connection failed"))

        tools = GroupTools(mock_client)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_list_groups()

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False

    async def test_set_group_error_handling(self, mock_client):
        """Test error handling in set_group."""
        # Make call_service raise an exception
        mock_client.call_service = AsyncMock(side_effect=Exception("Service failed"))

        tools = GroupTools(mock_client)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(
                object_id="test_group",
                entities=["light.lamp1"],
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False

    async def test_remove_group_error_handling(self, mock_client):
        """Test error handling in remove_group."""
        # Make call_service raise an exception
        mock_client.call_service = AsyncMock(side_effect=Exception("Service failed"))

        tools = GroupTools(mock_client)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_remove_group(
                object_id="test_group",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
