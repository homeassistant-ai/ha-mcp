"""Issue #2611: the entity dialog's "Main entity of the device" switch.

The dialog writes ``name`` ``""`` when the entity
has an integration name and ``null`` otherwise, and moves an entity with an area
of its own to its device's area. ``ha_set_entity`` maps ``name=""`` to ``null``
(revert to default), so ``use_device_name`` carries the switch.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.entity_update_fields import build_name_visibility_fields
from ha_mcp.tools.tools_entities import register_entity_tools

ENTRY = {
    "entity_id": "sensor.sun_next_dawn",
    "name": "",
    "original_name": "Next dawn",
    "device_id": "dev1",
}


def _ws_ok() -> dict:
    return {"success": True, "result": {"entity_entry": dict(ENTRY)}}


def _ws_get(entry: dict | None = None) -> dict:
    return {"success": True, "result": dict(ENTRY if entry is None else entry)}


class TestBuildNameField:
    def test_false_sends_null_name(self) -> None:
        message: dict = {}
        build_name_visibility_fields(
            message, [], None, None, None, None, use_device_name=False
        )
        assert message["name"] is None

    def test_omitted_leaves_name_out(self) -> None:
        message: dict = {}
        build_name_visibility_fields(message, [], None, None, None, None)
        assert "name" not in message

    def test_empty_string_name_still_clears(self) -> None:
        """Existing callers' ``name=''`` keeps meaning revert-to-default."""
        message: dict = {}
        build_name_visibility_fields(message, [], None, "", None, None)
        assert message["name"] is None


class TestSetEntityUseDeviceName:
    @pytest.fixture
    def tools(self):
        registered: dict = {}
        mcp = MagicMock()

        def capture(method):
            fmcp = getattr(method, "__fastmcp__", None)
            registered[(fmcp.name if fmcp else None) or method.__name__] = method

        mcp.add_tool = capture
        client = MagicMock()
        # use_device_name=True reads the entry (device check) before it writes.
        client.send_websocket_message = AsyncMock(side_effect=[_ws_get(), _ws_ok()])
        register_entity_tools(mcp, client)
        return registered, client

    async def test_false_writes_null_name(self, tools) -> None:
        registered, client = tools
        client.send_websocket_message = AsyncMock(return_value=_ws_ok())
        await registered["ha_set_entity"](
            entity_id="sensor.sun_next_dawn", use_device_name=False
        )
        sent = client.send_websocket_message.call_args.args[0]
        assert sent["name"] is None

    async def test_true_refused_without_device(self, tools) -> None:
        """Core would render an empty name: nothing to follow."""
        registered, client = tools
        client.send_websocket_message = AsyncMock(
            return_value=_ws_get(dict(ENTRY, device_id=None))
        )
        with pytest.raises(ToolError) as exc_info:
            await registered["ha_set_entity"](
                entity_id="sensor.sun_next_dawn", use_device_name=True
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "device" in error["message"]
        assert all(
            c.args[0]["type"] != "config/entity_registry/update"
            for c in client.send_websocket_message.call_args_list
        )

    async def test_rejected_together_with_name(self, tools) -> None:
        registered, client = tools
        with pytest.raises(ToolError) as exc_info:
            await registered["ha_set_entity"](
                entity_id="sensor.sun_next_dawn", name="Dawn", use_device_name=True
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "use_device_name" in error["message"]
        client.send_websocket_message.assert_not_called()

    async def test_rejected_in_bulk(self, tools) -> None:
        registered, client = tools
        with pytest.raises(ToolError) as exc_info:
            await registered["ha_set_entity"](
                entity_id=["sensor.a", "sensor.b"], use_device_name=True
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        client.send_websocket_message.assert_not_called()
