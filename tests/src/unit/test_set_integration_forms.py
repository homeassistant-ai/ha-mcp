"""ha_set_integration without an answer: setup, options and subentry forms (#2632)."""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools import config_entry_flow_progress
from ha_mcp.tools.tools_integrations import IntegrationTools

from .test_integration_schema import _HOST, _MENU, _client, _error, _form


@pytest.mark.asyncio
async def test_set_integration_returns_the_setup_form() -> None:
    client = _client(
        _form("user", _HOST, last_step=False),
        {"component.demo.config.step.user.data_description.host": "Device IP."},
    )

    resp = await IntegrationTools(client).ha_set_integration(domain="Demo ")

    client.start_config_flow.assert_awaited_once_with("demo")
    # Nothing is submitted; the flow stays open and flow_id continues it.
    client.submit_config_flow_step.assert_not_called()
    client.abort_config_flow.assert_not_called()
    assert resp["flow_id"] == "f1"
    assert resp["step_id"] == "user"
    assert resp["fields"][0]["name"] == "host"
    assert resp["fields"][0]["description"] == "Device IP."
    assert resp["more_forms"] == "yes"


_SUBENTRY_RULE = "subentry_type and subentry_id apply only with entry_id"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "says"),
    [
        ({"domain": "demo", "entry_id": "e1"}, "not both"),
        ({"domain": "demo", "subentry_type": "conversation"}, _SUBENTRY_RULE),
        (
            {"entry_id": "e1", "subentry_type": "conversation", "flow_id": "f1"},
            "drop flow_id",
        ),
        (
            {"entry_id": "e1", "subentry_type": "conversation", "config": {"h": 1}},
            "drop config",
        ),
    ],
)
async def test_set_integration_subentry_form_takes_menu_choice_only(
    call: dict[str, Any], says: str
) -> None:
    client = _client(_form("user", _HOST))

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(**call)

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert says in error["message"]
    client.start_config_flow.assert_not_called()
    client.start_options_flow.assert_not_called()
    client.start_config_subentry_flow.assert_not_called()


def _entry_client(first_step: dict[str, Any], **entry: Any):
    client = _client(
        first_step, {"component.demo.options.step.advanced.data.host": "Host"}
    )
    client.get_config_entry.return_value = {"entry_id": "e1", "domain": "demo", **entry}
    return client


@pytest.mark.asyncio
async def test_set_integration_describes_the_options_form_behind_a_menu() -> None:
    client = _entry_client(dict(_MENU))
    client.submit_options_flow_step.return_value = _form("advanced", _HOST)

    resp = await IntegrationTools(client).ha_set_integration(
        entry_id="e1", menu_choice="advanced"
    )

    client.submit_options_flow_step.assert_awaited_once_with(
        "f1", {"next_step_id": "advanced"}
    )
    # The flow stays open at the form behind the menu.
    client.abort_options_flow.assert_not_called()
    assert resp["flow_id"] == "f1"
    assert resp["step_id"] == "advanced"
    assert [(f["name"], f["description"]) for f in resp["fields"]] == [("host", "Host")]
    assert "entry" not in resp


@pytest.mark.asyncio
async def test_set_integration_describes_the_subentry_form_behind_a_menu() -> None:
    client = _entry_client(dict(_MENU))
    client.submit_config_subentry_flow_step.return_value = _form("advanced", _HOST)

    resp = await IntegrationTools(client).ha_set_integration(
        entry_id="e1",
        subentry_type="conversation",
        subentry_id="sub1",
        menu_choice="advanced",
    )

    client.start_config_subentry_flow.assert_awaited_once_with(
        "e1", "conversation", subentry_id="sub1", show_advanced_options=False
    )
    client.submit_config_subentry_flow_step.assert_awaited_once_with(
        "f1", {"next_step_id": "advanced"}
    )
    client.abort_config_subentry_flow.assert_awaited_once_with("f1")
    assert resp["subentry_schema"]["menu_choice"] == "advanced"
    client.start_options_flow.assert_not_called()


@pytest.mark.asyncio
async def test_set_integration_options_form_refuses_an_ignored_entry() -> None:
    client = _entry_client(dict(_MENU), source="ignore")

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(entry_id="e1")

    assert "ignored" in _error(exc_info)["error"]["message"]
    client.start_options_flow.assert_not_called()


