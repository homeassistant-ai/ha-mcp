"""Unit tests for ValidationErrorMiddleware."""

import json
import logging
from types import SimpleNamespace

import pytest

from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.validation_middleware import (
    ValidationErrorMiddleware,
    _closest_parameter,
)


def _make_mcp() -> FastMCP:
    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool()
    async def ha_test_dict_param(config: dict) -> dict:
        return {"ok": True}

    @mcp.tool()
    async def ha_test_list_param(items: list) -> dict:
        return {"ok": True}

    @mcp.tool()
    async def ha_test_int_param(count: int) -> dict:
        return {"count": count}

    @mcp.tool()
    async def ha_test_two_params(config: dict, items: list) -> dict:
        return {"ok": True}

    return mcp


@pytest.mark.asyncio
async def test_wrapped_fastmcp_validation_error_is_structured():
    """fastmcp >= 3.4.3 wraps an arg-validation pydantic error in
    ``fastmcp.exceptions.ValidationError`` (chained via ``from e``); the
    middleware must recover the pydantic cause and still emit a structured
    ToolError. Version-independent: the wrapped error is synthesised, so this
    exercises the 3.4.3 code path even on older fastmcp.
    """
    from pydantic import BaseModel
    from pydantic import ValidationError as PydanticValidationError

    from ha_mcp._vendor.fastmcp.exceptions import (
        ValidationError as FastMCPValidationError,
    )

    class _Args(BaseModel):
        config: dict

    with pytest.raises(PydanticValidationError) as pyd_info:
        _Args(config="{}")  # str where a dict is required -> dict_type error

    wrapped = FastMCPValidationError(str(pyd_info.value))
    wrapped.__cause__ = pyd_info.value

    async def _raise_wrapped(_context):
        raise wrapped

    middleware = ValidationErrorMiddleware()
    with pytest.raises(ToolError) as exc_info:
        await middleware.on_call_tool(None, _raise_wrapped)

    msg = json.loads(str(exc_info.value))["error"]["message"]
    assert "config" in msg
    assert "JSON object" in msg


@pytest.mark.asyncio
async def test_string_for_dict_gives_actionable_message():
    """Passing a JSON string where a dict is expected raises a structured ToolError."""
    mcp = _make_mcp()
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_test_dict_param", {"config": '{"key": "value"}'})

    body = json.loads(str(exc_info.value))
    assert body["success"] is False
    msg = body["error"]["message"]
    assert "config" in msg
    assert "JSON object" in msg
    assert "Valid parameters" not in msg
    assert "valid_parameters" not in body


@pytest.mark.asyncio
async def test_string_for_list_gives_actionable_message():
    """Passing a JSON string where a list is expected names the array hint."""
    mcp = _make_mcp()
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_test_list_param", {"items": "[1, 2, 3]"})

    body = json.loads(str(exc_info.value))
    msg = body["error"]["message"]
    assert "items" in msg
    assert "JSON array" in msg


@pytest.mark.asyncio
async def test_malformed_json_container_preserves_decoder_location():
    """JSON-like strings retain the decoder detail through FastMCP middleware."""
    from typing import Annotated

    from ha_mcp.tools.util_helpers import JSON_STRING_COERCION

    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool()
    async def ha_test_coerced_dict(
        config: Annotated[dict, JSON_STRING_COERCION],
    ) -> dict:
        return {"ok": True}

    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_test_coerced_dict", {"config": '{"key": }'})

    msg = json.loads(str(exc_info.value))["error"]["message"]
    assert "config" in msg
    assert "Invalid JSON" in msg
    assert "line 1 column 9" in msg


@pytest.mark.asyncio
async def test_multiple_type_errors_are_joined():
    """Multiple bad params surface together, joined by ';'."""
    mcp = _make_mcp()
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool(
            "ha_test_two_params", {"config": '{"a": 1}', "items": "[1]"}
        )

    msg = json.loads(str(exc_info.value))["error"]["message"]
    assert "config" in msg
    assert "items" in msg
    assert ";" in msg


