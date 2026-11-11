"""Unit tests for tools_traces module."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_traces import (
    TraceTools,
    _format_trace_list,
    _resolve_trace_item_id,
)


def _cores_traces() -> list[dict]:
    """Five traces shaped like Core's ``trace/list`` short dicts."""
    return [
        {
            "run_id": f"run_{i}",
            "timestamp": {
                "start": f"2025-11-30T15:0{i}:00+00:00",
                "finish": f"2025-11-30T15:0{i}:01+00:00",
            },
            "state": "stopped",
        }
        for i in range(5)
    ]


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
                "timestamp": {
                    "start": "2025-11-30T15:00:00+00:00",
                    "finish": "2025-11-30T15:00:01+00:00",
                },
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

    @pytest.mark.parametrize(
        ("limit", "offset", "order", "expected_run_ids", "has_more"),
        [
            pytest.param(
                3, 0, "newest", ["run_4", "run_3", "run_2"], True, id="newest-window"
            ),
            pytest.param(2, 0, "newest", ["run_4", "run_3"], True, id="small-page"),
            pytest.param(
                10,
                0,
                "newest",
                ["run_4", "run_3", "run_2", "run_1", "run_0"],
                False,
                id="all-newest-first",
            ),
            pytest.param(
                3, 0, "oldest", ["run_0", "run_1", "run_2"], True, id="oldest-window"
            ),
            pytest.param(2, 2, "newest", ["run_2", "run_1"], True, id="offset-newest"),
            pytest.param(2, 2, "oldest", ["run_2", "run_3"], True, id="offset-oldest"),
            pytest.param(10, 5, "newest", [], False, id="offset-equals-total"),
            pytest.param(10, 10, "newest", [], False, id="offset-beyond-total"),
        ],
    )
    def test_pages_traces(
        self, limit, offset, order, expected_run_ids, has_more
    ) -> None:
        """limit/offset/order select one window of Core's sorted rows."""
        result = _format_trace_list(
            "automation.test", _cores_traces(), limit, offset=offset, order=order
        )

        assert [trace["run_id"] for trace in result["traces"]] == expected_run_ids
        assert result["trace_count"] == len(expected_run_ids)
        assert result["total_available"] == 5
        assert result["offset"] == offset
        assert result["order"] == order
        assert result["has_more"] is has_more

    def test_list_preserves_core_short_dict_fields(self):
        """Every field Core's trace/list returns reaches the caller unchanged."""
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

        assert result["traces"][0] == core_trace

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


class TestTraceFetchPooledClient:
    """`ha_get_automation_traces` drives the pooled client end-to-end (issue
    #1813 item #5): no dedicated socket, structured errors preserved."""

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

    @pytest.mark.asyncio
    async def test_sections_without_run_is_rejected(self):
        """sections is only meaningful when a run is resolved."""
        client = _make_pooled_client(_dispatch())
        tools = TraceTools(client)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_automation_traces(
                entity_id="automation.test", sections="conditions"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"

    @pytest.mark.asyncio
    async def test_detailed_without_run_is_rejected(self):
        """detailed=True is only meaningful when a run is resolved."""
        client = _make_pooled_client(_dispatch())
        tools = TraceTools(client)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_automation_traces(
                entity_id="automation.test", detailed=True
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
