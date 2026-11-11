"""Unit tests for config subentries folded into existing tools."""

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools.tools_config_helpers import register_config_helper_tools
from ha_mcp.tools.tools_integrations import IntegrationTools


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.get_config_entry = AsyncMock(
        return_value={
            "entry_id": "entry-1",
            "domain": "ollama",
            "title": "Ollama",
            "supports_options": False,
        }
    )
    client.list_config_subentries = AsyncMock()
    client.start_config_subentry_flow = AsyncMock()
    client.submit_config_subentry_flow_step = AsyncMock()
    client.abort_config_subentry_flow = AsyncMock()
    client.delete_config_subentry = AsyncMock()
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": {"levels": {}}}
    )
    return client


@pytest.fixture
def helper_tools(mock_client):
    registered: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        name = (
            method.__fastmcp__.name
            if hasattr(method, "__fastmcp__")
            else method.__name__
        )
        registered[name] = method

    mock_mcp = MagicMock()
    mock_mcp.add_tool = capture_add_tool
    register_config_helper_tools(mock_mcp, mock_client)
    return registered


async def test_get_integration_includes_subentries(mock_client):
    mock_client.list_config_subentries.return_value = {
        "success": True,
        "result": [
            {
                "subentry_id": "subentry-1",
                "subentry_type": "conversation",
                "title": "Local Model",
            }
        ],
    }

    result = await IntegrationTools(mock_client).ha_get_integration(
        entry_id="entry-1",
        include_subentries=True,
    )

    assert result["success"] is True
    assert result["subentry_count"] == 1
    assert result["subentries"][0]["subentry_id"] == "subentry-1"
    mock_client.list_config_subentries.assert_awaited_once_with("entry-1")


async def test_get_integration_includes_subentry_schema_and_aborts(mock_client):
    mock_client.list_config_subentries.return_value = {"success": True, "result": []}
    mock_client.start_config_subentry_flow.return_value = {
        "flow_id": "flow-1",
        "type": "form",
        "step_id": "set_options",
        "data_schema": [{"name": "model"}],
    }

    result = await IntegrationTools(mock_client).ha_get_integration(
        entry_id="entry-1",
        include_subentry_schema=True,
        subentry_type="conversation",
        show_advanced_options=True,
    )

    assert result["subentry_schema"]["flow_type"] == "form"
    assert result["subentry_schema"]["fields"] == [{"name": "model"}]
    mock_client.start_config_subentry_flow.assert_awaited_once_with(
        "entry-1",
        "conversation",
        subentry_id=None,
        show_advanced_options=True,
    )
    mock_client.abort_config_subentry_flow.assert_awaited_once_with("flow-1")


async def test_get_integration_includes_subentry_menu_schema_and_aborts(mock_client):
    mock_client.list_config_subentries.return_value = {"success": True, "result": []}
    mock_client.start_config_subentry_flow.return_value = {
        "flow_id": "flow-1",
        "type": "menu",
        "step_id": "user",
        "menu_options": ["conversation", "ai_task_data"],
    }

    result = await IntegrationTools(mock_client).ha_get_integration(
        entry_id="entry-1",
        include_subentry_schema=True,
        subentry_type="conversation",
    )

    assert result["subentry_schema"]["flow_type"] == "menu"
    assert result["subentry_schema"]["menu_options"] == [
        "conversation",
        "ai_task_data",
    ]
    mock_client.abort_config_subentry_flow.assert_awaited_once_with("flow-1")


async def test_get_integration_subentry_schema_abort_failure_warns(mock_client, caplog):
    mock_client.list_config_subentries.return_value = {"success": True, "result": []}
    mock_client.start_config_subentry_flow.return_value = {
        "flow_id": "flow-1",
        "type": "form",
        "step_id": "set_options",
        "data_schema": [{"name": "model"}],
    }
    mock_client.abort_config_subentry_flow.side_effect = TimeoutError("slow abort")

    with caplog.at_level(logging.WARNING):
        result = await IntegrationTools(mock_client).ha_get_integration(
            entry_id="entry-1",
            include_subentry_schema=True,
            subentry_type="conversation",
        )

    assert result["subentry_schema"]["fields"] == [{"name": "model"}]
    assert "flow flow-1 abort failed: slow abort" in caplog.text


