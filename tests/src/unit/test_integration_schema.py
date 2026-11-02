"""ha_get_integration's options and subentry flow schemas (#2632).

A schema exists only inside an open flow: the tool opens the entry's options
(or subentry) flow, describes its form in the same compact shape the helper
describe uses (fields, Core's help text, current values), and aborts it. A
flow that opens with a menu lists its branches; ``menu_choice``, which only
ha_set_integration(describe=True) takes, describes the form behind one of them.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.redaction import REDACTED_SET
from ha_mcp.tools import config_entry_flow_introspect, integration_schema
from ha_mcp.tools.integration_schema import (
    fetch_config_flow_schema,
    fetch_config_subentry_schema,
    fetch_options_schema,
    redact_component_options,
)
from ha_mcp.tools.tools_integrations import (
    IntegrationTools,
    fetch_entry_options_with_status,
)

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
    client.start_config_flow.return_value = first_step
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
    assert "menu_choice to ha_set_integration(" in schema["note"]
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


async def _describe(kind: str, client: Any, menu_choice: str | None) -> None:
    if kind == "probe":
        entry = {"supports_options": True, "options": {"host": "h"}}
        await redact_component_options(client, "e1", entry, [])
    elif kind == "entry_options":
        await fetch_entry_options_with_status(client, "e1")
    elif kind == "options":
        # strict, as ha_set_integration(describe=True) reads it.
        await fetch_options_schema(
            client, "e1", _resp(), strict=True, menu_choice=menu_choice
        )
    elif kind == "subentry":
        await fetch_config_subentry_schema(
            client,
            "e1",
            _resp(),
            subentry_type="device",
            subentry_id=None,
            show_advanced_options=False,
            menu_choice=menu_choice,
        )
    else:
        await fetch_config_flow_schema(client, "demo", {}, menu_choice=menu_choice)


_FLOW_METHODS = {
    "options": ("submit_options_flow_step", "abort_options_flow"),
    "subentry": ("submit_config_subentry_flow_step", "abort_config_subentry_flow"),
    "setup": ("submit_config_flow_step", "abort_config_flow"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(_FLOW_METHODS))
async def test_a_failed_menu_choice_submit_fails_every_describe(kind: str) -> None:
    client = _client(_MENU)
    submit, abort = _FLOW_METHODS[kind]
    getattr(client, submit).side_effect = HomeAssistantAPIError(
        "API error: 400 - Invalid step", status_code=400
    )

    with pytest.raises(HomeAssistantAPIError, match="Invalid step"):
        await _describe(kind, client, "advanced")

    getattr(client, abort).assert_awaited_once_with("f1")


_ABORTS = {"probe": "abort_options_flow", "entry_options": "abort_options_flow"} | {
    kind: methods[1] for kind, methods in _FLOW_METHODS.items()
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(_ABORTS))
async def test_a_cancelled_describe_still_closes_its_flow(kind: str) -> None:
    client = _client(_form("init", _HOST))
    sent: list[str] = []

    async def abort(flow_id: str) -> None:
        await asyncio.sleep(0)
        sent.append(flow_id)

    getattr(client, _ABORTS[kind]).side_effect = abort

    with anyio.CancelScope() as scope:
        scope.cancel()
        await _describe(kind, client, None)

    assert sent == ["f1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(_ABORTS))
@pytest.mark.parametrize(("status", "level"), [(404, "DEBUG"), (500, "WARNING")])
async def test_a_failed_describe_abort_is_a_warning_unless_the_flow_is_gone(
    kind: str, status: int, level: str, caplog: pytest.LogCaptureFixture
) -> None:
    client = _client(_form("init", _HOST))
    getattr(client, _ABORTS[kind]).side_effect = HomeAssistantAPIError(
        f"API error: {status}", status_code=status
    )

    with caplog.at_level("DEBUG", logger="ha_mcp.tools.config_entry_flow_introspect"):
        await _describe(kind, client, None)

    assert [r.levelname for r in caplog.records if "abort" in r.getMessage()] == [level]


_LEFT_OPEN = {
    "options": ("abort_options_flow", "the options flow f1 was closed"),
    "subentry": ("abort_config_subentry_flow", "the subentry flow f1 was closed"),
    "setup": ("abort_config_flow", "already_in_progress"),
}
_ABORT_ANSWERS = [
    pytest.param(None, False, id="aborted"),
    pytest.param(
        HomeAssistantAPIError("API error: 404", status_code=404), False, id="gone"
    ),
    pytest.param(
        HomeAssistantAPIError("API error: 500", status_code=500), True, id="http-500"
    ),
    pytest.param(TimeoutError(), True, id="timeout"),
]


async def _described(
    kind: str, client: Any, resp: dict[str, Any], menu_choice: str | None = None
) -> None:
    if kind == "options":
        await fetch_options_schema(
            client, "e1", resp, strict=True, menu_choice=menu_choice
        )
    elif kind == "subentry":
        await fetch_config_subentry_schema(
            client,
            "e1",
            resp,
            subentry_type="device",
            subentry_id=None,
            show_advanced_options=False,
            menu_choice=menu_choice,
        )
    else:
        await fetch_config_flow_schema(client, "demo", resp, menu_choice=menu_choice)


# How the describe ends: (first step, menu_choice, the error it raises once
# the flow is closed).
_ENDINGS = {
    "described": (_form("init", _HOST), None, None),
    # menu_choice on a flow that opens with a form: refused after the start.
    "refused": (_form("init", _HOST), "advanced", ToolError),
    # The menu submit fails with Home Assistant's raw error.
    "raw-error": (_MENU, "advanced", HomeAssistantAPIError),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(_LEFT_OPEN))
@pytest.mark.parametrize("ending", list(_ENDINGS))
@pytest.mark.parametrize(("abort_error", "left_open"), _ABORT_ANSWERS)
async def test_a_describe_says_when_its_flow_may_still_be_open(
    kind: str, ending: str, abort_error: Exception | None, left_open: bool
) -> None:
    first_step, menu_choice, error = _ENDINGS[ending]
    client = _client(first_step)
    abort, consequence = _LEFT_OPEN[kind]
    getattr(client, abort).side_effect = abort_error
    getattr(client, _FLOW_METHODS[kind][0]).side_effect = HomeAssistantAPIError(
        "API error: 500", status_code=500
    )
    resp = _resp()

    if error is None:
        await _described(kind, client, resp)
        warnings = resp.get("warnings", [])
    else:
        # An error left open comes back structured (a raw one converted), with
        # the warning.
        with pytest.raises(ToolError if left_open else error) as exc_info:
            await _described(kind, client, resp, menu_choice=menu_choice)
        is_tool_error = isinstance(exc_info.value, ToolError)
        warnings = _error(exc_info).get("warnings", []) if is_tool_error else []

    assert [w for w in warnings if consequence in w] == (warnings if left_open else [])
    assert len(warnings) == int(left_open)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [True, False], ids=["raw", "no-form"])
async def test_an_error_left_open_on_the_entry_read_keeps_the_warning(
    monkeypatch: pytest.MonkeyPatch, raw: bool
) -> None:
    # ha_get_integration's options probe (not strict) degrades a raw error to
    # warnings; a step with no form to describe still fails, with the warning.
    monkeypatch.setattr(
        integration_schema, "describe_form", AsyncMock(side_effect=RuntimeError("x"))
    )
    client = _client(_form("init", _HOST) if raw else {**_MENU, "type": "progress"})
    client.abort_options_flow.side_effect = TimeoutError()
    resp = _resp()

    if not raw:
        with pytest.raises(ToolError) as exc_info:
            await fetch_options_schema(client, "e1", resp)
        resp = _error(exc_info)
    else:
        await fetch_options_schema(client, "e1", resp)
        assert resp["warnings"][1].startswith("options schema probe failed for e1")
    assert "the options flow f1 was closed" in resp["warnings"][0]
    assert len(resp["warnings"]) == 1 + raw


@pytest.mark.asyncio
async def test_a_failed_options_probe_keeps_cores_message_in_the_warning() -> None:
    client = _client(_MENU)
    client.start_options_flow.side_effect = HomeAssistantAPIError(
        "API error: 500 - Config entry not loaded", status_code=500
    )
    resp = _resp()

    await fetch_options_schema(client, "e1", resp)

    assert "options_schema" not in resp
    assert any("Config entry not loaded" in w for w in resp["warnings"])


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
async def test_options_behind_a_menu_are_reported_unread_on_the_legacy_read() -> None:
    # The branch form shows one branch's fields, not the entry's options.
    client = _client(dict(_MENU))
    resp = _resp()

    await fetch_options_schema(client, "e1", resp)

    assert resp["entry"]["options"] == {}
    assert any("options for e1 could not be read" in w for w in resp["warnings"])


@pytest.mark.asyncio
async def test_a_component_read_behind_a_menu_keeps_its_options_without_a_warning() -> (
    None
):
    client = _client(dict(_MENU))
    resp = {"entry": {"entry_id": "e1", "domain": "demo", "options": {"a": 1}}}

    await fetch_options_schema(client, "e1", resp, populate_options=False)

    assert resp["entry"]["options"] == {"a": 1}
    assert "warnings" not in resp


async def _never_answers(*_args: Any) -> None:
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_an_options_abort_that_never_answers_does_not_hold_the_describe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_entry_flow_introspect, "_ABORT_TIMEOUT_S", 0.01)
    client = _client(_form("init", _HOST))
    client.abort_options_flow.side_effect = _never_answers
    resp = _resp()

    await asyncio.wait_for(fetch_options_schema(client, "e1", resp), timeout=2)

    assert resp["options_schema"]["flow_type"] == "form"


@pytest.mark.asyncio
async def test_a_redaction_probe_abort_that_never_answers_does_not_hold_the_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_entry_flow_introspect, "_ABORT_TIMEOUT_S", 0.01)
    client = _client(_form("init", _HOST, _API_KEY))
    client.abort_options_flow.side_effect = _never_answers
    entry = {"supports_options": True, "options": {"host": "h", "api_key": "k"}}

    await asyncio.wait_for(redact_component_options(client, "e1", entry, []), timeout=2)

    assert entry["options"] == {"host": "h", "api_key": REDACTED_SET}


@pytest.mark.asyncio
async def test_an_options_probe_abort_that_never_answers_does_not_hold_the_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_entry_flow_introspect, "_ABORT_TIMEOUT_S", 0.01)
    client = _client(_form("init", _HOST))
    client.abort_options_flow.side_effect = _never_answers

    options, ok = await asyncio.wait_for(
        fetch_entry_options_with_status(client, "e1"), timeout=2
    )

    assert ok is True
    assert set(options) == {"host"}


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


# --- the setup (config) flow of a domain, read in list mode -----------------


@pytest.mark.asyncio
async def test_setup_form_is_described_with_help_under_config() -> None:
    client = _client(
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
    client = _client(
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
    client = _client(
        {"type": "abort", "flow_id": "f1", "reason": "single_instance_allowed"}
    )
    resp: dict[str, Any] = {}

    await fetch_config_flow_schema(client, "sun", resp, menu_choice="light")

    assert resp["config_flow_schema"]["aborted"] == "single_instance_allowed"


@pytest.mark.asyncio
async def test_setup_menu_lists_branches_until_menu_choice_picks_one() -> None:
    client = _client(dict(_MENU))

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
    client = _client(
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
    client = _client(
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
    client = _client(dict(_MENU))
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
@pytest.mark.parametrize("kind", ["options", "subentry"])
async def test_an_entry_flow_that_finishes_gets_the_no_form_error(kind: str) -> None:
    client = _client({"type": "create_entry", "flow_id": "f1", "title": "Chat"})
    getattr(client, _ABORTS[kind]).side_effect = TimeoutError()

    with pytest.raises(ToolError) as exc_info:
        await _describe(kind, client, None)

    # The abort comes after the error; the error does not claim it worked.
    message = _error(exc_info)["error"]["message"]
    assert "'create_entry' step, which has no form" in message
    assert message.endswith("the flow is being aborted")
    assert "did not confirm" in _error(exc_info)["warnings"][0]


_PROGRESS_STEP = {
    "type": "progress",
    "flow_id": "f1",
    "step_id": "discover",
    "progress_action": "scanning",
}


@pytest.mark.asyncio
async def test_setup_flow_in_progress_is_aborted_and_named_with_cores_text() -> None:
    client = _client(
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
async def test_setup_flow_is_aborted_when_the_describe_is_refused() -> None:
    client = _client(_form("user", _HOST))

    with pytest.raises(ToolError):
        await fetch_config_flow_schema(client, "demo", {}, menu_choice="advanced")

    client.abort_config_flow.assert_awaited_once_with("f1")


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["demo", None], ids=["domain", "no-domain"])
async def test_list_mode_include_schema_is_ignored_and_points_to_set(
    domain: str | None,
) -> None:
    client = _client(_form("user", _HOST))
    tools = IntegrationTools(client)
    tools._list_entries = AsyncMock(return_value={"success": True, "entries": []})  # type: ignore[method-assign]

    resp = await tools.ha_get_integration(domain=domain, include_schema=True)

    client.start_config_flow.assert_not_called()
    assert any("describe=True" in w for w in resp["warnings"])


# --- strings Home Assistant could not send are a warning, a missing one not --


@pytest.mark.asyncio
async def test_a_failed_strings_read_is_a_warning_on_the_described_form() -> None:
    client = _client(_form("init", _HOST))
    client.send_websocket_message.side_effect = RuntimeError("ws down")
    resp = _resp()

    await fetch_options_schema(client, "e1", resp)

    assert resp["options_schema"]["fields"][0]["name"] == "host"
    assert "warnings" not in resp["options_schema"]
    assert any("strings for demo could not be read" in w for w in resp["warnings"])


@pytest.mark.asyncio
async def test_a_failed_strings_read_is_a_warning_on_an_abort() -> None:
    client = _client({"type": "abort", "flow_id": "f1", "reason": "no_options"})
    client.send_websocket_message.side_effect = RuntimeError("ws down")
    resp = _resp()

    await fetch_options_schema(client, "e1", resp)

    schema = resp["options_schema"]
    assert schema["aborted"] == "no_options"
    assert "warnings" not in schema
    assert any("strings for demo could not be read" in w for w in resp["warnings"])


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["subentry_schema", "config_flow_schema"])
async def test_a_failed_strings_read_is_a_top_level_warning_on_every_describe(
    key: str,
) -> None:
    client = _client(_form("user", _HOST))
    client.send_websocket_message.side_effect = RuntimeError("ws down")
    resp = _resp()

    if key == "subentry_schema":
        await fetch_config_subentry_schema(
            client,
            "e1",
            resp,
            subentry_type="device",
            subentry_id=None,
            show_advanced_options=False,
        )
    else:
        await fetch_config_flow_schema(client, "demo", resp)

    assert resp[key]["fields"][0]["name"] == "host"
    assert "warnings" not in resp[key]
    assert any("strings for demo could not be read" in w for w in resp["warnings"])


@pytest.mark.asyncio
async def test_a_string_core_does_not_ship_is_no_warning() -> None:
    client = _client({"type": "abort", "flow_id": "f1", "reason": "no_options"})
    resp = _resp()

    await fetch_options_schema(client, "e1", resp)

    assert not any("strings for" in w for w in resp.get("warnings", []))
    assert "warnings" not in resp["options_schema"]
    assert "reason_text" not in resp["options_schema"]


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


@pytest.mark.asyncio
async def test_an_unreadable_entry_domain_is_a_warning_beside_the_reason() -> None:
    client = _client({"type": "abort", "flow_id": "f1", "reason": "no_models"})
    client.get_config_entry.side_effect = HomeAssistantAPIError(
        "API error: 500", status_code=500
    )
    resp: dict[str, Any] = {"entry": {"entry_id": "e1"}}

    await fetch_config_subentry_schema(
        client,
        "e1",
        resp,
        subentry_type="conversation",
        subentry_id=None,
        show_advanced_options=False,
    )

    assert resp["subentry_schema"]["aborted"] == "no_models"
    assert "reason_text" not in resp["subentry_schema"]
    assert resp["warnings"] == [
        "The domain of config entry e1 could not be read, so Home Assistant's "
        "texts for this flow step are missing"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["probe", "entry_schema"])
async def test_a_swallowed_options_flow_failure_logs_its_traceback(
    kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    client = _client(_form("init", _HOST))
    client.start_options_flow.side_effect = RuntimeError("ws down")

    with caplog.at_level("WARNING", logger="ha_mcp.tools.integration_schema"):
        if kind == "probe":
            entry = {"supports_options": True, "options": {"host": "h"}}
            await redact_component_options(client, "e1", entry, [])
        else:
            await fetch_options_schema(client, "e1", _resp())

    (record,) = [
        r for r in caplog.records if r.name == "ha_mcp.tools.integration_schema"
    ]
    assert record.exc_info is not None
