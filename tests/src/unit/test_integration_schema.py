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
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.redaction import REDACTED_SET
from ha_mcp.tools.integration_schema import (
    fetch_config_flow_schema,
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


@pytest.mark.asyncio
async def test_options_flow_that_aborts_at_once_returns_cores_reason_text() -> None:
    client = _client(
        {
            "type": "abort",
            "flow_id": "f1",
            "reason": "not_supported",
            "description_placeholders": {"model": "X1"},
        },
        {"component.demo.options.abort.not_supported": "{model} has no options."},
    )
    resp = _resp()

    await fetch_options_schema(client, "e1", resp)

    schema = resp["options_schema"]
    assert schema["flow_type"] == "abort"
    assert schema["aborted"] == "not_supported"
    assert schema["reason_text"] == "X1 has no options."
    assert resp["entry"]["options"] == {}
    client.send_websocket_message.assert_awaited_once_with(
        {
            "type": "frontend/get_translations",
            "language": "en",
            "category": "options",
            "integration": ["demo"],
        }
    )


@pytest.mark.asyncio
async def test_options_abort_reads_the_reason_under_its_translation_domain() -> None:
    client = _client(
        {
            "type": "abort",
            "flow_id": "f1",
            "reason": "reauth_required",
            "translation_domain": "other",
        },
        {"component.other.options.abort.reauth_required": "Sign in again."},
    )
    resp = _resp()

    await fetch_options_schema(client, "e1", resp)

    assert resp["options_schema"]["reason_text"] == "Sign in again."


@pytest.mark.asyncio
async def test_subentry_flow_that_aborts_at_once_returns_cores_reason_text() -> None:
    client = _client(
        {"type": "abort", "flow_id": "f1", "reason": "no_models"},
        {"component.ollama.config_subentries.conversation.abort.no_models": "None."},
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
    assert schema["aborted"] == "no_models"
    assert schema["reason_text"] == "None."


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


# --- the setup (config) flow of a domain, read in list mode -----------------


def _config_client(first_step: dict[str, Any], strings: dict[str, str] | None = None):
    client = _client(first_step, strings)
    client.start_config_flow.return_value = first_step
    return client


@pytest.mark.asyncio
async def test_setup_form_is_described_with_help_under_config() -> None:
    client = _config_client(
        _form("user", _HOST, last_step=False),
        {"component.demo.config.step.user.data_description.host": "Device IP."},
    )
    resp: dict[str, Any] = {}

    await fetch_config_flow_schema(client, "demo", resp)

    schema = resp["config_flow_schema"]
    assert schema["flow_type"] == "form"
    assert schema["fields"][0]["description"] == "Device IP."
    assert schema["more_forms"] == "yes"
    client.start_config_flow.assert_awaited_once_with("demo")
    client.abort_config_flow.assert_awaited_once_with("f1")


@pytest.mark.asyncio
async def test_setup_flow_that_aborts_returns_cores_reason_text() -> None:
    client = _config_client(
        {
            "type": "abort",
            "flow_id": "f1",
            "reason": "single_instance_allowed",
            "description_placeholders": None,
        },
        {
            "component.sun.config.abort.single_instance_allowed": (
                "Already configured. Only a single configuration possible."
            )
        },
    )
    resp: dict[str, Any] = {}

    await fetch_config_flow_schema(client, "sun", resp)

    schema = resp["config_flow_schema"]
    assert schema["aborted"] == "single_instance_allowed"
    assert schema["reason_text"] == (
        "Already configured. Only a single configuration possible."
    )
    assert "fields" not in schema


@pytest.mark.asyncio
async def test_menu_choice_on_an_aborted_setup_flow_still_returns_the_reason() -> None:
    client = _config_client(
        {"type": "abort", "flow_id": "f1", "reason": "single_instance_allowed"}
    )
    resp: dict[str, Any] = {}

    await fetch_config_flow_schema(client, "sun", resp, menu_choice="light")

    assert resp["config_flow_schema"]["aborted"] == "single_instance_allowed"


@pytest.mark.asyncio
async def test_setup_menu_lists_branches_until_menu_choice_picks_one() -> None:
    client = _config_client(dict(_MENU))

    resp: dict[str, Any] = {}
    await fetch_config_flow_schema(client, "group", resp)
    assert resp["config_flow_schema"]["menu_options"] == ["basic", "advanced"]
    client.submit_config_flow_step.assert_not_called()

    client.submit_config_flow_step.return_value = _form("advanced", _HOST)
    resp = {}
    await fetch_config_flow_schema(client, "group", resp, menu_choice="advanced")

    client.submit_config_flow_step.assert_awaited_once_with(
        "f1", {"next_step_id": "advanced"}
    )
    assert resp["config_flow_schema"]["menu_choice"] == "advanced"
    assert resp["config_flow_schema"]["fields"][0]["name"] == "host"


# --- a describe step that does not stop at a form, menu or abort -----------


def _error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc_info.value))


