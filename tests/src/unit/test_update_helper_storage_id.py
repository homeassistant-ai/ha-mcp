"""ha_config_set_helper resolves an update's target as the removal and the
backups do (#2632): a bare id is a storage id, and only ``<type>.<name>`` is
an entity_id. A renamed entity can carry another helper's id as its name, so
an update never goes to the entity a bare id names, and the reply and the
registry fields name the helper's own entity or none.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools import component_helper_collections as chc
from ha_mcp.tools.config_helpers import update as hc_update

from .test_helper_config_fold import (
    _FETCH_SCHEMAS,
    _STORED_ZONE,
)

pytestmark = pytest.mark.asyncio

_NOT_FOUND = {"success": False, "error": {"code": "not_found", "message": "x"}}
_FOUND = {"success": True, "item_id": "b", "item": {"id": "b"}}


@pytest.mark.parametrize(
    ("helper_id", "first", "looked_up_by_entity"),
    [
        ("a", _FOUND, False),
        ("input_boolean.a", _NOT_FOUND, True),
        ("input_boolean.a", None, False),
        (
            "input_boolean.a",
            {"success": False, "error": {"code": "unavailable"}},
            False,
        ),
    ],
    ids=["stored id", "entity_id", "failed read", "other failure"],
)
async def test_only_an_entity_id_the_component_misses_is_read_by_its_entity(
    helper_id: str, first: Any, looked_up_by_entity: bool
) -> None:
    """A failed read is not "no such item", so only the component's
    not_found for an entity_id leads to the entity read."""
    reads: list[dict[str, Any]] = []

    async def read(_client: Any, _command: str, **kwargs: Any) -> Any:
        reads.append(kwargs)
        return first if "item_id" in kwargs else _FOUND

    with patch.object(chc, "_read", read):
        item = await chc.read_stored_helper_item(
            MagicMock(), "input_boolean", helper_id, "input_boolean.a"
        )

    assert [r.get("entity_id") for r in reads][1:] == (
        ["input_boolean.a"] if looked_up_by_entity else []
    )
    assert item == (_FOUND if first is _FOUND or first is _NOT_FOUND else None)


async def test_a_bare_id_the_component_misses_is_refused_unread() -> None:
    """input_boolean.a can be another helper's entity, as after a removal."""
    reads: list[dict[str, Any]] = []

    async def read(_client: Any, _command: str, **kwargs: Any) -> Any:
        reads.append(kwargs)
        return _NOT_FOUND if "item_id" in kwargs else _FOUND

    with patch.object(chc, "_read", read), pytest.raises(ToolError) as exc_info:
        await chc.read_stored_helper_item(
            MagicMock(), "input_boolean", "a", "input_boolean.a"
        )

    assert json.loads(str(exc_info.value))["error"]["code"] == "ENTITY_NOT_FOUND"
    assert [r for r in reads if "entity_id" in r] == []


_RENAMED_PORCH = [
    {
        "entity_id": "input_boolean.porch",
        "platform": "input_boolean",
        "unique_id": "hall",
    },
    {
        "entity_id": "input_boolean.porch_2",
        "platform": "input_boolean",
        "unique_id": "porch",
    },
]


def _porch_core(rows: list[dict[str, Any]], sent: list[dict[str, Any]]) -> MagicMock:
    """Core's answers by command for the stored helpers porch and hall."""
    items = [{"id": "porch", "name": "Porch"}, {"id": "hall", "name": "Hall"}]
    not_found = {"success": False, "error": "x", "error_code": "not_found"}

    async def ws(msg: dict[str, Any]) -> dict[str, Any]:
        sent.append(msg)
        kind = msg["type"]
        if kind == "input_boolean/list":
            return {"success": True, "result": items}
        if kind == "config/entity_registry/list":
            return {"success": True, "result": rows}
        if kind == "config/entity_registry/get":
            row = next((r for r in rows if r["entity_id"] == msg["entity_id"]), None)
            return {"success": True, "result": row} if row else not_found
        if kind == "input_boolean/update":
            return {"success": True, "result": {"id": msg["input_boolean_id"]}}
        return {"success": True, "result": {"entity_entry": {}}}

    client = MagicMock()
    client.send_websocket_message = AsyncMock(side_effect=ws)
    return client


async def _legacy_update(client: MagicMock, **changes: Any) -> dict[str, Any]:
    args = {"name": None, "icon": None, "area_id": None, "wait": False, **changes}
    with (
        patch.object(
            hc_update, "read_stored_helper_item", AsyncMock(return_value=None)
        ),
        patch(_FETCH_SCHEMAS, AsyncMock(return_value=None)),
    ):
        result: dict[str, Any] = await hc_update._execute_update_simple_helper(
            client, "input_boolean", "input_boolean.porch", "porch", args["name"],
            args["icon"], args["area_id"], None, None, args["wait"], False, {},
        )  # fmt: skip
    return result


