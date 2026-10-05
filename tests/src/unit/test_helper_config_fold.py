"""ha_config_set_helper: SIMPLE-type fields in ``config``, Core-sourced schemas,
component-routed writes, and the component-aware catalog (issue #2479)."""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from typing import Annotated, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools import tools_config_helpers as tch
from ha_mcp.tools.component_api import ComponentCaps
from ha_mcp.tools.component_helper_collections import (
    collection_payload as client_payload,
)
from ha_mcp.tools.config_helpers import create as hc_create
from ha_mcp.tools.config_helpers import schemas as hc_schemas
from ha_mcp.tools.config_helpers import typed_config as hc_typed
from ha_mcp.tools.config_helpers import update as hc_update
from ha_mcp.tools.helpers import HIDDEN_PARAM, hidden_param_names
from ha_mcp.tools.tools_config_helpers import register_config_helper_tools

from .test_updates_repairs import _client as _ws

pytestmark = pytest.mark.asyncio

_HIDDEN = {
    "min_value", "max_value", "step", "unit_of_measurement", "options", "initial",
    "mode", "has_date", "has_time", "restore", "duration", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday", "latitude",
    "longitude", "radius", "passive", "user_id", "device_trackers", "picture",
    "tag_id", "description", "pattern",
}  # fmt: skip

_CORE_SCHEMAS: dict[str, Any] = {
    t: {"create": [{"name": "name", "required": True, "type": "string"}], "update": []}
    for t in hc_schemas.SIMPLE_HELPER_TYPES
}
_CORE_SCHEMAS["input_number"] = {
    "create": [
        {"name": "name", "required": True, "type": "string"},
        {"name": "min", "required": True, "type": "float"},
        {"name": "max", "required": True, "type": "float"},
        {"name": "step", "required": False, "type": "float", "default": 1},
        {"name": "mode", "options": [["box", "box"], ["slider", "slider"]]},
        {"name": "icon", "required": False, "type": "string"},
    ],
    "update": [{"name": "min", "required": True, "type": "float"}],
}


async def _registered_tool() -> Any:
    mcp = FastMCP("t")
    register_config_helper_tools(mcp, MagicMock())
    return mcp, await mcp.get_tool("ha_config_set_helper")


async def test_schema_publishes_config_but_not_flat_fields() -> None:
    _, tool = await _registered_tool()
    properties = tool.parameters["properties"]
    assert not _HIDDEN & properties.keys()
    assert {"helper_type", "name", "icon", "config", "wait"} <= properties.keys()
    # Every hidden field is still documented in the config description.
    description = properties["config"]["description"]
    for field in _HIDDEN - {"min_value", "max_value"}:
        if field not in {"monday", "tuesday", "wednesday", "thursday", "friday",
                         "saturday", "sunday"}:  # fmt: skip
            assert field in description, field
    assert "monday..sunday" in description


async def test_hidden_param_names_reads_the_marker() -> None:
    def fn(a: int, b: Annotated[int | None, HIDDEN_PARAM] = None) -> None:
        """Signature only."""

    assert hidden_param_names(fn) == frozenset({"b"})


