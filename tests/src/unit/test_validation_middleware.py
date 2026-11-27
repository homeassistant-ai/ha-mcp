"""Unit tests for ValidationErrorMiddleware."""

import json
import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.automation import config as automation_config
from homeassistant.components.script import config as script_config

from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.exceptions import HomeAssistantCommandTimeout
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.errors import DEFAULT_SUGGESTIONS, ErrorCode
from ha_mcp.tools.component_api import get_component_caps
from ha_mcp.tools.validation_middleware import ValidationErrorMiddleware
from ha_mcp.utils.usage_logger import get_recent_logs, shutdown_usage_logger

from ._component_frames import older_component


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
        # A str where a dict is required -> dict_type error.
        _Args.model_validate({"config": "{}"})

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

    from ha_mcp.tools.coercion import JSON_STRING_COERCION

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

    from ha_mcp.tools.coercion import JSON_STRING_COERCION

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

    from ha_mcp.tools.coercion import JSON_STRING_COERCION

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


async def test_shared_word_outranks_closer_spelling() -> None:
    """``force`` shares a whole word with ``force_reload``; the closer-spelled
    ``forced`` shares none, so the hint names ``force_reload``."""
    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool()
    async def ha_test_reload(forced: bool = False, force_reload: bool = False) -> dict:
        return {"ok": True}

    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_test_reload", {"force": True})

    msg = json.loads(str(exc_info.value))["error"]["message"]
    assert "`force`: unknown parameter, did you mean `force_reload`?" in msg


def _suggestions(body: dict) -> list[str]:
    """All suggestions of a rejection: `suggestions` holds every one once there
    are two or more, otherwise `suggestion` holds the only one."""
    error = body["error"]
    return error.get("suggestions") or [error["suggestion"]]


def _make_config_set_like_mcp(client: HomeAssistantClient) -> FastMCP:
    """Tools shaped like ha_config_set_automation/_script for the config-key hint,
    the middleware reading Core's keys through ``client``."""
    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware(lambda: client))

    @mcp.tool()
    async def ha_config_set_automation(
        config: dict | None = None,
        entity_id: str | None = None,
        python_transform: str | None = None,
    ) -> dict:
        return {"ok": True}

    @mcp.tool()
    async def ha_config_set_script(
        script_id: str,
        config: dict | None = None,
        python_transform: str | None = None,
    ) -> dict:
        return {"ok": True}

    return mcp


@pytest.mark.asyncio
async def test_misplaced_automation_config_key_says_it_belongs_in_config(
    component_core_client: HomeAssistantClient,
) -> None:
    """A config root key passed as a top-level argument says it belongs inside
    `config` instead of only listing valid parameters (issue #2649 section 1)."""
    mcp = _make_config_set_like_mcp(component_core_client)
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_config_set_automation", {"alias": "Goodnight"})

    body = json.loads(str(exc_info.value))
    msg = body["error"]["message"]
    assert "`alias`: unknown parameter" in msg
    assert "inside `config`" in msg
    assert msg.endswith("Valid parameters: config, entity_id, python_transform.")
    # The guidance is repeated in the suggestions, not only in the message.
    assert any(
        "Move `alias` inside the `config` argument" in s for s in _suggestions(body)
    )


async def test_a_singular_automation_key_core_renames_is_told_to_move_into_config(
    component_core_client: HomeAssistantClient,
) -> None:
    """Core still takes ``action`` for ``actions``; it is not a key of the
    config schema itself, only of the renames in front of it."""
    mcp = _make_config_set_like_mcp(component_core_client)
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_config_set_automation", {"action": []})

    (only,) = _suggestions(json.loads(str(exc_info.value)))
    assert "Move `action` inside the `config` argument" in only