async def test_a_bare_id_the_component_misses_hints_at_create() -> None:
    """The component path keeps the create hint the WebSocket path gives."""

    async def read(_client: Any, _command: str, **_kwargs: Any) -> Any:
        return _NOT_FOUND

    with patch.object(chc, "_read", read), pytest.raises(ToolError) as exc_info:
        await hc_update._execute_update_simple_helper(
            MagicMock(), "input_boolean", "typo", "typo", "Brand New Helper",
            None, None, None, None, False, False, {},
        )  # fmt: skip

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "ENTITY_NOT_FOUND"
    assert any("Brand New Helper" in s for s in error["suggestions"])


async def test_a_refused_bare_id_says_which_helper_and_where_to_find_it() -> None:
    """Without a name the refusal still names the target and the listing."""
    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": []}
    )

    with pytest.raises(ToolError) as exc_info:
        await chc.stored_helper_target(client, "input_boolean", "typo")

    payload = json.loads(str(exc_info.value))
    assert (payload["target"], payload["helper_type"]) == ("typo", "input_boolean")
    error = payload["error"]
    hints = [error.get("suggestion", ""), *error.get("suggestions", [])]
    assert any(
        "ha_config_list_helpers(helper_type='input_boolean')" in s for s in hints
    )


async def test_a_bare_id_no_helper_has_updates_nothing() -> None:
    """input_boolean.attic is hall's entity: attic would update hall."""
    sent: list[dict[str, Any]] = []
    rows = [{"entity_id": "input_boolean.attic", "platform": "input_boolean",
             "unique_id": "hall"}]  # fmt: skip
    with (
        patch.object(
            hc_update, "read_stored_helper_item", AsyncMock(return_value=None)
        ),
        pytest.raises(ToolError) as exc_info,
    ):
        await hc_update._execute_update_simple_helper(
            _porch_core(rows, sent), "input_boolean", "input_boolean.attic",
            "attic", "A", None, None, None, None, False, False, {},
        )  # fmt: skip

    error = json.loads(str(exc_info.value))
    assert error["error"]["code"] == "ENTITY_NOT_FOUND"
    assert "omit helper_id" in str(error)
    assert [m for m in sent if m["type"] == "input_boolean/update"] == []


async def test_registry_fields_of_an_update_go_to_the_helper_s_own_entity() -> None:
    """input_boolean.porch would give hall the icon porch was given."""
    sent: list[dict[str, Any]] = []

    result = await _legacy_update(_porch_core(_RENAMED_PORCH, sent), icon="mdi:x")

    assert [
        m["entity_id"] for m in sent if m["type"] == "config/entity_registry/update"
    ] == ["input_boolean.porch_2"]
    assert result["entity_id"] == "input_boolean.porch_2"


async def test_an_updated_helper_without_an_entity_names_none_and_why() -> None:
    """input_boolean.porch is no entity of porch: the reply names no entity and
    says why it has none."""
    result = await _legacy_update(_porch_core([], []), name="P", wait=True)

    assert result.get("entity_id") is None
    assert "the input_boolean has no entity in the registry" in result["warnings"][0]
    assert "not waited on" in result["warnings"][0]


async def test_a_component_reply_without_an_entity_names_none() -> None:
    """<type>.<id> would name another helper's entity."""
    read = AsyncMock(
        return_value={"success": True, "item_id": "porch", "item": {"id": "porch"}}
    )
    write = AsyncMock(
        return_value={"success": True, "item": {"id": "porch", "name": "P"},
                      "registry_applied": {}, "warnings": []}
    )  # fmt: skip
    with (
        patch.object(hc_update, "read_stored_helper_item", read),
        patch.object(hc_update, "write_helper_item", write),
        patch(_FETCH_SCHEMAS, AsyncMock(return_value=None)),
    ):
        result = await hc_update._execute_update_simple_helper(
            MagicMock(), "input_boolean", "input_boolean.porch", "porch", "P", None,
            None, None, None, False, False, {},
        )  # fmt: skip

    assert result.get("entity_id") is None


async def test_an_error_names_no_guessed_entity_when_the_component_read_none() -> None:
    """The component read no entity for zone z: zone.z can be another zone's."""
    read = AsyncMock(
        return_value={"success": True, "item_id": "z", "item": _STORED_ZONE}
    )
    with (
        patch.object(hc_update, "read_stored_helper_item", read),
        patch.object(hc_update, "write_helper_item", AsyncMock()),
        pytest.raises(ToolError) as exc_info,
    ):
        await hc_update._execute_update_simple_helper(
            MagicMock(), "zone", "zone.z", "z", None, "", None, None, None,
            False, False, {},
        )  # fmt: skip

    assert "zone.z" not in str(exc_info.value)