@pytest.mark.asyncio
async def test_scalar_string_is_coerced_not_wrapped():
    """Tripwire for the design assumption: non-strict Pydantic coerces a
    numeric string to int, so the middleware never sees an int error. If a
    FastMCP upgrade flips to strict validation this test fails loudly,
    signalling that int_type/bool_type hints would start mattering."""
    mcp = _make_mcp()
    result = await mcp.call_tool("ha_test_int_param", {"count": "42"})
    assert result.structured_content == {"count": 42}


@pytest.mark.asyncio
async def test_dict_passes_through():
    """Passing a proper dict round-trips through the middleware unchanged."""
    mcp = _make_mcp()
    result = await mcp.call_tool("ha_test_dict_param", {"config": {"key": "value"}})
    assert result.structured_content == {"ok": True}


@pytest.mark.asyncio
async def test_error_is_structured():
    """Error follows ha-mcp's structured format with success/error fields."""
    mcp = _make_mcp()
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_test_dict_param", {"config": "not a dict"})

    body = json.loads(str(exc_info.value))
    assert body["success"] is False
    assert "code" in body["error"]
    assert "message" in body["error"]


@pytest.mark.asyncio
async def test_union_param_malformed_container_names_param_not_union_tags():
    """A wrong-container value on a `str | list[str]` param yields ONE message
    keyed by the real param name, not pydantic union-arm tags (`str`/`list[str]`).

    Regression for #1601: extending JSON_STRING_COERCION to ~28 union params
    widened the surface where a malformed container (e.g. a JSON-object string)
    fails every union arm and pydantic emits one error per arm. Without grouping,
    the user saw `entity_id.str` / `entity_id.list[str]` instead of `entity_id`.
    """
    from typing import Annotated

    from ha_mcp.tools.util_helpers import JSON_STRING_COERCION

    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool()
    async def ha_test_union_param(
        entity_id: Annotated[str | list[str], JSON_STRING_COERCION],
    ) -> dict:
        return {"ok": True}

    with pytest.raises(ToolError) as exc_info:
        # A JSON-object string coerces to a dict, which fails both the str and
        # the list[str] arm of the union.
        await mcp.call_tool("ha_test_union_param", {"entity_id": '{"a": 1}'})

    msg = json.loads(str(exc_info.value))["error"]["message"]
    assert "entity_id" in msg
    # No leaked pydantic union-arm tags, and a single line for the single param.
    assert "list[str]" not in msg
    assert "`str`" not in msg
    assert ";" not in msg


@pytest.mark.asyncio
async def test_list_element_error_keeps_index_in_message():
    """A bad ELEMENT in a list[dict] param keeps its index (`monday.1`), not
    just the param name. Grouping must collapse union-arm tags WITHOUT dropping
    real path elements like list indices (regression for the #1601 grouping)."""
    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool()
    async def ha_test_schedule(monday: list[dict]) -> dict:
        return {"ok": True}

    with pytest.raises(ToolError) as exc_info:
        # element 1 is a string, not a dict object
        await mcp.call_tool("ha_test_schedule", {"monday": [{"from": "07:00"}, "oops"]})

    msg = json.loads(str(exc_info.value))["error"]["message"]
    assert "monday.1" in msg


@pytest.mark.asyncio
async def test_union_error_details_are_deduped():
    """`details` collapses duplicate error types from union arms (#1601)."""
    from typing import Annotated

    from ha_mcp.tools.util_helpers import JSON_STRING_COERCION

    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool()
    async def ha_test_union_details(
        entity_id: Annotated[str | list[str], JSON_STRING_COERCION],
    ) -> dict:
        return {"ok": True}

    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_test_union_details", {"entity_id": '{"a": 1}'})

    details = json.loads(str(exc_info.value))["error"].get("details", "")
    # both arms fail; each distinct type appears at most once
    assert details.count("string_type") <= 1
    assert details.count("list_type") <= 1


