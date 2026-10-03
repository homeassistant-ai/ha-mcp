"""ha_config_set_helper: SIMPLE-type fields in ``config``, Core-sourced schemas,
component-routed writes, and the component-aware catalog (issue #2479)."""

from __future__ import annotations

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
from ha_mcp.tools.config_helpers import validation as hc_validation
from ha_mcp.tools.helpers import HIDDEN_PARAM, hidden_param_names
from ha_mcp.tools.tools_config_helpers import register_config_helper_tools

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
        {"name": "pattern", "required": False, "type": "string"},
    ],
    "update": [{"name": "min", "required": True, "type": "float"}],
}


def _type_kw(**values: Any) -> dict[str, Any]:
    """Every typed field, as ha_config_set_helper passes them to the executors."""
    return {name: values.get(name) for name in _HIDDEN}


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
def capture_create():
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


async def test_flat_and_config_fields_reach_create_identically(capture_create) -> None:
    mcp, _ = await _registered_tool()
    base = {"helper_type": "input_number", "name": "Target", "action": "create"}
    await _call(mcp, **base, min_value=1, max_value="9", step=2)
    await _call(mcp, **base, config={"min": 1, "max": 9, "step": 2})
    flat, folded = capture_create.kwargs
    assert flat == folded
    assert (folded["min_value"], folded["max_value"], folded["step"]) == (1, 9, 2)


async def test_config_accepts_core_names_and_name(capture_create) -> None:
    mcp, _ = await _registered_tool()
    await _call(
        mcp,
        helper_type="counter",
        action="create",
        config={"name": "Laps", "minimum": 0, "maximum": "5", "icon": "mdi:run"},
    )
    (kwargs,) = capture_create.kwargs
    assert (kwargs["min_value"], kwargs["max_value"]) == (0, 5)


async def test_config_rejects_unknown_and_conflicting_keys(capture_create) -> None:
    mcp, _ = await _registered_tool()
    base = {"helper_type": "input_number", "name": "T", "action": "create"}
    with pytest.raises(ToolError, match="Extra inputs are not permitted"):
        await _call(mcp, **base, config={"minn": 1})
    with pytest.raises(ToolError, match="both as a parameter and in config"):
        await _call(mcp, **base, min_value=1, config={"min": 2})
    with pytest.raises(ToolError, match="not applicable"):
        await _call(mcp, **base, config={"latitude": 1.0})
    assert capture_create.kwargs == []


async def test_error_context_uses_core_fields_this_tool_accepts() -> None:
    token = hc_schemas._CORE_HELPER_SCHEMAS.set((_CORE_SCHEMAS, "create"))
    try:
        context = hc_schemas._simple_helper_error_context("input_number")
    finally:
        hc_schemas._CORE_HELPER_SCHEMAS.reset(token)
    names = [field["name"] for field in context["data_schema"]]
    assert names == ["name", "min", "max", "step", "mode", "icon"]  # no pattern
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
            False, **_type_kw(min_value=1.0, max_value=9.0),
        )  # fmt: skip
    client.send_websocket_message.assert_not_called()
    args, kwargs = write.call_args
    assert args[1:3] == ("input_number", "create")
    assert args[3] == {"name": "Target", "min": 1.0, "max": 9.0}
    assert kwargs["registry"] == {"area_id": "kitchen"}
    assert result["entity_id"] == "input_number.target"
    assert result["data"]["area_id"] == "kitchen"