class _Captured:
    def __init__(self) -> None:
        self.args: list[tuple[Any, ...]] = []
        self.kwargs: list[dict[str, Any]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.args.append(args)
        self.kwargs.append(kwargs)
        return {"success": True}


@pytest.fixture
def capture_create() -> Iterator[_Captured]:
    captured = _Captured()
    with (
        patch.object(tch, "_execute_create_simple_helper", captured),
        patch.object(tch, "_check_name_collision", AsyncMock()),
        patch.object(tch, "validate_registry_ids", AsyncMock()),
        patch.object(hc_typed, "fetch_helper_schemas", AsyncMock(return_value=None)),
    ):
        yield captured


async def _call(mcp: FastMCP, **arguments: Any) -> Any:
    async with Client(mcp) as client:
        return await client.call_tool("ha_config_set_helper", arguments)


def _fields(captured: _Captured, call: int = 0) -> dict[str, Any]:
    """The Core fields an executor received (its last positional argument)."""
    return captured.args[call][-1]  # type: ignore[no-any-return]


async def test_flat_and_config_fields_reach_create_identically(
    capture_create: _Captured,
) -> None:
    mcp, _ = await _registered_tool()
    base = {"helper_type": "input_number", "name": "Target", "action": "create"}
    await _call(mcp, **base, min_value=1, max_value="9", step=2)
    await _call(mcp, **base, config={"min": 1, "max": 9, "step": 2})
    assert _fields(capture_create, 0) == _fields(capture_create, 1)
    assert _fields(capture_create, 1) == {"min": 1, "max": 9, "step": 2}


async def test_config_accepts_core_names_and_name(capture_create: _Captured) -> None:
    mcp, _ = await _registered_tool()
    await _call(
        mcp,
        helper_type="counter",
        action="create",
        config={"name": "Laps", "minimum": 0, "maximum": "5", "icon": "mdi:run"},
    )
    assert _fields(capture_create) == {"minimum": 0, "maximum": 5}
    # positional: client, helper_type, name, icon
    assert capture_create.args[0][2:4] == ("Laps", "mdi:run")


async def test_counter_range_params_use_counters_core_names(
    capture_create: _Captured,
) -> None:
    """Core's counter calls the range minimum/maximum, not min/max."""
    mcp, _ = await _registered_tool()
    await _call(mcp, helper_type="counter", name="C", min_value=0, max_value=5)
    assert _fields(capture_create) == {"minimum": 0, "maximum": 5}


async def test_core_value_types_pass_through(capture_create: _Captured) -> None:
    """Core takes seconds for a timer and a fractional number initial."""
    mcp, _ = await _registered_tool()
    base = {"action": "create", "name": "T"}
    await _call(mcp, helper_type="timer", **base, config={"duration": 300})
    await _call(mcp, helper_type="input_number", **base, initial=2.5,
                min_value=0, max_value=5)  # fmt: skip
    await _call(mcp, helper_type="input_boolean", **base, config={"initial": True})
    timer, number, boolean = (_fields(capture_create, i) for i in range(3))
    assert (timer["duration"], number["initial"]) == (300, 2.5)
    assert boolean["initial"] is True


async def test_unknown_config_keys_reach_core_for_its_suggestion(
    capture_create: _Captured,
) -> None:
    """Core rejects a key its schema lacks and names the closest one, so the
    tool sends it rather than replacing that with its own generic error."""
    mcp, _ = await _registered_tool()
    await _call(mcp, helper_type="input_number", name="T", config={"minn": 1})
    assert _fields(capture_create) == {"minn": 1}


@pytest.mark.parametrize(
    ("helper_type", "key"),
    [("input_boolean", "type"), ("input_number", "input_number_id"), ("tag", "id")],
)
async def test_config_cannot_choose_the_command_or_the_item(
    capture_create: _Captured, helper_type: str, key: str
) -> None:
    """``type`` and the item ids address the WebSocket request; a config key
    with that name would send another command or write another item."""
    mcp, _ = await _registered_tool()
    with pytest.raises(ToolError, match=key):
        await _call(mcp, helper_type=helper_type, name="T", config={key: "x"})
    assert capture_create.args == []


async def test_websocket_messages_keep_their_command_and_target() -> None:
    """Defence in depth below the config check: fields never override the
    command, nor the update's target id."""
    client = _ws_by_type(
        {
            "input_boolean/list": [{"id": "b", "name": "B"}],
            "config/entity_registry/get": {"unique_id": "b"},
        }
    )
    with patch.object(hc_update, "read_helper_item", AsyncMock(return_value=None)):
        await hc_update._execute_legacy_update(
            client, "input_boolean", "input_boolean.b", "b", None, None,
            {"type": "input_boolean/delete", "input_boolean_id": "other"},
        )  # fmt: skip
    (update,) = _sent(client, "input_boolean/update")
    assert update["input_boolean_id"] == "b"
    assert not _sent(client, "input_boolean/delete")
    message = hc_create._build_create_message(
        "input_boolean", "B", None, {"type": "input_boolean/delete"}
    )
    assert message["type"] == "input_boolean/create"


async def test_one_field_passed_twice_with_different_values_is_rejected(
    capture_create: _Captured,
) -> None:
    mcp, _ = await _registered_tool()
    base = {"helper_type": "input_number", "name": "T", "action": "create"}
    for flat, folded in ({"step": 1}, {"step": 2}), ({"min_value": 1}, {"min": 2}):
        with pytest.raises(ToolError, match="both as a parameter and in config"):
            await _call(mcp, **base, **flat, config=folded)
    assert capture_create.args == []
    await _call(mcp, **base, min_value=1, config={"min": 1})  # agreeing is fine
    assert _fields(capture_create) == {"min": 1}


async def test_flow_helper_rejects_storage_params_passed_top_level() -> None:
    mcp, _ = await _registered_tool()
    with (
        patch.object(hc_typed, "fetch_helper_schemas", AsyncMock(return_value=None)),
        pytest.raises(ToolError, match="not applicable"),
    ):
        await _call(mcp, helper_type="derivative", name="D", min_value=1)


async def test_error_context_lists_cores_fields_when_served() -> None:
    token = hc_schemas._CORE_HELPER_SCHEMAS.set((_CORE_SCHEMAS, "create"))
    try:
        context = hc_schemas._simple_helper_error_context("input_number")
    finally:
        hc_schemas._CORE_HELPER_SCHEMAS.reset(token)
    assert context["data_schema"] == _CORE_SCHEMAS["input_number"]["create"]
    assert (
        hc_schemas._simple_helper_error_context("input_number")["data_schema"]
        == (hc_schemas.SIMPLE_HELPER_SCHEMAS["input_number"])
    )


async def test_create_routes_through_component() -> None:
    client = MagicMock()
    client.send_websocket_message = AsyncMock()
    write = AsyncMock(
        return_value={
            "success": True,
            "item": {"id": "target", "name": "Target", "min": 1.0, "max": 9.0},
            "entity_id": "input_number.target",
            "registry_applied": {"area_id": "kitchen"},
            "warnings": [],
        }
    )
    with patch.object(hc_create, "write_helper_item", write):
        result = await hc_create._execute_create_simple_helper(
            client, "input_number", "Target", None, "kitchen", None, None, True,
            False, {"min": 1.0, "max": 9.0},
        )  # fmt: skip
    client.send_websocket_message.assert_not_called()
    args, kwargs = write.call_args
    assert args[1:3] == ("input_number", "create")
    assert args[3] == {"name": "Target", "min": 1.0, "max": 9.0}
    assert kwargs["registry"] == {"area_id": "kitchen"}
    assert result["entity_id"] == "input_number.target"
    assert result["data"]["area_id"] == "kitchen"


_STORED_SCHEDULE = {
    "id": "s", "name": "S", "monday": [{"from": "07:00:00", "to": "08:00:00"}],
}  # fmt: skip
_TUESDAY = [{"from": "09:00", "to": "10:00"}]


async def test_schedule_update_keeps_unpassed_days_via_component() -> None:
    read = AsyncMock(
        return_value={"success": True, "item_id": "s", "item": _STORED_SCHEDULE}
    )
    write = AsyncMock(
        return_value={"success": True, "item": {}, "entity_id": "schedule.s",
                      "registry_applied": {}, "warnings": []}
    )  # fmt: skip
    with (
        patch.object(hc_update, "read_helper_item", read),
        patch.object(hc_update, "write_helper_item", write),
    ):
        await hc_update._execute_update_simple_helper(
            MagicMock(), "schedule", "schedule.s", "s", None, None, None, None,
            None, False, False, {"tuesday": _TUESDAY},
        )  # fmt: skip
    payload = write.call_args.args[3]
    assert payload == {"name": "S", "monday": _STORED_SCHEDULE["monday"],
                       "tuesday": _TUESDAY}  # fmt: skip


def _ws_by_type(replies: dict[str, Any]) -> MagicMock:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        side_effect=lambda m: {"success": True, "result": replies.get(m["type"], {})}
    )
    return client