@pytest.mark.asyncio
async def test_set_integration_options_form_fails_when_the_flow_does() -> None:
    client = _entry_client({})
    client.start_options_flow.side_effect = HomeAssistantAPIError(
        "API error: 400 - flow failed", status_code=400
    )

    with pytest.raises(ToolError):
        await IntegrationTools(client).ha_set_integration(entry_id="e1")


def test_ha_get_integration_takes_no_menu_choice() -> None:
    params = inspect.signature(IntegrationTools.ha_get_integration).parameters
    assert "menu_choice" not in params


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "text", "code", "says"),
    [
        # Core answers 400 for failed dependencies: the domain is installed.
        (400, "Failed dependencies mqtt", "SERVICE_CALL_FAILED", "mqtt"),
        (404, "Invalid handler specified", "RESOURCE_NOT_FOUND", "Invalid handler"),
        # Only a 400 or 404 is about the flow start; a 5xx is the tool's own error.
        (500, "", None, None),
    ],
)
async def test_set_integration_whose_flow_cannot_start(
    status: int, text: str, code: str | None, says: str | None
) -> None:
    client = _client({})
    client.start_config_flow.side_effect = HomeAssistantAPIError(
        f"API error: {status} - {text}", status_code=status
    )

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(domain="no_such_domain")

    error = json.loads(str(exc_info.value))["error"]
    if code is None:
        assert "could not start a config flow" not in error["message"]
        assert "spelled correctly" in error["suggestion"]
    else:
        assert error["code"] == code
        assert "no_such_domain" in error["message"]
        assert says in error["message"]
        assert (status == 400) == ("Home Assistant log" in json.dumps(error))


@pytest.mark.asyncio
async def test_set_integration_lists_a_setup_menu_then_answers_it() -> None:
    client = _client(dict(_MENU))

    listed = await IntegrationTools(client).ha_set_integration(domain="group")

    assert listed["flow_type"] == "menu"
    assert listed["menu_options"] == ["basic", "advanced"]
    client.submit_config_flow_step.assert_not_called()

    client.submit_config_flow_step.return_value = _form("advanced", _HOST)
    chosen = await IntegrationTools(client).ha_set_integration(
        domain="group", menu_choice="advanced"
    )

    client.submit_config_flow_step.assert_awaited_once_with(
        "f1", {"next_step_id": "advanced"}
    )
    assert chosen["step_id"] == "advanced"
    assert chosen["fields"][0]["name"] == "host"


@pytest.mark.asyncio
async def test_set_integration_of_a_setup_flow_that_aborts_gives_cores_text() -> None:
    client = _client(
        {"type": "abort", "flow_id": "f1", "reason": "single_instance_allowed"},
        {
            "component.sun.config.abort.single_instance_allowed": (
                "Already configured. Only a single configuration possible."
            )
        },
    )

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(domain="sun")

    body = _error(exc_info)
    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    assert body["reason_text"] == (
        "Already configured. Only a single configuration possible."
    )


@pytest.mark.asyncio
async def test_set_integration_of_a_progress_step_keeps_the_flow_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_entry_flow_progress, "PROGRESS_BUDGET_S", 0.0)
    client = _client(
        {
            "type": "progress",
            "flow_id": "f1",
            "step_id": "discover",
            "progress_action": "scanning",
        },
        {"component.demo.config.progress.scanning": "Scanning the network."},
    )

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(domain="demo")

    body = _error(exc_info)
    assert body["error"]["code"] == "TIMEOUT_OPERATION"
    assert body["flow_id"] == "f1"
    assert body["progress_action"] == "scanning"
    assert body["progress_action_text"] == "Scanning the network."
    client.abort_config_flow.assert_not_called()


@pytest.mark.asyncio
async def test_set_integration_warns_when_the_strings_cannot_be_read() -> None:
    client = _client(_form("user", _HOST))
    client.send_websocket_message.side_effect = RuntimeError("ws down")

    resp = await IntegrationTools(client).ha_set_integration(domain="demo")

    assert resp["fields"][0]["name"] == "host"
    assert any("strings for demo could not be read" in w for w in resp["warnings"])
