"""ha_remove_helpers_integrations resolves a storage helper's target as
ha_config_set_helper and the backups do (#2632): a storage id first, else the
registry entry of the entity_id. A renamed entity can carry another helper's id as its object
id, so the storage id wins, and an entity_id the registry does not know is
never turned into a delete by its object id.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_integrations import IntegrationTools

# my_button's entity was renamed; another helper's entity now holds
# input_button.my_button.
RENAMED_ROWS: list[dict[str, Any]] = [
    {
        "entity_id": "input_button.my_button",
        "platform": "input_button",
        "unique_id": "other",
    },
    {
        "entity_id": "input_button.renamed",
        "platform": "input_button",
        "unique_id": "my_button",
    },
]


@pytest.fixture(autouse=True)
def _immediate_registry_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """The double answers at once; the retry backoff only adds time."""
    monkeypatch.setattr("ha_mcp.tools.tools_integrations._REGISTRY_RETRY_BASE_DELAY", 0)


def _core(
    stored: list[str], rows: list[dict[str, Any]], list_ok: bool = True
) -> MagicMock:
    """Core's answers by command: the stored ids, the registry, deletes."""
    not_found = {"success": False, "error": "x", "error_code": "not_found"}

    async def ws(msg: dict[str, Any]) -> dict[str, Any]:
        kind = msg["type"]
        if kind == "input_button/list":
            if not list_ok:
                return {"success": False, "error": "timeout"}
            return {"success": True, "result": [{"id": i} for i in stored]}
        if kind == "config/entity_registry/list":
            return {"success": True, "result": rows}
        if kind == "config/entity_registry/get":
            row = next((r for r in rows if r["entity_id"] == msg["entity_id"]), None)
            return {"success": True, "result": row} if row else not_found
        if kind == "input_button/delete":
            if msg["input_button_id"] in stored:
                return {"success": True}
            return not_found
        raise AssertionError(f"unexpected ws message: {msg}")

    client = MagicMock()
    client.send_websocket_message = AsyncMock(side_effect=ws)
    client.get_entity_state = AsyncMock(return_value=None)
    return client


def _deleted(client: MagicMock) -> list[str]:
    return [
        call.args[0]["input_button_id"]
        for call in client.send_websocket_message.call_args_list
        if call.args[0]["type"] == "input_button/delete"
    ]


async def _remove(client: MagicMock, target: str, wait: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = await IntegrationTools(
        client
    ).ha_remove_helpers_integrations(
        target=target, helper_type="input_button", confirm=True, wait=wait
    )
    return result


async def test_a_stored_id_wins_over_an_entity_with_its_text() -> None:
    """The bare id deletes that helper and names its own entity."""
    client = _core(["my_button", "other"], RENAMED_ROWS)

    result = await _remove(client, "my_button")

    assert _deleted(client) == ["my_button"]
    assert result["entity_ids"] == ["input_button.renamed"]


async def test_a_stored_id_removal_waits_for_its_own_entity() -> None:
    client = _core(["my_button", "other"], RENAMED_ROWS)
    verify = AsyncMock(return_value=True)

    with patch("ha_mcp.tools.tools_integrations.verify_entity_removed", verify):
        await _remove(client, "my_button", wait=True)

    assert verify.await_args.args[1] == "input_button.renamed"


async def test_a_repeated_bare_id_removal_deletes_no_other_helper() -> None:
    """After my_button's removal input_button.my_button is still other's
    entity: a repeated bare my_button would delete other."""
    client = _core(["other"], RENAMED_ROWS)

    with pytest.raises(ToolError) as exc_info:
        await _remove(client, "my_button")

    assert json.loads(str(exc_info.value))["error"]["code"] == "ENTITY_NOT_FOUND"
    assert _deleted(client) == []


async def test_an_entity_id_removes_the_helper_its_entity_belongs_to() -> None:
    client = _core(["other"], RENAMED_ROWS)

    await _remove(client, "input_button.my_button")

    assert _deleted(client) == ["other"]


async def test_an_unknown_entity_id_is_not_deleted_by_its_object_id() -> None:
    """input_button.my_button names no entity, and stored my_button belongs to
    input_button.renamed: deleting by the object id would remove it."""
    client = _core(["my_button"], [RENAMED_ROWS[1]])

    with pytest.raises(ToolError) as exc_info:
        await _remove(client, "input_button.my_button")

    assert json.loads(str(exc_info.value))["error"]["code"] == "ENTITY_NOT_FOUND"
    assert _deleted(client) == []


async def test_an_unreadable_list_deletes_nothing() -> None:
    """Without the stored ids the storage id cannot win: input_button.my_button
    would resolve to "other", a different helper."""
    client = _core(["my_button", "other"], RENAMED_ROWS, list_ok=False)

    with pytest.raises(ToolError) as exc_info:
        await _remove(client, "my_button")

    assert "Failed to fetch input_button config list" in str(exc_info.value)
    assert _deleted(client) == []
