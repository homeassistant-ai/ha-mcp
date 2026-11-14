"""One flow step per ``ha_set_integration`` call (integration_flow_steps)."""

import contextlib
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.redaction import REDACTED_SET
from ha_mcp.tools import config_entry_flow_errors, integration_flow_steps
from ha_mcp.tools.integration_flow_steps import (
    FlowTarget,
    drive_flow,
    run_integration_step,
    settle,
)
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._tool_errors import error_body, error_of

SETUP = FlowTarget("setup", "knx")
OPTIONS = FlowTarget("options", "demo", "entry1")
RECONFIGURE = FlowTarget("reconfigure", "filesize", "entry2")


def _form(step_id: str, *fields: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "type": "form",
        "flow_id": "f1",
        "step_id": step_id,
        "data_schema": list(fields),
        **extra,
    }


def _field(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "type": "string", "required": True, **extra}


MENU = {
    "type": "menu",
    "flow_id": "f1",
    "step_id": "user",
    "menu_options": ["routing", "tunneling"],
}
CREATED = {
    "type": "create_entry",
    "flow_id": "f1",
    "title": "Routing",
    "result": {"entry_id": "new1", "title": "Routing as 0.0.240"},
}


def _client(**overrides: Any) -> Any:
    client = AsyncMock()
    client.submit_config_flow_step = AsyncMock(return_value=CREATED)
    client.submit_options_flow_step = AsyncMock(return_value=CREATED)
    client.abort_config_flow = AsyncMock(return_value={})
    client.abort_options_flow = AsyncMock(return_value={})
    for name, value in overrides.items():
        setattr(client, name, value)
    return client