async def test_get_integration_schema_requires_subentry_type(mock_client):
    mock_client.list_config_subentries.return_value = {"success": True, "result": []}

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(mock_client).ha_get_integration(
            entry_id="entry-1",
            include_subentry_schema=True,
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "subentry_type" in error_data["error"]["message"]


async def test_get_integration_subentries_failure_response(mock_client):
    mock_client.list_config_subentries.return_value = {
        "success": False,
        "error": {"message": "unknown entry"},
    }

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(mock_client).ha_get_integration(
            entry_id="entry-1",
            include_subentries=True,
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "unknown entry" in error_data["error"]["message"]


async def test_get_integration_subentries_rejects_non_list_result(mock_client):
    mock_client.list_config_subentries.return_value = {
        "success": True,
        "result": {"subentry_id": "not-a-list"},
    }

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(mock_client).ha_get_integration(
            entry_id="entry-1",
            include_subentries=True,
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "Unexpected config subentry list response" in error_data["error"]["message"]


async def test_get_integration_list_mode_warning_names_only_supplied_keys(mock_client):
    mock_client._request = AsyncMock(return_value=[])

    result = await IntegrationTools(mock_client).ha_get_integration(
        include_subentries=True,
        show_advanced_options=True,
    )

    warnings = result.get("warnings", [])
    assert len(warnings) == 1
    assert "include_subentries" in warnings[0]
    assert "show_advanced_options" in warnings[0]
    assert "include_diagnostics" not in warnings[0]


def _form(step_id: str, *names: str) -> dict[str, Any]:
    return {
        "flow_id": "flow-1",
        "type": "form",
        "step_id": step_id,
        "data_schema": [{"name": name} for name in names],
    }


async def _set_subentry(helper_tools: dict[str, Any], **kwargs: Any) -> Any:
    return await helper_tools["ha_config_set_helper"](
        helper_type="config_subentry",
        entry_id="entry-1",
        subentry_type="conversation",
        **kwargs,
    )


async def test_config_subentry_create_returns_the_new_subentry_id(
    helper_tools, mock_client
):
    """Core's create_entry result carries no subentry_id; the tool reports it."""
    listed = [{"subentry_id": "old", "subentry_type": "conversation"}]
    mock_client.list_config_subentries.side_effect = [
        {"success": True, "result": listed},
        {"success": True, "result": [*listed, {"subentry_id": "new"}]},
    ]
    mock_client.start_config_subentry_flow.return_value = _form("set_options", "model")
    mock_client.submit_config_subentry_flow_step.return_value = {"type": "create_entry"}

    result = await _set_subentry(
        helper_tools, config={"model": "m"}, show_advanced_options=True
    )

    assert result["operation"] == "created"
    assert result["subentry_id"] == "new"
    # Only a Home Assistant older than 2026.6 reads the flag.
    mock_client.start_config_subentry_flow.assert_awaited_once_with(
        "entry-1", "conversation", subentry_id=None, show_advanced_options=True
    )


async def test_concurrent_creates_each_return_their_subentry_id(mock_client):
    from ha_mcp.tools.config_helpers.flow import _handle_set_config_subentry

    ids: list[str] = []

    async def listing(entry_id):
        await asyncio.sleep(0)
        return {"success": True, "result": [{"subentry_id": i} for i in ids]}

    async def submit(flow_id, data):
        await asyncio.sleep(0)
        ids.append(f"sub-{data['model']}")
        return {"type": "create_entry"}

    flows = iter(["a", "b"])
    mock_client.list_config_subentries = AsyncMock(side_effect=listing)
    mock_client.start_config_subentry_flow = AsyncMock(
        side_effect=lambda *a, **k: {**_form("user", "model"), "flow_id": next(flows)}
    )
    mock_client.submit_config_subentry_flow_step = AsyncMock(side_effect=submit)

    first, second = await asyncio.gather(
        *(
            _handle_set_config_subentry(
                mock_client,
                None,
                "entry-1",
                "conversation",
                None,
                {"model": model},
                False,
            )
            for model in ("m", "n")
        )
    )

    assert (first["subentry_id"], second["subentry_id"]) == ("sub-m", "sub-n")


async def test_a_later_subentry_form_is_answered_in_its_own_call(
    helper_tools, mock_client
):
    second = {**_form("second", "model"), "flow_id": "flow-1"}
    mock_client.start_config_subentry_flow.return_value = _form("first", "name")
    mock_client.submit_config_subentry_flow_step.side_effect = [
        second,
        {"type": "create_entry", "title": "Local Model"},
    ]
    mock_client._request = AsyncMock(return_value=second)

    shown = await _set_subentry(helper_tools, config={"name": "Local Model"})

    assert shown["flow_type"] == "form"
    assert shown["step_id"] == "second"

    result = await _set_subentry(
        helper_tools, flow_id=shown["flow_id"], config={"model": "gemma3:27b"}
    )

    assert result["operation"] == "created"
    mock_client._request.assert_awaited_once_with(
        "GET", "/config/config_entries/subentries/flow/flow-1"
    )
    assert mock_client.submit_config_subentry_flow_step.await_args_list == [
        call("flow-1", {"name": "Local Model"}),
        call("flow-1", {"model": "gemma3:27b"}),
    ]


async def test_a_subentry_menu_is_answered_with_menu_choice(helper_tools, mock_client):
    mock_client.start_config_subentry_flow.return_value = {
        "flow_id": "flow-1",
        "type": "menu",
        "step_id": "user",
        "menu_options": ["conversation"],
    }
    mock_client.submit_config_subentry_flow_step.side_effect = [
        _form("set_options", "model"),
        {"type": "create_entry", "title": "Local Model"},
    ]

    result = await _set_subentry(
        helper_tools, menu_choice="conversation", config={"model": "gemma3:27b"}
    )

    assert result["operation"] == "created"
    assert mock_client.submit_config_subentry_flow_step.await_args_list == [
        call("flow-1", {"next_step_id": "conversation"}),
        call("flow-1", {"model": "gemma3:27b"}),
    ]


async def test_a_rejected_subentry_submit_shows_the_form_and_keeps_the_flow(
    helper_tools, mock_client
):
    mock_client.start_config_subentry_flow.return_value = _form(
        "set_options", "name", "model"
    )
    mock_client.submit_config_subentry_flow_step.side_effect = HomeAssistantAPIError(
        "API error: 400 - validation failed",
        status_code=400,
        response_data={
            "errors": {"model": "invalid_model"},
            "message": "User input malformed",
        },
    )

    with pytest.raises(ToolError) as exc_info:
        await _set_subentry(
            helper_tools, config={"name": "Local Model", "model": "missing-model"}
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "model: invalid_model" in error_data["error"]["message"]
    assert error_data["field_errors"] == {"model": "invalid_model"}
    assert error_data["data_schema"] == [{"name": "name"}, {"name": "model"}]
    assert error_data["submitted_keys"] == ["model", "name"]
    assert error_data["flow_id"] == "flow-1"
    mock_client.abort_config_subentry_flow.assert_not_awaited()


@pytest.mark.parametrize("reason", ["reconfigure_successful", "reauth_successful"])
async def test_a_subentry_reconfigure_ends_in_cores_success_abort(
    helper_tools, mock_client, reason
):
    mock_client.start_config_subentry_flow.return_value = _form("set_options", "model")
    mock_client.submit_config_subentry_flow_step.return_value = {
        "type": "abort",
        "reason": reason,
    }

    result = await _set_subentry(
        helper_tools,
        subentry_id="subentry-1",
        action="update",
        config={"model": "gemma3:27b"},
    )

    assert result["success"] is True
    assert result["operation"] == "reconfigured"
    assert result["subentry_id"] == "subentry-1"


async def test_config_set_helper_rejects_update_without_subentry_id(
    helper_tools, mock_client
):
    with pytest.raises(ToolError) as exc_info:
        await _set_subentry(
            helper_tools, action="update", config={"model": "gemma3:27b"}
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "subentry_id" in error_data["error"]["message"]
    mock_client.start_config_subentry_flow.assert_not_awaited()


async def test_config_set_helper_rejects_invalid_config_json(helper_tools, mock_client):
    with pytest.raises(ToolError) as exc_info:
        await _set_subentry(helper_tools, config="{not-json")

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert error_data["parameter"] == "config"
    mock_client.start_config_subentry_flow.assert_not_awaited()


async def test_an_open_subentry_flow_is_ended_with_abort(
    helper_tools, mock_client, opened_flows
):
    opened_flows["flow-1"] = ("subentry", "entry-1/conversation/")

    result = await _set_subentry(helper_tools, flow_id="flow-1", abort=True)

    assert result["aborted"] is True
    mock_client.abort_config_subentry_flow.assert_awaited_once_with("flow-1")
    assert "flow-1" not in opened_flows


async def test_remove_helpers_integrations_deletes_config_subentry(mock_client):
    mock_client.delete_config_subentry.return_value = {
        "success": True,
        "result": {"subentry_id": "subentry-1"},
    }

    result = await IntegrationTools(mock_client).ha_remove_helpers_integrations(
        target="entry-1",
        helper_type="config_subentry",
        subentry_id="subentry-1",
        confirm=True,
    )

    assert result["success"] is True
    assert result["method"] == "config_subentry_delete"
    assert result["subentry_id"] == "subentry-1"
    mock_client.delete_config_subentry.assert_awaited_once_with("entry-1", "subentry-1")


async def test_remove_helpers_integrations_config_subentry_requires_confirm(
    mock_client,
):
    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(mock_client).ha_remove_helpers_integrations(
            target="entry-1",
            helper_type="config_subentry",
            subentry_id="subentry-1",
            confirm=False,
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "Deletion not confirmed" in error_data["error"]["message"]
    mock_client.delete_config_subentry.assert_not_awaited()


async def test_remove_helpers_integrations_requires_subentry_id(mock_client):
    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(mock_client).ha_remove_helpers_integrations(
            target="entry-1",
            helper_type="config_subentry",
            confirm=True,
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "subentry_id" in error_data["error"]["message"]


async def test_remove_helpers_integrations_subentry_not_found_raises(
    mock_client,
):
    """Path 4: HA returns code="not_found" when the subentry is already
    absent → raises RESOURCE_NOT_FOUND. Silent success would mask a
    typo'd subentry_id (or wrong parent entry_id) until the user noticed
    nothing was removed."""
    mock_client.delete_config_subentry.return_value = {
        "success": False,
        "error": {"code": "not_found", "message": "Subentry not found"},
    }

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(mock_client).ha_remove_helpers_integrations(
            target="entry-1",
            helper_type="config_subentry",
            subentry_id="ghost-subentry",
            confirm=True,
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["success"] is False
    assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert "ghost-subentry" in error_data["error"]["message"]
    # Pin the diagnostic-hint wording (same rationale as Paths 1/3).
    assert "May indicate" in error_data["error"]["message"]
    assert "ha_get_integration" in error_data["error"]["message"]
    assert "already_deleted" not in json.dumps(error_data)


async def test_remove_helpers_integrations_subentry_other_error_surfaces_service_call_failed(
    mock_client,
):
    """Path 4 negative case: a non-not_found error code (e.g. HA returns
    a permission, validation, or unknown-error code) must NOT silently
    classify as idempotent success. SERVICE_CALL_FAILED is the expected
    surface, with the underlying error message preserved.
    """
    mock_client.delete_config_subentry.return_value = {
        "success": False,
        "error": {
            "code": "permission_denied",
            "message": "Insufficient permissions to delete subentry",
        },
    }

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(mock_client).ha_remove_helpers_integrations(
            target="entry-1",
            helper_type="config_subentry",
            subentry_id="subentry-1",
            confirm=True,
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "Insufficient permissions" in error_data["error"]["message"]
    # Symmetric with the string-form sibling test below: a regression
    # that re-routes a non-not_found error into the already_deleted
    # success shape must not pass this test.
    assert "already_deleted" not in json.dumps(error_data)


async def test_remove_helpers_integrations_subentry_string_error_surfaces_service_call_failed(
    mock_client,
):
    """Path 4 narrow stays narrow: the idempotent ``not_found`` branch
    only fires when ``error`` is a dict carrying ``code="not_found"``.
    A string-form ``"error": "not found"`` (no structured code) must
    NOT classify as idempotent — it surfaces as SERVICE_CALL_FAILED.
    Without this pin, a regression that loosened to substring-matching
    against ``error_msg`` would pass CI silently.
    """
    mock_client.delete_config_subentry.return_value = {
        "success": False,
        "error": "Subentry not found",
    }

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(mock_client).ha_remove_helpers_integrations(
            target="entry-1",
            helper_type="config_subentry",
            subentry_id="subentry-1",
            confirm=True,
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "already_deleted" not in json.dumps(error_data)
