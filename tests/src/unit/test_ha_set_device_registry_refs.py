"""Registry-reference validation for ``ha_set_device`` (issue #2159).

``config/device_registry/update`` stores whatever ``area_id`` / ``labels`` it is
handed, so a typo or a since-deleted ID left the device pointing at registry
entries that do not exist while the tool reported success. The write now
preflights both against their registries, and fails closed when a registry
cannot be read — a degraded lookup must not let a dangling reference through.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_registry import RegistryTools


def _ws_handler(*, area_ids=(), label_ids=()):
    """Answer the registry-list preflights, then ack the device update."""

    async def handler(msg):
        msg_type = msg.get("type")
        if msg_type == "config/area_registry/list":
            return {"success": True, "result": [{"area_id": a} for a in area_ids]}
        if msg_type == "config/label_registry/list":
            return {"success": True, "result": [{"label_id": lbl} for lbl in label_ids]}
        return {"success": True, "result": {"name_by_user": "Hub"}}

    return handler


@pytest.fixture
def tools():
    client = MagicMock()
    client.send_websocket_message = AsyncMock()
    return RegistryTools(client)


def _sent_types(tools):
    return [
        call.args[0]["type"]
        for call in tools._client.send_websocket_message.call_args_list
    ]


class TestSetDeviceRegistryReferences:
    """Unknown area/label IDs must be rejected before the registry write."""

    async def test_unknown_area_rejected(self, tools):
        tools._client.send_websocket_message.side_effect = _ws_handler(
            area_ids=("living_room",)
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_device(device_id="dev1", area_id="ghost_area")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert error_data["area_id"] == "ghost_area"
        assert "config/device_registry/update" not in _sent_types(tools)

    async def test_unknown_label_rejected(self, tools):
        tools._client.send_websocket_message.side_effect = _ws_handler(
            label_ids=("important",)
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_device(device_id="dev1", labels=["ghost_label"])

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert error_data["unknown_labels"] == ["ghost_label"]
        assert "config/device_registry/update" not in _sent_types(tools)

    async def test_registry_lookup_failure_fails_closed(self, tools):
        tools._client.send_websocket_message.return_value = {
            "success": False,
            "error": {"message": "registry unavailable"},
        }

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_device(device_id="dev1", area_id="living_room")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "CONNECTION_FAILED"
        assert _sent_types(tools) == ["config/area_registry/list"]

    async def test_existing_area_and_labels_pass_through(self, tools):
        tools._client.send_websocket_message.side_effect = _ws_handler(
            area_ids=("living_room",), label_ids=("important",)
        )

        result = await tools.ha_set_device(
            device_id="dev1", area_id="living_room", labels=["important"]
        )

        assert result["success"] is True
        update_call = tools._client.send_websocket_message.call_args.args[0]
        assert update_call["type"] == "config/device_registry/update"
        assert update_call["area_id"] == "living_room"
        assert update_call["labels"] == ["important"]

    async def test_empty_label_list_clears_without_lookup(self, tools):
        """An empty list is the documented clear sentinel — nothing to validate."""
        tools._client.send_websocket_message.side_effect = _ws_handler()

        result = await tools.ha_set_device(device_id="dev1", labels=[])

        assert result["success"] is True
        assert _sent_types(tools) == ["config/device_registry/update"]
