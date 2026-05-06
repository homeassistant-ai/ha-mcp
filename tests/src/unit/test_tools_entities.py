"""Unit tests for entity management tools module."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_entities import register_entity_tools


class TestHaSetEntityLabels:
    """Test ha_set_entity labels parameter."""

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
    def set_entity_tool(self, mock_mcp, mock_client):
        """Register tools and return the ha_set_entity function."""
        register_entity_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_set_entity"]

    @pytest.mark.asyncio
    async def test_set_labels_list(self, mock_mcp, mock_client):
        """Setting labels with a list should include labels in the registry update."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "entity_entry": {
                        "entity_id": "light.test",
                        "name": None,
                        "original_name": "Test",
                        "icon": None,
                        "area_id": None,
                        "disabled_by": None,
                        "hidden_by": None,
                        "aliases": [],
                        "labels": ["outdoor", "smart"],
                    }
                },
            }
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(entity_id="light.test", labels=["outdoor", "smart"])

        assert result["success"] is True
        assert "labels=['outdoor', 'smart']" in str(result["updates"])

        # Verify WebSocket message includes labels
        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["type"] == "config/entity_registry/update"
        assert call_args["labels"] == ["outdoor", "smart"]

    @pytest.mark.asyncio
    async def test_set_labels_empty_list_clears(self, mock_mcp, mock_client):
        """Setting labels to empty list should clear all labels."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "entity_entry": {
                        "entity_id": "light.test",
                        "name": None,
                        "original_name": "Test",
                        "icon": None,
                        "area_id": None,
                        "disabled_by": None,
                        "hidden_by": None,
                        "aliases": [],
                        "labels": [],
                    }
                },
            }
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(entity_id="light.test", labels=[])

        assert result["success"] is True

        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["labels"] == []

    @pytest.mark.asyncio
    async def test_set_labels_json_string(self, mock_mcp, mock_client):
        """Labels as JSON array string should be parsed."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "entity_entry": {
                        "entity_id": "light.test",
                        "name": None,
                        "original_name": "Test",
                        "icon": None,
                        "area_id": None,
                        "disabled_by": None,
                        "hidden_by": None,
                        "aliases": [],
                        "labels": ["label1", "label2"],
                    }
                },
            }
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(entity_id="light.test", labels='["label1", "label2"]')

        assert result["success"] is True
        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["labels"] == ["label1", "label2"]

    @pytest.mark.asyncio
    async def test_set_labels_invalid_returns_error(self, set_entity_tool):
        """Invalid labels parameter should raise ToolError."""
        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(entity_id="light.test", labels="not_json{")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        error = error_data.get("error", {})
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        assert "labels" in error_msg.lower() or "invalid" in error_msg.lower()

    @pytest.mark.asyncio
    async def test_labels_none_not_included_in_message(self, mock_mcp, mock_client):
        """When labels is None, it should not be included in WebSocket message."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "entity_entry": {
                        "entity_id": "light.test",
                        "name": "New Name",
                        "original_name": "Test",
                        "icon": None,
                        "area_id": None,
                        "disabled_by": None,
                        "hidden_by": None,
                        "aliases": [],
                        "labels": [],
                    }
                },
            }
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        await tool(entity_id="light.test", name="New Name")

        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert "labels" not in call_args


class TestHaSetEntityExposeTo:
    """Test ha_set_entity expose_to parameter."""

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
    def set_entity_tool(self, mock_mcp, mock_client):
        """Register tools and return the ha_set_entity function."""
        register_entity_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_set_entity"]

    @pytest.mark.asyncio
    async def test_expose_to_single_assistant(self, mock_mcp, mock_client):
        """expose_to with single assistant should send expose message."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": {"exposed_entities": {}}}
        )

        # For expose-only calls, the tool also fetches entity state
        entity_entry = {
            "entity_id": "light.test",
            "name": None,
            "original_name": "Test",
            "icon": None,
            "area_id": None,
            "disabled_by": None,
            "hidden_by": None,
            "aliases": [],
            "labels": [],
        }

        mock_client.send_websocket_message = AsyncMock(
            side_effect=[
                {"success": True},  # expose call
                {"success": True, "result": entity_entry},  # get entity call
            ]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id="light.test",
            expose_to={"conversation": True},
        )

        assert result["success"] is True
        assert result["exposure"] == {"conversation": True}

        # Verify the expose WebSocket message
        first_call = mock_client.send_websocket_message.call_args_list[0][0][0]
        assert first_call["type"] == "homeassistant/expose_entity"
        assert first_call["assistants"] == ["conversation"]
        assert first_call["entity_ids"] == ["light.test"]
        assert first_call["should_expose"] is True

    @pytest.mark.asyncio
    async def test_expose_to_mixed_true_false(self, mock_mcp, mock_client):
        """expose_to with mixed true/false should make separate API calls."""
        entity_entry = {
            "entity_id": "light.test",
            "name": None,
            "original_name": "Test",
            "icon": None,
            "area_id": None,
            "disabled_by": None,
            "hidden_by": None,
            "aliases": [],
            "labels": [],
        }

        mock_client.send_websocket_message = AsyncMock(
            side_effect=[
                {"success": True},  # expose=true call
                {"success": True},  # expose=false call
                {"success": True, "result": entity_entry},  # get entity call
            ]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id="light.test",
            expose_to={"conversation": True, "cloud.alexa": False},
        )

        assert result["success"] is True
        assert result["exposure"] == {"conversation": True, "cloud.alexa": False}

        # Should have made 3 calls: expose true, expose false, get entity
        assert mock_client.send_websocket_message.call_count == 3

    @pytest.mark.asyncio
    async def test_expose_to_invalid_assistant_rejected(self, set_entity_tool):
        """Invalid assistant name in expose_to should raise ToolError."""
        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(
                entity_id="light.test",
                expose_to={"invalid_assistant": True},
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        error = error_data.get("error", {})
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        assert (
            "invalid_assistant" in error_msg.lower() or "invalid" in error_msg.lower()
        )

    @pytest.mark.asyncio
    async def test_expose_to_json_string(self, mock_mcp, mock_client):
        """expose_to as JSON string should be parsed."""
        entity_entry = {
            "entity_id": "light.test",
            "name": None,
            "original_name": "Test",
            "icon": None,
            "area_id": None,
            "disabled_by": None,
            "hidden_by": None,
            "aliases": [],
            "labels": [],
        }

        mock_client.send_websocket_message = AsyncMock(
            side_effect=[
                {"success": True},  # expose call
                {"success": True, "result": entity_entry},  # get entity call
            ]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id="light.test",
            expose_to=json.dumps({"conversation": True}),
        )

        assert result["success"] is True
        assert result["exposure"] == {"conversation": True}

    @pytest.mark.asyncio
    async def test_expose_to_invalid_json_string(self, set_entity_tool):
        """Invalid JSON string for expose_to should raise ToolError."""
        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(
                entity_id="light.test",
                expose_to="not valid json{",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False

    @pytest.mark.asyncio
    async def test_expose_to_none_not_triggered(self, mock_mcp, mock_client):
        """When expose_to is None, no exposure API call should be made."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "entity_entry": {
                        "entity_id": "light.test",
                        "name": "New Name",
                        "original_name": "Test",
                        "icon": None,
                        "area_id": None,
                        "disabled_by": None,
                        "hidden_by": None,
                        "aliases": [],
                        "labels": [],
                    }
                },
            }
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(entity_id="light.test", name="New Name")

        assert result["success"] is True
        # Only 1 call (entity registry update), no exposure call
        assert mock_client.send_websocket_message.call_count == 1
        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["type"] == "config/entity_registry/update"


class TestHaSetEntityCombined:
    """Test ha_set_entity with combined registry + labels + expose_to updates."""

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

    @pytest.mark.asyncio
    async def test_combined_name_labels_expose(self, mock_mcp, mock_client):
        """Combined name + labels + expose_to should update registry then expose."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=[
                # First call: entity registry update (name + labels)
                {
                    "success": True,
                    "result": {
                        "entity_entry": {
                            "entity_id": "light.test",
                            "name": "My Light",
                            "original_name": "Test",
                            "icon": None,
                            "area_id": None,
                            "disabled_by": None,
                            "hidden_by": None,
                            "aliases": [],
                            "labels": ["outdoor"],
                        }
                    },
                },
                # Second call: expose entity
                {"success": True},
            ]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id="light.test",
            name="My Light",
            labels=["outdoor"],
            expose_to={"conversation": True},
        )

        assert result["success"] is True
        assert result["entity_entry"]["name"] == "My Light"
        assert result["entity_entry"]["labels"] == ["outdoor"]
        assert result["exposure"] == {"conversation": True}

        # Verify first call was registry update with name + labels
        first_call = mock_client.send_websocket_message.call_args_list[0][0][0]
        assert first_call["type"] == "config/entity_registry/update"
        assert first_call["name"] == "My Light"
        assert first_call["labels"] == ["outdoor"]

        # Verify second call was expose
        second_call = mock_client.send_websocket_message.call_args_list[1][0][0]
        assert second_call["type"] == "homeassistant/expose_entity"

    @pytest.mark.asyncio
    async def test_no_updates_returns_error(self, mock_mcp, mock_client):
        """Calling ha_set_entity with no parameters should raise ToolError."""
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(entity_id="light.test")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        error = error_data.get("error", {})
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        assert "No updates specified" in error_msg

    @pytest.mark.asyncio
    async def test_expose_failure_after_registry_success_raises_tool_error(
        self, mock_mcp, mock_client
    ):
        """If registry update succeeds but expose fails, raise ToolError with partial context."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=[
                # Registry update succeeds
                {
                    "success": True,
                    "result": {
                        "entity_entry": {
                            "entity_id": "light.test",
                            "name": "Updated",
                            "original_name": "Test",
                            "icon": None,
                            "area_id": None,
                            "disabled_by": None,
                            "hidden_by": None,
                            "aliases": [],
                            "labels": [],
                        }
                    },
                },
                # Expose fails
                {
                    "success": False,
                    "error": {"message": "Exposure not supported"},
                },
            ]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(
                entity_id="light.test",
                name="Updated",
                expose_to={"conversation": True},
            )

        result = json.loads(str(exc_info.value))
        assert result["success"] is False
        assert result.get("partial") is True
        assert "entity_entry" in result
        assert result["entity_entry"]["name"] == "Updated"
        assert "exposure_succeeded" in result
        assert "exposure_failed" in result
        assert result["exposure_failed"] == {"conversation": True}

    @pytest.mark.asyncio
    async def test_expose_only_failure_raises_tool_error_without_partial(
        self, mock_mcp, mock_client
    ):
        """If only expose_to is set and it fails, raise ToolError without partial flag."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": {"message": "Exposure not supported"},
            }
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(
                entity_id="light.test",
                expose_to={"conversation": True},
            )

        result = json.loads(str(exc_info.value))
        assert result["success"] is False
        assert "partial" not in result
        assert result["exposure_succeeded"] == {}
        assert result["exposure_failed"] == {"conversation": True}

    @pytest.mark.asyncio
    async def test_expose_mixed_partial_failure_raises_tool_error(
        self, mock_mcp, mock_client
    ):
        """If first exposure group succeeds but second fails, raise ToolError with context."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=[
                {"success": True},  # expose_true succeeds
                {
                    "success": False,
                    "error": {"message": "Failed"},
                },  # expose_false fails
            ]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(
                entity_id="light.test",
                expose_to={"conversation": True, "cloud.alexa": False},
            )

        result = json.loads(str(exc_info.value))
        assert result["success"] is False
        assert result["exposure_succeeded"] == {"conversation": True}
        assert result["exposure_failed"] == {"cloud.alexa": False}

    @pytest.mark.asyncio
    async def test_expose_only_entity_not_found_raises_tool_error(
        self, mock_mcp, mock_client
    ):
        """If only expose_to is set and entity fetch fails, raise ToolError."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=[
                {"success": True},  # expose call succeeds
                {
                    "success": False,
                    "error": {"message": "Entity not found"},
                },  # get entity fails
            ]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(
                entity_id="light.nonexistent",
                expose_to={"conversation": True},
            )

        result = json.loads(str(exc_info.value))
        assert result["success"] is False
        assert "not found" in result["error"]["message"]
        assert result["exposure_succeeded"] == {"conversation": True}

    @pytest.mark.asyncio
    async def test_enabled_invalid_value_raises_tool_error(self, mock_mcp, mock_client):
        """Invalid value for enabled should raise ToolError."""
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(entity_id="light.test", enabled="maybe")

        result = json.loads(str(exc_info.value))
        assert result["success"] is False
        error = result.get("error", {})
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        assert "enabled" in error_msg.lower() or "boolean" in error_msg.lower()

    @pytest.mark.asyncio
    async def test_hidden_invalid_value_raises_tool_error(self, mock_mcp, mock_client):
        """Invalid value for hidden should raise ToolError."""
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(entity_id="light.test", hidden="maybe")

        result = json.loads(str(exc_info.value))
        assert result["success"] is False
        error = result.get("error", {})
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        assert "hidden" in error_msg.lower() or "boolean" in error_msg.lower()

    @pytest.mark.asyncio
    async def test_expose_to_all_three_assistants(self, mock_mcp, mock_client):
        """All 3 assistants in a single expose_to call should work."""
        entity_entry = {
            "entity_id": "light.test",
            "name": None,
            "original_name": "Test",
            "icon": None,
            "area_id": None,
            "disabled_by": None,
            "hidden_by": None,
            "aliases": [],
            "labels": [],
        }

        mock_client.send_websocket_message = AsyncMock(
            side_effect=[
                {"success": True},  # expose_true call
                {"success": True},  # expose_false call
                {"success": True, "result": entity_entry},  # get entity call
            ]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id="light.test",
            expose_to={
                "conversation": True,
                "cloud.alexa": True,
                "cloud.google_assistant": False,
            },
        )

        assert result["success"] is True
        assert result["exposure"] == {
            "conversation": True,
            "cloud.alexa": True,
            "cloud.google_assistant": False,
        }

    @pytest.mark.asyncio
    async def test_expose_to_list_returns_error(self, mock_mcp, mock_client):
        """Passing a list instead of dict for expose_to should raise ToolError."""
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(
                entity_id="light.test",
                expose_to=["conversation"],
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False

    @pytest.mark.asyncio
    async def test_registry_failure_with_labels(self, mock_mcp, mock_client):
        """Registry update failure when labels are included should raise ToolError."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": {"message": "Entity not found"},
            }
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(
                entity_id="light.nonexistent",
                labels=["outdoor"],
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        error = error_data.get("error", {})
        assert isinstance(error, dict)
        assert "suggestions" in error or "suggestion" in error


class TestHaSetEntityLabelOperations:
    """Test ha_set_entity label_operation parameter (add/remove)."""

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

    @pytest.mark.asyncio
    async def test_label_add_operation(self, mock_mcp, mock_client):
        """label_operation='add' should add to existing labels."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=[
                # First call: get current entity (to fetch existing labels)
                {
                    "success": True,
                    "result": {
                        "entity_id": "light.test",
                        "labels": ["existing_label"],
                    },
                },
                # Second call: update entity with combined labels
                {
                    "success": True,
                    "result": {
                        "entity_entry": {
                            "entity_id": "light.test",
                            "name": None,
                            "original_name": "Test",
                            "icon": None,
                            "area_id": None,
                            "disabled_by": None,
                            "hidden_by": None,
                            "aliases": [],
                            "labels": ["existing_label", "new_label"],
                        }
                    },
                },
            ]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id="light.test",
            labels=["new_label"],
            label_operation="add",
        )

        assert result["success"] is True
        # Verify the update call included both old and new labels
        update_call = mock_client.send_websocket_message.call_args_list[1][0][0]
        assert "existing_label" in update_call["labels"]
        assert "new_label" in update_call["labels"]

    @pytest.mark.asyncio
    async def test_label_remove_operation(self, mock_mcp, mock_client):
        """label_operation='remove' should remove specified labels."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=[
                # First call: get current entity (to fetch existing labels)
                {
                    "success": True,
                    "result": {
                        "entity_id": "light.test",
                        "labels": ["keep_label", "remove_label"],
                    },
                },
                # Second call: update entity with remaining labels
                {
                    "success": True,
                    "result": {
                        "entity_entry": {
                            "entity_id": "light.test",
                            "name": None,
                            "original_name": "Test",
                            "icon": None,
                            "area_id": None,
                            "disabled_by": None,
                            "hidden_by": None,
                            "aliases": [],
                            "labels": ["keep_label"],
                        }
                    },
                },
            ]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id="light.test",
            labels=["remove_label"],
            label_operation="remove",
        )

        assert result["success"] is True
        # Verify the update call excluded the removed label
        update_call = mock_client.send_websocket_message.call_args_list[1][0][0]
        assert "keep_label" in update_call["labels"]
        assert "remove_label" not in update_call["labels"]

    @pytest.mark.asyncio
    async def test_label_add_no_duplicates(self, mock_mcp, mock_client):
        """label_operation='add' should not create duplicates."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=[
                # First call: get current entity
                {
                    "success": True,
                    "result": {
                        "entity_id": "light.test",
                        "labels": ["label_a", "label_b"],
                    },
                },
                # Second call: update entity
                {
                    "success": True,
                    "result": {
                        "entity_entry": {
                            "entity_id": "light.test",
                            "name": None,
                            "original_name": "Test",
                            "icon": None,
                            "area_id": None,
                            "disabled_by": None,
                            "hidden_by": None,
                            "aliases": [],
                            "labels": ["label_a", "label_b", "label_c"],
                        }
                    },
                },
            ]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id="light.test",
            labels=["label_b", "label_c"],  # label_b already exists
            label_operation="add",
        )

        assert result["success"] is True
        update_call = mock_client.send_websocket_message.call_args_list[1][0][0]
        # Should have 3 unique labels, not 4
        assert len(update_call["labels"]) == 3