def _sent(client: MagicMock, message_type: str) -> list[dict[str, Any]]:
    return [
        c.args[0]
        for c in client.send_websocket_message.call_args_list
        if c.args[0]["type"] == message_type
    ]


async def test_schedule_update_keeps_unpassed_days_via_websocket() -> None:
    client = _ws_by_type(
        {
            "schedule/list": [_STORED_SCHEDULE],
            "config/entity_registry/get": {"unique_id": "s"},
            "schedule/update": _STORED_SCHEDULE,
        }
    )
    with patch.object(hc_update, "read_helper_item", AsyncMock(return_value=None)):
        await hc_update._execute_update_simple_helper(
            client, "schedule", "schedule.s", "s", None, None, None, None,
            None, False, False, {"tuesday": _TUESDAY},
        )  # fmt: skip
    (update,) = _sent(client, "schedule/update")
    assert update["monday"] == _STORED_SCHEDULE["monday"]
    assert update["tuesday"] == _TUESDAY


async def test_websocket_update_of_a_person_reads_its_storage_items() -> None:
    """person/list nests the editable items under "storage"."""
    stored = {"id": "p", "name": "P", "user_id": None, "device_trackers": ["a.b"]}
    client = _ws_by_type(
        {
            "person/list": {"storage": [stored], "config": []},
            "config/entity_registry/get": {"unique_id": "p"},
            "person/update": stored,
        }
    )
    with patch.object(hc_update, "read_helper_item", AsyncMock(return_value=None)):
        await hc_update._execute_update_simple_helper(
            client, "person", "person.p", "p", "Pat", None, None, None, None,
            False, False, {},
        )  # fmt: skip
    (update,) = _sent(client, "person/update")
    assert update["device_trackers"] == ["a.b"]
    assert update["name"] == "Pat"


