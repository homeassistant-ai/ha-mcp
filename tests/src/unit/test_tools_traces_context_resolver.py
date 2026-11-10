"""Direct tests for resolve_context_trace and resolve_global_context."""

import json

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.trace_lookup import (
    resolve_context_trace,
    resolve_global_context,
)

from .test_tools_traces import _make_pooled_client


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
        result = await resolve_context_trace(client, "automation", "uid_1", "ctx-1")

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
        result = await resolve_context_trace(client, "automation", "uid_1", "ctx-1")

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
    async def test_per_item_no_direct_hit_trace_list_connection_error_is_classified(
        self,
    ):
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
        result = await resolve_context_trace(client, "automation", "uid_1", "ctx-1")

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
