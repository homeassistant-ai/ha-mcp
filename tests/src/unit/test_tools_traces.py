"""Unit tests for tools_traces module."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_traces import (
    TraceTools,
    _format_trace_list,
    _resolve_trace_item_id,
)


class TestFormatTraceList:
    """Test _format_trace_list function."""

    def test_empty_traces_without_diagnostics(self):
        """Empty traces without diagnostics return basic structure."""
        result = _format_trace_list("automation.test", [], 10)

        assert result["success"] is True
        assert result["entity_id"] == "automation.test"
        assert result["trace_count"] == 0
        assert result["total_available"] == 0
        assert result["traces"] == []
        assert "diagnostics" not in result

    def test_empty_traces_with_diagnostics(self):
        """Empty traces with diagnostics include diagnostics in result."""
        diagnostics = {
            "entity_exists": True,
            "entity_enabled": True,
            "trace_storage_enabled": True,
            "last_triggered": "2025-11-30T15:00:00Z",
            "suggestion": "Traces may have been cleared.",
        }

        result = _format_trace_list("automation.test", [], 10, diagnostics)

        assert result["success"] is True
        assert result["trace_count"] == 0
        assert "diagnostics" in result
        assert result["diagnostics"]["entity_exists"] is True
        assert result["diagnostics"]["last_triggered"] == "2025-11-30T15:00:00Z"

    def test_non_empty_traces_ignore_diagnostics(self):
        """Non-empty traces do not include diagnostics even if provided."""
        traces = [
            {
                "run_id": "123.456",
                "timestamp": "2025-11-30T15:00:00Z",
                "state": "stopped",
                "trigger": "time pattern",
            }
        ]
        diagnostics = {
            "entity_exists": True,
            "entity_enabled": True,
            "trace_storage_enabled": True,
            "last_triggered": "2025-11-30T15:00:00Z",
            "suggestion": "Test suggestion",
        }

        result = _format_trace_list("automation.test", traces, 10, diagnostics)

        assert result["success"] is True
        assert result["trace_count"] == 1
        assert "diagnostics" not in result

    def test_traces_limited_by_limit_param(self):
        """Traces are limited by the limit parameter."""
        traces = [
            {
                "run_id": f"{i}.0",
                "timestamp": f"2025-11-30T15:0{i}:00Z",
                "state": "stopped",
            }
            for i in range(5)
        ]

        result = _format_trace_list("automation.test", traces, 3)

        assert result["trace_count"] == 3
        assert result["total_available"] == 5

    def test_returns_newest_traces_when_total_exceeds_limit(self):
        """When traces exceed limit, return the newest N (not the oldest).

        HA's trace/list returns traces in chronological order (oldest first).
        Slicing [:limit] would return the oldest N, leaving recent traces
        unreachable when stored_traces > limit.
        """
        traces = [
            {
                "run_id": f"run_{i}",
                "timestamp": f"2025-11-30T15:0{i}:00Z",
                "state": "stopped",
            }
            for i in range(5)
        ]

        result = _format_trace_list("automation.test", traces, 2)

        assert result["trace_count"] == 2
        run_ids = [t["run_id"] for t in result["traces"]]
        assert "run_4" in run_ids
        assert "run_3" in run_ids
        assert "run_0" not in run_ids
        assert "run_1" not in run_ids

    def test_returned_traces_are_newest_first(self):
        """Returned traces are ordered newest-first for user convenience."""
        traces = [
            {
                "run_id": f"run_{i}",
                "timestamp": f"2025-11-30T15:0{i}:00Z",
                "state": "stopped",
            }
            for i in range(5)
        ]

        result = _format_trace_list("automation.test", traces, 3)

        run_ids = [t["run_id"] for t in result["traces"]]
        assert run_ids == ["run_4", "run_3", "run_2"]

    def test_order_oldest_returns_oldest_first(self):
        """order='oldest' returns the oldest N traces in chronological order."""
        traces = [
            {
                "run_id": f"run_{i}",
                "timestamp": f"2025-11-30T15:0{i}:00Z",
                "state": "stopped",
            }
            for i in range(5)
        ]

        result = _format_trace_list("automation.test", traces, 3, order="oldest")

        run_ids = [t["run_id"] for t in result["traces"]]
        assert run_ids == ["run_0", "run_1", "run_2"]
        assert result["order"] == "oldest"

    def test_offset_pages_through_newest_first(self):
        """offset skips past the most-recent traces when order='newest'."""
        traces = [
            {
                "run_id": f"run_{i}",
                "timestamp": f"2025-11-30T15:0{i}:00Z",
                "state": "stopped",
            }
            for i in range(5)
        ]

        result = _format_trace_list("automation.test", traces, 2, offset=2)

        run_ids = [t["run_id"] for t in result["traces"]]
        assert run_ids == ["run_2", "run_1"]
        assert result["offset"] == 2

    def test_offset_pages_through_oldest_first(self):
        """offset skips past the earliest traces when order='oldest'."""
        traces = [
            {
                "run_id": f"run_{i}",
                "timestamp": f"2025-11-30T15:0{i}:00Z",
                "state": "stopped",
            }
            for i in range(5)
        ]

        result = _format_trace_list(
            "automation.test", traces, 2, offset=2, order="oldest"
        )

        run_ids = [t["run_id"] for t in result["traces"]]
        assert run_ids == ["run_2", "run_3"]

    def test_has_more_true_when_more_traces_remain(self):
        """has_more is True when the requested page does not cover the buffer."""
        traces = [
            {
                "run_id": f"run_{i}",
                "timestamp": f"2025-11-30T15:0{i}:00Z",
                "state": "stopped",
            }
            for i in range(5)
        ]

        result = _format_trace_list("automation.test", traces, 2)

        assert result["has_more"] is True

    def test_has_more_false_when_buffer_exhausted(self):
        """has_more is False when offset+returned reaches total."""
        traces = [
            {
                "run_id": f"run_{i}",
                "timestamp": f"2025-11-30T15:0{i}:00Z",
                "state": "stopped",
            }
            for i in range(3)
        ]

        result = _format_trace_list("automation.test", traces, 10)

        assert result["has_more"] is False

    def test_offset_equal_to_total_returns_empty(self):
        """offset == total returns no traces and has_more=False (boundary)."""
        traces = [
            {
                "run_id": f"run_{i}",
                "timestamp": f"2025-11-30T15:0{i}:00Z",
                "state": "stopped",
            }
            for i in range(3)
        ]

        result = _format_trace_list("automation.test", traces, 10, offset=3)

        assert result["traces"] == []
        assert result["trace_count"] == 0
        assert result["has_more"] is False

    def test_offset_beyond_total_returns_empty(self):
        """offset >= total returns no traces and has_more=False."""
        traces = [
            {
                "run_id": f"run_{i}",
                "timestamp": f"2025-11-30T15:0{i}:00Z",
                "state": "stopped",
            }
            for i in range(3)
        ]

        result = _format_trace_list("automation.test", traces, 10, offset=10)

        assert result["traces"] == []
        assert result["trace_count"] == 0
        assert result["total_available"] == 3
        assert result["has_more"] is False

    def test_trace_with_error_included(self):
        """Traces with errors include the error field."""
        traces = [
            {
                "run_id": "123.456",
                "timestamp": "2025-11-30T15:00:00Z",
                "state": "error",
                "error": "Service not found",
            }
        ]

        result = _format_trace_list("automation.test", traces, 10)

        assert result["traces"][0]["error"] == "Service not found"

    def test_script_execution_field_included(self):
        """Script traces include script_execution as execution field."""
        traces = [
            {
                "run_id": "123.456",
                "timestamp": "2025-11-30T15:00:00Z",
                "state": "stopped",
                "script_execution": "finished",
            }
        ]

        result = _format_trace_list("script.test", traces, 10)

        assert result["traces"][0]["execution"] == "finished"

    def test_list_preserves_core_short_dict_fields(self):
        """Every field Core's trace/list returns reaches the caller."""
        core_trace = {
            "last_step": "action/0",
            "run_id": "r1",
            "state": "stopped",
            "script_execution": "finished",
            "timestamp": {
                "start": "2026-11-08T16:00:04.319026+00:00",
                "finish": "2026-11-08T16:00:04.319959+00:00",
            },
            "domain": "automation",
            "item_id": "trace_capture",
            "trigger": None,
            "not_triggered": True,
        }

        result = _format_trace_list("automation.test", [core_trace], 10)

        assert result["traces"][0] == {**core_trace, "execution": "finished"}

    def test_list_orders_mixed_not_triggered_by_core_start_time(self):
        """Run and not-triggered traces page in Core's timestamp order."""
        traces = [
            {
                "run_id": "run_middle",
                "state": "stopped",
                "timestamp": {
                    "start": "2026-11-08T16:00:02+00:00",
                    "finish": "2026-11-08T16:00:02.100000+00:00",
                },
            },
            {
                "run_id": "not_triggered_newest",
                "state": "stopped",
                "not_triggered": True,
                "timestamp": {
                    "start": "2026-11-08T16:00:04+00:00",
                    "finish": "2026-11-08T16:00:04.100000+00:00",
                },
            },
            {
                "run_id": "run_oldest",
                "state": "stopped",
                "timestamp": {
                    "start": "2026-11-08T16:00:01+00:00",
                    "finish": "2026-11-08T16:00:01.100000+00:00",
                },
            },
        ]

        newest = _format_trace_list("automation.test", traces, 2)
        oldest = _format_trace_list("automation.test", traces, 2, order="oldest")

        assert [trace["run_id"] for trace in newest["traces"]] == [
            "not_triggered_newest",
            "run_middle",
        ]
        assert [trace["run_id"] for trace in oldest["traces"]] == [
            "run_oldest",
            "run_middle",
        ]

    def test_list_orders_traces_with_finish_only_and_malformed_timestamps(self):
        """A missing start falls back to finish, then to an empty sort key."""
        traces = [
            {
                "run_id": "finish_only",
                "state": "stopped",
                "timestamp": {"finish": "2026-11-08T16:00:02+00:00"},
            },
            {
                "run_id": "missing_both",
                "state": "stopped",
                "timestamp": {},
            },
            {
                "run_id": "start_only",
                "state": "stopped",
                "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
            },
        ]

        newest = _format_trace_list("automation.test", traces, 3)

        assert [trace["run_id"] for trace in newest["traces"]] == [
            "finish_only",
            "start_only",
            "missing_both",
        ]

    def test_list_skips_non_dict_trace_entries(self):
        """A malformed Core row is skipped instead of crashing the list."""
        result = _format_trace_list(
            "automation.test",
            [
                {"run_id": "r1", "state": "stopped", "timestamp": {}},
                "not-a-trace",
            ],
            10,
        )

        assert [trace["run_id"] for trace in result["traces"]] == ["r1"]
        assert result["trace_count"] == 1

    def test_list_handles_non_string_timestamp(self):
        """A non-string timestamp sorts as empty instead of crashing."""
        result = _format_trace_list(
            "automation.test",
            [{"run_id": "r1", "state": "stopped", "timestamp": 123}],
            10,
        )

        assert [trace["run_id"] for trace in result["traces"]] == ["r1"]


