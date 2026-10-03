"""Issue #2611: Home Assistant 2026.10's "Use device name" entity switch.

HA stores the switch as an entity-registry ``name`` of ``""``; ``ha_set_entity``
maps ``name=""`` to ``null`` (revert to default), so an agent could not turn
the switch on. ``use_device_name`` carries the new state explicitly.
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


class TestBuildNameField:
    def test_true_sends_empty_name(self) -> None:
        message: dict = {}
        updates: list[str] = []
        build_name_visibility_fields(
            message, updates, None, None, None, None, use_device_name=True
        )
        assert message["name"] == ""
        assert updates

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
        client.send_websocket_message = AsyncMock(return_value=_ws_ok())
        client.get_config = AsyncMock(return_value={"version": "2026.10.0"})
        register_entity_tools(mcp, client)
        return registered, client

    async def test_true_writes_empty_name_on_2026_10(self, tools) -> None:
        registered, client = tools
        result = await registered["ha_set_entity"](
            entity_id="sensor.sun_next_dawn", use_device_name=True
        )
        sent = client.send_websocket_message.call_args.args[0]
        assert sent["type"] == "config/entity_registry/update"
        assert sent["name"] == ""
        assert result["success"] is True

    async def test_false_writes_null_name(self, tools) -> None:
        registered, client = tools
        await registered["ha_set_entity"](
            entity_id="sensor.sun_next_dawn", use_device_name=False
        )
        sent = client.send_websocket_message.call_args.args[0]
        assert sent["name"] is None

    @pytest.mark.parametrize("version", ["2026.9.4", "2025.12.1"])
    async def test_refused_before_2026_10(self, tools, version: str) -> None:
        """Older Cores store ``""`` but render it as the default name, so the
        switch would silently do the opposite of what was asked."""
        registered, client = tools
        client.get_config = AsyncMock(return_value={"version": version})
        with pytest.raises(ToolError) as exc_info:
            await registered["ha_set_entity"](
                entity_id="sensor.sun_next_dawn", use_device_name=True
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "2026.10" in error["message"]
        client.send_websocket_message.assert_not_called()

    async def test_false_is_not_version_gated(self, tools) -> None:
        registered, client = tools
        client.get_config = AsyncMock(return_value={"version": "2026.9.4"})
        await registered["ha_set_entity"](
            entity_id="sensor.sun_next_dawn", use_device_name=False
        )
        assert client.send_websocket_message.call_args.args[0]["name"] is None

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


class TestGetEntityReportsSwitch:
    @pytest.fixture
    def tools(self):
        registered: dict = {}
        mcp = MagicMock()

        def capture(method):
            fmcp = getattr(method, "__fastmcp__", None)
            registered[(fmcp.name if fmcp else None) or method.__name__] = method

        mcp.add_tool = capture
        client = MagicMock()
        register_entity_tools(mcp, client)
        return registered, client

    @pytest.mark.parametrize(
        ("name", "expected"), [("", True), (None, False), ("Custom", False)]
    )
    async def test_uses_device_name_flag(self, tools, name, expected) -> None:
        registered, client = tools
        entry = dict(ENTRY, name=name)
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": entry}
        )
        result = await registered["ha_get_entity"](entity_id="sensor.sun_next_dawn")
        assert result["entity_entry"]["uses_device_name"] is expected
        assert result["entity_entry"]["name"] == name