async def test_schedule_update_keeps_unpassed_days() -> None:
    existing = {
        "id": "s",
        "name": "S",
        "monday": [{"from": "07:00:00", "to": "08:00:00"}],
    }
    message = hc_update._build_update_message(
        "schedule", "s", existing, None, None,
        **_type_kw(tuesday=[{"from": "09:00", "to": "10:00"}]),
    )  # fmt: skip
    assert message["name"] == "S"  # full-replace: name is required
    assert message["monday"] == existing["monday"]
    assert message["tuesday"] == [{"from": "09:00:00", "to": "10:00:00"}]
    assert message["sunday"] == []


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
            "cat1", True, False, **_type_kw(max_value=80.0),
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
            True, False, **_type_kw(),
        )  # fmt: skip
    assert read.call_args.kwargs == {"item_id": "abc"}
    args, kwargs = write.call_args
    assert args[3] == {"name": "T2"}
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
            client, "input_boolean", "B", None, None, None, "", False, False,
            **_type_kw(),
        )  # fmt: skip
    assert write.call_args.kwargs["registry"] == {"category": ""}


async def test_blank_clears_and_quote_only_is_rejected(capture_create) -> None:
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
    from ha_mcp.tools.util_helpers import apply_entity_category

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


async def test_input_text_unit_and_pattern() -> None:
    created = hc_create._create_fields_input_text(
        None, None, None, None, unit_of_measurement="u", pattern="[a-z]+"
    )
    assert created == {"unit_of_measurement": "u", "pattern": "[a-z]+"}
    existing = {"pattern": "[0-9]+", "unit_of_measurement": "u", "max": 5}
    kept = hc_update._update_fields_input_text(existing, None, None, None, None)
    assert (kept["pattern"], kept["unit_of_measurement"]) == ("[0-9]+", "u")


_TAG_ENTITY = {"entity_id": "tag.front_door", "platform": "tag", "unique_id": "abc-1"}


def _ws(*responses: dict[str, Any]) -> MagicMock:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(side_effect=list(responses))
    return client


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
            client, "input_boolean", "B", None, None, None, None, False, False,
            **_type_kw(),
        )  # fmt: skip


async def test_websocket_tag_create_reports_the_real_entity() -> None:
    client = _ws(
        {"success": True, "result": {"id": "abc-1", "name": "Front door"}},
        {"success": True, "result": [_TAG_ENTITY]},
    )
    with patch.object(hc_create, "write_helper_item", AsyncMock(return_value=None)):
        result = await hc_create._execute_create_simple_helper(
            client, "tag", "Front door", None, None, None, None, False, False,
            **_type_kw(tag_id="abc-1"),
        )  # fmt: skip
    assert result["entity_id"] == "tag.front_door"


@pytest.mark.parametrize("helper_id", ["tag.front_door", "abc-1", "tag.abc-1"])
async def test_websocket_tag_update_resolves_the_tag_id(helper_id: str) -> None:
    replies = {
        "config/entity_registry/list": [_TAG_ENTITY],
        "tag/update": {"id": "abc-1", "name": "Front"},
    }
    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        side_effect=lambda m: {"success": True, "result": replies.get(m["type"], {})}
    )
    with patch.object(hc_update, "read_helper_item", AsyncMock(return_value=None)):
        result = await hc_update._execute_update_simple_helper(
            client, "tag", helper_id, helper_id, "Front", None, None, None, None,
            False, False, **_type_kw(),
        )  # fmt: skip
    sent = [c.args[0] for c in client.send_websocket_message.call_args_list]
    assert {"type": "tag/update", "tag_id": "abc-1", "name": "Front"} in sent
    assert result["entity_id"] == "tag.front_door"


async def test_websocket_tag_update_applies_registry_fields() -> None:
    replies = {
        "config/entity_registry/list": [_TAG_ENTITY],
        "tag/update": {"id": "abc-1", "name": "Front"},
        "config/entity_registry/update": {"entity_entry": {"area_id": "kitchen"}},
    }
    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        side_effect=lambda m: {"success": True, "result": replies.get(m["type"], {})}
    )
    with patch.object(hc_update, "read_helper_item", AsyncMock(return_value=None)):
        await hc_update._execute_update_simple_helper(
            client, "tag", "tag.front_door", "tag.front_door", None, None,
            "kitchen", None, None, False, False, **_type_kw(),
        )  # fmt: skip
    sent = [c.args[0] for c in client.send_websocket_message.call_args_list]
    assert any(
        m["type"] == "config/entity_registry/update"
        and m.get("entity_id") == "tag.front_door"
        and m.get("area_id") == "kitchen"
        for m in sent
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
            None, None, False, False, **_type_kw(),
        )  # fmt: skip
    assert read.call_args.kwargs == {"item_id": "abc-1"}
    assert result["entity_id"] == "tag.front_door"