@pytest.mark.asyncio
async def test_setup_flow_that_creates_an_entry_names_it_even_with_menu_choice() -> (
    None
):
    client = _config_client(
        {
            "type": "create_entry",
            "flow_id": "f1",
            "result": {"entry_id": "new1", "domain": "demo", "title": "Sun"},
        }
    )

    with pytest.raises(ToolError) as exc_info:
        await fetch_config_flow_schema(client, "demo", {}, menu_choice="x")

    assert _error(exc_info)["entry_id"] == "new1"


@pytest.mark.asyncio
async def test_setup_flow_that_creates_an_entry_fails_and_names_the_entry() -> None:
    client = _config_client(
        {
            "type": "create_entry",
            "flow_id": "f1",
            "title": "Sun",
            "result": {"entry_id": "new1", "domain": "demo", "title": "Sun"},
        }
    )
    resp: dict[str, Any] = {}

    with pytest.raises(ToolError) as exc_info:
        await fetch_config_flow_schema(client, "demo", resp)

    body = _error(exc_info)
    assert "new1" in body["error"]["message"]
    assert body["entry_id"] == "new1"
    assert body["title"] == "Sun"
    assert "status" not in body
    assert "ha_remove_helpers_integrations" in body["error"]["suggestion"]
    assert "config_flow_schema" not in resp


@pytest.mark.asyncio
async def test_menu_choice_whose_step_creates_an_entry_fails() -> None:
    client = _config_client(dict(_MENU))
    client.submit_config_flow_step.return_value = {
        "type": "create_entry",
        "flow_id": "f1",
        "title": "Basic",
        "result": {"entry_id": "new2", "title": "Basic"},
    }

    with pytest.raises(ToolError) as exc_info:
        await fetch_config_flow_schema(client, "group", {}, menu_choice="basic")

    assert _error(exc_info)["entry_id"] == "new2"


@pytest.mark.asyncio
async def test_options_flow_that_creates_an_entry_fails_and_says_options_saved() -> (
    None
):
    client = _client({"type": "create_entry", "flow_id": "f1", "title": ""})
    resp = _resp()

    with pytest.raises(ToolError) as exc_info:
        await fetch_options_schema(client, "e1", resp)

    assert "options were saved" in _error(exc_info)["error"]["message"]
    assert "options_schema" not in resp


@pytest.mark.asyncio
async def test_subentry_flow_that_creates_an_entry_fails_and_names_the_subentry() -> (
    None
):
    client = _client({"type": "create_entry", "flow_id": "f1", "title": "Chat"})

    with pytest.raises(ToolError) as exc_info:
        await fetch_config_subentry_schema(
            client,
            "e1",
            _resp("ollama"),
            subentry_type="conversation",
            subentry_id=None,
            show_advanced_options=False,
        )

    body = _error(exc_info)
    assert "subentry" in body["error"]["message"]
    assert body["title"] == "Chat"


_PROGRESS_STEP = {
    "type": "progress",
    "flow_id": "f1",
    "step_id": "discover",
    "progress_action": "scanning",
}


@pytest.mark.asyncio
async def test_setup_flow_in_progress_is_aborted_and_named_with_cores_text() -> None:
    client = _config_client(
        dict(_PROGRESS_STEP),
        {"component.demo.config.progress.scanning": "Scanning the network."},
    )

    with pytest.raises(ToolError) as exc_info:
        await fetch_config_flow_schema(client, "demo", {})

    body = _error(exc_info)
    assert "progress" in body["error"]["message"]
    assert body["progress_action"] == "scanning"
    assert body["progress_action_text"] == "Scanning the network."
    client.abort_config_flow.assert_awaited_once_with("f1")


@pytest.mark.asyncio
async def test_options_flow_in_progress_is_aborted_and_reported() -> None:
    client = _client(dict(_PROGRESS_STEP))
    resp = _resp()

    with pytest.raises(ToolError) as exc_info:
        await fetch_options_schema(client, "e1", resp)

    assert _error(exc_info)["progress_action"] == "scanning"
    client.abort_options_flow.assert_awaited_once_with("f1")


@pytest.mark.asyncio
async def test_subentry_flow_at_an_external_step_is_aborted_and_named() -> None:
    client = _client({"type": "external", "flow_id": "f1", "step_id": "auth"})

    with pytest.raises(ToolError) as exc_info:
        await fetch_config_subentry_schema(
            client,
            "e1",
            _resp("ollama"),
            subentry_type="conversation",
            subentry_id=None,
            show_advanced_options=False,
        )

    assert "'external'" in _error(exc_info)["error"]["message"]
    client.abort_config_subentry_flow.assert_awaited_once_with("f1")


