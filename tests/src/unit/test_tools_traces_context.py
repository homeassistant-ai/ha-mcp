"""Context-id lookup tests for ha_get_automation_traces."""

import json

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_traces import TraceTools

from .test_tools_traces import TestTraceFetchPooledClient, _make_pooled_client


class TestTraceContextLookup:
    """``ha_get_automation_traces`` resolves a context id through Core."""

    @pytest.mark.asyncio
    async def test_context_id_resolves_to_run(self):
        """A known context id fetches that run's detail."""
        detail_result = {
            "success": True,
            "result": {
                "run_id": "r1",
                "state": "stopped",
                "trace": {},
                "config": {"alias": "Test"},
                "context": {"id": "ctx-1"},
            },
        }
        context_result = {
            "success": True,
            "result": {
                "ctx-1": {
                    "run_id": "r1",
                    "domain": "automation",
                    "item_id": "uid_1",
                }
            },
        }
        client = _make_pooled_client(
            TestTraceFetchPooledClient._dispatch(
                detail_result=detail_result, context_result=context_result
            )
        )

        result = await TraceTools(client).ha_get_automation_traces(
            automation_id="automation.test", context_id="ctx-1"
        )

        assert result["success"] is True
        assert result["run_id"] == "r1"
        assert result["context_id"] == "ctx-1"
        sent_types = [
            call.args[0]["type"]
            for call in client.send_websocket_message.await_args_list
        ]
        assert "trace/contexts" in sent_types
        assert "trace/get" in sent_types

    @pytest.mark.asyncio
    async def test_unknown_context_id_is_not_found(self):
        """A context with no trace gets a not-found error, not an empty success."""
        client = _make_pooled_client(
            TestTraceFetchPooledClient._dispatch(
                context_result={"success": True, "result": {}}
            )
        )

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                automation_id="automation.test", context_id="missing"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "missing" in error["error"]["message"]

    @pytest.mark.asyncio
    async def test_run_id_and_context_id_are_mutually_exclusive(self):
        """Passing both selectors is rejected before any WebSocket call."""
        client = _make_pooled_client(TestTraceFetchPooledClient._dispatch())

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                automation_id="automation.test",
                run_id="r1",
                context_id="ctx-1",
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        client.send_websocket_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_context_id_is_rejected(self):
        """An empty context id is a validation error, not a missing trace."""
        client = _make_pooled_client(TestTraceFetchPooledClient._dispatch())

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                automation_id="automation.test", context_id="   "
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        client.send_websocket_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_context_lookup_transport_failure_is_classified(self):
        """A dead transport during trace/contexts reports CONNECTION_FAILED."""
        client = _make_pooled_client(
            TestTraceFetchPooledClient._dispatch(
                context_result={
                    "success": False,
                    "error": "Failed to connect to Home Assistant",
                }
            )
        )

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                automation_id="automation.test", context_id="ctx-1"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "CONNECTION_FAILED"

    @pytest.mark.asyncio
    async def test_context_lookup_command_failure_is_service_call_failed(self):
        """A non-connection trace/contexts failure keeps its command error."""
        client = _make_pooled_client(
            TestTraceFetchPooledClient._dispatch(
                context_result={"success": False, "error": "unknown_command"}
            )
        )

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                automation_id="automation.test", context_id="ctx-1"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "SERVICE_CALL_FAILED"
