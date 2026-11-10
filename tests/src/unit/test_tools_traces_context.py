"""Context-id lookup tests for ha_get_automation_traces."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools.tools_traces import TraceTools
from ha_mcp.tools.trace_lookup import (
    ContextTraceMatch,
    TraceLogbook,
    resolve_context_trace,
    resolve_global_context,
)

from .test_tools_traces import _dispatch, _make_pooled_client


class TestTraceListWarnings:
    """Warnings attached to trace list responses."""

    @pytest.mark.asyncio
    async def test_item_only_empty_list_warns(self) -> None:
        """domain+item_id with no traces carries an item-only warning."""
        client = _make_pooled_client(_dispatch())

        result = await TraceTools(client).ha_get_automation_traces(
            domain="automation", item_id="uid_1"
        )

        assert result["trace_count"] == 0
        assert result["warnings"] == [
            "No traces found for automation/uid_1; "
            "the item_id may be incorrect or the automation/script has not run."
        ]


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
            _dispatch(detail_result=detail_result, context_result=context_result)
        )

        result = await TraceTools(client).ha_get_automation_traces(
            entity_id="automation.test", context_id="ctx-1"
        )

        assert result["success"] is True
        assert result["run_id"] == "r1"
        assert result["context_id"] == "ctx-1"
        assert result["context_match"] == "trace"
        assert result["context_scanned"] == 0
        sent_types = [
            call.args[0]["type"]
            for call in client.send_websocket_message.await_args_list
        ]
        assert "trace/contexts" in sent_types
        assert "trace/get" in sent_types

    @pytest.mark.asyncio
    async def test_parent_context_resolves_to_child_run(self):
        """A triggering event context resolves through the trace's parent_id."""
        details = {
            "r1": {
                "run_id": "r1",
                "state": "stopped",
                "trace": {},
                "config": {"alias": "Test"},
                "context": {"id": "own-1", "parent_id": "parent-1"},
            }
        }

        def _d(mtype, message):
            if mtype == "config/entity_registry/get":
                return {"success": True, "result": {"unique_id": "uid_1"}}
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            if mtype == "trace/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "run_id": "r1",
                            "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
                        }
                    ],
                }
            if mtype == "trace/get":
                return {
                    "success": True,
                    "result": details[message["run_id"]],
                }
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)

        result = await TraceTools(client).ha_get_automation_traces(
            entity_id="automation.test", context_id="parent-1"
        )

        assert result["run_id"] == "r1"
        assert result["context_id"] == "parent-1"
        assert result["context_match"] == "parent"
        assert result["context_scanned"] == 1
        trace_gets = [
            call.args[0]["type"]
            for call in client.send_websocket_message.await_args_list
            if call.args[0].get("type") == "trace/get"
        ]
        assert len(trace_gets) == 1

    @pytest.mark.asyncio
    async def test_scanned_detail_is_reused_without_refetch(self):
        """The context scan's trace/get copy is used instead of reading again."""
        scanned = {
            "run_id": "r1",
            "state": "stopped",
            "trace": {},
            "context": {"id": "own-1", "parent_id": "parent-1"},
        }
        client = _make_pooled_client(
            _dispatch(
                detail_result={
                    "success": False,
                    "error": "The trace could not be found",
                }
            )
        )

        with patch(
            "ha_mcp.tools.tools_traces.resolve_context_trace",
            AsyncMock(
                return_value=ContextTraceMatch("r1", "parent", 1, False, (), scanned)
            ),
        ):
            result = await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test", context_id="parent-1"
            )

        assert result["run_id"] == "r1"
        assert result["context_match"] == "parent"
        assert result["context_scanned"] == 1
        assert all(
            call.args[0].get("type") != "trace/get"
            for call in client.send_websocket_message.await_args_list
        )

    @pytest.mark.asyncio
    async def test_context_scan_truncated_is_reported(self):
        """A bounded parent scan reports that older traces were not checked."""
        detail_result = {
            "success": True,
            "result": {
                "run_id": "r1",
                "state": "stopped",
                "trace": {},
                "context": {"id": "own", "parent_id": "parent-1"},
            },
        }
        client = _make_pooled_client(_dispatch(detail_result=detail_result))

        with patch(
            "ha_mcp.tools.tools_traces.resolve_context_trace",
            AsyncMock(
                return_value=ContextTraceMatch(
                    "r1",
                    "parent",
                    50,
                    True,
                    ("r49: The trace could not be found",),
                )
            ),
        ):
            result = await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test", context_id="parent-1"
            )

        assert result["context_scan_truncated"] is True
        assert result["context_scanned"] == 50
        assert result["context_scan_errors"] == ["r49: The trace could not be found"]

    @pytest.mark.asyncio
    async def test_unknown_context_id_is_not_found(self):
        """A context with no trace gets a not-found error, not an empty success."""
        client = _make_pooled_client(
            _dispatch(context_result={"success": True, "result": {}})
        )

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test", context_id="missing"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "missing" in error["error"]["message"]

    @pytest.mark.asyncio
    async def test_malformed_context_result_is_not_found(self):
        """A non-map trace/contexts result is treated as no matching trace."""
        client = _make_pooled_client(
            _dispatch(context_result={"success": True, "result": []})
        )

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test", context_id="ctx-1"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_run_id_and_context_id_are_mutually_exclusive(self):
        """Passing both selectors is rejected before any WebSocket call."""
        client = _make_pooled_client(_dispatch())

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test",
                run_id="r1",
                context_id="ctx-1",
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        client.send_websocket_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_context_id_is_rejected(self):
        """An empty context id is a validation error, not a missing trace."""
        client = _make_pooled_client(_dispatch())

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test", context_id="   "
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        client.send_websocket_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_context_only_resolves_through_global_contexts(self):
        """No entity_id: the global trace/contexts map finds the run."""
        detail_result = {
            "success": True,
            "result": {
                "run_id": "r1",
                "state": "stopped",
                "trace": {},
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
            _dispatch(detail_result=detail_result, context_result=context_result)
        )

        result = await TraceTools(client).ha_get_automation_traces(context_id="ctx-1")

        assert "entity_id" not in result
        assert result["domain"] == "automation"
        assert result["item_id"] == "uid_1"
        assert result["run_id"] == "r1"
        assert result["context_match"] == "trace"
        contexts_message = next(
            call.args[0]
            for call in client.send_websocket_message.await_args_list
            if call.args[0].get("type") == "trace/contexts"
        )
        assert contexts_message == {"type": "trace/contexts"}

    @pytest.mark.asyncio
    async def test_context_only_parent_resolves_through_global_scan(self):
        """No entity_id: the bounded global scan matches a parent context."""
        detail = {
            "run_id": "r1",
            "state": "stopped",
            "trace": {},
            "context": {"id": "own-1", "parent_id": "parent-1"},
        }

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            if mtype == "trace/list":
                if message["domain"] == "automation":
                    return {
                        "success": True,
                        "result": [
                            {
                                "run_id": "r1",
                                "domain": "automation",
                                "item_id": "uid_1",
                                "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
                            }
                        ],
                    }
                return {"success": True, "result": []}
            if mtype == "trace/get":
                return {"success": True, "result": detail}
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        result = await TraceTools(client).ha_get_automation_traces(
            context_id="parent-1"
        )

        assert "entity_id" not in result
        assert result["domain"] == "automation"
        assert result["item_id"] == "uid_1"
        assert result["run_id"] == "r1"
        assert result["context_match"] == "parent"
        assert result["context_scanned"] == 1

    @pytest.mark.asyncio
    async def test_domain_only_lists_across_items(self):
        """No entity_id: trace/list runs for the whole domain."""
        list_result = {
            "success": True,
            "result": [
                {
                    "run_id": "r1",
                    "domain": "automation",
                    "item_id": "one",
                    "timestamp": {"start": "2026-11-08T16:00:00+00:00"},
                },
                {
                    "run_id": "r2",
                    "domain": "automation",
                    "item_id": "two",
                    "timestamp": {"start": "2026-11-08T16:01:00+00:00"},
                },
            ],
        }
        client = _make_pooled_client(_dispatch(list_result=list_result))

        result = await TraceTools(client).ha_get_automation_traces(domain="automation")

        assert result["domain"] == "automation"
        assert "entity_id" not in result
        assert result["trace_count"] == 2
        list_message = next(
            call.args[0]
            for call in client.send_websocket_message.await_args_list
            if call.args[0].get("type") == "trace/list"
        )
        assert list_message == {"type": "trace/list", "domain": "automation"}

    @pytest.mark.asyncio
    async def test_domain_list_row_fetches_detail_by_item_id(self):
        """A list row's domain/item_id/run_id fetches its detail."""
        detail_result = {
            "success": True,
            "result": {
                "run_id": "r1",
                "state": "stopped",
                "trace": {"action/0": [{"path": "action/0"}]},
                "context": {"id": "ctx-1"},
            },
        }
        client = _make_pooled_client(_dispatch(detail_result=detail_result))

        result = await TraceTools(client).ha_get_automation_traces(
            domain="automation", item_id="uid_1", run_id="r1"
        )

        assert result["domain"] == "automation"
        assert result["item_id"] == "uid_1"
        assert result["run_id"] == "r1"
        assert "entity_id" not in result
        trace_get = next(
            call.args[0]
            for call in client.send_websocket_message.await_args_list
            if call.args[0].get("type") == "trace/get"
        )
        assert trace_get == {
            "type": "trace/get",
            "domain": "automation",
            "item_id": "uid_1",
            "run_id": "r1",
        }

    @pytest.mark.asyncio
    async def test_item_id_without_domain_is_rejected(self):
        client = _make_pooled_client(_dispatch())

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                item_id="uid_1", run_id="r1"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        client.send_websocket_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_entity_id_and_item_id_are_mutually_exclusive(self):
        client = _make_pooled_client(_dispatch())

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test", domain="automation", item_id="uid_1"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        client.send_websocket_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_global_scan_not_found_keeps_the_scan_details(self):
        """A failed domain read is reported instead of looking like no run."""

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            if mtype == "trace/list":
                if message["domain"] == "script":
                    return {"success": False, "error": "script list denied"}
                return {"success": True, "result": []}
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(context_id="parent-1")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error["scan_truncated"] is False
        assert any("script list denied" in item for item in error["scan_errors"])

    @pytest.mark.asyncio
    async def test_context_domain_mismatch_is_rejected(self):
        client = _make_pooled_client(
            _dispatch(
                context_result={
                    "success": True,
                    "result": {
                        "ctx-1": {
                            "run_id": "r1",
                            "domain": "automation",
                            "item_id": "uid_1",
                        }
                    },
                }
            )
        )

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                context_id="ctx-1", domain="script"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"

    @pytest.mark.asyncio
    async def test_component_unknown_command_falls_back_and_warns(self):
        """An explicit unknown_command from the component falls back to the
        public scan."""
        detail = {
            "run_id": "r1",
            "state": "stopped",
            "trace": {},
            "context": {"id": "own-1", "parent_id": "parent-1"},
        }

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            if mtype == "ha_mcp_tools/trace_context":
                return {
                    "success": False,
                    "error_code": "unknown_command",
                    "error": "Unknown command",
                }
            if mtype == "trace/list":
                if message.get("domain") == "script":
                    return {"success": True, "result": []}
                return {
                    "success": True,
                    "result": [
                        {
                            "run_id": "r1",
                            "domain": "automation",
                            "item_id": "uid_1",
                            "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
                        }
                    ],
                }
            if mtype == "trace/get":
                return {"success": True, "result": detail}
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        caps = SimpleNamespace(capabilities=frozenset({"trace_context"}))
        with patch(
            "ha_mcp.tools.trace_lookup.get_component_caps",
            AsyncMock(return_value=caps),
        ):
            result = await TraceTools(client).ha_get_automation_traces(
                context_id="parent-1"
            )

        assert result["context_match"] == "parent"
        assert result["context_scanned"] == 1
        assert any(
            "public fallback scan" in warning for warning in result["warnings"]
        )

    @pytest.mark.asyncio
    async def test_component_context_matches_without_a_public_scan(self):
        """The component trace-store pass returns the match and its trace."""
        payload = {
            "match": "parent",
            "run_id": "r1",
            "domain": "automation",
            "item_id": "uid_1",
            "trace": {
                "run_id": "r1",
                "state": "stopped",
                "trace": {},
                "context": {"id": "own-1", "parent_id": "parent-1"},
            },
            "scanned": 7,
            "truncated": False,
            "errors": [],
            "other_matches": [
                {
                    "domain": "script",
                    "item_id": "uid_2",
                    "run_id": "r2",
                    "match": "parent",
                }
            ],
        }

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            if mtype == "ha_mcp_tools/trace_context":
                return {"success": True, "result": payload}
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        caps = SimpleNamespace(capabilities=frozenset({"trace_context"}))
        with patch(
            "ha_mcp.tools.trace_lookup.get_component_caps",
            AsyncMock(return_value=caps),
        ):
            result = await TraceTools(client).ha_get_automation_traces(
                context_id="parent-1"
            )

        assert result["context_match"] == "parent"
        assert result["context_scanned"] == 7
        assert result["domain"] == "automation"
        assert result["item_id"] == "uid_1"
        assert result["context_other_matches"] == [
            {
                "domain": "script",
                "item_id": "uid_2",
                "run_id": "r2",
                "match": "parent",
            }
        ]
        types = [
            call.args[0].get("type")
            for call in client.send_websocket_message.await_args_list
        ]
        assert "ha_mcp_tools/trace_context" in types
        assert "trace/list" not in types
        assert "trace/get" not in types
        assert not any(
            "public fallback scan" in warning
            for warning in result.get("warnings", [])
        )

    @pytest.mark.asyncio
    async def test_public_fallback_lists_other_matching_runs(self):
        """The public parent scan selects one run and reports the other matches."""
        older = {
            "run_id": "older",
            "state": "stopped",
            "trace": {},
            "context": {"id": "own-older", "parent_id": "parent-1"},
            "timestamp": {"start": "2026-11-08T16:00:00+00:00"},
        }
        newer = {
            "run_id": "newer",
            "state": "stopped",
            "trace": {},
            "context": {"id": "own-newer", "parent_id": "parent-1"},
            "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
        }

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            if mtype == "trace/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "run_id": "newer",
                            "item_id": "uid_1",
                            "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
                        },
                        {
                            "run_id": "older",
                            "item_id": "uid_1",
                            "timestamp": {"start": "2026-11-08T16:00:00+00:00"},
                        },
                    ],
                }
            if mtype == "trace/get":
                if message["run_id"] == "newer":
                    return {"success": True, "result": newer}
                if message["run_id"] == "older":
                    return {"success": True, "result": older}
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        result = await TraceTools(client).ha_get_automation_traces(
            domain="automation", item_id="uid_1", context_id="parent-1"
        )

        assert result["run_id"] == "newer"
        assert result["context_match"] == "parent"
        assert result["context_other_matches"] == [
            {
                "domain": "automation",
                "item_id": "uid_1",
                "run_id": "older",
                "match": "parent",
            }
        ]

    @pytest.mark.asyncio
    async def test_public_fallback_aggregates_direct_map_and_scan(self):
        """trace/contexts plus a parent scan returns all matching runs."""
        own_detail = {
            "run_id": "own-run",
            "state": "stopped",
            "trace": {},
            "context": {"id": "ctx-1"},
            "timestamp": {"start": "2026-11-08T16:00:00+00:00"},
        }
        parent_detail = {
            "run_id": "parent-run",
            "state": "stopped",
            "trace": {},
            "context": {"id": "own-parent", "parent_id": "ctx-1"},
            "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
        }

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {
                    "success": True,
                    "result": {"ctx-1": {"run_id": "own-run"}},
                }
            if mtype == "trace/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "run_id": "parent-run",
                            "item_id": "uid_1",
                            "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
                        }
                    ],
                }
            if mtype == "trace/get":
                if message["run_id"] == "own-run":
                    return {"success": True, "result": own_detail}
                if message["run_id"] == "parent-run":
                    return {"success": True, "result": parent_detail}
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        result = await TraceTools(client).ha_get_automation_traces(
            entity_id="automation.test", context_id="ctx-1"
        )

        assert result["run_id"] == "parent-run"
        assert result["context_match"] == "parent"
        assert result["context_other_matches"] == [
            {
                "domain": "automation",
                "item_id": "test",
                "run_id": "own-run",
                "match": "trace",
            }
        ]

    @pytest.mark.asyncio
    async def test_global_fallback_aggregates_direct_map_and_scan(self):
        """Global trace/contexts plus a cross-domain scan returns all matches."""
        own_detail = {
            "run_id": "auto-run",
            "state": "stopped",
            "trace": {},
            "context": {"id": "ctx-1"},
            "timestamp": {"start": "2026-11-08T16:00:00+00:00"},
        }
        parent_detail = {
            "run_id": "script-run",
            "state": "stopped",
            "trace": {},
            "context": {"id": "own-script", "parent_id": "ctx-1"},
            "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
        }

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {
                    "success": True,
                    "result": {
                        "ctx-1": {
                            "run_id": "auto-run",
                            "domain": "automation",
                            "item_id": "uid_a",
                        }
                    },
                }
            if mtype == "trace/list":
                if message["domain"] == "automation":
                    return {"success": True, "result": []}
                if message["domain"] == "script":
                    return {
                        "success": True,
                        "result": [
                            {
                                "run_id": "script-run",
                                "item_id": "uid_s",
                                "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
                            }
                        ],
                    }
            if mtype == "trace/get":
                if message["run_id"] == "auto-run":
                    return {"success": True, "result": own_detail}
                if message["run_id"] == "script-run":
                    return {"success": True, "result": parent_detail}
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        result = await TraceTools(client).ha_get_automation_traces(
            context_id="ctx-1"
        )

        assert result["run_id"] == "script-run"
        assert result["context_match"] == "parent"
        assert result["context_other_matches"] == [
            {
                "domain": "automation",
                "item_id": "uid_a",
                "run_id": "auto-run",
                "match": "trace",
            }
        ]

    @pytest.mark.asyncio
    async def test_fallback_selection_uses_start_timestamp_only(self):
        """The public fallback does not fall back to timestamp.finish for selection."""
        finish_only = {
            "run_id": "finish-only",
            "state": "stopped",
            "trace": {},
            "context": {"id": "own-finish", "parent_id": "parent-1"},
            "timestamp": {"finish": "2026-11-08T17:00:00+00:00"},
        }
        start_only = {
            "run_id": "start-only",
            "state": "stopped",
            "trace": {},
            "context": {"id": "own-start", "parent_id": "parent-1"},
            "timestamp": {"start": "2026-11-08T16:00:00+00:00"},
        }

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            if mtype == "trace/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "run_id": "finish-only",
                            "item_id": "uid_1",
                            "timestamp": {"finish": "2026-11-08T17:00:00+00:00"},
                        },
                        {
                            "run_id": "start-only",
                            "item_id": "uid_1",
                            "timestamp": {"start": "2026-11-08T16:00:00+00:00"},
                        },
                    ],
                }
            if mtype == "trace/get":
                if message["run_id"] == "finish-only":
                    return {"success": True, "result": finish_only}
                if message["run_id"] == "start-only":
                    return {"success": True, "result": start_only}
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        result = await TraceTools(client).ha_get_automation_traces(
            domain="automation", item_id="uid_1", context_id="parent-1"
        )

        assert result["run_id"] == "start-only"
        assert result["context_other_matches"] == [
            {
                "domain": "automation",
                "item_id": "uid_1",
                "run_id": "finish-only",
                "match": "parent",
            }
        ]

    @pytest.mark.asyncio
    async def test_component_without_a_trace_store_uses_the_public_scan(self):
        """An unavailable component store is not an authoritative miss."""

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            if mtype == "ha_mcp_tools/trace_context":
                return {
                    "success": True,
                    "result": {
                        "available": False,
                        "match": None,
                        "trace": None,
                        "scanned": 0,
                    },
                }
            if mtype == "trace/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "run_id": "r1",
                            "item_id": "uid_1",
                            "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
                        }
                    ],
                }
            if mtype == "trace/get":
                return {
                    "success": True,
                    "result": {
                        "run_id": "r1",
                        "state": "stopped",
                        "trace": {},
                        "context": {"id": "own-1", "parent_id": "parent-1"},
                    },
                }
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        caps = SimpleNamespace(capabilities=frozenset({"trace_context"}))
        with patch(
            "ha_mcp.tools.trace_lookup.get_component_caps",
            AsyncMock(return_value=caps),
        ):
            result = await TraceTools(client).ha_get_automation_traces(
                context_id="parent-1"
            )

        assert result["context_match"] == "parent"
        types = [
            call.args[0].get("type")
            for call in client.send_websocket_message.await_args_list
        ]
        assert "trace/list" in types
        assert "trace/get" in types

    @pytest.mark.asyncio
    async def test_component_no_match_is_authoritative(self):
        """A component miss does not fall back to the 50-trace public scan."""

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            if mtype == "ha_mcp_tools/trace_context":
                return {
                    "success": True,
                    "result": {
                        "match": None,
                        "run_id": None,
                        "domain": None,
                        "item_id": None,
                        "trace": None,
                        "scanned": 7,
                        "truncated": False,
                        "errors": [],
                    },
                }
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        caps = SimpleNamespace(capabilities=frozenset({"trace_context"}))
        with (
            patch(
                "ha_mcp.tools.trace_lookup.get_component_caps",
                AsyncMock(return_value=caps),
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await TraceTools(client).ha_get_automation_traces(context_id="parent-1")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error["scanned"] == 7
        assert error["scan_truncated"] is False
        types = [
            call.args[0].get("type")
            for call in client.send_websocket_message.await_args_list
        ]
        assert "trace/list" not in types

    @pytest.mark.asyncio
    async def test_domain_scoped_context_resolves_the_direct_map(self):
        """domain is validated against the run's own context entry."""
        detail_result = {
            "success": True,
            "result": {
                "run_id": "r1",
                "state": "stopped",
                "trace": {},
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
            _dispatch(detail_result=detail_result, context_result=context_result)
        )

        result = await TraceTools(client).ha_get_automation_traces(
            context_id="ctx-1", domain="automation"
        )

        assert result["context_match"] == "trace"
        assert result["domain"] == "automation"
        assert result["item_id"] == "uid_1"

    @pytest.mark.asyncio
    async def test_item_and_context_resolves_inside_that_item(self):
        """domain+item_id+context_id scans one Core trace key."""
        detail_result = {
            "success": True,
            "result": {
                "run_id": "r1",
                "state": "stopped",
                "trace": {},
                "context": {"id": "own-1", "parent_id": "parent-1"},
            },
        }

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            if mtype == "trace/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "run_id": "r1",
                            "item_id": "uid_1",
                            "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
                        }
                    ],
                }
            if mtype == "trace/get":
                return {"success": True, "result": detail_result["result"]}
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        result = await TraceTools(client).ha_get_automation_traces(
            domain="automation", item_id="uid_1", context_id="parent-1"
        )

        assert result["context_match"] == "parent"
        assert result["item_id"] == "uid_1"
        contexts_message = next(
            call.args[0]
            for call in client.send_websocket_message.await_args_list
            if call.args[0].get("type") == "trace/contexts"
        )
        assert contexts_message == {
            "type": "trace/contexts",
            "domain": "automation",
            "item_id": "uid_1",
        }

    @pytest.mark.asyncio
    async def test_item_only_lists_that_trace_key(self):
        """domain+item_id lists one item's runs."""
        list_result = {
            "success": True,
            "result": [
                {
                    "run_id": "r1",
                    "item_id": "uid_1",
                    "timestamp": {"start": "2026-11-08T16:00:00+00:00"},
                }
            ],
        }
        client = _make_pooled_client(_dispatch(list_result=list_result))

        result = await TraceTools(client).ha_get_automation_traces(
            domain="automation", item_id="uid_1"
        )

        assert result["trace_count"] == 1
        assert "entity_id" not in result
        list_message = next(
            call.args[0]
            for call in client.send_websocket_message.await_args_list
            if call.args[0].get("type") == "trace/list"
        )
        assert list_message == {
            "type": "trace/list",
            "domain": "automation",
            "item_id": "uid_1",
        }

    @pytest.mark.asyncio
    async def test_all_connection_failures_are_connection_failed(self):
        """Both domains failing on transport is not reported as no such run."""

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            if mtype == "trace/list":
                return {"success": False, "error": "connection closed"}
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(context_id="parent-1")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "CONNECTION_FAILED"

    @pytest.mark.asyncio
    async def test_whitespace_sections_with_detailed_fetches_logbook(self):
        """detailed=True still fetches logbook for a whitespace section string."""
        detail_result = {
            "success": True,
            "result": {
                "run_id": "r1",
                "state": "stopped",
                "trace": {},
                "context": {"id": "ctx-1"},
                "timestamp": {"start": "2026-11-08T16:00:00+00:00"},
            },
        }
        client = _make_pooled_client(_dispatch(detail_result=detail_result))
        logbook = TraceLogbook([{"name": "Light"}], "component", [], 1, False)

        with patch(
            "ha_mcp.tools.tools_traces.fetch_trace_logbook",
            AsyncMock(return_value=logbook),
        ):
            result = await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test",
                run_id="r1",
                sections="   ",
                detailed=True,
            )

        assert result["logbook_entries"] == [{"name": "Light"}]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            ToolError("logbook timed out"),
            HomeAssistantConnectionError("REST logbook unavailable"),
        ],
    )
    async def test_logbook_failure_keeps_the_trace_with_a_warning(self, error):
        """A failed optional logbook read must not discard the trace."""
        detail_result = {
            "success": True,
            "result": {
                "run_id": "r1",
                "state": "stopped",
                "trace": {"action/0": [{"path": "action/0"}]},
                "context": {"id": "ctx-1"},
            },
        }
        client = _make_pooled_client(_dispatch(detail_result=detail_result))

        with patch(
            "ha_mcp.tools.tools_traces.fetch_trace_logbook",
            AsyncMock(side_effect=error),
        ):
            result = await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test",
                run_id="r1",
                sections="trace,logbook",
            )

        assert result["trace"]["action/0"][0]["path"] == "action/0"
        assert result["logbook_entries"] == []
        assert any("logbook unavailable" in warning for warning in result["warnings"])

    @pytest.mark.asyncio
    async def test_run_id_without_entity_id_is_rejected(self):
        client = _make_pooled_client(_dispatch())

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(run_id="r1")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        client.send_websocket_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_selector_is_rejected(self):
        client = _make_pooled_client(_dispatch())

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces()

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        client.send_websocket_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_context_lookup_transport_failure_is_classified(self):
        """A dead transport during trace/contexts reports CONNECTION_FAILED."""
        client = _make_pooled_client(
            _dispatch(
                context_result={
                    "success": False,
                    "error": "Failed to connect to Home Assistant",
                }
            )
        )

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test", context_id="ctx-1"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "CONNECTION_FAILED"

    @pytest.mark.asyncio
    async def test_context_lookup_command_failure_is_service_call_failed(self):
        """A non-connection trace/contexts failure keeps its command error."""
        client = _make_pooled_client(
            _dispatch(context_result={"success": False, "error": "unknown_command"})
        )

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test", context_id="ctx-1"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "SERVICE_CALL_FAILED"

    @pytest.mark.asyncio
    async def test_global_context_without_domain_item_is_resource_not_found(self):
        """A global context hit lacking domain/item_id raises a structured tool
        error."""

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {
                    "success": True,
                    "result": {"ctx-1": {"run_id": "own-run"}},
                }
            if mtype == "trace/list":
                return {"success": True, "result": []}
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(client).ha_get_automation_traces(context_id="ctx-1")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "context resolved without a domain/item" in error["error"]["message"]