@pytest.mark.asyncio
async def test_unknown_domain_is_a_warning_not_a_failure() -> None:
    client = _config_client({})
    client.start_config_flow.side_effect = HomeAssistantAPIError(
        "API error: 404 - Invalid handler specified",
        status_code=404,
        response_data={"message": "Invalid handler specified"},
    )
    resp: dict[str, Any] = {"entries": []}

    await fetch_config_flow_schema(client, "no_such_domain", resp)

    assert "config_flow_schema" not in resp
    assert any(
        "no_such_domain" in w and "Invalid handler specified" in w
        for w in resp["warnings"]
    )


@pytest.mark.asyncio
async def test_setup_flow_is_aborted_when_the_describe_is_refused() -> None:
    client = _config_client(_form("user", _HOST))

    with pytest.raises(ToolError):
        await fetch_config_flow_schema(client, "demo", {}, menu_choice="advanced")

    client.abort_config_flow.assert_awaited_once_with("f1")


@pytest.mark.asyncio
async def test_list_mode_include_schema_describes_the_domains_setup_flow() -> None:
    client = _config_client(_form("user", _HOST))
    tools = IntegrationTools(client)
    tools._list_entries = AsyncMock(return_value={"success": True, "entries": []})  # type: ignore[method-assign]

    resp = await tools.ha_get_integration(domain="Demo ", include_schema=True)

    client.start_config_flow.assert_awaited_once_with("demo")
    assert resp["config_flow_schema"]["fields"][0]["name"] == "host"


@pytest.mark.asyncio
async def test_list_mode_include_schema_without_domain_is_reported_as_ignored() -> None:
    client = _config_client(_form("user", _HOST))
    tools = IntegrationTools(client)
    tools._list_entries = AsyncMock(return_value={"success": True, "entries": []})  # type: ignore[method-assign]

    resp = await tools.ha_get_integration(include_schema=True)

    client.start_config_flow.assert_not_called()
    assert any("include_schema" in w for w in resp["warnings"])


# --- strings Home Assistant could not send are a warning, a missing one not --


@pytest.mark.asyncio
async def test_a_failed_strings_read_is_a_warning_on_the_described_form() -> None:
    client = _client(_form("init", _HOST))
    client.send_websocket_message.side_effect = RuntimeError("ws down")
    resp = _resp()

    await fetch_options_schema(client, "e1", resp)

    assert resp["options_schema"]["fields"][0]["name"] == "host"
    assert any("could not be read" in w for w in resp["options_schema"]["warnings"])


@pytest.mark.asyncio
async def test_a_failed_strings_read_is_a_warning_on_an_abort() -> None:
    client = _client({"type": "abort", "flow_id": "f1", "reason": "no_options"})
    client.send_websocket_message.side_effect = RuntimeError("ws down")
    resp = _resp()

    await fetch_options_schema(client, "e1", resp)

    schema = resp["options_schema"]
    assert schema["aborted"] == "no_options"
    assert any("could not be read" in w for w in schema["warnings"])


@pytest.mark.asyncio
async def test_a_string_core_does_not_ship_is_no_warning() -> None:
    client = _client({"type": "abort", "flow_id": "f1", "reason": "no_options"})
    resp = _resp()

    await fetch_options_schema(client, "e1", resp)

    assert "warnings" not in resp["options_schema"]
    assert "reason_text" not in resp["options_schema"]


@pytest.mark.asyncio
async def test_a_failed_abort_of_the_setup_describe_is_logged_as_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _config_client(_form("user", _HOST))
    client.abort_config_flow.side_effect = HomeAssistantAPIError(
        "API error: 500", status_code=500
    )

    with caplog.at_level("DEBUG", logger="ha_mcp.tools.integration_schema"):
        await fetch_config_flow_schema(client, "demo", {})

    assert [r.levelname for r in caplog.records if "f1" in r.getMessage()] == [
        "WARNING"
    ]


@pytest.mark.asyncio
async def test_an_entry_without_a_domain_reads_the_reason_under_its_entry() -> None:
    client = _client(
        {"type": "abort", "flow_id": "f1", "reason": "no_models"},
        {"component.ollama.config_subentries.conversation.abort.no_models": "None."},
    )
    client.get_config_entry.return_value = {"entry_id": "e1", "domain": "ollama"}
    resp = {"entry": {"entry_id": "e1"}}

    await fetch_config_subentry_schema(
        client,
        "e1",
        resp,
        subentry_type="conversation",
        subentry_id=None,
        show_advanced_options=False,
    )

    assert resp["subentry_schema"]["reason_text"] == "None."
    sent = [c.args[0] for c in client.send_websocket_message.await_args_list]
    assert all(m["integration"] == ["ollama"] for m in sent)
