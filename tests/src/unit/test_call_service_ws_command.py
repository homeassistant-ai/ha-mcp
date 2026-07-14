"""Unit tests for ha_call_service's ws_command escape hatch (issue #1839).

Covers the raw one-shot WebSocket-command path (``ServiceTools._call_ws_command``)
reached via ``ws_command=...`` on ``ha_call_service``, plus a couple of
service-mode regression checks to confirm the unchanged path still works when
``ws_command`` is omitted.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_service import ServiceTools


def _make_tools(send_websocket_message_return: dict | None = None) -> ServiceTools:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        return_value=send_websocket_message_return
    )
    device_tools = MagicMock()
    return ServiceTools(client, device_tools)


class TestWsCommandSuccess:
    async def test_success_dispatches_type_plus_data_and_echoes_parameters(self):
        tools = _make_tools({"success": True, "result": None})
        data = {"domain": "sun", "issue_id": "x", "ignore": True}

        result = await tools.ha_call_service(
            ws_command="repairs/ignore_issue", data=data
        )

        tools._client.send_websocket_message.assert_awaited_once_with(
            {
                "type": "repairs/ignore_issue",
                "domain": "sun",
                "issue_id": "x",
                "ignore": True,
            }
        )
        assert result["success"] is True
        assert result["ws_command"] == "repairs/ignore_issue"
        assert result["parameters"] == data
        assert result["result"] is None
        assert "repairs/ignore_issue" in result["message"]

    async def test_success_with_no_data_sends_type_only_and_treats_data_as_empty(self):
        tools = _make_tools({"success": True, "result": {"issues": []}})

        result = await tools.ha_call_service(ws_command="repairs/list_issues")

        tools._client.send_websocket_message.assert_awaited_once_with(
            {"type": "repairs/list_issues"}
        )
        assert result["success"] is True
        assert result["ws_command"] == "repairs/list_issues"
        # No data was passed in -> parameters echoes back None, not {}.
        assert result["parameters"] is None
        assert result["result"] == {"issues": []}


class TestWsCommandValidation:
    async def test_ws_command_with_domain_raises_not_both(self):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(
                ws_command="repairs/ignore_issue", domain="light"
            )

        assert "not both" in str(excinfo.value)
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_ws_command_with_service_raises_not_both(self):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(
                ws_command="repairs/ignore_issue", service="turn_on"
            )

        assert "not both" in str(excinfo.value)
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_empty_ws_command_raises_non_empty(self):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(ws_command="  ")

        assert "non-empty" in str(excinfo.value)
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_subscribe_events_raises_streaming_error(self):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(ws_command="subscribe_events")

        message = str(excinfo.value)
        assert "streaming/subscription" in message
        assert "ha_eval_template" in message
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_render_template_raises_streaming_error(self):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(ws_command="render_template")

        message = str(excinfo.value)
        assert "streaming/subscription" in message
        assert "ha_eval_template" in message
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_ha_mcp_tools_prefix_raises_reserved_namespace_error(self):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(ws_command="ha_mcp_tools/overview")

        assert "ha_mcp_tools" in str(excinfo.value)
        tools._client.send_websocket_message.assert_not_awaited()


class TestWsCommandBackendFailure:
    async def test_backend_failure_raises_service_call_failed_with_error_text(self):
        tools = _make_tools({"success": False, "error": "boom"})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(
                ws_command="repairs/ignore_issue",
                data={"domain": "sun", "issue_id": "x", "ignore": True},
            )

        assert "boom" in str(excinfo.value)


class TestServiceModeRegression:
    async def test_missing_service_raises_domain_and_service_required(self):
        tools = _make_tools()

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(domain="light")

        assert "domain and service are required" in str(excinfo.value)
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_happy_service_call_still_works_without_ws_command(self):
        client = MagicMock()
        client.call_service = AsyncMock(return_value=[])
        client.send_websocket_message = AsyncMock()
        device_tools = MagicMock()
        tools = ServiceTools(client, device_tools)

        result = await tools.ha_call_service(domain="light", service="turn_on")

        assert result["success"] is True
        assert result["domain"] == "light"
        assert result["service"] == "turn_on"
        client.call_service.assert_awaited_once()
        client.send_websocket_message.assert_not_awaited()
