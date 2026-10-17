"""ha_remove_entity on a stored helper's entity (#2632): the stored helper
stays and gets its entity back when Home Assistant restarts, so the reply says
so and names the tool that removes the helper itself. A YAML helper has a
registry entry but no stored item, so its removal names no tool.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools.tools_entities import EntityTools

pytestmark = pytest.mark.asyncio

_NOT_FOUND = {"success": False, "error": "Entity not found", "error_code": "not_found"}


def _core(
    entry: dict[str, Any] | Exception | None,
    stored: list[str] | None = None,
    list_reply: dict[str, Any] | None = None,
    get_reply: dict[str, Any] | None = None,
) -> MagicMock:
    """Core's registry holding ``entry`` until it is removed, and the stored ids."""
    removed: list[str] = []

    async def ws(msg: dict[str, Any]) -> dict[str, Any]:
        kind = msg["type"]
        if kind == "config/entity_registry/get":
            if isinstance(entry, Exception):
                raise entry
            if get_reply is not None:
                return get_reply
            held = isinstance(entry, dict) and entry["entity_id"] == msg["entity_id"]
            if not held or removed:
                return _NOT_FOUND
            return {"success": True, "result": entry}
        if kind == "config/entity_registry/remove":
            removed.append(msg["entity_id"])
            return {"success": True, "result": None}
        if kind.endswith("/list"):
            return list_reply or {
                "success": True,
                "result": [{"id": i} for i in stored or []],
            }
        raise AssertionError(f"unexpected ws message: {msg}")

    client = MagicMock()
    client.send_websocket_message = AsyncMock(side_effect=ws)
    client.removed = removed
    return client


def _entry(platform: str, unique_id: str = "porch") -> dict[str, Any]:
    return {
        "entity_id": f"{platform}.renamed",
        "platform": platform,
        "unique_id": unique_id,
    }


async def _remove(client: MagicMock, entity_id: str) -> dict[str, Any]:
    result: dict[str, Any] = await EntityTools(client).ha_remove_entity(
        entity_id=entity_id
    )
    return result


@pytest.mark.parametrize(
    ("platform", "remove"),
    [
        (
            "input_boolean",
            "ha_remove_helpers_integrations(helper_type='input_boolean', "
            "target='porch', confirm=True)",
        ),
        ("zone", "ha_remove_zone(zone_id='porch')"),
    ],
)
async def test_a_stored_helper_s_entity_names_the_call_that_removes_it(
    platform: str, remove: str
) -> None:
    """The stored porch stays, so removing its entity alone removes nothing.
    The registry is read before the entry is gone."""
    client = _core(_entry(platform), stored=["porch"])

    result = await _remove(client, f"{platform}.renamed")

    assert client.removed == [f"{platform}.renamed"]
    assert remove in result["warnings"][0]
    assert "restarts" in result["warnings"][0]


async def test_a_yaml_helper_s_entity_names_no_tool() -> None:
    """input_boolean porch has a registry entry but no stored item."""
    client = _core(_entry("input_boolean"), stored=[])

    result = await _remove(client, "input_boolean.renamed")

    assert client.removed == ["input_boolean.renamed"]
    assert "warnings" not in result


async def test_another_integration_s_entity_gets_no_helper_note() -> None:
    client = _core({"entity_id": "sensor.old", "platform": "hue", "unique_id": "abc"})

    result = await _remove(client, "sensor.old")

    assert client.removed == ["sensor.old"]
    assert "warnings" not in result


async def test_an_entity_without_a_registry_entry_gets_no_note() -> None:
    """Its removal reports the missing entry itself."""
    client = _core(None)

    result = await _remove(client, "input_boolean.renamed")

    assert "warnings" not in result


@pytest.mark.parametrize(
    "client",
    [
        _core(HomeAssistantConnectionError("socket closed")),
        _core(None, get_reply={"success": False, "error": "403 Forbidden"}),
        _core(
            _entry("input_boolean"),
            list_reply={"success": False, "error": "403 Forbidden"},
        ),
    ],
    ids=["registry read dropped", "registry read refused", "list read refused"],
)
async def test_an_unread_helper_is_named_as_unknown_not_removed_silently(
    client: MagicMock,
) -> None:
    """A plain success would hide a stored helper the removal leaves."""
    result = await _remove(client, "input_boolean.renamed")

    assert client.removed == ["input_boolean.renamed"]
    assert "could not be read" in result["warnings"][0]
