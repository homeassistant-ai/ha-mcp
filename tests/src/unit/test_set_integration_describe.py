"""ha_set_integration(describe=True): a setup, options or subentry form (#2632).

The describes whose step can finish the flow (the setup flow's first step, and
any menu choice) live on the write tool; ha_get_integration describes only the
first options or subentry form and takes no ``menu_choice``.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools.tools_integrations import IntegrationTools

from .test_integration_schema import _HOST, _MENU, _client, _error, _form


@pytest.mark.asyncio
async def test_set_integration_describe_returns_the_setup_form() -> None:
    client = _client(_form("user", _HOST))

    resp = await IntegrationTools(client).ha_set_integration(
        domain="Demo ", describe=True
    )

    client.start_config_flow.assert_awaited_once_with("demo")
    client.abort_config_flow.assert_awaited_once_with("f1")
    assert resp["config_flow_schema"]["fields"][0]["name"] == "host"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        {"domain": "demo", "menu_choice": "light"},
        {"entry_id": "e1", "subentry_type": "conversation"},
        {"entry_id": "e1", "subentry_id": "sub1"},
        {"describe": True},
        {"domain": "demo", "describe": True, "config": {"host": "x"}},
        {"domain": "demo", "describe": True, "entry_id": "e1"},
        {"domain": "demo", "describe": True, "subentry_type": "conversation"},
        {"entry_id": "e1", "describe": True, "enabled": True},
        {"domain": "demo", "describe": True, "log_persistence": "permanent"},
    ],
    ids=[
        "menu-without-describe",
        "subentry-type-without-describe",
        "subentry-id-without-describe",
        "no-domain-or-entry",
        "with-config",
        "domain-and-entry",
        "subentry-with-domain",
        "entry-with-enabled",
        "with-log-persistence",
    ],
)
async def test_set_integration_describe_takes_one_flow_and_menu_choice_only(
    call: dict[str, Any],
) -> None:
    client = _client(_form("user", _HOST))

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(**call)

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    # log_persistence without log_level is refused before the describe.
    assert "describe=True" in error["message"] or "log_persistence" in call
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
        entry_id="e1", describe=True, menu_choice="advanced"
    )

    client.submit_options_flow_step.assert_awaited_once_with(
        "f1", {"next_step_id": "advanced"}
    )
    client.abort_options_flow.assert_awaited_once_with("f1")
    schema = resp["options_schema"]
    assert schema["menu_choice"] == "advanced"
    assert [(f["name"], f["description"]) for f in schema["fields"]] == [
        ("host", "Host")
    ]
    assert "entry" not in resp


@pytest.mark.asyncio
async def test_set_integration_describes_the_subentry_form_behind_a_menu() -> None:
    client = _entry_client(dict(_MENU))
    client.submit_config_subentry_flow_step.return_value = _form("advanced", _HOST)

    resp = await IntegrationTools(client).ha_set_integration(
        entry_id="e1",
        describe=True,
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
async def test_set_integration_options_describe_refuses_an_ignored_entry() -> None:
    client = _entry_client(dict(_MENU), source="ignore")

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(entry_id="e1", describe=True)

    assert "ignored" in _error(exc_info)["error"]["message"]
    client.start_options_flow.assert_not_called()


@pytest.mark.asyncio
async def test_set_integration_options_describe_fails_when_the_flow_does() -> None:
    client = _entry_client({})
    client.start_options_flow.side_effect = HomeAssistantAPIError(
        "API error: 400 - flow failed", status_code=400
    )

    with pytest.raises(ToolError):
        await IntegrationTools(client).ha_set_integration(entry_id="e1", describe=True)


def test_ha_get_integration_takes_no_menu_choice() -> None:
    params = inspect.signature(IntegrationTools.ha_get_integration).parameters
    assert "menu_choice" not in params


@pytest.mark.asyncio
async def test_options_describe_of_an_entry_without_options_starts_no_flow() -> None:
    client = _client(_form("init", _HOST))
    client.get_config_entry.return_value = {
        "entry_id": "e1",
        "domain": "sun",
        "supports_options": False,
    }

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(entry_id="e1", describe=True)

    body = _error(exc_info)
    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "has no options flow" in body["error"]["message"]
    client.start_options_flow.assert_not_called()


@pytest.mark.asyncio
async def test_set_integration_describe_whose_flow_cannot_start_is_no_missing_domain() -> (
    None
):
    # Core answers 400 for failed dependencies: the domain is installed.
    client = _client({})
    client.start_config_flow.side_effect = HomeAssistantAPIError(
        "API error: 400 - Failed dependencies mqtt",
        status_code=400,
    )

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(
            domain="tasmota", describe=True
        )

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "SERVICE_CALL_FAILED"
    assert "Failed dependencies mqtt" in error["message"]


@pytest.mark.asyncio
async def test_set_integration_describe_of_a_domain_without_a_flow_fails() -> None:
    client = _client({})
    client.start_config_flow.side_effect = HomeAssistantAPIError(
        "API error: 404 - Invalid handler specified",
        status_code=404,
        response_data={"message": "Invalid handler specified"},
    )

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(
            domain="no_such_domain", describe=True
        )

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "RESOURCE_NOT_FOUND"
    assert "no_such_domain" in error["message"]
    assert "Invalid handler specified" in error["message"]