class TestHaSetEntityBulkOperations:
    """Test ha_set_entity bulk operations with multiple entity_ids."""

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

    @pytest.mark.asyncio
    async def test_bulk_labels_set(self, mock_mcp, mock_client):
        """Bulk operation should update labels on multiple entities."""
        entity_entry = {
            "entity_id": "light.test",
            "name": None,
            "original_name": "Test",
            "icon": None,
            "area_id": None,
            "disabled_by": None,
            "hidden_by": None,
            "aliases": [],
            "labels": ["outdoor"],
        }
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {"entity_entry": entity_entry},
            }
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id=["light.a", "light.b", "light.c"],
            labels=["outdoor"],
        )

        assert result["success"] is True
        assert result["total"] == 3
        assert result["succeeded_count"] == 3
        assert result["failed_count"] == 0
        assert len(result["succeeded"]) == 3

    @pytest.mark.asyncio
    async def test_bulk_expose_to(self, mock_mcp, mock_client):
        """Bulk operation should update expose_to on multiple entities."""
        entity_entry = {
            "entity_id": "light.test",
            "name": None,
            "original_name": "Test",
            "icon": None,
            "area_id": None,
            "disabled_by": None,
            "hidden_by": None,
            "aliases": [],
            "labels": [],
        }
        mock_client.send_websocket_message = AsyncMock(
            side_effect=[
                {"success": True},  # expose call for light.a
                {"success": True, "result": entity_entry},  # get entity for light.a
                {"success": True},  # expose call for light.b
                {"success": True, "result": entity_entry},  # get entity for light.b
            ]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id=["light.a", "light.b"],
            expose_to={"conversation": True},
        )

        assert result["success"] is True
        assert result["succeeded_count"] == 2

    @pytest.mark.asyncio
    async def test_bulk_rejects_single_entity_params(self, mock_mcp, mock_client):
        """Bulk operation should reject single-entity parameters with ToolError."""
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(
                entity_id=["light.a", "light.b"],
                name="Test Name",  # Single-entity param
                labels=["outdoor"],
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        error = error_data.get("error", {})
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        assert "Single-entity parameters" in error_msg or "name" in error_msg

    @pytest.mark.asyncio
    async def test_bulk_partial_failure(self, mock_mcp, mock_client):
        """Bulk operation should report partial failures."""
        entity_entry = {
            "entity_id": "light.a",
            "name": None,
            "original_name": "Test",
            "icon": None,
            "area_id": None,
            "disabled_by": None,
            "hidden_by": None,
            "aliases": [],
            "labels": ["outdoor"],
        }
        mock_client.send_websocket_message = AsyncMock(
            side_effect=[
                # light.a succeeds
                {"success": True, "result": {"entity_entry": entity_entry}},
                # light.b fails
                {"success": False, "error": {"message": "Entity not found"}},
            ]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id=["light.a", "light.b"],
            labels=["outdoor"],
        )

        assert result["success"] is False
        assert result["partial"] is True
        assert result["succeeded_count"] == 1
        assert result["failed_count"] == 1
        assert len(result["succeeded"]) == 1
        assert len(result["failed"]) == 1

    @pytest.mark.asyncio
    async def test_bulk_empty_list_returns_error(self, mock_mcp, mock_client):
        """Bulk operation with empty entity_id list should raise ToolError."""
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(
                entity_id=[],
                labels=["outdoor"],
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        error = error_data.get("error", {})
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        assert "empty" in error_msg.lower()

    @pytest.mark.asyncio
    async def test_bulk_label_add_operation(self, mock_mcp, mock_client):
        """Bulk operation with label_operation='add' should work."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=[
                # Get labels for light.a
                {"success": True, "result": {"labels": ["existing"]}},
                # Update light.a
                {
                    "success": True,
                    "result": {
                        "entity_entry": {
                            "entity_id": "light.a",
                            "name": None,
                            "original_name": "A",
                            "icon": None,
                            "area_id": None,
                            "disabled_by": None,
                            "hidden_by": None,
                            "aliases": [],
                            "labels": ["existing", "new_label"],
                        }
                    },
                },
                # Get labels for light.b
                {"success": True, "result": {"labels": ["other"]}},
                # Update light.b
                {
                    "success": True,
                    "result": {
                        "entity_entry": {
                            "entity_id": "light.b",
                            "name": None,
                            "original_name": "B",
                            "icon": None,
                            "area_id": None,
                            "disabled_by": None,
                            "hidden_by": None,
                            "aliases": [],
                            "labels": ["other", "new_label"],
                        }
                    },
                },
            ]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id=["light.a", "light.b"],
            labels=["new_label"],
            label_operation="add",
        )

        assert result["success"] is True
        assert result["succeeded_count"] == 2


class TestHaSetEntityRegistryDisableGuardrail:
    """Test that registry-disable (enabled=False) is blocked for automations and scripts."""

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
    def set_entity_tool(self, mock_mcp, mock_client):
        """Register tools and return the ha_set_entity function."""
        register_entity_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_set_entity"]

    @pytest.mark.asyncio
    async def test_disable_automation_blocked(self, set_entity_tool, mock_client):
        """enabled=False on automation entity should raise ToolError."""
        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(entity_id="automation.test", enabled=False)

        error_text = str(exc_info.value)
        assert "automation" in error_text.lower()
        assert "turn_off" in error_text
        # Ensure no WebSocket call was made
        mock_client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_disable_script_blocked(self, set_entity_tool, mock_client):
        """enabled=False on script entity should raise ToolError."""
        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(entity_id="script.my_script", enabled=False)

        error_text = str(exc_info.value)
        assert "script" in error_text.lower()
        assert "turn_off" in error_text
        mock_client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_disable_automation_string_false_blocked(
        self, set_entity_tool, mock_client
    ):
        """enabled='false' (string) on automation entity should also be blocked."""
        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(entity_id="automation.morning", enabled="false")

        error_text = str(exc_info.value)
        assert "automation" in error_text.lower()
        assert "turn_off" in error_text
        mock_client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_disable_automation_string_capital_false_blocked(
        self, set_entity_tool, mock_client
    ):
        """enabled='False' (capital F, common from Python agents) should also be blocked."""
        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(entity_id="automation.evening", enabled="False")

        error_text = str(exc_info.value)
        assert "automation" in error_text.lower()
        assert "turn_off" in error_text
        mock_client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_disable_automation_single_element_list_blocked(
        self, set_entity_tool, mock_client
    ):
        """enabled=False on single-element list ['automation.test'] should also be blocked."""
        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(entity_id=["automation.test"], enabled=False)

        error_text = str(exc_info.value)
        assert "automation" in error_text.lower()
        assert "turn_off" in error_text
        mock_client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_enable_automation_allowed(self, set_entity_tool, mock_client):
        """enabled=True on automation entity should be allowed (re-enabling is fine)."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "entity_entry": {
                        "entity_id": "automation.test",
                        "name": None,
                        "original_name": "Test",
                        "icon": None,
                        "area_id": None,
                        "disabled_by": None,
                        "hidden_by": None,
                        "aliases": [],
                        "labels": [],
                    }
                },
            }
        )

        result = await set_entity_tool(entity_id="automation.test", enabled=True)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_disable_other_domain_allowed(self, set_entity_tool, mock_client):
        """enabled=False on non-automation/script entities should still work."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "entity_entry": {
                        "entity_id": "sensor.temperature",
                        "name": None,
                        "original_name": "Temperature",
                        "icon": None,
                        "area_id": None,
                        "disabled_by": "user",
                        "hidden_by": None,
                        "aliases": [],
                        "labels": [],
                    }
                },
            }
        )

        result = await set_entity_tool(entity_id="sensor.temperature", enabled=False)
        assert result["success"] is True


class TestHaRemoveEntity:
    """Test ha_remove_entity tool."""

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
    def remove_entity_tool(self, mock_mcp, mock_client):
        """Register tools and return the ha_remove_entity function."""
        register_entity_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_remove_entity"]

    @pytest.mark.asyncio
    async def test_remove_entity_success(self, remove_entity_tool, mock_client):
        """Successfully removing an entity should return success with entity_id."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": None}
        )
        entity_id = "input_boolean.test_entity"

        result = await remove_entity_tool(entity_id=entity_id)

        assert result["success"] is True
        assert result["entity_id"] == entity_id

        # Verify correct WebSocket message type and payload
        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["type"] == "config/entity_registry/remove"
        assert call_args["entity_id"] == entity_id

    @pytest.mark.asyncio
    async def test_remove_entity_not_found(self, remove_entity_tool, mock_client):
        """Removing a non-existent entity should raise ToolError containing 'not found'.

        Note: send_websocket_message returns errors as plain strings, never as dicts.
        Detection: "not found" in error_msg.lower() -- NOT error.get("code").
        """
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": "Command failed: Entity not found",
            }
        )

        with pytest.raises(ToolError) as exc_info:
            await remove_entity_tool(entity_id="sensor.definitely_not_real_12345")

        error_msg = str(exc_info.value).lower()
        assert "not found" in error_msg

    @pytest.mark.asyncio
    async def test_remove_entity_exception(self, remove_entity_tool, mock_client):
        """WebSocket connection failure should raise ToolError."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=Exception("conn failed")
        )

        with pytest.raises(ToolError):
            await remove_entity_tool(entity_id="sensor.test_entity")

    @pytest.mark.asyncio
    async def test_remove_entity_general_failure(self, remove_entity_tool, mock_client):
        """Generic failures should raise ToolError with SERVICE_CALL_FAILED message."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": "Permission denied"}
        )

        with pytest.raises(ToolError) as exc_info:
            await remove_entity_tool(entity_id="sensor.test_entity")

        error_msg = str(exc_info.value).lower()
        assert "permission denied" in error_msg