@pytest.mark.asyncio
async def test_tool_error_from_tool_body_passes_through_unconverted():
    """A ToolError raised inside the tool is NOT re-wrapped by the middleware."""
    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool()
    async def ha_test_raises_tool_error(entity_id: str) -> dict:
        raise ToolError('{"success": false, "error": {"code": "ENTITY_NOT_FOUND"}}')

    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_test_raises_tool_error", {"entity_id": "light.x"})

    body = json.loads(str(exc_info.value))
    assert body["error"]["code"] == "ENTITY_NOT_FOUND"


def _make_dashboard_like_mcp() -> FastMCP:
    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool()
    async def ha_test_get_dashboard(
        url_path: str | None = None,
        list_only: bool = False,
        force_reload: bool = False,
        entity_id: str | None = None,
    ) -> dict:
        return {"ok": True}

    return mcp


@pytest.mark.asyncio
async def test_unknown_parameter_names_closest_valid_parameter():
    """Invented argument names get a did-you-mean hint and the valid parameter
    list instead of pydantic's bare "Unexpected keyword argument" (#2462)."""
    mcp = _make_dashboard_like_mcp()
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool(
            "ha_test_get_dashboard", {"dashboard_url": "my-dashboard", "force": True}
        )

    msg = json.loads(str(exc_info.value))["error"]["message"]
    assert "`dashboard_url`: unknown parameter, did you mean `url_path`?" in msg
    assert "`force`: unknown parameter, did you mean `force_reload`?" in msg
    assert msg.endswith(
        "Valid parameters: url_path, list_only, force_reload, entity_id."
    )


@pytest.mark.asyncio
async def test_unknown_parameter_typo_suggests_by_similarity():
    """A plain misspelling with no whole shared word still gets its match."""
    mcp = _make_dashboard_like_mcp()
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_test_get_dashboard", {"entitiy_id": "light.x"})

    msg = json.loads(str(exc_info.value))["error"]["message"]
    assert "`entitiy_id`: unknown parameter, did you mean `entity_id`?" in msg


@pytest.mark.asyncio
async def test_unknown_parameter_without_a_close_match_still_lists_valid_names():
    """No guess is offered when nothing resembles the name."""
    mcp = _make_dashboard_like_mcp()
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_test_get_dashboard", {"zzz": 1})

    body = json.loads(str(exc_info.value))
    msg = body["error"]["message"]
    assert msg == (
        "`zzz`: unknown parameter. "
        "Valid parameters: url_path, list_only, force_reload, entity_id."
    )
    assert body["valid_parameters"] == [
        "url_path",
        "list_only",
        "force_reload",
        "entity_id",
    ]


@pytest.mark.asyncio
async def test_supplied_parameter_is_never_suggested():
    """A parameter the call already supplied is not offered for a second argument."""
    mcp = _make_dashboard_like_mcp()
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool(
            "ha_test_get_dashboard",
            {"url_path": "my-dashboard", "dashboard_url": "my-dashboard"},
        )

    msg = json.loads(str(exc_info.value))["error"]["message"]
    assert "`dashboard_url`: unknown parameter." in msg
    assert "did you mean" not in msg


@pytest.mark.asyncio
async def test_type_error_and_unknown_parameter_in_one_call():
    """Both hint kinds share one message without a doubled sentence terminator."""
    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool()
    async def ha_test_config_tool(config: dict, entity_id: str | None = None) -> dict:
        return {"ok": True}

    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool(
            "ha_test_config_tool", {"config": '{"a": 1}', "entitiy_id": "light.x"}
        )

    msg = json.loads(str(exc_info.value))["error"]["message"]
    assert "`config`: expected a JSON object." in msg
    assert "`entitiy_id`: unknown parameter, did you mean `entity_id`?" in msg
    assert "Valid parameters: config, entity_id." in msg
    assert ".." not in msg


