"""Unit tests for config subentries folded into existing tools."""

import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

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

    def capture_tool(**kwargs):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = capture_tool
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
    assert result["subentry_schema"]["data_schema"] == [{"name": "model"}]
    mock_client.start_config_subentry_flow.assert_awaited_once_with(
        "entry-1",
        "conversation",
        subentry_id=None,
        show_advanced_options=True,
    )
    mock_client.abort_config_subentry_flow.assert_awaited_once_with("flow-1")


async def test_get_integration_subentry_schema_abort_failure_warns(
    mock_client, caplog
):
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

    assert result["subentry_schema"]["data_schema"] == [{"name": "model"}]
    assert "Failed to abort config subentry introspection flow flow-1" in caplog.text


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


async def test_config_set_helper_creates_config_subentry(helper_tools, mock_client):
    mock_client.start_config_subentry_flow.return_value = {
        "flow_id": "flow-1",
        "type": "form",
        "step_id": "set_options",
        "data_schema": [{"name": "name"}, {"name": "model"}],
    }
    mock_client.submit_config_subentry_flow_step.return_value = {
        "type": "create_entry",
        "title": "Local Model",
    }

    result = await helper_tools["ha_config_set_helper"](
        helper_type="config_subentry",
        entry_id="entry-1",
        subentry_type="conversation",
        config={"name": "Local Model", "model": "gemma3:27b", "ignored": "drop"},
    )

    assert result["success"] is True
    assert result["operation"] == "created"
    mock_client.submit_config_subentry_flow_step.assert_awaited_once_with(
        "flow-1", {"name": "Local Model", "model": "gemma3:27b"}
    )


async def test_config_set_helper_subentry_preserves_flow_api_error_context(
    helper_tools, mock_client
):
    mock_client.start_config_subentry_flow.return_value = {
        "flow_id": "flow-1",
        "type": "form",
        "step_id": "set_options",
        "data_schema": [{"name": "name"}, {"name": "model"}],
    }
    mock_client.submit_config_subentry_flow_step.side_effect = HomeAssistantAPIError(
        "API error: 400 - validation failed",
        status_code=400,
        response_data={
            "errors": {"model": "invalid_model"},
            "message": "User input malformed",
        },
    )

    with pytest.raises(ToolError) as exc_info:
        await helper_tools["ha_config_set_helper"](
            helper_type="config_subentry",
            entry_id="entry-1",
            subentry_type="conversation",
            config={"name": "Local Model", "model": "missing-model"},
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "model: invalid_model" in error_data["error"]["message"]
    assert error_data["field_errors"] == {"model": "invalid_model"}
    assert error_data["data_schema"] == [
        {"name": "name"},
        {"name": "model"},
    ]
    assert error_data["submitted_keys"] == ["model", "name"]
    mock_client.abort_config_subentry_flow.assert_awaited_once_with("flow-1")


async def test_config_set_helper_reconfigures_config_subentry(
    helper_tools, mock_client
):
    mock_client.start_config_subentry_flow.return_value = {
        "flow_id": "flow-1",
        "type": "form",
        "step_id": "set_options",
        "data_schema": [{"name": "model"}],
    }
    mock_client.submit_config_subentry_flow_step.return_value = {
        "type": "abort",
        "reason": "reconfigure_successful",
    }

    result = await helper_tools["ha_config_set_helper"](
        helper_type="config_subentry",
        entry_id="entry-1",
        subentry_type="conversation",
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
        await helper_tools["ha_config_set_helper"](
            helper_type="config_subentry",
            entry_id="entry-1",
            subentry_type="conversation",
            action="update",
            config={"model": "gemma3:27b"},
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "subentry_id" in error_data["error"]["message"]
    mock_client.start_config_subentry_flow.assert_not_awaited()


async def test_delete_helpers_integrations_deletes_config_subentry(mock_client):
    mock_client.delete_config_subentry.return_value = {
        "success": True,
        "result": {"subentry_id": "subentry-1"},
    }

    result = await IntegrationTools(mock_client).ha_delete_helpers_integrations(
        target="entry-1",
        helper_type="config_subentry",
        subentry_id="subentry-1",
        confirm=True,
    )

    assert result["success"] is True
    assert result["method"] == "config_subentry_delete"
    assert result["subentry_id"] == "subentry-1"
    mock_client.delete_config_subentry.assert_awaited_once_with(
        "entry-1", "subentry-1"
    )


async def test_delete_helpers_integrations_requires_subentry_id(mock_client):
    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(mock_client).ha_delete_helpers_integrations(
            target="entry-1",
            helper_type="config_subentry",
            confirm=True,
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "subentry_id" in error_data["error"]["message"]