class TestHaSetEntityShowAs:
    """ha_set_entity device_class (Show As) and options parameter."""

    @pytest.fixture
    def mock_mcp(self):
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
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    def _registry_response(self, **overrides):
        entry = {
            "entity_id": "binary_sensor.test",
            "name": None,
            "original_name": "Test",
            "icon": None,
            "area_id": None,
            "disabled_by": None,
            "hidden_by": None,
            "aliases": [],
            "labels": [],
            "categories": {},
            "device_class": None,
            "original_device_class": None,
            "options": {},
        }
        entry.update(overrides)
        return {"success": True, "result": {"entity_entry": entry}}

    @pytest.mark.asyncio
    async def test_device_class_sets_top_level_field(self, mock_mcp, mock_client):
        """device_class='window' must send top-level device_class on the WS update."""
        mock_client.send_websocket_message = AsyncMock(
            return_value=self._registry_response(device_class="window")
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(entity_id="binary_sensor.test", device_class="window")

        assert result["success"] is True
        ws_msg = mock_client.send_websocket_message.call_args[0][0]
        assert ws_msg["type"] == "config/entity_registry/update"
        assert ws_msg["device_class"] == "window"
        # Show As must NOT be routed through options — that's a separate slot
        # the UI does not read.
        assert "options" not in ws_msg
        assert "options_domain" not in ws_msg
        assert "device_class='window'" in str(result["updates"])

    @pytest.mark.asyncio
    async def test_device_class_empty_string_clears(self, mock_mcp, mock_client):
        """device_class='' clears the override (sends None) and surfaces 'cleared' in updates."""
        mock_client.send_websocket_message = AsyncMock(
            return_value=self._registry_response(device_class=None)
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(entity_id="binary_sensor.test", device_class="")

        ws_msg = mock_client.send_websocket_message.call_args[0][0]
        assert ws_msg["device_class"] is None
        assert "device_class cleared" in str(result["updates"])

    @pytest.mark.asyncio
    async def test_options_single_domain_pairs_options_domain(
        self, mock_mcp, mock_client
    ):
        """options={'sensor': {'display_precision': 2}} -> one WS call w/ options_domain=sensor."""
        mock_client.send_websocket_message = AsyncMock(
            return_value=self._registry_response(
                options={"sensor": {"display_precision": 2}}
            )
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        await tool(
            entity_id="sensor.temp",
            options={"sensor": {"display_precision": 2}},
        )

        assert mock_client.send_websocket_message.call_count == 1
        ws_msg = mock_client.send_websocket_message.call_args[0][0]
        # When options is the only param, the main registry update is skipped
        # and the per-domain update carries exactly these keys — no spurious
        # device_class=None or other accidental fields.
        assert set(ws_msg.keys()) == {
            "type",
            "entity_id",
            "options_domain",
            "options",
        }
        assert ws_msg["options_domain"] == "sensor"
        assert ws_msg["options"] == {"display_precision": 2}

    @pytest.mark.asyncio
    async def test_options_multi_domain_splits_into_separate_calls(
        self, mock_mcp, mock_client
    ):
        """A multi-domain options dict must be split — HA schema requires one domain per call.

        Uses side_effect so each call gets its own response, exercising the loop's
        entity_entry reassignment from each per-domain response.
        """
        sensor_response = self._registry_response(
            options={"sensor": {"display_precision": 1}}
        )
        weather_response = self._registry_response(
            options={
                "sensor": {"display_precision": 1},
                "weather": {"forecast_type": "hourly"},
            }
        )
        mock_client.send_websocket_message = AsyncMock(
            side_effect=[sensor_response, weather_response]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id="sensor.temp",
            options={
                "sensor": {"display_precision": 1},
                "weather": {"forecast_type": "hourly"},
            },
        )

        calls = [c.args[0] for c in mock_client.send_websocket_message.call_args_list]
        domains = [c["options_domain"] for c in calls]
        assert domains == ["sensor", "weather"]  # insertion order preserved
        for c in calls:
            assert "options_domain" in c and "options" in c
        # Final entity_entry reflects the LAST per-domain response (loop reassigns).
        assert result["entity_entry"]["options"]["weather"] == {
            "forecast_type": "hourly"
        }
        assert result["entity_entry"]["options"]["sensor"] == {"display_precision": 1}

    @pytest.mark.asyncio
    async def test_options_partial_failure_surfaces_partial_state(
        self, mock_mcp, mock_client
    ):
        """If domain N+1 fails after 1..N succeeded, the error must report partial=True
        and list which domains were already written. Otherwise callers retry blindly
        and risk applying earlier changes twice or missing them entirely.
        """
        ok = self._registry_response(options={"sensor": {"display_precision": 1}})
        fail = {"success": False, "error": {"message": "unsupported_domain"}}
        mock_client.send_websocket_message = AsyncMock(side_effect=[ok, fail])
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(
                entity_id="sensor.temp",
                options={
                    "sensor": {"display_precision": 1},
                    "weather": {"forecast_type": "hourly"},
                },
            )

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "weather" in body["error"]["message"]
        assert "unsupported_domain" in body["error"]["message"]
        # create_error_response merges `context` into the top-level response.
        assert body["options_domain"] == "weather"
        assert body["partial"] is True
        assert body["options_succeeded"] == {"sensor": {"display_precision": 1}}

    @pytest.mark.asyncio
    async def test_options_json_string_is_parsed(self, mock_mcp, mock_client):
        """options as a JSON string should be parsed transparently."""
        mock_client.send_websocket_message = AsyncMock(
            return_value=self._registry_response()
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        await tool(
            entity_id="sensor.temp",
            options='{"sensor": {"display_precision": 0}}',
        )

        ws_msg = mock_client.send_websocket_message.call_args[0][0]
        assert ws_msg["options_domain"] == "sensor"
        assert ws_msg["options"] == {"display_precision": 0}

    @pytest.mark.asyncio
    async def test_options_invalid_shape_raises_validation_error(
        self, mock_mcp, mock_client
    ):
        """options must be a dict mapping domain to sub-dict — list rejected,
        and the error message names the actual type the caller passed."""
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(entity_id="sensor.temp", options=["not", "a", "dict"])

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "got list" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_options_inner_value_must_be_dict(self, mock_mcp, mock_client):
        """options={'sensor': 'not-a-dict'} must be rejected with a targeted message."""
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(entity_id="sensor.temp", options={"sensor": "not-a-dict"})

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "sensor" in body["error"]["message"]
        assert "str" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_options_empty_dict_rejected(self, mock_mcp, mock_client):
        """options={} must be rejected at validation rather than falling through to
        the generic 'No updates specified' error."""
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(entity_id="sensor.temp", options={})

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "empty" in body["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_bulk_rejects_device_class(self, mock_mcp, mock_client):
        """device_class is single-entity-only — passing it with a list raises."""
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(
                entity_id=["binary_sensor.a", "binary_sensor.b"],
                device_class="window",
            )

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "device_class" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_bulk_rejects_options(self, mock_mcp, mock_client):
        """options is single-entity-only — passing it with a list raises."""
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(
                entity_id=["sensor.a", "sensor.b"],
                options={"sensor": {"display_precision": 2}},
            )

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "options" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_options_first_call_failure_is_not_partial(
        self, mock_mcp, mock_client
    ):
        """When the very first per-domain call fails (no prior registry update,
        no prior succeeded domains), partial must be False and the message must
        say 'Failed to update' — not 'Partially updated'. A regression that
        flips this would silently turn clean failures into partial-success lies.
        """
        fail = {"success": False, "error": {"message": "unsupported_domain"}}
        mock_client.send_websocket_message = AsyncMock(side_effect=[fail])
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(
                entity_id="sensor.temp",
                options={"sensor": {"display_precision": 2}},
            )

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert body["partial"] is False
        assert body["options_succeeded"] == {}
        assert body["error"]["message"].startswith("Failed to update options for")
        assert "Partially" not in body["error"]["message"]
        # entity_entry must NOT be present on a clean failure: the captured
        # entity_entry is the empty stub {}, and surfacing it would be
        # indistinguishable from "entity has nothing set". Mirrors the
        # expose_to failure path which gates entity_entry on prior_mutation.
        assert "entity_entry" not in body

    @pytest.mark.asyncio
    async def test_options_failure_with_empty_error_envelope_uses_fallback(
        self, mock_mcp, mock_client
    ):
        """If HA returns success=False with no usable error key, the error
        message must NOT degrade to literal "{}" — _extract_ws_error's fallback
        string surfaces instead.
        """
        fail = {"success": False}  # no error key at all
        mock_client.send_websocket_message = AsyncMock(side_effect=[fail])
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(
                entity_id="sensor.temp",
                options={"sensor": {"display_precision": 2}},
            )

        body = json.loads(str(exc_info.value))
        msg = body["error"]["message"]
        assert "no error detail returned by Home Assistant" in msg
        # Critical: must NOT have rendered the empty dict literal.
        assert ": {}" not in msg
        assert "{}" not in msg.split(":", 1)[1]

    @pytest.mark.asyncio
    async def test_device_class_and_options_in_one_call(self, mock_mcp, mock_client):
        """device_class and options together: one main registry update carries
        device_class, then a separate per-domain WS call carries options. A
        future refactor could short-circuit the options loop when the main
        update already succeeded — this test catches that.
        """
        main_response = self._registry_response(device_class="window")
        opts_response = self._registry_response(
            device_class="window",
            options={"binary_sensor": {}, "sensor": {"display_precision": 2}},
        )
        mock_client.send_websocket_message = AsyncMock(
            side_effect=[main_response, opts_response]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        await tool(
            entity_id="binary_sensor.test",
            device_class="window",
            options={"sensor": {"display_precision": 2}},
        )

        calls = [c.args[0] for c in mock_client.send_websocket_message.call_args_list]
        assert len(calls) == 2
        # Main update carries device_class but NOT options_domain
        assert calls[0]["device_class"] == "window"
        assert "options_domain" not in calls[0]
        # Per-domain update carries options_domain and the right sub-dict
        assert calls[1]["options_domain"] == "sensor"
        assert calls[1]["options"] == {"display_precision": 2}
        assert "device_class" not in calls[1]


class TestHaGetEntityRegistryOptions:
    """ha_get_entity must surface device_class + options so agents can read state."""

    @pytest.fixture
    def mock_mcp(self):
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
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_get_entity_includes_device_class_and_options(
        self, mock_mcp, mock_client
    ):
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "entity_id": "binary_sensor.test",
                    "name": None,
                    "original_name": "Test",
                    "icon": None,
                    "area_id": None,
                    "disabled_by": None,
                    "hidden_by": None,
                    "aliases": [],
                    "labels": [],
                    "categories": {},
                    "device_class": "window",
                    "original_device_class": None,
                    "options": {"sensor": {"display_precision": 2}},
                    "platform": "template",
                    "device_id": None,
                    "unique_id": "abc",
                },
            }
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity"]

        result = await tool(entity_id="binary_sensor.test")

        entry = result["entity_entry"]
        assert entry["device_class"] == "window"
        assert entry["original_device_class"] is None
        assert entry["options"] == {"sensor": {"display_precision": 2}}