@pytest.mark.asyncio
async def test_unknown_parameter_on_a_tool_without_parameters():
    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool()
    async def ha_test_no_params() -> dict:
        return {"ok": True}

    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_test_no_params", {"entity_id": "light.x"})

    msg = json.loads(str(exc_info.value))["error"]["message"]
    assert msg.endswith("Valid parameters: none.")


def test_shared_word_outranks_closer_spelling():
    assert _closest_parameter("force", ["forced", "force_reload"]) == "force_reload"


def test_trailing_declared_name_outranks_closer_spelling():
    """A prefixed name points at the parameter it ends with: the renamed
    screenshot argument ``dashboard_url_path`` means ``url_path``, not the
    closer-spelled legacy ``dashboard_path``."""
    screenshot_parameters = ["dashboard_path", "url_path", "view_path", "width"]
    assert _closest_parameter("dashboard_url_path", screenshot_parameters) == "url_path"


def _unknown_argument_error():
    from pydantic import ValidationError as PydanticValidationError
    from pydantic import validate_call

    @validate_call
    def _target(entity_id: str | None = None) -> None:
        return None

    with pytest.raises(PydanticValidationError) as info:
        _target(entitiy_id="light.x")
    return info.value


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup", ["raises", "returns_none", "no_properties"])
async def test_unknown_parameter_falls_back_when_schema_lookup_fails(lookup, caplog):
    """A failed schema lookup keeps pydantic's wording, adds no parameter list,
    and logs a warning instead of breaking the structured error."""

    async def get_tool(_name):
        if lookup == "raises":
            raise RuntimeError("lookup failed")
        return SimpleNamespace(parameters={}) if lookup == "no_properties" else None

    context = SimpleNamespace(
        fastmcp_context=SimpleNamespace(fastmcp=SimpleNamespace(get_tool=get_tool)),
        message=SimpleNamespace(name="ha_test_tool", arguments={"entitiy_id": "x"}),
    )
    error = _unknown_argument_error()

    async def _raise(_context):
        raise error

    with (
        caplog.at_level(logging.WARNING, logger="ha_mcp.tools.validation_middleware"),
        pytest.raises(ToolError) as exc_info,
    ):
        await ValidationErrorMiddleware().on_call_tool(context, _raise)

    body = json.loads(str(exc_info.value))
    assert body["success"] is False
    msg = body["error"]["message"]
    assert "`entitiy_id`: Unexpected keyword argument" in msg
    assert "Valid parameters" not in msg
    assert "unknown-argument hint omitted" in caplog.text


@pytest.mark.asyncio
async def test_unknown_parameter_hint_through_the_search_proxy():
    """Through ha_call_read_tool the hint names the inner tool's parameters."""
    from ha_mcp._vendor.fastmcp import Client
    from ha_mcp.transforms.categorized_search import CategorizedSearchTransform

    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool(annotations={"readOnlyHint": True})
    async def ha_test_get_dashboard(
        url_path: str | None = None, force_reload: bool = False
    ) -> dict:
        return {"ok": True}

    mcp.add_transform(CategorizedSearchTransform())

    async with Client(mcp) as client:
        result = await client.call_tool(
            "ha_call_read_tool",
            {
                "name": "ha_test_get_dashboard",
                "arguments": {"dashboard_url": "my-dashboard"},
            },
            raise_on_error=False,
        )
        supplied = await client.call_tool(
            "ha_call_read_tool",
            {
                "name": "ha_test_get_dashboard",
                "arguments": {"url_path": "my-dashboard", "dashboard_url": "x"},
            },
            raise_on_error=False,
        )

    assert result.is_error
    text = result.content[0].text
    assert "`dashboard_url`: unknown parameter, did you mean `url_path`?" in text
    assert "Valid parameters: url_path, force_reload." in text
    assert supplied.is_error
    assert "did you mean" not in supplied.content[0].text
