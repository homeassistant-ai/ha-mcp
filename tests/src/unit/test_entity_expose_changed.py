"""ha_set_entity reports an exposure that changed nothing (Core 2026.11+).

Core 2026.11 answers ``homeassistant/expose_entity`` with the entity ids it
actually changed (core#188809); before that the reply carried no result, and
the tool can only assume the request changed something.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.entity_expose_batch import apply_bulk_expose
from ha_mcp.tools.tools_entities import register_entity_tools

_ENTRY = {"entity_id": "light.test", "original_name": "Test", "aliases": []}


def _set_entity_tool(client: MagicMock):
    tools = {}
    mcp = MagicMock()
    mcp.add_tool = lambda method: tools.setdefault(method.__fastmcp__.name, method)
    register_entity_tools(mcp, client)
    return tools["ha_set_entity"]


@pytest.mark.parametrize(
    ("expose_reply", "already_set"),
    [
        ({"success": True, "result": {"changed_entity_ids": []}}, True),
        ({"success": True, "result": {"changed_entity_ids": ["light.test"]}}, False),
        ({"success": True, "result": None}, False),
    ],
)
async def test_single_entity_update_says_when_nothing_changed(
    expose_reply: dict, already_set: bool
) -> None:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        side_effect=[expose_reply, {"success": True, "result": _ENTRY}]
    )
    result = await _set_entity_tool(client)(
        entity_id="light.test", expose_to={"conversation": True}
    )
    assert ("already set" in " ".join(result["updates"])) is already_set


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


async def test_bulk_exposure_on_older_core_reports_no_change_set() -> None:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(return_value={"success": True})
    error, changed = await apply_bulk_expose(
        client, ["light.a"], {"conversation": True}, str
    )
    assert (error, changed) == (None, None)