@pytest.mark.asyncio
async def test_a_helper_field_passed_top_level_is_told_it_goes_in_config() -> None:
    """A helper's fields are Home Assistant's to name and the tool declares
    none of them, so an undeclared argument is told where such a field goes
    as well as that a stored tool list may be stale."""
    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool()
    async def ha_config_set_helper(
        helper_type: str, config: dict | None = None
    ) -> dict:
        return {"ok": True}

    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool(
            "ha_config_set_helper", {"helper_type": "input_number", "min_value": 0}
        )

    body = json.loads(str(exc_info.value))
    assert "`min_value`: unknown parameter" in body["error"]["message"]
    assert "inside `config`" in body["error"]["message"]
    assert any("stale" in s for s in _suggestions(body))


@pytest.mark.asyncio
async def test_misplaced_script_config_key_says_it_belongs_in_config(
    component_core_client: HomeAssistantClient,
) -> None:
    """Same as above for ha_config_set_script (issue #2649 section 1)."""
    mcp = _make_config_set_like_mcp(component_core_client)
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool(
            "ha_config_set_script",
            {"script_id": "blink", "sequence": [{"action": "x"}]},
        )

    body = json.loads(str(exc_info.value))
    msg = body["error"]["message"]
    assert "`sequence`: unknown parameter" in msg
    assert "inside `config`" in msg
    assert any(
        "Move `sequence` inside the `config` argument" in s for s in _suggestions(body)
    )


_TRACE_CALLS = {
    "script": ("ha_config_set_script", {"script_id": "blink", "trace": {}}),
    "automation": ("ha_config_set_automation", {"trace": {}}),
}


async def _trace_rejection(client: HomeAssistantClient, domain: str) -> dict:
    mcp = _make_config_set_like_mcp(client)
    tool, arguments = _TRACE_CALLS[domain]
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool(tool, arguments)
    return json.loads(str(exc_info.value))


@pytest.mark.parametrize("domain", ["script", "automation"])
async def test_a_config_field_core_takes_is_told_to_move_into_config(
    component_core_client: HomeAssistantClient, domain: str
) -> None:
    """``trace`` is a field of both of Core's config schemas; the component
    reads the keys from Core."""
    body = await _trace_rejection(component_core_client, domain)

    assert "Move `trace` inside the `config` argument" in _suggestions(body)[0]


def _assert_keys_unread(body: dict, domain: str, reason: str) -> None:
    message = body["error"]["message"]
    article = "an" if domain == "automation" else "a"
    assert f"if it is {article} {domain} config field, pass it inside `config`" in (
        message
    )
    note = _suggestions(body)[-1]
    assert reason in note
    assert f"Core's {domain} fields could not be read" in note
    assert not any("Move `trace`" in s for s in _suggestions(body))


@pytest.mark.parametrize("domain", ["script", "automation"])
async def test_without_the_component_the_hint_says_core_was_not_read(
    core_client: HomeAssistantClient, domain: str
) -> None:
    _assert_keys_unread(
        await _trace_rejection(core_client, domain),
        domain,
        "No ha_mcp_tools component answered",
    )


@pytest.mark.parametrize("domain", ["script", "automation"])
async def test_a_component_without_the_config_keys_is_answered_the_same(
    monkeypatch: pytest.MonkeyPatch,
    component_core_client: HomeAssistantClient,
    domain: str,
) -> None:
    older_component(monkeypatch, "config_keys")

    _assert_keys_unread(
        await _trace_rejection(component_core_client, domain),
        domain,
        "component is too old",
    )


@pytest.mark.parametrize(
    ("domain", "core_module", "schema"),
    [
        ("script", script_config, "SCRIPT_ENTITY_SCHEMA"),
        ("automation", automation_config, "PLATFORM_SCHEMA"),
    ],
)
async def test_a_component_whose_keys_command_fails_points_at_the_log(
    monkeypatch: pytest.MonkeyPatch,
    component_core_client: HomeAssistantClient,
    domain: str,
    core_module: object,
    schema: str,
) -> None:
    """A current component on a Core without the schema fails the command; it
    is neither absent nor too old."""
    monkeypatch.delattr(core_module, schema)

    _assert_keys_unread(
        await _trace_rejection(component_core_client, domain),
        domain,
        "component's answer failed",
    )


