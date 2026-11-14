"""Unit tests for entity management tools module."""

import json
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools.tools_entities import register_entity_tools


def _registry_list(id_field, *ids):
    """WS response for a registry-list preflight.

    ``ha_set_entity`` validates referenced area / label / category IDs before
    it touches the entity registry (issue #2159), so mocks for calls carrying
    those parameters must answer the lookup first.
    """
    return {"success": True, "result": [{id_field: entry_id} for entry_id in ids]}


class TestHaSetEntityLabels:
    """Test ha_set_entity labels parameter."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server."""
        mcp = MagicMock()
        self.registered_tools = {}

        def capture_add_tool(method):
            fmcp = getattr(method, "__fastmcp__", None)
            name = (fmcp.name if fmcp else None) or method.__name__
            self.registered_tools[name] = method

        mcp.add_tool = capture_add_tool
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
            side_effect=[
                _registry_list("label_id", "outdoor", "smart"),
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
                            "labels": ["outdoor", "smart"],
                        }
                    },
                },
            ]
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
            side_effect=[
                _registry_list("label_id", "label1", "label2"),
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
                            "labels": ["label1", "label2"],
                        }
                    },
                },
            ]
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

    @pytest.mark.asyncio
    async def test_invalid_area_id_rejected_before_registry_update(
        self, set_entity_tool, mock_client
    ):
        """Unknown area IDs must not create dangling entity assignments."""
        mock_client.send_websocket_message.return_value = {
            "success": True,
            "result": [{"area_id": "living_room"}],
        }

        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(entity_id="light.test", area_id="extérieur nord")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert error_data["area_id"] == "extérieur nord"
        assert mock_client.send_websocket_message.call_args_list == [
            (({"type": "config/area_registry/list"},), {})
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "area_lookup",
        [
            {"success": False, "error": {"message": "registry unavailable"}},
            HomeAssistantConnectionError("connection lost"),
        ],
    )
    async def test_area_registry_lookup_failure_rejects_entity_update(
        self, set_entity_tool, mock_client, area_lookup
    ):
        """Area assignment fails closed when its registry cannot be read."""
        mock_client.send_websocket_message.side_effect = area_lookup

        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(entity_id="light.test", area_id="living_room")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "CONNECTION_FAILED"
        assert error_data["area_id"] == "living_room"
        assert mock_client.send_websocket_message.call_args_list == [
            (({"type": "config/area_registry/list"},), {})
        ]

    @pytest.mark.asyncio
    async def test_area_id_validated_once_per_call(self, set_entity_tool, mock_client):
        """The preflight is a single lookup, not one per write path.

        Validation lives at the tool entry so the bulk path is covered too;
        a second per-entity lookup would multiply into N round-trips there.
        """
        mock_client.send_websocket_message.return_value = {
            "success": True,
            "result": [],
        }

        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(entity_id="light.test", area_id="living_room")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert mock_client.send_websocket_message.call_args_list == [
            (({"type": "config/area_registry/list"},), {})
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label_operation", ["set", "add"])
    async def test_unknown_label_rejected(
        self, set_entity_tool, mock_client, label_operation
    ):
        """Assigning a label that is not in the registry must not be persisted."""
        label_lookup = _registry_list("label_id", "outdoor")
        if label_operation == "add":
            # "add" resolves the entity's current labels before the pre-write
            # validation runs.
            mock_client.send_websocket_message.side_effect = [
                {"success": True, "result": {"labels": []}},
                label_lookup,
            ]
            expected_calls = [
                {"type": "config/entity_registry/get", "entity_id": "light.test"},
                {"type": "config/label_registry/list"},
            ]
        else:
            mock_client.send_websocket_message.side_effect = [label_lookup]
            expected_calls = [{"type": "config/label_registry/list"}]

        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(
                entity_id="light.test",
                labels=["ghost_label"],
                label_operation=label_operation,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert error_data["unknown_labels"] == ["ghost_label"]
        assert [
            c[0][0] for c in mock_client.send_websocket_message.call_args_list
        ] == expected_calls

    @pytest.mark.asyncio
    async def test_unknown_label_removal_still_allowed(
        self, set_entity_tool, mock_client
    ):
        """Removing a dangling label is the cleanup path — it must not be blocked."""
        mock_client.send_websocket_message.side_effect = [
            {"success": True, "result": {"labels": ["ghost_label", "outdoor"]}},
            {
                "success": True,
                "result": {"entity_entry": {"entity_id": "light.test"}},
            },
        ]

        result = await set_entity_tool(
            entity_id="light.test",
            labels=["ghost_label"],
            label_operation="remove",
        )

        assert result["success"] is True
        sent_types = [
            call[0][0]["type"]
            for call in mock_client.send_websocket_message.call_args_list
        ]
        assert "config/label_registry/list" not in sent_types
        update_call = mock_client.send_websocket_message.call_args_list[1][0][0]
        assert update_call["type"] == "config/entity_registry/update"
        assert update_call["labels"] == ["outdoor"]

    @pytest.mark.asyncio
    async def test_unknown_category_rejected(self, set_entity_tool, mock_client):
        """A category is validated against its own scope's registry."""
        mock_client.send_websocket_message.return_value = _registry_list(
            "category_id", "lighting"
        )

        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(
                entity_id="light.test",
                categories={"automation": "ghost_category"},
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert error_data["category"] == "ghost_category"
        assert mock_client.send_websocket_message.call_args_list == [
            (
                (
                    {
                        "type": "config/category_registry/list",
                        "scope": "automation",
                    },
                ),
                {},
            )
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kwargs", "lookup_type"),
        [
            ({"labels": ["outdoor"]}, "config/label_registry/list"),
            (
                {"categories": {"automation": "lighting"}},
                "config/category_registry/list",
            ),
        ],
    )
    async def test_registry_lookup_failure_rejects_update(
        self, set_entity_tool, mock_client, kwargs, lookup_type
    ):
        """Label and category assignment fail closed when the registry is unreadable."""
        mock_client.send_websocket_message.return_value = {
            "success": False,
            "error": {"message": "registry unavailable"},
        }

        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(entity_id="light.test", **kwargs)

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "CONNECTION_FAILED"
        sent_types = [
            call[0][0]["type"]
            for call in mock_client.send_websocket_message.call_args_list
        ]
        assert sent_types == [lookup_type]

    @pytest.mark.asyncio
    async def test_bulk_unknown_label_rejected_before_any_update(
        self, set_entity_tool, mock_client
    ):
        """One entry-point preflight guards the bulk path — no entity is touched."""
        mock_client.send_websocket_message.return_value = _registry_list(
            "label_id", "outdoor"
        )

        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(
                entity_id=["light.a", "light.b"], labels=["ghost_label"]
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert mock_client.send_websocket_message.call_args_list == [
            (({"type": "config/label_registry/list"},), {})
        ]


class TestHaSetEntityExposeTo:
    """Test ha_set_entity expose_to parameter."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server."""
        mcp = MagicMock()
        self.registered_tools = {}

        def capture_add_tool(method):
            fmcp = getattr(method, "__fastmcp__", None)
            name = (fmcp.name if fmcp else None) or method.__name__
            self.registered_tools[name] = method

        mcp.add_tool = capture_add_tool
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
    """Test ha_set_entity with combined registry, label and device updates."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server."""
        mcp = MagicMock()
        self.registered_tools = {}

        def capture_add_tool(method):
            fmcp = getattr(method, "__fastmcp__", None)
            name = (fmcp.name if fmcp else None) or method.__name__
            self.registered_tools[name] = method

        mcp.add_tool = capture_add_tool
        return mcp

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

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
            side_effect=[
                _registry_list("label_id", "outdoor"),
                {"success": False, "error": "Command failed: Entity not found"},
            ]
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

    @pytest.mark.asyncio
    async def test_device_rename_no_device_does_not_mark_partial(
        self, mock_mcp, mock_client
    ):
        """Entity with no associated device is a no-op, not a partial failure."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "entity_id": "light.test",
                    "device_id": None,
                },
            }
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id="light.test",
            new_device_name="Living Room",
        )

        assert result["success"] is True
        assert "partial" not in result
        device_rename = result["device_rename"]
        assert any(
            "no associated device" in w for w in device_rename.get("warnings", [])
        )


class TestHaSetEntityLabelOperations:
    """Test ha_set_entity label_operation parameter (add/remove)."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server."""
        mcp = MagicMock()
        self.registered_tools = {}

        def capture_add_tool(method):
            fmcp = getattr(method, "__fastmcp__", None)
            name = (fmcp.name if fmcp else None) or method.__name__
            self.registered_tools[name] = method

        mcp.add_tool = capture_add_tool
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
                # Second call: label-registry preflight for the added IDs
                _registry_list("label_id", "existing_label", "new_label"),
                # Third call: update entity with combined labels
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
        update_call = mock_client.send_websocket_message.call_args_list[2][0][0]
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
                # Second call: label-registry preflight for the added IDs
                _registry_list("label_id", "label_a", "label_b", "label_c"),
                # Third call: update entity
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
        update_call = mock_client.send_websocket_message.call_args_list[2][0][0]
        # Should have 3 unique labels, not 4
        assert len(update_call["labels"]) == 3


class TestHaSetEntityBulkOperations:
    """Test ha_set_entity bulk operations with multiple entity_ids."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server."""
        mcp = MagicMock()
        self.registered_tools = {}

        def capture_add_tool(method):
            fmcp = getattr(method, "__fastmcp__", None)
            name = (fmcp.name if fmcp else None) or method.__name__
            self.registered_tools[name] = method

        mcp.add_tool = capture_add_tool
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
        update_ack = {"success": True, "result": {"entity_entry": entity_entry}}
        mock_client.send_websocket_message = AsyncMock(
            side_effect=lambda message: (
                _registry_list("label_id", "outdoor")
                if message["type"] == "config/label_registry/list"
                else update_ack
            )
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

        def send(message: dict[str, Any]) -> dict[str, Any]:
            if message["type"] == "config/label_registry/list":
                return _registry_list("label_id", "outdoor")
            if message["entity_id"] == "light.a":
                return {"success": True, "result": {"entity_entry": entity_entry}}
            return {"success": False, "error": "Command failed: Entity not found"}

        mock_client.send_websocket_message = AsyncMock(side_effect=send)
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

        def send(message: dict[str, Any]) -> dict[str, Any]:
            if message["type"] == "config/label_registry/list":
                return _registry_list("label_id", "new_label")
            if message["type"] == "config/entity_registry/get":
                labels = {"light.a": ["existing"], "light.b": ["other"]}
                return {
                    "success": True,
                    "result": {"labels": labels[message["entity_id"]]},
                }
            return {"success": True, "result": {"entity_entry": dict(message)}}

        mock_client.send_websocket_message = AsyncMock(side_effect=send)
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id=["light.a", "light.b"],
            labels=["new_label"],
            label_operation="add",
        )

        assert result["success"] is True
        assert result["succeeded_count"] == 2
        entries = {
            entry["entity_id"]: entry["entity_entry"] for entry in result["succeeded"]
        }
        assert set(entries["light.a"]["labels"]) == {"existing", "new_label"}
        assert set(entries["light.b"]["labels"]) == {"other", "new_label"}

    @pytest.mark.asyncio
    async def test_bulk_categories_set(self, mock_mcp, mock_client):
        """Bulk operation should include categories in each per-entity registry update."""
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
            "categories": {"automation": "cat_id"},
        }
        update_ack = {"success": True, "result": {"entity_entry": entity_entry}}
        mock_client.send_websocket_message = AsyncMock(
            side_effect=lambda message: (
                _registry_list("category_id", "cat_id")
                if message["type"] == "config/category_registry/list"
                else update_ack
            )
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_set_entity"]

        result = await tool(
            entity_id=["light.a", "light.b"],
            categories={"automation": "cat_id"},
        )

        assert result["success"] is True
        assert result["succeeded_count"] == 2
        # Verify each per-entity WS message included categories
        update_calls = [
            call[0][0]
            for call in mock_client.send_websocket_message.call_args_list
            if call[0][0]["type"] == "config/entity_registry/update"
        ]
        assert len(update_calls) == 2
        for ws_msg in update_calls:
            assert ws_msg.get("categories") == {"automation": "cat_id"}


class TestHaSetEntityRegistryDisableGuardrail:
    """Test that registry-disable (enabled=False) is blocked for automations and scripts."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server."""
        mcp = MagicMock()
        self.registered_tools = {}

        def capture_add_tool(method):
            fmcp = getattr(method, "__fastmcp__", None)
            name = (fmcp.name if fmcp else None) or method.__name__
            self.registered_tools[name] = method

        mcp.add_tool = capture_add_tool
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
        assert (
            "ha_config_set_automation(entity_id='automation.test', enabled=False)"
            in error_text
        )
        assert "ha_call_service('automation', 'turn_off'" not in error_text
        assert "re-enabled" not in error_text
        assert "reloaded" not in error_text
        error_data = json.loads(error_text)
        suggestions = error_data["error"].get("suggestions", [])
        assert any(
            "ha_config_set_automation(entity_id=..., enabled=...)" in item
            for item in suggestions
        )
        assert not any("automation.turn_off" in item for item in suggestions)
        # Ensure no WebSocket call was made
        mock_client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_disable_script_blocked(self, set_entity_tool, mock_client):
        """enabled=False on script entity should raise ToolError."""
        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(entity_id="script.my_script", enabled=False)

        error_text = str(exc_info.value)
        assert "script" in error_text.lower()
        assert (
            "ha_config_set_script(script_id='script.my_script', run='stop')"
            in error_text
        )
        assert "ha_call_service" not in error_text
        assert "re-enabled" not in error_text
        assert "reloaded" not in error_text
        assert "only to stop a currently running execution" in error_text
        assert "does not disable the script" in error_text
        assert "no script runtime enable/disable service" in error_text.lower()
        error_data = json.loads(error_text)
        suggestions = error_data["error"].get("suggestions", [])
        assert not any(
            "Registry-level hiding is not available" in item for item in suggestions
        )
        assert not any("retry" in item.lower() for item in suggestions)
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
        assert (
            "ha_config_set_automation(entity_id='automation.test', enabled=False)"
            in error_text
        )
        assert "ha_call_service('automation', 'turn_off'" not in error_text
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

        def capture_add_tool(method):
            fmcp = getattr(method, "__fastmcp__", None)
            name = (fmcp.name if fmcp else None) or method.__name__
            self.registered_tools[name] = method

        mcp.add_tool = capture_add_tool
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
        """Core's not_found code is reported as ENTITY_NOT_FOUND, whatever its text."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": "Command failed: Entity missing",
                "error_code": "not_found",
            }
        )

        with pytest.raises(ToolError) as exc_info:
            await remove_entity_tool(entity_id="sensor.definitely_not_real_12345")

        assert json.loads(str(exc_info.value))["error"]["code"] == "ENTITY_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_remove_entity_exception(self, remove_entity_tool, mock_client):
        """WebSocket connection failure should raise ToolError."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=[{"success": True, "result": None}, Exception("conn failed")]
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

        def capture_add_tool(method):
            fmcp = getattr(method, "__fastmcp__", None)
            name = (fmcp.name if fmcp else None) or method.__name__
            self.registered_tools[name] = method

        mcp.add_tool = capture_add_tool
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
        fail = {"success": False, "error": "Command failed: unsupported_domain"}
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
        # create_error_response merges `context` into the top-level response.
        assert body["options_domain"] == "weather"
        assert body["partial"] is True
        assert body["options_succeeded"] == {"sensor": {"display_precision": 1}}

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
    async def test_options_failure_with_empty_error_envelope_uses_fallback(
        self, mock_mcp, mock_client
    ):
        """If HA returns success=False without an error key,
        raise_core_refusal's 'Unknown error' fallback surfaces instead of an
        empty detail.
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
        assert msg.endswith(": Unknown error")

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

        def capture_add_tool(method):
            fmcp = getattr(method, "__fastmcp__", None)
            name = (fmcp.name if fmcp else None) or method.__name__
            self.registered_tools[name] = method

        mcp.add_tool = capture_add_tool
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

    @pytest.mark.asyncio
    async def test_get_entity_surfaces_config_entry_id(self, mock_mcp, mock_client):
        # Issue #1457: agents trying to read a UI-created template helper
        # need the parent config entry id to call ha_get_integration. Before
        # this change the field was dropped from the response, forcing them
        # to fall back to a domain-list scan.
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "entity_id": "sensor.weather_message",
                    "name": None,
                    "original_name": "Weather Message",
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
                    "platform": "template",
                    "device_id": None,
                    "config_entry_id": "01KB6B1Z1SA8B6AVAEMQ4FM8SD",
                    "unique_id": "01KB6B1Z1SA8B6AVAEMQ4FM8SD",
                },
            }
        )
        register_entity_tools(mock_mcp, mock_client)
        tool = self.registered_tools["ha_get_entity"]

        result = await tool(entity_id="sensor.weather_message")

        entry = result["entity_entry"]
        assert entry["config_entry_id"] == "01KB6B1Z1SA8B6AVAEMQ4FM8SD"
        assert entry["platform"] == "template"


class TestHaSetEntityAliases:
    """ha_set_entity aliases keep HA's computed-name (``null``) entry (#2495)."""

    @pytest.fixture
    def set_entity_tool(self, mock_client: MagicMock) -> Callable[..., Any]:
        mcp = MagicMock()
        tools: dict[str, Any] = {}

        def capture_add_tool(method: Callable[..., Any]) -> None:
            fmcp = getattr(method, "__fastmcp__", None)
            tools[(fmcp.name if fmcp else None) or method.__name__] = method

        mcp.add_tool = capture_add_tool
        register_entity_tools(mcp, mock_client)
        tool: Callable[..., Any] = tools["ha_set_entity"]
        return tool

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @staticmethod
    def _update_call(mock_client: MagicMock) -> dict[str, Any]:
        calls: list[dict[str, Any]] = [
            c[0][0]
            for c in mock_client.send_websocket_message.call_args_list
            if c[0][0]["type"] == "config/entity_registry/update"
        ]
        assert len(calls) == 1
        return calls[0]

    @pytest.mark.asyncio
    async def test_plain_list_keeps_existing_computed_name(
        self, set_entity_tool: Callable[..., Any], mock_client: MagicMock
    ) -> None:
        """Registry has [null]; a string-only list must not drop it."""
        mock_client.send_websocket_message.side_effect = [
            {"success": True, "result": {"aliases": [None]}},
            {"success": True, "result": {"entity_entry": {"entity_id": "light.t"}}},
        ]

        result = await set_entity_tool(entity_id="light.t", aliases=["Maanlamp"])

        assert result["success"] is True
        get_call = mock_client.send_websocket_message.call_args_list[0][0][0]
        assert get_call == {
            "type": "config/entity_registry/get",
            "entity_id": "light.t",
        }
        assert self._update_call(mock_client)["aliases"] == [None, "Maanlamp"]

    @pytest.mark.asyncio
    async def test_plain_list_respects_disabled_computed_name(
        self, set_entity_tool: Callable[..., Any], mock_client: MagicMock
    ) -> None:
        """Registry has no null (switch off); the list is sent as given."""
        mock_client.send_websocket_message.side_effect = [
            {"success": True, "result": {"aliases": ["old"]}},
            {"success": True, "result": {"entity_entry": {"entity_id": "light.t"}}},
        ]

        await set_entity_tool(entity_id="light.t", aliases=["new"])

        assert self._update_call(mock_client)["aliases"] == ["new"]

    @pytest.mark.asyncio
    async def test_empty_list_keeps_computed_name(
        self, set_entity_tool: Callable[..., Any], mock_client: MagicMock
    ) -> None:
        """Clearing aliases must not make the entity unaddressable by name."""
        mock_client.send_websocket_message.side_effect = [
            {"success": True, "result": {"aliases": [None, "old"]}},
            {"success": True, "result": {"entity_entry": {"entity_id": "light.t"}}},
        ]

        await set_entity_tool(entity_id="light.t", aliases=[])

        assert self._update_call(mock_client)["aliases"] == [None]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "aliases",
        [[None, "Maanlamp"], '[null, "Maanlamp"]'],
        ids=["list", "json-string"],
    )
    async def test_explicit_null_passes_through_without_lookup(
        self,
        set_entity_tool: Callable[..., Any],
        mock_client: MagicMock,
        aliases: list[str | None] | str,
    ) -> None:
        """Caller-provided null is authoritative: no registry read, sent verbatim."""
        mock_client.send_websocket_message.return_value = {
            "success": True,
            "result": {"entity_entry": {"entity_id": "light.t"}},
        }

        result = await set_entity_tool(entity_id="light.t", aliases=aliases)

        assert result["success"] is True
        assert mock_client.send_websocket_message.call_count == 1
        assert self._update_call(mock_client)["aliases"] == [None, "Maanlamp"]

    @pytest.mark.asyncio
    async def test_lookup_failure_is_reported(
        self, set_entity_tool: Callable[..., Any], mock_client: MagicMock
    ) -> None:
        """A failed registry read must not fall through to a name-dropping write."""
        mock_client.send_websocket_message.return_value = {
            "success": False,
            "error": {"message": "boom"},
        }

        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(entity_id="light.t", aliases=["x"])

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "boom" in error_data["error"]["message"]
        assert mock_client.send_websocket_message.call_count == 1

    @pytest.mark.asyncio
    async def test_switch_off_strips_null_without_lookup(
        self, set_entity_tool: Callable[..., Any], mock_client: MagicMock
    ) -> None:
        """use_entity_name_alias=False removes the own-name entry, even if passed."""
        mock_client.send_websocket_message.return_value = {
            "success": True,
            "result": {"entity_entry": {"entity_id": "light.t"}},
        }

        result = await set_entity_tool(
            entity_id="light.t",
            aliases=[None, "Maanlamp"],
            use_entity_name_alias=False,
        )

        assert mock_client.send_websocket_message.call_count == 1
        assert self._update_call(mock_client)["aliases"] == ["Maanlamp"]
        assert "entity-name alias off" in result["updates"]

    @pytest.mark.asyncio
    async def test_switch_on_adds_null_without_lookup(
        self, set_entity_tool: Callable[..., Any], mock_client: MagicMock
    ) -> None:
        mock_client.send_websocket_message.return_value = {
            "success": True,
            "result": {"entity_entry": {"entity_id": "light.t"}},
        }

        result = await set_entity_tool(
            entity_id="light.t", aliases=["Maanlamp"], use_entity_name_alias=True
        )

        assert mock_client.send_websocket_message.call_count == 1
        assert self._update_call(mock_client)["aliases"] == [None, "Maanlamp"]
        assert "entity-name alias on" in result["updates"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("switch", "expected"),
        [(False, ["old"]), (True, [None, "old"])],
        ids=["off", "on"],
    )
    async def test_switch_alone_rewrites_current_aliases(
        self,
        set_entity_tool: Callable[..., Any],
        mock_client: MagicMock,
        switch: bool,
        expected: list[str | None],
    ) -> None:
        """Without aliases, the switch reads the registry and keeps the strings."""
        mock_client.send_websocket_message.side_effect = [
            {"success": True, "result": {"aliases": [None, "old"]}},
            {"success": True, "result": {"entity_entry": {"entity_id": "light.t"}}},
        ]

        result = await set_entity_tool(
            entity_id="light.t", use_entity_name_alias=switch
        )

        assert result["success"] is True
        assert self._update_call(mock_client)["aliases"] == expected

    @pytest.mark.asyncio
    async def test_kept_null_is_reported_in_updates(
        self, set_entity_tool: Callable[..., Any], mock_client: MagicMock
    ) -> None:
        mock_client.send_websocket_message.side_effect = [
            {"success": True, "result": {"aliases": [None]}},
            {"success": True, "result": {"entity_entry": {"entity_id": "light.t"}}},
        ]

        result = await set_entity_tool(entity_id="light.t", aliases=["x"])

        assert "entity-name alias kept" in result["updates"]

    @pytest.mark.asyncio
    async def test_switch_rejected_in_bulk(
        self, set_entity_tool: Callable[..., Any], mock_client: MagicMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(
                entity_id=["light.a", "light.b"], use_entity_name_alias=False
            )

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "use_entity_name_alias" in body["error"]["message"]
        mock_client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("aliases", [[1, "x"], '["x", 2]', "not json"])
    async def test_non_string_entries_rejected(
        self,
        set_entity_tool: Callable[..., Any],
        mock_client: MagicMock,
        aliases: list[Any] | str,
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await set_entity_tool(entity_id="light.t", aliases=aliases)

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        mock_client.send_websocket_message.assert_not_called()