async def test_stale_stored_initial_names_its_source() -> None:
    existing = {"options": ["a", "b"], "initial": "b"}
    with pytest.raises(ToolError) as excinfo:
        hc_update._update_fields_input_select(
            existing, options=["a", "d"], initial=None
        )
    assert "the stored initial='b'" in str(excinfo.value)
    assert "Pass `initial` with the new options." in str(excinfo.value)
    assert hc_update._update_fields_input_select(
        existing, options=["a", "d"], initial="a"
    )


async def test_update_checks_the_merged_range() -> None:
    existing = {"min": 0.0, "max": 80.0, "step": 5.0}
    with pytest.raises(ToolError, match="cannot be greater than max_value"):
        hc_update._update_fields_input_number(
            existing, 90, None, None, None, None, None
        )
    with pytest.raises(ToolError, match="cannot be greater than max_value"):
        hc_update._update_fields_counter({"maximum": 3}, None, 9, None, None, None)
    with pytest.raises(ToolError, match="cannot be greater than max_value"):
        hc_update._update_fields_input_text({"max": 4}, 9, None, None, None)
    # Untouched bounds are not re-judged: a rename keeps a stored wide step.
    odd = {"min": 0.0, "max": 1.0, "step": 5.0}
    assert hc_update._update_fields_input_number(
        odd, None, None, None, None, None, None
    )


async def test_equal_bounds_allowed_only_for_input_text_length() -> None:
    hc_validation._validate_numeric_range(
        "input_text", 4, 4, None
    )  # exact length, as Core
    for numeric in ("input_number", "counter"):
        with pytest.raises(ToolError, match="must differ"):
            hc_validation._validate_numeric_range(numeric, 4, 4, None)


async def test_cleared_icon_is_left_out_of_the_item() -> None:
    existing = {"id": "b", "name": "B", "icon": "mdi:star"}
    kept = hc_update._build_standard_update_message(
        "input_boolean", "b", existing, None, None, **_type_kw()
    )
    cleared = hc_update._build_standard_update_message(
        "input_boolean", "b", existing, None, "", **_type_kw()
    )
    assert kept["icon"] == "mdi:star"
    assert "icon" not in cleared


async def test_write_falls_back_to_websocket_when_component_unavailable() -> None:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": {"id": "b", "name": "B"}}
    )
    with patch.object(hc_create, "write_helper_item", AsyncMock(return_value=None)):
        result = await hc_create._execute_create_simple_helper(
            client, "input_boolean", "B", None, None, None, None, False, False,
            **_type_kw(),
        )  # fmt: skip
    client.send_websocket_message.assert_awaited_once()
    assert result["entity_id"] == "input_boolean.b"


class TestCatalogTransform:
    @pytest.fixture
    def available(self, monkeypatch):
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

    async def test_registration_advertises_core_fields_and_drops_wait(
        self, available
    ) -> None:
        # The helper tools' registration installs the transform.
        _, tool = await _registered_tool()
        properties = tool.parameters["properties"]
        assert "wait" not in properties
        description = properties["config"]["description"]
        assert hc_schemas._SIMPLE_CONFIG_KEYS_DESCRIPTION not in description
        assert (
            "input_number: min (float, required), max (float, required), "
            "step (float, default 1), mode (box|slider)." in description
        )
        assert "pattern" not in description

    async def test_static_contract_without_component(self, monkeypatch):
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
        assert "wait" not in rewritten.parameters["properties"]
        assert "wait" in tool.parameters["properties"]  # a copy, original untouched