def _make_pooled_client(dispatch):
    """Build a mock REST client whose ``send_websocket_message`` dispatches on
    message type via ``dispatch`` (``(type, message) -> response dict``).

    Mirrors the pooled client tools_traces now drives (issue #1813 item #5):
    a single ``send_websocket_message`` owns every trace WS command, so there is
    no dedicated connect/auth/disconnect per call.
    """
    client = MagicMock()
    client.base_url = "http://test.local:8123"
    client.token = "test-token"
    client.verify_ssl = True

    async def _send(message):
        return dispatch(message["type"], message)

    client.send_websocket_message = AsyncMock(side_effect=_send)
    client.get_entity_state = AsyncMock(return_value={"state": "on", "attributes": {}})
    return client


def _dispatch(*, list_result=None, detail_result=None, context_result=None):
    """Return a pooled-client dispatcher for trace commands."""

    def _d(mtype, message):
        if mtype == "config/entity_registry/get":
            return {"success": True, "result": {"unique_id": "uid_1"}}
        if mtype == "trace/list":
            return (
                list_result
                if list_result is not None
                else {"success": True, "result": []}
            )
        if mtype == "trace/get":
            return detail_result
        if mtype == "trace/contexts":
            return context_result
        if mtype in ("automation/config", "script/config"):
            return {"success": True, "result": {}}
        return {"success": False, "error": f"unexpected {mtype}"}

    return _d