class TestTraceResolverDirectMatches:
    """Direct tests of ``resolve_context_trace`` and ``resolve_global_context``."""

    @pytest.mark.asyncio
    async def test_per_item_direct_hit_survives_trace_list_failure(self):
        """A trace/contexts hit is returned even when trace/list fails."""

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {"ctx-1": {"run_id": "own-run"}}}
            if mtype == "trace/list":
                return {"success": False, "error": "trace/list denied"}
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        result = await resolve_context_trace(
            client, "automation", "uid_1", "ctx-1"
        )

        assert result.run_id == "own-run"
        assert result.match == "trace"
        assert result.domain == "automation"
        assert result.item_id == "uid_1"
        assert any("trace/list denied" in error for error in result.scan_errors)

    @pytest.mark.asyncio
    async def test_global_direct_hit_survives_trace_list_failure(self):
        """A global trace/contexts hit is returned even when trace/list fails."""

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {
                    "success": True,
                    "result": {
                        "ctx-1": {
                            "run_id": "own-run",
                            "domain": "automation",
                            "item_id": "uid_a",
                        }
                    },
                }
            if mtype == "trace/list":
                return {"success": False, "error": "trace/list denied"}
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        result = await resolve_global_context(client, "ctx-1")

        assert result.run_id == "own-run"
        assert result.match == "trace"
        assert result.domain == "automation"
        assert result.item_id == "uid_a"
        assert any("trace/list denied" in error for error in result.scan_errors)

    @pytest.mark.asyncio
    async def test_direct_match_wins_when_trace_data_unavailable(self):
        """A direct hit is kept selected when its trace/get read fails."""

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {"ctx-1": {"run_id": "own-run"}}}
            if mtype == "trace/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "run_id": "newer-parent",
                            "item_id": "uid_1",
                            "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
                        }
                    ],
                }
            if mtype == "trace/get":
                if message["run_id"] == "own-run":
                    return {"success": False, "error": "direct trace unavailable"}
                if message["run_id"] == "newer-parent":
                    return {
                        "success": True,
                        "result": {
                            "run_id": "newer-parent",
                            "context": {
                                "id": "own-parent",
                                "parent_id": "ctx-1",
                            },
                            "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
                        },
                    }
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        result = await resolve_context_trace(
            client, "automation", "uid_1", "ctx-1"
        )

        assert result.run_id == "own-run"
        assert result.match == "trace"
        assert result.other_matches == (
            {
                "domain": "automation",
                "item_id": "uid_1",
                "run_id": "newer-parent",
                "match": "parent",
            },
        )
        assert any("direct trace unavailable" in error for error in result.scan_errors)

    @pytest.mark.asyncio
    async def test_global_entry_missing_domain_item_id_is_returned(self):
        """A global direct entry without domain/item_id is returned as None fields."""

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {
                    "success": True,
                    "result": {"ctx-1": {"run_id": "own-run"}},
                }
            if mtype == "trace/list":
                return {"success": True, "result": []}
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        result = await resolve_global_context(client, "ctx-1")

        assert result.run_id == "own-run"
        assert result.match == "trace"
        assert result.domain is None
        assert result.item_id is None

    @pytest.mark.asyncio
    async def test_per_item_no_direct_hit_trace_list_connection_error_is_classified(self):
        """Without a direct hit, trace/list failure keeps its connection
        classification."""

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            if mtype == "trace/list":
                return {"success": False, "error": "trace/list timed out"}
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)

        with pytest.raises(ToolError) as exc_info:
            await resolve_context_trace(client, "automation", "uid_1", "ctx-1")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "CONNECTION_FAILED"

    @pytest.mark.asyncio
    async def test_direct_run_not_duplicated_in_other_matches(self):
        """A direct run that also appears in the scan is not listed as an other
        match."""

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {"ctx-1": {"run_id": "own-run"}}}
            if mtype == "trace/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "run_id": "own-run",
                            "item_id": "uid_1",
                            "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
                        },
                        {
                            "run_id": "newer-parent",
                            "item_id": "uid_1",
                            "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
                        },
                    ],
                }
            if mtype == "trace/get":
                if message["run_id"] == "own-run":
                    return {"success": False, "error": "direct trace unavailable"}
                if message["run_id"] == "newer-parent":
                    return {
                        "success": True,
                        "result": {
                            "run_id": "newer-parent",
                            "context": {
                                "id": "own-parent",
                                "parent_id": "ctx-1",
                            },
                            "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
                        },
                    }
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        result = await resolve_context_trace(
            client, "automation", "uid_1", "ctx-1"
        )

        assert result.run_id == "own-run"
        assert result.other_matches == (
            {
                "domain": "automation",
                "item_id": "uid_1",
                "run_id": "newer-parent",
                "match": "parent",
            },
        )

    @pytest.mark.asyncio
    async def test_global_direct_hit_with_scanned_matches_and_read_failure(self):
        """Global direct hit survives a trace/get failure and scans one read only."""

        def _d(mtype, message):
            if mtype == "trace/contexts":
                return {
                    "success": True,
                    "result": {
                        "ctx-1": {
                            "run_id": "own-run",
                            "domain": "automation",
                            "item_id": "uid_a",
                        }
                    },
                }
            if mtype == "trace/list":
                if message.get("domain") == "script":
                    return {"success": True, "result": []}
                return {
                    "success": True,
                    "result": [
                        {
                            "run_id": "parent-run",
                            "item_id": "uid_b",
                            "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
                        }
                    ],
                }
            if mtype == "trace/get":
                if message["run_id"] == "own-run":
                    return {"success": False, "error": "direct trace unavailable"}
                if message["run_id"] == "parent-run":
                    return {
                        "success": True,
                        "result": {
                            "run_id": "parent-run",
                            "context": {"id": "own-parent", "parent_id": "ctx-1"},
                            "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
                        },
                    }
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_d)
        result = await resolve_global_context(client, "ctx-1")

        assert result.run_id == "own-run"
        assert result.domain == "automation"
        assert result.item_id == "uid_a"
        assert result.other_matches == (
            {
                "domain": "automation",
                "item_id": "uid_b",
                "run_id": "parent-run",
                "match": "parent",
            },
        )
        assert any("direct trace unavailable" in error for error in result.scan_errors)
        direct_reads = [
            call.args[0]
            for call in client.send_websocket_message.await_args_list
            if call.args[0].get("type") == "trace/get"
            and call.args[0].get("run_id") == "own-run"
        ]
        assert len(direct_reads) == 1