@pytest.fixture(autouse=True)
def _no_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core's strings are not read: texts come back as the test sets them."""

    async def describe_form(_client, _domain, _path, step):
        return {"fields": step.get("data_schema")}

    async def flow_texts(_client, _flow, _step, _kind, keys):
        return {key: f"text of {value}" for key, value in keys.items()}

    monkeypatch.setattr(integration_flow_steps, "describe_form", describe_form)
    # No component: a flow is bound by the server's own list alone.
    monkeypatch.setattr(
        integration_flow_steps, "flow_record", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(integration_flow_steps, "_flow_texts", flow_texts)
    monkeypatch.setattr(config_entry_flow_errors, "_flow_texts", flow_texts)


def _start(step: dict[str, Any]) -> AsyncMock:
    return AsyncMock(return_value=step)


async def _drive(
    client: Any, target: FlowTarget = SETUP, *, opened: bool = True, **kwargs: Any
) -> Any:
    """``drive_flow``; a ``flow_id`` counts as opened for ``target`` unless
    ``opened`` is false or the test recorded it otherwise."""
    if opened and isinstance(kwargs.get("flow_id"), str):
        integration_flow_steps._opened_flows.setdefault(
            kwargs["flow_id"], target.anchor
        )
    args: dict[str, Any] = {
        "flow_id": None,
        "config": None,
        "menu_choice": None,
        "abort": False,
        "start": _start(_form("user", _field("host"))),
    }
    args.update(kwargs)
    return await drive_flow(client, target, **args)


async def _run(client: Any, **kwargs: Any) -> Any:
    """``run_integration_step`` on a setup flow, the way the tool calls it."""
    args: dict[str, Any] = {
        "domain": "knx",
        "entry_id": None,
        "flow_id": None,
        "config": None,
        "menu_choice": None,
        "abort": False,
    }
    args.update(kwargs)
    if isinstance(args["flow_id"], str):
        anchor = (
            ("setup", args["domain"])
            if args["domain"]
            else ("options", args["entry_id"])
        )
        integration_flow_steps._opened_flows.setdefault(args["flow_id"], anchor)
    return await run_integration_step(client, **args)


# --- a new flow -------------------------------------------------------------


async def test_new_flow_submits_config_to_its_first_form_and_finishes():
    client = _client()
    result = await _drive(client, config={"host": "10.0.0.2"})
    client.submit_config_flow_step.assert_awaited_once_with("f1", {"host": "10.0.0.2"})
    assert result == {
        "success": True,
        "entry_id": "new1",
        "title": "Routing as 0.0.240",
        "domain": "knx",
        "message": "knx integration created successfully",
    }


async def test_a_config_list_answers_the_flows_forms_in_order():
    """Each object goes to the form the flow is then at, and an integration
    whose flow ends early reports the answers it never asked for."""
    client = _client(
        start_config_flow=AsyncMock(return_value=_form("user", _field("host"))),
        submit_config_flow_step=AsyncMock(
            side_effect=[_form("routing", _field("individual_address")), CREATED]
        ),
    )
    answers = [{"host": "10.0.0.2"}, {"individual_address": "0.0.240"}, {"x": 1}]

    result = await _run(client, config=answers)

    assert [call.args for call in client.submit_config_flow_step.await_args_list] == [
        ("f1", answers[0]),
        ("f1", answers[1]),
    ]
    assert result["entry_id"] == "new1"
    assert "before the last 1" in result["warnings"][0]


async def test_new_flow_answers_its_menu_then_the_form_in_one_call():
    client = _client(
        submit_config_flow_step=AsyncMock(
            side_effect=[_form("routing", _field("individual_address")), CREATED]
        )
    )
    result = await _drive(
        client,
        start=_start(MENU),
        menu_choice="routing",
        config={"individual_address": "0.0.240"},
    )
    assert [call.args for call in client.submit_config_flow_step.await_args_list] == [
        ("f1", {"next_step_id": "routing"}),
        ("f1", {"individual_address": "0.0.240"}),
    ]
    assert result["entry_id"] == "new1"


async def test_new_flow_without_config_stops_at_the_form_after_its_menu():
    """An omitted config is not defaulted to {} when a menu_choice is given."""
    client = _client(
        start_config_flow=AsyncMock(return_value=MENU),
        submit_config_flow_step=AsyncMock(
            return_value=_form("routing", _field("individual_address"))
        ),
    )
    result = await _run(client, menu_choice="routing")
    client.submit_config_flow_step.assert_awaited_once_with(
        "f1", {"next_step_id": "routing"}
    )
    assert result["flow_id"] == "f1"
    assert result["step_id"] == "routing"


@pytest.mark.parametrize(
    "answer", [{"menu_choice": "routing"}, {"config": {"host": "h"}}]
)
async def test_a_flow_finished_at_its_start_submits_nothing(answer):
    client = _client()
    result = await _drive(client, start=_start(CREATED), **answer)
    assert result["entry_id"] == "new1"
    client.submit_config_flow_step.assert_not_awaited()


@pytest.mark.parametrize("flow_id", [{}, {"flow_id": ""}, {"flow_id": 123}])
async def test_a_flow_started_without_flow_id_is_refused(flow_id):
    client = _client()
    with pytest.raises(ToolError) as exc_info:
        await _drive(client, start=_start({"type": "form", **flow_id}))
    assert error_of(exc_info)["code"] == "SERVICE_CALL_FAILED"


async def test_a_new_flow_aborted_on_its_answer_is_refused():
    aborted = {"type": "abort", "flow_id": "f1", "reason": "already_configured"}
    client = _client(submit_config_flow_step=AsyncMock(return_value=aborted))
    with pytest.raises(ToolError):
        await _drive(client, config={"host": "h"})


# --- the answer to a step ---------------------------------------------------


async def test_a_rejected_form_is_an_error_and_keeps_the_flow_open():
    shown = _form("routing", _field("individual_address"), errors={"base": "no_router"})
    client = _client(submit_config_flow_step=AsyncMock(return_value=shown))
    with pytest.raises(ToolError) as exc_info:
        await _drive(
            client,
            start=_start(_form("routing", _field("individual_address"))),
            config={"individual_address": "0.0.240"},
        )
    error = error_body(exc_info)
    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert error["flow_id"] == "f1"
    assert error["errors"] == {"base": "no_router"}
    assert error["error_text"] == {"base": "text of no_router"}
    assert "The flow is still open" in " ".join(error["error"]["suggestions"])
    client.abort_config_flow.assert_not_awaited()


async def test_a_form_shown_first_with_advisory_errors_is_a_step():
    """KNX's routing form opens with no_router_discovered after
    connection_type: a new step with errors, not the submitted one shown again."""
    shown = _form(
        "routing", _field("individual_address"), errors={"base": "no_router_discovered"}
    )
    client = _client(submit_config_flow_step=AsyncMock(return_value=shown))
    result = await _drive(
        client,
        start=_start(_form("connection_type", _field("connection_type"))),
        config={"connection_type": "routing"},
    )
    assert result["success"] is True
    assert result["step_id"] == "routing"
    assert result["errors"] == {"base": "no_router_discovered"}
    client.abort_config_flow.assert_not_awaited()


async def test_a_menu_answer_lists_its_options():
    client = _client(submit_config_flow_step=AsyncMock(return_value=MENU))
    result = await _drive(client, config={"host": "h"})
    assert result["flow_type"] == "menu"
    assert result["menu_options"] == ["routing", "tunneling"]
    assert "menu_choice" in result["message"]


async def test_an_undeclared_key_is_left_to_home_assistant_to_refuse():
    """Core's schema decides what a form takes: the key is sent, and Core's
    refusal comes back with the form's schema and the flow still open."""
    refusal = HomeAssistantAPIError("bad", status_code=400)
    refusal.response_data = {
        "errors": {"base": ["extra keys not allowed @ data['port']"]}
    }
    client = _client(submit_config_flow_step=AsyncMock(side_effect=refusal))
    with pytest.raises(ToolError) as exc_info:
        await _drive(client, config={"host": "h", "port": 1})
    client.submit_config_flow_step.assert_awaited_once_with(
        "f1", {"host": "h", "port": 1}
    )
    error = error_body(exc_info)
    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "extra keys not allowed" in error["error"]["message"]
    assert error["flow_id"] == "f1"
    assert error["data_schema"] == [_field("host")]
    assert any("still open" in hint for hint in error["error"]["suggestions"])
    client.abort_config_flow.assert_not_awaited()


async def test_an_options_form_keeps_the_values_the_caller_left_out():
    stored = _form(
        "init",
        _field("string", required=False, description={"suggested_value": "Default"}),
        _field("int", required=False, description={"suggested_value": 10}),
    )
    client = _client(
        get_config_entry=AsyncMock(
            return_value={"domain": "demo", "supports_options": True}
        ),
        start_options_flow=AsyncMock(return_value=stored),
    )
    result = await _run(
        client, domain=None, entry_id="entry1", config={"string": "BAT"}
    )
    client.submit_options_flow_step.assert_awaited_once_with(
        "f1", {"string": "BAT", "int": 10}
    )
    assert result["updated"] is True
    assert result["entry_id"] == "entry1"


# --- a flow_id continues the open flow ---------------------------------------


@pytest.mark.parametrize(
    ("target", "url", "submit"),
    [
        (SETUP, "/config/config_entries/flow/f1", "submit_config_flow_step"),
        (OPTIONS, "/config/config_entries/options/flow/f1", "submit_options_flow_step"),
    ],
)
async def test_flow_id_reads_the_step_back_then_answers_it(target, url, submit):
    client = _client(_request=AsyncMock(return_value=_form("user", _field("host"))))
    await _drive(client, target, flow_id="f1", config={"host": "h"})
    client._request.assert_awaited_once_with("GET", url)
    getattr(client, submit).assert_awaited_once_with("f1", {"host": "h"})


async def test_flow_id_alone_reads_the_step_again_without_submitting():
    """flow_id alone is a read, not the ``config = {}`` default of a new flow."""
    client = _client(_request=AsyncMock(return_value=_form("user", _field("host"))))
    result = await _run(client, flow_id="f1")
    assert result["step_id"] == "user"
    client._request.assert_awaited_once_with("GET", "/config/config_entries/flow/f1")
    client.start_config_flow.assert_not_awaited()
    client.submit_config_flow_step.assert_not_awaited()


@pytest.mark.parametrize(
    ("target", "opened_with"),
    [
        (SETUP, None),
        (SETUP, ("setup", "zha")),
        (SETUP, ("reconfigure", "entry2")),
        (OPTIONS, ("options", "entry9")),
        (RECONFIGURE, None),
        (RECONFIGURE, ("reconfigure", "entry9")),
        (RECONFIGURE, ("setup", "filesize")),
    ],
    ids=[
        "setup-unknown",
        "other-integration",
        "reconfigure-as-setup",
        "other-entry",
        "reconfigure-unknown",
        "other-entrys-reconfigure",
        "setup-as-reconfigure",
    ],
)
@pytest.mark.parametrize("call", [{"config": {"host": "h"}}, {"abort": True}])
async def test_a_flow_not_opened_for_the_calls_anchor_is_refused(
    opened_flows, target, opened_with, call
):
    """A flow_id continues or ends only with the kind and anchor this server
    opened it with: not another integration's or entry's flow, not a
    reconfigure flow as a setup flow, and not one it did not open (the UI's,
    or one from before an ha-mcp restart)."""
    if opened_with is not None:
        opened_flows["f1"] = opened_with
    client = _client()
    with pytest.raises(ToolError) as exc_info:
        await _drive(client, target, opened=False, flow_id="f1", **call)
    assert "of this call's target" in error_of(exc_info)["message"]
    client._request.assert_not_awaited()
    client.submit_config_flow_step.assert_not_awaited()
    client.submit_options_flow_step.assert_not_awaited()
    client.abort_config_flow.assert_not_awaited()
    client.abort_options_flow.assert_not_awaited()


_RECONFIGURE_RECORD = {
    "found": True,
    "handler": "filesize",
    "source": "reconfigure",
    "entry_id": "entry2",
    "subentry_id": None,
}


@pytest.mark.parametrize(
    ("target", "record", "continued"),
    [
        (RECONFIGURE, _RECONFIGURE_RECORD, True),
        (RECONFIGURE, {**_RECONFIGURE_RECORD, "entry_id": "entry9"}, False),
        (SETUP, {**_RECONFIGURE_RECORD, "handler": "knx", "source": "user"}, True),
        (SETUP, {**_RECONFIGURE_RECORD, "handler": "knx", "source": "reauth"}, False),
    ],
    ids=["its-entry", "another-entry", "user-setup", "reauth-as-setup"],
)
async def test_a_flow_the_server_forgot_is_bound_by_cores_record(
    monkeypatch: pytest.MonkeyPatch, target, record, continued
):
    """After an ha-mcp restart the flow's target is the one Core recorded: a
    reconfigure flow continues only for its own entry, and a reauth flow Core
    started for the domain is not a setup flow to answer."""
    monkeypatch.setattr(
        integration_flow_steps, "flow_record", AsyncMock(return_value=record)
    )
    client = _client(_request=AsyncMock(return_value=_form("user", _field("host"))))

    if continued:
        result = await _drive(client, target, opened=False, flow_id="f1")
        assert result["flow_id"] == "f1"
    else:
        with pytest.raises(ToolError):
            await _drive(client, target, opened=False, flow_id="f1")
        client._request.assert_not_awaited()


async def test_a_flow_of_the_calls_own_anchor_is_answered():
    client = _client(_request=AsyncMock(return_value=_form("user", _field("host"))))
    await _drive(client, OPTIONS, flow_id="f1", config={"host": "h"})
    client.submit_options_flow_step.assert_awaited_once_with("f1", {"host": "h"})


async def test_a_rejected_answer_says_the_flow_is_still_open():
    client = _client(
        _request=AsyncMock(return_value=MENU),
        submit_config_flow_step=AsyncMock(
            side_effect=HomeAssistantAPIError("bad", status_code=400)
        ),
    )
    with pytest.raises(ToolError) as exc_info:
        await _drive(client, flow_id="f1", menu_choice="routing")
    assert "still open" in error_of(exc_info)["suggestion"]


async def test_a_flow_home_assistant_no_longer_has_is_not_found():
    client = _client(
        _request=AsyncMock(side_effect=HomeAssistantAPIError("gone", status_code=404))
    )
    with pytest.raises(ToolError) as exc_info:
        await _drive(client, flow_id="f1", config={"host": "h"})
    error = error_body(exc_info)
    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert error["flow_kind"] == "setup"


@pytest.mark.parametrize(
    ("step", "menu_choice", "config", "says"),
    [
        (MENU, "secure", None, "not offered"),
        (_form("user", _field("host")), "routing", None, "not a menu"),
        (MENU, None, {"host": "h"}, "pass menu_choice"),
        (MENU, "routing", {"host": "h"}, "not both"),
    ],
)
async def test_a_wrong_kind_of_answer_is_refused_and_the_flow_stays_open(
    step, menu_choice, config, says
):
    client = _client(_request=AsyncMock(return_value=step))
    with pytest.raises(ToolError) as exc_info:
        await _drive(client, flow_id="f1", menu_choice=menu_choice, config=config)
    error = error_body(exc_info)
    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert says in error["error"]["message"]
    assert "abort=True" in error["error"]["suggestion"]
    client.submit_config_flow_step.assert_not_awaited()
    client.abort_config_flow.assert_not_awaited()
    if says == "not both":  # refused before the GET, which re-runs the step
        client._request.assert_not_awaited()


@pytest.mark.parametrize("target", [SETUP, OPTIONS], ids=["setup", "options"])
async def test_a_redaction_placeholder_in_config_is_refused_and_nothing_is_sent(target):
    client = _client(_request=AsyncMock(return_value=_form("user", _field("host"))))
    with pytest.raises(ToolError) as exc_info:
        await integration_flow_steps.submit_step(
            client,
            target,
            "f1",
            config={"host": REDACTED_SET},
            menu_choice=None,
            current=_form("user", _field("host")),
        )
    error = error_body(exc_info)
    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "redaction placeholder" in error["error"]["message"]
    client._request.assert_not_awaited()
    client.submit_config_flow_step.assert_not_awaited()
    client.submit_options_flow_step.assert_not_awaited()


async def test_a_setup_form_does_not_get_the_values_the_caller_left_out():
    """A setup form's suggested value is a hint, not something to submit."""
    shown = _form(
        "user",
        _field("host"),
        _field("name", required=False, description={"suggested_value": "Hint"}),
    )
    client = _client(_request=AsyncMock(return_value=shown))
    await _drive(client, SETUP, flow_id="f1", config={"host": "h"})
    client.submit_config_flow_step.assert_awaited_once_with("f1", {"host": "h"})


# --- abort --------------------------------------------------------------------


async def test_a_failed_abort_keeps_the_flow_bound_and_a_gone_one_is_not_open(
    opened_flows,
):
    opened_flows["f1"] = RECONFIGURE.anchor
    client = _client()
    client.abort_config_flow.side_effect = HomeAssistantAPIError(
        "boom", status_code=500
    )
    with pytest.raises(HomeAssistantAPIError):
        await _drive(client, RECONFIGURE, flow_id="f1", abort=True)
    assert opened_flows == {"f1": RECONFIGURE.anchor}
    client.abort_config_flow.side_effect = HomeAssistantAPIError(
        "gone", status_code=404
    )
    with pytest.raises(ToolError) as exc_info:
        await _drive(client, RECONFIGURE, flow_id="f1", abort=True)
    assert error_of(exc_info)["code"] == "RESOURCE_NOT_FOUND"
    client._request.assert_not_awaited()


async def test_abort_without_flow_id_is_refused():
    client = _client()
    with pytest.raises(ToolError):
        await _drive(client, abort=True)
    client.abort_config_flow.assert_not_awaited()


@pytest.mark.parametrize(
    ("kwargs", "abort_fn", "anchor"),
    [
        ({"domain": "knx"}, "abort_config_flow", ("setup", "knx")),
        ({"entry_id": "entry1"}, "abort_options_flow", ("options", "entry1")),
    ],
    ids=["setup", "options"],
)
async def test_abort_reaches_the_clients_abort_through_the_tool(
    opened_flows, kwargs, abort_fn, anchor
):
    opened_flows["f1"] = anchor
    client = _client(
        get_config_entry=AsyncMock(
            return_value={"domain": "demo", "supports_options": True}
        ),
    )
    result = await IntegrationTools(client).ha_set_integration(
        flow_id="f1", abort=True, **kwargs
    )
    getattr(client, abort_fn).assert_awaited_once_with("f1")
    assert result["aborted"] is True
    assert opened_flows == {}
    client._request.assert_not_awaited()
    client.start_config_flow.assert_not_awaited()
    client.start_options_flow.assert_not_awaited()
    client.submit_config_flow_step.assert_not_awaited()


async def test_a_server_error_on_re_reading_a_setup_flow_is_not_reported_as_applied():
    client = _client(
        _request=AsyncMock(side_effect=HomeAssistantAPIError("boom", status_code=500))
    )
    with pytest.raises(HomeAssistantAPIError):
        await _drive(client, flow_id="f1")
    client.submit_config_flow_step.assert_not_awaited()


# --- settle -------------------------------------------------------------------


async def test_an_external_step_is_refused_and_its_flow_aborted():
    client = _client()
    with pytest.raises(ToolError) as exc_info:
        await settle(client, SETUP, "f1", {"type": "external", "step_id": "auth"})
    assert error_of(exc_info)["code"] == "SERVICE_CALL_FAILED"
    client.abort_config_flow.assert_awaited_once_with("f1")


async def test_a_core_abort_raises_with_its_reason():
    client = _client()
    with pytest.raises(ToolError) as exc_info:
        await settle(
            client, SETUP, "f1", {"type": "abort", "reason": "already_configured"}
        )
    error = error_body(exc_info)
    assert error["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "already_configured" in error["error"]["message"]


async def test_a_reconfigure_success_abort_is_returned():
    step = {"type": "abort", "reason": "reconfigure_successful"}
    assert await settle(_client(), RECONFIGURE, "f1", step) is step


@pytest.mark.parametrize("reason", ["reconfigure_successful", "reauth_successful"])
async def test_a_success_reason_outside_reconfigure_raises_as_applied(reason):
    with pytest.raises(ToolError) as exc_info:
        await settle(_client(), SETUP, "f1", {"type": "abort", "reason": reason})
    error = error_body(exc_info)
    assert error["status"] == "applied_but_unverified"
    assert "updated an existing config entry" in error["error"]["message"]


@pytest.mark.parametrize(
    "target",
    [
        RECONFIGURE,
        FlowTarget("subentry", "demo", "entry1", subentry_type="x", subentry_id="s1"),
    ],
    ids=["entry", "subentry"],
)
async def test_a_reconfigure_flow_that_creates_an_entry_is_unverified(target):
    with pytest.raises(ToolError) as exc_info:
        await settle(_client(), target, "f1", CREATED)
    assert error_body(exc_info)["status"] == "applied_but_unverified"


async def test_an_unknown_step_type_is_unexpected():
    with pytest.raises(ToolError) as exc_info:
        await settle(_client(), SETUP, "f1", {"type": "show_progress_v2"})
    assert error_of(exc_info)["code"] == "INTERNAL_UNEXPECTED"


# --- run_integration_step ------------------------------------------------------


async def test_a_new_flow_without_config_returns_its_first_step():
    client = _client(start_config_flow=AsyncMock(return_value=_form("user")))
    result = await _run(client, domain="local_calendar")
    client.submit_config_flow_step.assert_not_awaited()
    assert result["flow_id"] == "f1"
    assert result["step_id"] == "user"


async def test_an_entry_without_options_is_refused():
    client = _client(
        get_config_entry=AsyncMock(
            return_value={"domain": "sun", "supports_options": False}
        )
    )
    with pytest.raises(ToolError) as exc_info:
        await _run(client, domain=None, entry_id="entry1", config={})
    assert "supports_options" in error_of(exc_info)["message"]
    client.start_options_flow.assert_not_awaited()


# --- auto-backup wiring of ha_set_integration ---------------------------------


@pytest.mark.parametrize(
    ("args", "captures"),
    [
        ({"config": {"string": "BAT"}}, 1),
        ({"flow_id": "f1", "config": {"string": "BAT"}}, 1),
        ({"flow_id": "f1", "menu_choice": "advanced"}, 1),
        ({}, 1),
        ({"flow_id": "f1"}, 1),
        ({"flow_id": "f1", "abort": True}, 0),
        ({"flow_id": "f1", "abort": True, "config": {"string": "BAT"}}, 0),
        ({"reconfigure": True}, 1),
        ({"subentry_type": "device", "menu_choice": "save_changes"}, 1),
        ({"reconfigure": True, "flow_id": "f1", "config": {"host": "h"}}, 1),
        ({"log_level": "DEBUG"}, 0),
    ],
)
async def test_every_flow_call_but_an_abort_captures_a_backup(
    monkeypatch: pytest.MonkeyPatch, args: dict[str, Any], captures: int
):
    from ha_mcp.tools import auto_backup, tools_integrations

    calls: list[str] = []

    class _Mgr:
        async def maybe_snapshot(self, domain, entity_id, **_kwargs):
            calls.append(entity_id)

        @contextlib.asynccontextmanager
        async def config_entry_write_guard(self, _entry_id):
            yield

    class _Settings:
        enable_auto_backup = True

    monkeypatch.setattr(auto_backup, "get_global_settings", _Settings)
    monkeypatch.setattr(auto_backup, "get_backup_manager", lambda _c, _s: _Mgr())
    for name in ("run_integration_step", "set_log_level"):
        monkeypatch.setattr(
            tools_integrations, name, AsyncMock(return_value={"success": True})
        )
    monkeypatch.setattr(
        tools_integrations,
        "describe_flow_form",
        AsyncMock(return_value={"success": True}),
    )
    client = _client(get_config_entry=AsyncMock(return_value={"domain": "demo"}))
    await tools_integrations.IntegrationTools(client).ha_set_integration(
        entry_id="entry1", **args
    )
    assert len(calls) == captures