class TestResolveTraceItemIdPooled:
    """``_resolve_trace_item_id`` drives the pooled ``send_websocket_message``
    (issue #1813 item #5) and keeps its best-effort fall-back semantics."""

    @pytest.mark.asyncio
    async def test_resolves_unique_id_via_pooled_client(self):
        client = _make_pooled_client(
            lambda t, m: {"success": True, "result": {"unique_id": "uid_42"}}
        )

        item_id = await _resolve_trace_item_id(
            client, "automation.test", "fallback_obj"
        )

        assert item_id == "uid_42"
        client.send_websocket_message.assert_awaited_once_with(
            {"type": "config/entity_registry/get", "entity_id": "automation.test"}
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_object_id_on_pooled_failure(self):
        """A pooled ``{"success": False}`` (transport drop collapsed to a dict
        rather than raised) falls back to the object_id — the subsequent trace
        fetch surfaces the real error."""
        client = _make_pooled_client(
            lambda t, m: {"success": False, "error": "failed to connect"}
        )

        item_id = await _resolve_trace_item_id(
            client, "automation.test", "fallback_obj"
        )

        assert item_id == "fallback_obj"

    @pytest.mark.asyncio
    async def test_falls_back_to_object_id_when_pooled_call_raises(self):
        """The pooled call may RAISE rather than collapse to ``{success:False}``;
        the best-effort resolve still falls back to object_id — matching the
        helper's comment that it works whether the pooled client collapses OR
        raises."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            side_effect=RuntimeError("pooled connect exploded")
        )

        item_id = await _resolve_trace_item_id(
            client, "automation.test", "fallback_obj"
        )

        assert item_id == "fallback_obj"


class TestTraceToolRegistration:
    def test_register_trace_tools_registers_methods(self):
        """The module registers its tool class on the server."""
        mcp = MagicMock()
        client = MagicMock()
        with patch("ha_mcp.tools.tools_traces.register_tool_methods") as register:
            from ha_mcp.tools.tools_traces import register_trace_tools

            register_trace_tools(mcp, client)

        register.assert_called_once()
        assert register.call_args.args[0] is mcp
        assert isinstance(register.call_args.args[1], TraceTools)


class TestTraceFetchPooledClient:
    """`ha_get_automation_traces` drives the pooled client end-to-end (issue
    #1813 item #5): no dedicated socket, structured errors preserved."""

    @pytest.mark.asyncio
    async def test_script_entity_id_uses_script_domain(self):
        """A script entity id drives trace/list in the script domain."""
        list_result = {"success": True, "result": []}
        client = _make_pooled_client(_dispatch(list_result=list_result))

        result = await TraceTools(client).ha_get_automation_traces(
            entity_id="script.test"
        )

        assert result["success"] is True
        trace_list_calls = [
            call.args[0]
            for call in client.send_websocket_message.await_args_list
            if call.args[0]["type"] == "trace/list"
        ]
        assert trace_list_calls[0]["domain"] == "script"

    @pytest.mark.asyncio
    async def test_invalid_entity_prefix_is_rejected(self):
        """A non-automation/script entity is a validation error before reads."""
        client = _make_pooled_client(_dispatch())

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(entity_id="sensor.test")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        client.send_websocket_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_list_gathers_diagnostics(self):
        """An empty list path returns diagnostics from the item config."""
        client = _make_pooled_client(
            _dispatch(list_result={"success": True, "result": []})
        )

        result = await TraceTools(client).ha_get_automation_traces(
            entity_id="automation.test"
        )

        assert result["trace_count"] == 0
        assert "diagnostics" in result
        assert result["diagnostics"]["entity_exists"] is True

    @pytest.mark.asyncio
    async def test_unexpected_exception_maps_to_structured_error(self):
        """An unexpected failure is classified, not leaked as a raw exception."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            side_effect=RuntimeError("pooled connect exploded")
        )

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test"
            )

        error = json.loads(str(exc_info.value))
        assert error["success"] is False
        assert "pooled connect exploded" in str(error)

    @pytest.mark.asyncio
    async def test_list_drives_pooled_client(self):
        list_result = {
            "success": True,
            "result": [
                {
                    "run_id": "r1",
                    "timestamp": "2025-11-30T15:00:00Z",
                    "state": "stopped",
                }
            ],
        }
        client = _make_pooled_client(_dispatch(list_result=list_result))
        tools = TraceTools(client)

        result = await tools.ha_get_automation_traces(entity_id="automation.test")

        assert result["success"] is True
        assert result["trace_count"] == 1
        # Resolve + list both rode the pooled client; no dedicated socket exists.
        sent_types = [
            call.args[0]["type"]
            for call in client.send_websocket_message.await_args_list
        ]
        assert "config/entity_registry/get" in sent_types
        assert "trace/list" in sent_types

    @pytest.mark.asyncio
    async def test_detail_drives_pooled_client(self):
        detail_result = {
            "success": True,
            "result": {
                "timestamp": "2025-11-30T15:00:00Z",
                "state": "stopped",
                "trace": {},
                "config": {"alias": "Test"},
            },
        }
        client = _make_pooled_client(_dispatch(detail_result=detail_result))
        tools = TraceTools(client)

        result = await tools.ha_get_automation_traces(
            entity_id="automation.test", run_id="r1"
        )

        assert result["success"] is True
        assert result["run_id"] == "r1"
        sent_types = [
            call.args[0]["type"]
            for call in client.send_websocket_message.await_args_list
        ]
        assert "trace/get" in sent_types

    @pytest.mark.asyncio
    async def test_connection_error_maps_to_connection_failed(self):
        """A connection-shaped pooled failure on the fetch surfaces as
        CONNECTION_FAILED — the same structured error code the removed up-front
        connect check raised."""
        list_result = {
            "success": False,
            "error": "Failed to connect to Home Assistant",
        }
        client = _make_pooled_client(_dispatch(list_result=list_result))
        tools = TraceTools(client)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_automation_traces(entity_id="automation.test")

        error = json.loads(str(exc_info.value))
        assert error["success"] is False
        assert error["error"]["code"] == "CONNECTION_FAILED"

    @pytest.mark.asyncio
    async def test_trace_command_error_maps_to_service_call_failed(self):
        """A non-connection pooled failure keeps its SERVICE_CALL_FAILED shape."""
        list_result = {"success": False, "error": "unknown_command"}
        client = _make_pooled_client(_dispatch(list_result=list_result))
        tools = TraceTools(client)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_automation_traces(entity_id="automation.test")

        error = json.loads(str(exc_info.value))
        assert error["success"] is False
        assert error["error"]["code"] == "SERVICE_CALL_FAILED"

    @pytest.mark.asyncio
    async def test_detail_connection_error_maps_to_connection_failed(self):
        """The DETAIL path (run_id set) routes a connection-shaped pooled failure
        through the same ``_raise_trace_ws_failure`` classifier → CONNECTION_FAILED."""
        detail_result = {
            "success": False,
            "error": "Failed to connect to Home Assistant",
        }
        client = _make_pooled_client(_dispatch(detail_result=detail_result))
        tools = TraceTools(client)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_automation_traces(
                entity_id="automation.test", run_id="r1"
            )

        error = json.loads(str(exc_info.value))
        assert error["success"] is False
        assert error["error"]["code"] == "CONNECTION_FAILED"

    @pytest.mark.asyncio
    async def test_detail_command_error_maps_to_service_call_failed(self):
        """The DETAIL path keeps a non-connection pooled failure as
        SERVICE_CALL_FAILED."""
        detail_result = {"success": False, "error": "unknown_command"}
        client = _make_pooled_client(_dispatch(detail_result=detail_result))
        tools = TraceTools(client)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_automation_traces(
                entity_id="automation.test", run_id="r1"
            )

        error = json.loads(str(exc_info.value))
        assert error["success"] is False
        assert error["error"]["code"] == "SERVICE_CALL_FAILED"
