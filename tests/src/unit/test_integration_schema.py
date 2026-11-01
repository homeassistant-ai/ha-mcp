"""ha_get_integration's options and subentry flow schemas (#2632).

A schema exists only inside an open flow: the tool opens the entry's options
(or subentry) flow, describes its form in the same compact shape the helper
describe uses (fields, Core's help text, current values), and aborts it. A
flow that opens with a menu lists its branches; ``menu_choice`` describes the
form behind one of them.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.redaction import REDACTED_SET
from ha_mcp.tools.integration_schema import (
    fetch_config_subentry_schema,
    fetch_options_schema,
)
from ha_mcp.tools.tools_integrations import IntegrationTools

_HOST = {
    "name": "host",
    "required": True,
    "selector": {"text": {}},
    "description": {"suggested_value": "10.0.0.2"},
}
_API_KEY = {
    "name": "api_key",
    "required": True,
    "selector": {"text": {"type": "password"}},
}
_MENU = {
    "type": "menu",
    "flow_id": "f1",
    "step_id": "init",
    "menu_options": ["basic", "advanced"],
}


def _client(first_step: dict[str, Any], strings: dict[str, str] | None = None):
    client = AsyncMock()
    client.start_options_flow.return_value = first_step
    client.start_config_subentry_flow.return_value = first_step
    client.send_websocket_message.return_value = {
        "success": True,
        "result": {"resources": strings or {}},
    }
    return client


def _form(step_id: str, *fields: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "type": "form",
        "flow_id": "f1",
        "step_id": step_id,
        "data_schema": list(fields),
        **extra,
    }


def _resp(domain: str = "demo") -> dict[str, Any]:
    return {"entry": {"entry_id": "e1", "domain": domain, "options": {}}}


@pytest.mark.asyncio
async def test_options_form_is_described_with_help_and_current_values() -> None:
    client = _client(
        _form("init", _HOST),
        {"component.demo.options.step.init.data_description.host": "Device IP."},
    )
    resp = _resp()

    await fetch_options_schema(client, "e1", resp)

    assert resp["options_schema"]["flow_type"] == "form"
    assert resp["options_schema"]["step_id"] == "init"
    assert resp["options_schema"]["fields"] == [
        {
            "name": "host",
            "required": True,
            "type": "text",
            "description": "Device IP.",
            "current": "10.0.0.2",
        }
    ]
    assert "data_schema" not in resp["options_schema"]
    client.abort_options_flow.assert_awaited_once_with("f1")


@pytest.mark.asyncio
async def test_a_password_field_stays_recognisable_as_secret() -> None:
    client = _client(_form("init", _API_KEY))
    resp = _resp()

    await fetch_options_schema(client, "e1", resp)

    (field,) = resp["options_schema"]["fields"]
    assert field["input_type"] == "password"


@pytest.mark.asyncio
async def test_menu_without_a_choice_lists_the_branches_and_points_at_menu_choice() -> (
    None
):
    client = _client(dict(_MENU))
    resp = _resp()

    await fetch_options_schema(client, "e1", resp)

    schema = resp["options_schema"]
    assert schema["flow_type"] == "menu"
    assert schema["menu_options"] == ["basic", "advanced"]
    assert "menu_choice" in schema["note"]
    client.submit_options_flow_step.assert_not_called()
    client.abort_options_flow.assert_awaited_once_with("f1")


@pytest.mark.asyncio
async def test_menu_choice_describes_the_form_behind_that_branch() -> None:
    client = _client(
        dict(_MENU),
        {"component.demo.options.step.advanced.data.host": "Host"},
    )
    client.submit_options_flow_step.return_value = _form(
        "advanced", _HOST, last_step=True
    )
    resp = _resp()

    await fetch_options_schema(client, "e1", resp, menu_choice="advanced")

    client.submit_options_flow_step.assert_awaited_once_with(
        "f1", {"next_step_id": "advanced"}
    )
    schema = resp["options_schema"]
    assert schema["flow_type"] == "form"
    assert schema["step_id"] == "advanced"
    assert schema["menu_choice"] == "advanced"
    assert [(f["name"], f["description"]) for f in schema["fields"]] == [
        ("host", "Host")
    ]
    client.abort_options_flow.assert_awaited_once_with("f1")


@pytest.mark.asyncio
async def test_a_menu_choice_core_does_not_offer_is_refused_with_its_options() -> None:
    client = _client(dict(_MENU))

    with pytest.raises(ToolError) as exc_info:
        await fetch_options_schema(client, "e1", _resp(), menu_choice="expert")

    body = json.loads(str(exc_info.value))
    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert body["menu_options"] == ["basic", "advanced"]
    client.submit_options_flow_step.assert_not_called()
    client.abort_options_flow.assert_awaited_once_with("f1")


@pytest.mark.asyncio
async def test_menu_choice_on_a_flow_that_opens_with_a_form_is_refused() -> None:
    client = _client(_form("init", _HOST))

    with pytest.raises(ToolError) as exc_info:
        await fetch_options_schema(client, "e1", _resp(), menu_choice="advanced")

    body = json.loads(str(exc_info.value))
    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    client.abort_options_flow.assert_awaited_once_with("f1")


@pytest.mark.asyncio
async def test_legacy_read_fills_options_from_the_first_form() -> None:
    client = _client(_form("init", _HOST))
    resp = _resp()

    await fetch_options_schema(client, "e1", resp)

    assert resp["entry"]["options"] == {"host": "10.0.0.2"}


@pytest.mark.asyncio
async def test_subentry_form_reads_help_under_its_subentry_type() -> None:
    client = _client(
        _form("set_options", _HOST),
        {
            "component.ollama.config_subentries.conversation.step.set_options."
            "data_description.host": "Ollama server."
        },
    )
    resp = _resp("ollama")

    await fetch_config_subentry_schema(
        client,
        "e1",
        resp,
        subentry_type="conversation",
        subentry_id=None,
        show_advanced_options=False,
    )

    schema = resp["subentry_schema"]
    assert schema["subentry_type"] == "conversation"
    assert schema["fields"][0]["description"] == "Ollama server."
    client.send_websocket_message.assert_awaited_once_with(
        {
            "type": "frontend/get_translations",
            "language": "en",
            "category": "config_subentries",
            "integration": ["ollama"],
        }
    )
    client.abort_config_subentry_flow.assert_awaited_once_with("f1")


@pytest.mark.asyncio
async def test_subentry_menu_choice_submits_to_the_subentry_flow() -> None:
    client = _client(dict(_MENU))
    client.submit_config_subentry_flow_step.return_value = _form("advanced", _HOST)
    resp = _resp("ollama")

    await fetch_config_subentry_schema(
        client,
        "e1",
        resp,
        subentry_type="conversation",
        subentry_id="sub1",
        show_advanced_options=False,
        menu_choice="advanced",
    )

    client.submit_config_subentry_flow_step.assert_awaited_once_with(
        "f1", {"next_step_id": "advanced"}
    )
    assert resp["subentry_schema"]["fields"][0]["name"] == "host"
    client.abort_config_subentry_flow.assert_awaited_once_with("f1")


@pytest.mark.asyncio
async def test_component_read_keeps_its_options_and_redacts_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ha_mcp.redaction.get_global_settings",
        lambda: SimpleNamespace(redact_secrets=True, redact_app_option_keys=""),
    )
    live_key = {**_API_KEY, "description": {"suggested_value": "sk-LIVE"}}
    client = _client(_form("init", _HOST, live_key))
    resp = _resp()
    resp["entry"]["options"] = {"host": "10.0.0.9", "api_key": "sk-LIVE"}

    await fetch_options_schema(client, "e1", resp, populate_options=False)

    assert resp["entry"]["options"] == {"host": "10.0.0.9", "api_key": REDACTED_SET}
    fields = {f["name"]: f for f in resp["options_schema"]["fields"]}
    assert fields["api_key"]["current"] == REDACTED_SET
    assert "sk-LIVE" not in json.dumps(resp)


def _tools() -> IntegrationTools:
    return IntegrationTools(AsyncMock())


async def _detail(tools: IntegrationTools, **flags: Any) -> dict[str, Any]:
    return await tools._get_entry_detail_response(
        "e1",
        include_schema=flags.get("include_schema", False),
        include_subentries=False,
        include_subentry_schema=flags.get("include_subentry_schema", False),
        subentry_type="conversation",
        subentry_id=None,
        show_advanced_options=False,
        menu_choice="advanced",
        include_diagnostics=False,
        include_knx_project=False,
        device_id=None,
        fields_list=None,
        truncate_bytes=None,
        diagnostics_data_path=None,
        data_offset_int=0,
        data_limit_int=None,
    )


@pytest.mark.asyncio
async def test_menu_choice_with_both_schemas_is_refused() -> None:
    tools = _tools()
    tools._get_single_entry = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ToolError) as exc_info:
        await _detail(tools, include_schema=True, include_subentry_schema=True)

    body = json.loads(str(exc_info.value))
    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    tools._get_single_entry.assert_not_called()


@pytest.mark.asyncio
async def test_menu_choice_without_a_schema_is_reported_as_ignored() -> None:
    tools = _tools()
    tools._get_single_entry = AsyncMock(return_value={"success": True})  # type: ignore[method-assign]

    resp = await _detail(tools)

    assert any("menu_choice" in w for w in resp["warnings"])
