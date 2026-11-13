"""ha_set_entity reports an exposure that changed nothing.

Core answers ``homeassistant/expose_entity`` with the entity ids it actually
changed (core#188809).
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from ha_mcp.tools.entity_expose_batch import apply_bulk_expose
from ha_mcp.tools.tools_entities import register_entity_tools

_ENTRY = {"entity_id": "light.test", "original_name": "Test", "aliases": []}


def _set_entity_tool(client: MagicMock) -> Any:
    tools = {}
    mcp = MagicMock()
    mcp.add_tool = lambda method: tools.setdefault(method.__fastmcp__.name, method)
    register_entity_tools(mcp, client)
    return tools["ha_set_entity"]


async def test_empty_expose_map_still_returns_the_entry() -> None:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": _ENTRY}
    )
    result = await _set_entity_tool(client)(entity_id="light.test", expose_to={})
    assert result["entity_entry"]["entity_id"] == "light.test"


async def test_empty_expose_map_reuses_an_entry_already_read() -> None:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": {"entity_entry": _ENTRY}}
    )
    result = await _set_entity_tool(client)(
        entity_id="light.test", name="Test", expose_to={}
    )
    assert result["entity_entry"]["entity_id"] == "light.test"
    client.send_websocket_message.assert_awaited_once()


async def test_bulk_exposure_collects_what_core_changed() -> None:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        side_effect=[
            {"success": True, "result": {"changed_entity_ids": ["light.a"]}},
            {"success": True, "result": {"changed_entity_ids": ["light.b"]}},
        ]
    )
    error, changed = await apply_bulk_expose(
        client,
        ["light.a", "light.b", "light.c"],
        {"conversation": True, "cloud.alexa": False},
        str,
    )
    assert error is None
    assert changed == {"light.a", "light.b"}