async def test_a_component_that_stops_answering_says_so(
    component_core_client: HomeAssistantClient,
) -> None:
    """The component answered its info probe, then its keys command got no
    answer: it is installed, so the note does not say to install it."""
    assert await get_component_caps(component_core_client) is not None
    with patch.object(
        HomeAssistantWebSocketClient,
        "send_command",
        AsyncMock(side_effect=HomeAssistantCommandTimeout("Command timeout")),
    ):
        body = await _trace_rejection(component_core_client, "script")

    _assert_keys_unread(body, "script", "component could not be asked")


@pytest.mark.asyncio
async def test_non_config_key_on_config_tool_still_gets_did_you_mean(
    component_core_client: HomeAssistantClient,
) -> None:
    """Unknown arguments that are NOT config keys keep the did-you-mean hint."""
    mcp = _make_config_set_like_mcp(component_core_client)
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_config_set_automation", {"configg": {"alias": "x"}})

    msg = json.loads(str(exc_info.value))["error"]["message"]
    assert "`configg`: unknown parameter, did you mean `config`?" in msg
    assert "inside `config`" not in msg


@pytest.mark.asyncio
async def test_renamed_parameter_names_a_stale_client_tool_list() -> None:
    """A parameter an update renamed, with no alias kept for the old name
    (`identifier` -> `entity_id` in 9.1.0), arrives from a client still
    serving its old tool list; the rejection names that cause and the
    reconnect that fixes it (#2968, #2978)."""
    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool()
    async def ha_config_get_automation(entity_id: str) -> dict:
        return {"ok": True}

    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool(
            "ha_config_get_automation", {"identifier": "automation.morning"}
        )

    stale = [s for s in _suggestions(json.loads(str(exc_info.value))) if "stale" in s]
    assert len(stale) == 1
    hint = stale[0]
    # Conditional: the agent may also have invented the name (#2877).
    assert hint.startswith("If your tool list shows `identifier`")
    assert "not a server bug" in hint
    # Each Claude client setup with its own refresh step (steps end at `;`).
    steps = (
        ("Claude Desktop custom connector", "disconnect and reconnect"),
        ("claude_desktop_config.json", "reopen Claude Desktop"),
        ("claude.ai", "tools list"),
        ("Claude Code", "/mcp"),
        ("ChatGPT", "Refresh tools"),
    )
    for setup, step in steps:
        assert re.search(rf"{re.escape(setup)}[^;]*{re.escape(step)}", hint)
    assert "new conversation" in hint


@pytest.mark.asyncio
async def test_misplaced_config_key_gets_no_stale_tool_list_hint(
    component_core_client: HomeAssistantClient,
) -> None:
    """A config key passed top-level is the agent's mistake, not an old tool
    list, so only the move-into-config suggestion applies."""
    mcp = _make_config_set_like_mcp(component_core_client)
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_config_set_automation", {"alias": "Goodnight"})

    (only,) = _suggestions(json.loads(str(exc_info.value)))
    assert "Move `alias` inside the `config` argument" in only


@pytest.mark.asyncio
async def test_type_error_alone_gets_no_stale_tool_list_hint() -> None:
    """A wrong type for a declared parameter has nothing to do with the tool
    list, so it keeps the generic suggestions."""
    mcp = _make_mcp()
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_test_dict_param", {"config": '{"a": 1}'})

    assert (
        _suggestions(json.loads(str(exc_info.value)))
        == DEFAULT_SUGGESTIONS[ErrorCode.VALIDATION_FAILED]
    )