_STORED_ZONE = {
    "id": "z", "name": "Z", "icon": "mdi:school", "latitude": 1.0,
    "longitude": 2.0, "radius": 100.0, "passive": False,
}  # fmt: skip
_BARE_ZONE = {k: v for k, v in _STORED_ZONE.items() if k != "icon"}
_ZONE_PATHS = ["component", "websocket"]


def _zone_ws_client(stored: dict[str, Any], list_ok: bool = True) -> MagicMock:
    # Another zone with an icon comes first, so the lookup must match by id.
    listed = [{"id": "other", "icon": "mdi:map"}, stored]
    replies = {
        "zone/list": {"success": list_ok, "result": listed if list_ok else None},
        "config/entity_registry/get": {"success": True, "result": {"unique_id": "z"}},
        "zone/update": {"success": True, "result": stored},
    }
    client = MagicMock()
    # A copy per reply, as a real WS reply is: the update path writes into it.
    client.send_websocket_message = AsyncMock(
        side_effect=lambda m: deepcopy(
            replies.get(m["type"], {"success": True, "result": {}})
        )
    )
    return client


async def _zone_update(
    path: str, stored: dict[str, Any], icon: str | None
) -> dict[str, Any] | None:
    """Run a zone update; return the item fields it wrote, or None if none."""
    if path == "component":
        read = AsyncMock(return_value={"success": True, "item_id": "z", "item": stored})
        write = AsyncMock(
            return_value={"success": True, "item": stored, "entity_id": "zone.z",
                          "registry_applied": {}, "warnings": []}
        )  # fmt: skip
        with (
            patch.object(hc_update, "read_helper_item", read),
            patch.object(hc_update, "write_helper_item", write),
        ):
            await hc_update._execute_update_simple_helper(
                MagicMock(), "zone", "zone.z", "zone.z", None, icon, None, None,
                None, False, False, {"radius": 50.0},
            )  # fmt: skip
        return write.call_args.args[3] if write.called else None
    client = _zone_ws_client(stored)
    with patch.object(hc_update, "read_helper_item", AsyncMock(return_value=None)):
        await hc_update._execute_update_simple_helper(
            client, "zone", "zone.z", "zone.z", None, icon, None, None, None,
            False, False, {"radius": 50.0},
        )  # fmt: skip
    updates = _sent(client, "zone/update")
    return updates[0] if updates else None


@pytest.mark.parametrize("path", _ZONE_PATHS)
async def test_zone_icon_change_reaches_the_stored_icon(path: str) -> None:
    # The registry override alone would leave the stored icon to show again.
    written = await _zone_update(path, _STORED_ZONE, "mdi:home")
    assert written is not None and written["icon"] == "mdi:home"


@pytest.mark.parametrize("path", _ZONE_PATHS)
async def test_zone_icon_without_a_stored_one_stays_clearable(path: str) -> None:
    # Written into the item, it could never be removed again.
    written = await _zone_update(path, _BARE_ZONE, "mdi:home")
    assert written is not None and "icon" not in written
    cleared = await _zone_update(path, _BARE_ZONE, "")
    assert cleared is not None and "icon" not in cleared


@pytest.mark.parametrize("path", _ZONE_PATHS)
async def test_zone_update_without_icon_keeps_a_stored_icon(path: str) -> None:
    written = await _zone_update(path, _STORED_ZONE, None)
    assert written is not None
    # Left out, or the whole stored item written back with its icon.
    assert written.get("icon", _STORED_ZONE["icon"]) == _STORED_ZONE["icon"]
    assert written["radius"] == 50.0


