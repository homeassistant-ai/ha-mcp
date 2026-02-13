"""Unit tests for entity management tools module."""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from ha_mcp.tools.tools_entities import register_entity_tools

# Valid guide_response for tools requiring ha_get_tool_guide()
_GR = {"success": True}


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

        result = await tool(entity_id="light.test", labels=["outdoor", "smart"]),
        guide_response=_GR,

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

        result = await tool(entity_id="light.test", labels=[]),
        guide_response=_GR,

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

        result = await tool(
            entity_id="light.test", labels='["label1", "label2"]',
            guide_response=_GR,
        )

        assert result["success"] is True
        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["labels"] == ["label1", "label2"]

    @pytest.mark.asyncio
    async def test_set_labels_invalid_returns_error(self, set_entity_tool):
        """Invalid labels parameter should return an error."""
        result = await set_entity_tool(entity_id="light.test", labels="not_json{"),
        guide_response=_GR,

        assert result["success"] is False
        error = result.get("error", {})
        error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
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

        await tool(entity_id="light.test", name="New Name"),
        guide_response=_GR,

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
            guide_response=_GR,
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
            guide_response=_GR,
            expose_to={"conversation": True, "cloud.alexa": False},
        )

        assert result["success"] is True
        assert result["exposure"] == {"conversation": True, "cloud.alexa": False}

        # Should have made 3 calls: expose true, expose false, get entity
        assert mock_client.send_websocket_message.call_count == 3

    @pytest.mark.asyncio
    async def test_expose_to_invalid_assistant_rejected(self, set_entity_tool):
        """Invalid assistant name in expose_to should return error."""
        result = await set_entity_tool(
            entity_id="light.test",
            guide_response=_GR,
            expose_to={"invalid_assistant": True},
        )

        assert result["success"] is False
        error = result.get("error", {})
        error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
        assert "invalid_assistant" in error_msg.lower() or "invalid" in error_msg.lower()

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
            guide_response=_GR,
            expose_to=json.dumps({"conversation": True}),
        )

        assert result["success"] is True
        assert result["exposure"] == {"conversation": True}

    @pytest.mark.asyncio
    async def test_expose_to_invalid_json_string(self, set_entity_tool):
        """Invalid JSON string for expose_to should return error."""
        result = await set_entity_tool(
            entity_id="light.test",
            guide_response=_GR,
            expose_to="not valid json{",
        )

        assert result["success"] is False

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

        result = await tool(entity_id="light.test", name="New Name"),
        guide_response=_GR,

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
            guide_response=_GR,
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
        """Calling ha_set_entity with no parameters should return error."""
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(entity_id="light.test"),
        guide_response=_GR,

        assert result["success"] is False
        assert "No updates specified" in result["error"]
        assert "suggestions" in result
        assert isinstance(result["suggestions"], list)

    @pytest.mark.asyncio
    async def test_expose_failure_after_registry_success_returns_partial(
        self, mock_mcp, mock_client
    ):
        """If registry update succeeds but expose fails, return partial success info."""
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

        result = await tool(
            entity_id="light.test",
            guide_response=_GR,
            name="Updated",
            expose_to={"conversation": True},
        )

        assert result["success"] is False
        assert result.get("partial") is True
        assert "entity_entry" in result
        assert result["entity_entry"]["name"] == "Updated"
        # Should report which assistants succeeded and failed
        assert "exposure_succeeded" in result
        assert "exposure_failed" in result
        assert result["exposure_failed"] == {"conversation": True}

    @pytest.mark.asyncio
    async def test_expose_only_failure_returns_error_without_partial(
        self, mock_mcp, mock_client
    ):
        """If only expose_to is set and it fails, return error without partial flag."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": {"message": "Exposure not supported"},
            }
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id="light.test",
            guide_response=_GR,
            expose_to={"conversation": True},
        )

        assert result["success"] is False
        assert "partial" not in result
        assert result["exposure_succeeded"] == {}
        assert result["exposure_failed"] == {"conversation": True}

    @pytest.mark.asyncio
    async def test_expose_mixed_partial_failure_reports_succeeded(
        self, mock_mcp, mock_client
    ):
        """If first exposure group succeeds but second fails, report which succeeded."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=[
                {"success": True},  # expose_true succeeds
                {"success": False, "error": {"message": "Failed"}},  # expose_false fails
            ]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id="light.test",
            guide_response=_GR,
            expose_to={"conversation": True, "cloud.alexa": False},
        )

        assert result["success"] is False
        assert result["exposure_succeeded"] == {"conversation": True}
        assert result["exposure_failed"] == {"cloud.alexa": False}

    @pytest.mark.asyncio
    async def test_expose_only_entity_not_found_returns_error(
        self, mock_mcp, mock_client
    ):
        """If only expose_to is set and entity fetch fails, return error."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=[
                {"success": True},  # expose call succeeds
                {"success": False, "error": {"message": "Entity not found"}},  # get entity fails
            ]
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id="light.nonexistent",
            guide_response=_GR,
            expose_to={"conversation": True},
        )

        assert result["success"] is False
        assert "not found" in result["error"]
        assert "exposure_succeeded" in result

    @pytest.mark.asyncio
    async def test_enabled_invalid_value_returns_error(self, mock_mcp, mock_client):
        """Invalid value for enabled should return a validation error."""
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(entity_id="light.test", enabled="maybe"),
        guide_response=_GR,

        assert result["success"] is False
        error = result.get("error", {})
        error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
        assert "enabled" in error_msg.lower() or "boolean" in error_msg.lower()

    @pytest.mark.asyncio
    async def test_hidden_invalid_value_returns_error(self, mock_mcp, mock_client):
        """Invalid value for hidden should return a validation error."""
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(entity_id="light.test", hidden="maybe"),
        guide_response=_GR,

        assert result["success"] is False
        error = result.get("error", {})
        error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
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
            guide_response=_GR,
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
        """Passing a list instead of dict for expose_to should return error."""
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id="light.test",
            guide_response=_GR,
            expose_to=["conversation"],
        )

        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_registry_failure_with_labels(self, mock_mcp, mock_client):
        """Registry update failure when labels are included should return error."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": {"message": "Entity not found"},
            }
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id="light.nonexistent",
            guide_response=_GR,
            labels=["outdoor"],
        )

        assert result["success"] is False
        assert "suggestions" in result


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
            guide_response=_GR,
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
            guide_response=_GR,
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
            guide_response=_GR,
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
            guide_response=_GR,
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
            guide_response=_GR,
            expose_to={"conversation": True},
        )

        assert result["success"] is True
        assert result["succeeded_count"] == 2

    @pytest.mark.asyncio
    async def test_bulk_rejects_single_entity_params(self, mock_mcp, mock_client):
        """Bulk operation should reject single-entity parameters."""
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id=["light.a", "light.b"],
            guide_response=_GR,
            name="Test Name",  # Single-entity param
            labels=["outdoor"],
        )

        assert result["success"] is False
        assert "Single-entity parameters" in result["error"]
        assert "name" in result["error"]

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
            guide_response=_GR,
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
        """Bulk operation with empty entity_id list should return error."""
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id=[],
            guide_response=_GR,
            labels=["outdoor"],
        )

        assert result["success"] is False
        assert "empty" in result["error"].lower()

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
            guide_response=_GR,
            labels=["new_label"],
            label_operation="add",
        )

        assert result["success"] is True
        assert result["succeeded_count"] == 2