@pytest.mark.asyncio
async def test_a_rejected_call_is_recorded_for_the_issue_report(monkeypatch) -> None:
    """A rejected call never reaches the tool, so its usage-log wrapper never
    runs. Without an entry here, a report filed from a client with a stale
    tool list shows no trace of the call or of the name the client sent."""
    recorded: list[dict] = []
    monkeypatch.setattr(
        "ha_mcp.tools.validation_middleware.log_tool_call",
        lambda **entry: recorded.append(entry),
    )
    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool()
    async def ha_config_get_automation(entity_id: str) -> dict:
        return {"ok": True}

    with pytest.raises(ToolError):
        await mcp.call_tool(
            "ha_config_get_automation", {"identifier": "automation.morning"}
        )

    (entry,) = recorded
    assert entry["tool_name"] == "ha_config_get_automation"
    assert entry["success"] is False
    assert entry["parameters"] == {"identifier": "automation.morning"}
    assert "`identifier`: unknown parameter" in entry["error_message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        '{"code": "4711-secret"}',
        '{"code": "4711-secret"',
        {"payload": '{"code": "4711-secret"}'},
    ],
    ids=["json_string", "malformed_json_string", "nested_json_string"],
)
async def test_a_rejected_call_does_not_log_a_secret_inside_a_json_string(
    data: str | dict,
) -> None:
    """A client that stringifies `data` sends an alarm code as raw text. An
    ordinary call reaches the usage log parsed, where masking by key name
    covers the code; a call rejected for another argument must not put it in
    the log that ha_report_issue returns."""
    # A fresh log: an entry left by another case must not stand in for this one.
    shutdown_usage_logger()
    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool()
    async def ha_test_rejected_secret(data: dict | None = None) -> dict:
        return {"ok": True}

    with pytest.raises(ToolError):
        await mcp.call_tool("ha_test_rejected_secret", {"data": data, "bogus": 1})

    (entry,) = get_recent_logs(50)
    assert entry["tool_name"] == "ha_test_rejected_secret"
    assert "4711-secret" not in json.dumps(entry)


@pytest.mark.asyncio
async def test_stale_tool_list_hint_names_only_the_undeclared_arguments(
    component_core_client: HomeAssistantClient,
) -> None:
    """In a call that also has a misplaced config key and a type error, one
    stale-list suggestion names the undeclared arguments and nothing else, and
    the move-into-config suggestion stays."""
    mcp = _make_config_set_like_mcp(component_core_client)
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool(
            "ha_config_set_automation",
            {
                "alias": "Goodnight",
                "config": '{"triggers": []}',
                "identifier": "automation.goodnight",
                "force": True,
            },
        )

    suggestions = _suggestions(json.loads(str(exc_info.value)))
    assert any("Move `alias` inside the `config` argument" in s for s in suggestions)
    stale = [s for s in suggestions if "stale" in s]
    assert len(stale) == 1
    assert "`identifier`" in stale[0] and "`force`" in stale[0]
    assert "`alias`" not in stale[0] and "`config`" not in stale[0]


async def test_trailing_declared_name_outranks_closer_spelling() -> None:
    """A prefixed name points at the parameter it ends with: the renamed
    screenshot argument ``dashboard_url_path`` means ``url_path``, not the
    closer-spelled legacy ``dashboard_path``."""
    mcp = FastMCP("test")
    mcp.add_middleware(ValidationErrorMiddleware())

    @mcp.tool()
    async def ha_test_screenshot(
        dashboard_path: str | None = None,
        url_path: str | None = None,
        view_path: str | None = None,
        width: int = 1000,
    ) -> dict:
        return {"ok": True}

    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("ha_test_screenshot", {"dashboard_url_path": "lovelace"})

    msg = json.loads(str(exc_info.value))["error"]["message"]
    assert "`dashboard_url_path`: unknown parameter, did you mean `url_path`?" in msg


def _unknown_argument_error():
    from pydantic import ValidationError as PydanticValidationError
    from pydantic import validate_call

    @validate_call
    def _target(entity_id: str | None = None) -> None:
        return None

    invented = {"entitiy_id": "light.x"}
    with pytest.raises(PydanticValidationError) as info:
        _target(**invented)
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


@pytest.mark.usefixtures("tool_search_default")
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