async def test_zone_stored_icon_clear_is_refused_via_component() -> None:
    write = AsyncMock()
    read = AsyncMock(
        return_value={"success": True, "item_id": "z", "item": _STORED_ZONE}
    )
    with (
        patch.object(hc_update, "read_helper_item", read),
        patch.object(hc_update, "write_helper_item", write),
        pytest.raises(ToolError, match=r"stored icon \(mdi:school\)"),
    ):
        await hc_update._execute_update_simple_helper(
            MagicMock(), "zone", "zone.z", "zone.z", None, "", None, None, None,
            False, False, {},
        )  # fmt: skip
    write.assert_not_called()


@pytest.mark.parametrize(
    ("list_ok", "error"),
    [(True, r"stored icon \(mdi:school\)"), (False, "Failed to fetch zone config")],
)
async def test_zone_icon_clear_writes_nothing_via_websocket(
    list_ok: bool, error: str
) -> None:
    # Refused, or the stored icon unreadable: either way nothing may be written,
    # or the stored icon would show again under a reported success.
    client = _zone_ws_client(_STORED_ZONE, list_ok=list_ok)
    with (
        patch.object(hc_update, "read_helper_item", AsyncMock(return_value=None)),
        pytest.raises(ToolError, match=error),
    ):
        await hc_update._execute_update_simple_helper(
            client, "zone", "zone.z", "zone.z", None, "", None, None, None,
            False, False, {},
        )  # fmt: skip
    assert _sent(client, "zone/update") == []
    assert _sent(client, "config/entity_registry/update") == []


async def test_collection_payload_keeps_tag_id_on_create() -> None:
    create = {"type": "tag/create", "name": "T", "tag_id": "abc"}
    update = {"type": "tag/update", "tag_id": "abc", "name": "T2"}
    assert client_payload("tag", create) == {"name": "T", "tag_id": "abc"}
    assert client_payload("tag", update) == {"name": "T2"}


async def test_update_routes_through_component_with_merged_payload() -> None:
    client = MagicMock()
    client.send_websocket_message = AsyncMock()
    existing = {"id": "t", "name": "T", "min": 0.0, "max": 50.0, "step": 5.0}
    read = AsyncMock(return_value={"success": True, "item_id": "t", "item": existing})
    write = AsyncMock(
        return_value={"success": True, "item": {**existing, "max": 80.0},
                      "entity_id": "input_number.t", "registry_applied": {},
                      "warnings": []}
    )  # fmt: skip
    with (
        patch.object(hc_update, "read_helper_item", read),
        patch.object(hc_update, "write_helper_item", write),
    ):
        result = await hc_update._execute_update_simple_helper(
            client, "input_number", "input_number.t", "t", None, None, None, None,
            "cat1", True, False, {"max": 80.0},
        )  # fmt: skip
    client.send_websocket_message.assert_not_called()
    args, kwargs = write.call_args
    assert args[3] == {"name": "T", "min": 0.0, "max": 80.0, "step": 5.0}
    assert kwargs["item_id"] == "t"
    assert kwargs["registry"] == {"category": "cat1"}
    assert result["data"]["max"] == 80.0


async def test_tag_update_routes_through_component() -> None:
    client = MagicMock()
    client.send_websocket_message = AsyncMock()
    item = {"id": "abc", "description": "old"}
    read = AsyncMock(return_value={"success": True, "item_id": "abc", "item": item})
    write = AsyncMock(
        return_value={"success": True, "item": {**item, "name": "T2"},
                      "entity_id": "tag.t2", "registry_applied": {}, "warnings": []}
    )  # fmt: skip
    with (
        patch.object(hc_update, "read_helper_item", read),
        patch.object(hc_update, "write_helper_item", write),
    ):
        await hc_update._execute_update_simple_helper(
            client, "tag", "tag.abc", "abc", "T2", None, "kitchen", None, None,
            True, False, {},
        )  # fmt: skip
    assert read.call_args.kwargs == {"item_id": "abc"}
    args, kwargs = write.call_args
    assert args[3] == {"description": "old", "name": "T2"}
    assert kwargs["item_id"] == "abc"
    assert kwargs["registry"] == {"area_id": "kitchen"}


