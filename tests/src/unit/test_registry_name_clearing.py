"""Clearing and quote-only names on ``ha_set_device`` / ``ha_set_entity`` (issue #2585).

Some MCP clients cannot send an empty string: ``""`` arrives as two literal
quote characters and was stored as the device's name, while a single space
arrives intact. A whitespace-only value now clears like ``''`` does, a
quote-only name is rejected before any registry write, and a device rename
that did not happen is no longer echoed in ``updates``.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_entities import register_entity_tools
from ha_mcp.tools.tools_registry import RegistryTools

QUOTE_ONLY_NAMES = ['""', "''", '" "']


def _sent(client: MagicMock) -> list[dict[str, Any]]:
    return [call.args[0] for call in client.send_websocket_message.call_args_list]


def _sent_of_type(client: MagicMock, msg_type: str) -> list[dict[str, Any]]:
    return [msg for msg in _sent(client) if msg["type"] == msg_type]


def _assert_rejected_with_clear_hint(exc_info: pytest.ExceptionInfo[ToolError]) -> None:
    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    hints = " ".join([error.get("suggestion", ""), *error.get("suggestions", [])])
    assert "' '" in hints, f"hint must offer the single-space clear: {error}"


@pytest.fixture
def client() -> MagicMock:
    client = MagicMock()
    client.send_websocket_message = AsyncMock()
    return client


@pytest.fixture
def registry_tools(client: MagicMock) -> RegistryTools:
    return RegistryTools(client)


@pytest.fixture
def set_entity(client: MagicMock) -> Any:
    tools: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        tools[method.__fastmcp__.name] = method

    mcp = MagicMock()
    mcp.add_tool = capture_add_tool
    register_entity_tools(mcp, client)
    return tools["ha_set_entity"]


def _entity_ws_handler(
    *, device_id: str | None = "dev1", device_update_ok: bool = True
) -> Any:
    async def handler(msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg["type"]
        if msg_type == "config/area_registry/list":
            return {"success": True, "result": [{"area_id": "living_room"}]}
        if msg_type == "config/entity_registry/update":
            entry = {"entity_id": msg["entity_id"], "device_id": device_id}
            return {"success": True, "result": {"entity_entry": entry}}
        if msg_type == "config/entity_registry/get":
            return {
                "success": True,
                "result": {"entity_id": msg["entity_id"], "device_id": device_id},
            }
        if msg_type == "config/device_registry/update":
            if device_update_ok:
                return {"success": True, "result": {}}
            return {"success": False, "error": {"message": "device update refused"}}
        raise AssertionError(f"unexpected WS message: {msg}")

    return handler


class TestSetDeviceNameClearing:
    @pytest.mark.parametrize("name", QUOTE_ONLY_NAMES)
    async def test_quote_only_name_is_not_stored_as_device_name(
        self, registry_tools: RegistryTools, client: MagicMock, name: str
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await registry_tools.ha_set_device(device_id="dev1", name=name)

        _assert_rejected_with_clear_hint(exc_info)
        assert _sent(client) == []

    async def test_quote_only_disabled_by_is_rejected(
        self, registry_tools: RegistryTools, client: MagicMock
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await registry_tools.ha_set_device(device_id="dev1", disabled_by='""')

        _assert_rejected_with_clear_hint(exc_info)
        assert _sent(client) == []

    @pytest.mark.parametrize("name", ["Bob's Lamp", '12" Monitor'])
    async def test_name_containing_quotes_is_stored(
        self, registry_tools: RegistryTools, client: MagicMock, name: str
    ) -> None:
        client.send_websocket_message.return_value = {"success": True, "result": {}}

        await registry_tools.ha_set_device(device_id="dev1", name=name)

        assert _sent(client)[0]["name_by_user"] == name

    @pytest.mark.parametrize(
        ("field", "value", "sent_key"),
        [
            ("name", "", "name_by_user"),
            ("name", " ", "name_by_user"),
            ("area_id", " ", "area_id"),
            ("disabled_by", " ", "disabled_by"),
        ],
    )
    async def test_blank_value_clears_device_field(
        self,
        registry_tools: RegistryTools,
        client: MagicMock,
        field: str,
        value: str,
        sent_key: str,
    ) -> None:
        client.send_websocket_message.return_value = {"success": True, "result": {}}

        await registry_tools.ha_set_device(device_id="dev1", **{field: value})

        assert [msg["type"] for msg in _sent(client)] == [
            "config/device_registry/update"
        ]
        assert _sent(client)[0][sent_key] is None


class TestSetEntityNameClearing:
    async def test_padded_device_class_is_stripped(
        self, set_entity: Any, client: MagicMock
    ) -> None:
        client.send_websocket_message.side_effect = _entity_ws_handler()

        await set_entity(entity_id="binary_sensor.test", device_class=" window ")

        (update,) = _sent_of_type(client, "config/entity_registry/update")
        assert update["device_class"] == "window"

    @pytest.mark.parametrize("field", ["name", "icon", "device_class"])
    async def test_quote_only_value_is_not_stored(
        self, set_entity: Any, client: MagicMock, field: str
    ) -> None:
        client.send_websocket_message.side_effect = _entity_ws_handler()

        with pytest.raises(ToolError) as exc_info:
            await set_entity(entity_id="light.test", **{field: '""'})

        _assert_rejected_with_clear_hint(exc_info)
        assert _sent(client) == []

    async def test_quote_only_device_name_rejected_before_entity_write(
        self, set_entity: Any, client: MagicMock
    ) -> None:
        client.send_websocket_message.side_effect = _entity_ws_handler()

        with pytest.raises(ToolError) as exc_info:
            await set_entity(
                entity_id="light.test", icon="mdi:lamp", new_device_name='""'
            )

        _assert_rejected_with_clear_hint(exc_info)
        assert _sent(client) == []

    @pytest.mark.parametrize("field", ["name", "area_id", "icon"])
    async def test_whitespace_clears_entity_override(
        self, set_entity: Any, client: MagicMock, field: str
    ) -> None:
        client.send_websocket_message.side_effect = _entity_ws_handler()

        await set_entity(entity_id="light.test", **{field: " "})

        (update,) = _sent_of_type(client, "config/entity_registry/update")
        assert update[field] is None


class TestSetEntityDeviceNameEcho:
    @pytest.mark.parametrize("new_device_name", ["", " "])
    async def test_blank_device_name_neither_renames_nor_echoes(
        self, set_entity: Any, client: MagicMock, new_device_name: str
    ) -> None:
        client.send_websocket_message.side_effect = _entity_ws_handler()

        result = await set_entity(
            entity_id="light.test", icon="mdi:lamp", new_device_name=new_device_name
        )

        assert _sent_of_type(client, "config/device_registry/update") == []
        assert not any(u.startswith("device_name") for u in result["updates"])
        assert any("ha_set_device(name='')" in w for w in result["warnings"])
        assert "device_rename" not in result
        assert "partial" not in result

    @pytest.mark.parametrize("new_device_name", ["", " "])
    async def test_blank_device_name_alone_is_an_error(
        self, set_entity: Any, client: MagicMock, new_device_name: str
    ) -> None:
        client.send_websocket_message.side_effect = _entity_ws_handler()

        with pytest.raises(ToolError) as exc_info:
            await set_entity(entity_id="light.test", new_device_name=new_device_name)

        _assert_rejected_with_clear_hint(exc_info)
        assert _sent(client) == []

    async def test_deviceless_entity_rename_not_echoed(
        self, set_entity: Any, client: MagicMock
    ) -> None:
        client.send_websocket_message.side_effect = _entity_ws_handler(device_id=None)

        result = await set_entity(entity_id="light.test", new_device_name="Lamp")

        assert not any(u.startswith("device_name") for u in result["updates"])
        assert result["device_rename"]["warnings"]
        assert result["warnings"] == result["device_rename"]["warnings"]

    async def test_failed_device_rename_not_echoed(
        self, set_entity: Any, client: MagicMock
    ) -> None:
        client.send_websocket_message.side_effect = _entity_ws_handler(
            device_update_ok=False
        )

        result = await set_entity(entity_id="light.test", new_device_name="Lamp")

        assert not any(u.startswith("device_name") for u in result["updates"])
        assert result["partial"] is True

    async def test_successful_device_rename_is_echoed(
        self, set_entity: Any, client: MagicMock
    ) -> None:
        client.send_websocket_message.side_effect = _entity_ws_handler()

        result = await set_entity(entity_id="light.test", new_device_name="Lamp")

        assert any(u.startswith("device_name") for u in result["updates"])
