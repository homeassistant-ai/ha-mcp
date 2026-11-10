"""Component trace-store and public fallback tests for ha_get_automation_traces."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_traces import TraceTools

from .test_tools_traces import _make_pooled_client


class TestTraceContextComponent:
    """Component trace-store behavior."""

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
        assert any("public fallback scan" in warning for warning in result["warnings"])

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
            "public fallback scan" in warning for warning in result.get("warnings", [])
        )

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


class TestTraceContextFallback:
    """Public fallback scan aggregation and selection."""

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
        result = await TraceTools(client).ha_get_automation_traces(context_id="ctx-1")

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