async def test_empty_category_clears_via_the_component() -> None:
    client = MagicMock()
    write = AsyncMock(
        return_value={"success": True, "item": {"id": "b"}, "entity_id": "input_boolean.b",
                      "registry_applied": {"category": None}, "warnings": []}
    )  # fmt: skip
    with patch.object(hc_create, "write_helper_item", write):
        await hc_create._execute_create_simple_helper(
            client, "input_boolean", "B", None, None, None, "", False, False, {},
        )  # fmt: skip
    assert write.call_args.kwargs["registry"] == {"category": ""}


async def test_blank_clears_and_quote_only_is_rejected(
    capture_create: _Captured,
) -> None:
    mcp, _ = await _registered_tool()
    base = {"helper_type": "input_boolean", "name": "B", "action": "create"}
    await _call(mcp, **base, area_id="  ", icon=" ", category="")
    # positional: client, helper_type, name, icon, area_id, labels, category
    (args,) = capture_create.args
    assert (args[3], args[4], args[6]) == ("", "", "")
    with pytest.raises(ToolError, match="only quote characters"):
        await _call(mcp, **base, category='""')
    await _call(mcp, **base, config={"icon": " "})  # a config icon clears too
    assert capture_create.args[1][3] == ""


async def test_empty_category_clears_the_scope() -> None:
    from ha_mcp.tools.config_write_helpers import apply_entity_category

    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": []}
    )
    result: dict[str, Any] = {}
    await apply_entity_category(client, "input_boolean.b", "", "helpers", result)
    sent = [call.args[0] for call in client.send_websocket_message.call_args_list]
    assert {"type": "config/entity_registry/update", "entity_id": "input_boolean.b",
            "categories": {"helpers": None}} in sent  # fmt: skip
    assert result == {"category": None}


_TAG_ENTITY = {"entity_id": "tag.front_door", "platform": "tag", "unique_id": "abc-1"}


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [("invalid_format", "VALIDATION_INVALID_PARAMETER"),
     ("home_assistant_error", "SERVICE_CALL_FAILED")],
)  # fmt: skip
async def test_websocket_create_failure_code(error_code: str, expected: str) -> None:
    client = _ws({"success": False, "error": "bad", "error_code": error_code})
    with (
        patch.object(hc_create, "write_helper_item", AsyncMock(return_value=None)),
        pytest.raises(ToolError, match=expected),
    ):
        await hc_create._execute_create_simple_helper(
            client, "input_boolean", "B", None, None, None, None, False, False, {},
        )  # fmt: skip


async def test_websocket_tag_create_reports_the_real_entity() -> None:
    client = _ws(
        {"success": True, "result": {"id": "abc-1", "name": "Front door"}},
        {"success": True, "result": [_TAG_ENTITY]},
    )
    with patch.object(hc_create, "write_helper_item", AsyncMock(return_value=None)):
        result = await hc_create._execute_create_simple_helper(
            client, "tag", "Front door", None, None, None, None, False, False,
            {"tag_id": "abc-1"},
        )  # fmt: skip
    assert result["entity_id"] == "tag.front_door"


_TAG_REPLIES = {
    "config/entity_registry/list": [_TAG_ENTITY],
    "tag/list": [{"id": "abc-1", "name": "Front door"}],
    "tag/update": {"id": "abc-1", "name": "Front"},
}


@pytest.mark.parametrize("helper_id", ["tag.front_door", "abc-1", "tag.abc-1"])
async def test_websocket_tag_update_resolves_the_tag_id(helper_id: str) -> None:
    client = _ws_by_type(_TAG_REPLIES)
    with patch.object(hc_update, "read_helper_item", AsyncMock(return_value=None)):
        result = await hc_update._execute_update_simple_helper(
            client, "tag", helper_id, helper_id, "Front", None, None, None, None,
            False, False, {},
        )  # fmt: skip
    assert _sent(client, "tag/update") == [
        {"type": "tag/update", "tag_id": "abc-1", "name": "Front"}
    ]
    assert result["entity_id"] == "tag.front_door"


async def test_websocket_tag_update_sends_an_icon_for_core_to_judge() -> None:
    """A tag has no icon field; Core says so rather than the icon vanishing."""
    client = _ws_by_type(_TAG_REPLIES)
    with patch.object(hc_update, "read_helper_item", AsyncMock(return_value=None)):
        await hc_update._execute_update_simple_helper(
            client, "tag", "tag.front_door", "tag.front_door", None, "mdi:nfc",
            None, None, None, False, False, {},
        )  # fmt: skip
    (update,) = _sent(client, "tag/update")
    assert update["icon"] == "mdi:nfc"


async def test_websocket_tag_update_applies_registry_fields() -> None:
    client = _ws_by_type(
        {**_TAG_REPLIES,
         "config/entity_registry/update": {"entity_entry": {"area_id": "kitchen"}}}
    )  # fmt: skip
    with patch.object(hc_update, "read_helper_item", AsyncMock(return_value=None)):
        await hc_update._execute_update_simple_helper(
            client, "tag", "tag.front_door", "tag.front_door", None, None,
            "kitchen", None, None, False, False, {},
        )  # fmt: skip
    assert any(
        m.get("entity_id") == "tag.front_door" and m.get("area_id") == "kitchen"
        for m in _sent(client, "config/entity_registry/update")
    )


async def test_component_tag_update_reports_the_component_entity() -> None:
    client = _ws({"success": True, "result": [_TAG_ENTITY]})
    read = AsyncMock(
        return_value={"success": True, "item_id": "abc-1", "item": {"id": "abc-1"}}
    )
    write = AsyncMock(
        return_value={"success": True, "item": {"id": "abc-1", "name": "F"},
                      "entity_id": "tag.front_door", "registry_applied": {},
                      "warnings": []}
    )  # fmt: skip
    with (
        patch.object(hc_update, "read_helper_item", read),
        patch.object(hc_update, "write_helper_item", write),
    ):
        result = await hc_update._execute_update_simple_helper(
            client, "tag", "tag.front_door", "tag.front_door", "F", None, None,
            None, None, False, False, {},
        )  # fmt: skip
    assert read.call_args.kwargs == {"item_id": "abc-1"}
    assert result["entity_id"] == "tag.front_door"


async def test_write_falls_back_to_websocket_when_component_unavailable() -> None:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": {"id": "b", "name": "B"}}
    )
    with patch.object(hc_create, "write_helper_item", AsyncMock(return_value=None)):
        result = await hc_create._execute_create_simple_helper(
            client, "input_boolean", "B", None, None, None, None, False, False, {},
        )  # fmt: skip
    client.send_websocket_message.assert_awaited_once()
    assert result["entity_id"] == "input_boolean.b"


class TestCatalogTransform:
    @pytest.fixture
    def available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        caps = ComponentCaps(1, "2.2.2", frozenset({"helper_schemas", "helper_item",
                                                    "helper_write"}), {})  # fmt: skip
        monkeypatch.setattr(
            "ha_mcp.transforms.component_helpers.get_component_caps",
            AsyncMock(return_value=caps),
        )
        monkeypatch.setattr(
            "ha_mcp.transforms.component_helpers.fetch_helper_schemas",
            AsyncMock(return_value=_CORE_SCHEMAS),
        )

    async def test_registration_advertises_core_fields_and_keeps_wait(
        self, available: None
    ) -> None:
        # The helper tools' registration installs the transform.
        _, tool = await _registered_tool()
        properties = tool.parameters["properties"]
        assert "wait" in properties  # flow helpers and fallback writes use it
        description = properties["config"]["description"]
        assert hc_schemas._SIMPLE_CONFIG_KEYS_DESCRIPTION not in description
        assert (
            "input_number: min (float, required), max (float, required), "
            "step (float, default 1), mode (box|slider)." in description
        )

    async def test_static_contract_without_component(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ha_mcp.transforms.component_helpers import ComponentHelperSchemaTransform

        monkeypatch.setattr(
            "ha_mcp.transforms.component_helpers.get_component_caps",
            AsyncMock(return_value=None),
        )
        _, tool = await _registered_tool()
        properties = tool.parameters["properties"]
        assert "wait" in properties
        assert (
            hc_schemas._SIMPLE_CONFIG_KEYS_DESCRIPTION
            in properties["config"]["description"]
        )
        rewritten = ComponentHelperSchemaTransform._rewrite(tool, _CORE_SCHEMAS)
        assert "wait" in rewritten.parameters["properties"]
        # A copy: the original keeps the static key list.
        assert (
            hc_schemas._SIMPLE_CONFIG_KEYS_DESCRIPTION
            in tool.parameters["properties"]["config"]["description"]
        )
