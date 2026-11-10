"""Native context and logbook lookup tests for trace debugging."""

import json

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.trace_lookup import (
    PARENT_CONTEXT_SCAN_LIMIT,
    resolve_context_trace,
    resolve_global_context,
)

from .test_tools_traces import _make_pooled_client


def _context_client(*, contexts=None, traces=None, details=None):
    """Build a client that serves trace/contexts, trace/list and trace/get."""

    def _dispatch(mtype, message):
        if mtype == "trace/contexts":
            return {"success": True, "result": contexts or {}}
        if mtype == "trace/list":
            return {"success": True, "result": traces or []}
        if mtype == "trace/get":
            detail = (details or {}).get(message["run_id"])
            if detail is None:
                return {"success": False, "error": "The trace could not be found"}
            return {"success": True, "result": detail}
        return {"success": False, "error": f"unexpected {mtype}"}

    return _make_pooled_client(_dispatch)


class TestResolveContextTrace:
    @pytest.mark.asyncio
    async def test_own_context_wins_and_scan_is_allowed_for_other_matches(self):
        """A trace's own context id is selected and the scan still aggregates others."""
        client = _context_client(
            contexts={
                "ctx-1": {
                    "run_id": "r1",
                    "domain": "automation",
                    "item_id": "uid",
                }
            }
        )

        match = await resolve_context_trace(client, "automation", "uid", "ctx-1")

        assert match.run_id == "r1"
        assert match.match == "trace"
        assert match.scanned == 0
        assert match.truncated is False
        assert match.other_matches == ()
        sent_types = [
            call.args[0]["type"]
            for call in client.send_websocket_message.await_args_list
        ]
        assert sent_types == ["trace/contexts", "trace/list", "trace/get"]

    @pytest.mark.asyncio
    async def test_parent_context_finds_newest_matching_trace(self):
        """A triggering context resolves by the trace's parent_id."""
        traces = [
            {
                "run_id": "older_match",
                "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
            },
            {
                "run_id": "newer_match",
                "timestamp": {"start": "2026-11-08T16:00:02+00:00"},
            },
            {
                "run_id": "other",
                "timestamp": {"start": "2026-11-08T16:00:03+00:00"},
            },
        ]
        details = {
            "older_match": {
                "run_id": "older_match",
                "context": {"id": "own-1", "parent_id": "parent-1"},
            },
            "newer_match": {
                "run_id": "newer_match",
                "context": {"id": "own-2", "parent_id": "parent-1"},
            },
            "other": {
                "run_id": "other",
                "context": {"id": "own-3", "parent_id": "parent-2"},
            },
        }
        client = _context_client(traces=traces, details=details)

        match = await resolve_context_trace(client, "automation", "uid", "parent-1")

        assert match.run_id == "newer_match"
        assert match.match == "parent"
        # The scan checks every bounded candidate so a later own-context match
        # can still win, even after a parent match was seen.
        assert match.scanned == 3
        assert match.truncated is False

    @pytest.mark.asyncio
    async def test_own_context_match_wins_over_an_earlier_parent_match(self):
        """A run that owns the context id beats a newer run naming it as parent."""
        traces = [
            {
                "run_id": "newer_parent",
                "timestamp": {"start": "2026-11-08T16:00:02+00:00"},
            },
            {
                "run_id": "owner",
                "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
            },
        ]
        details = {
            "newer_parent": {
                "run_id": "newer_parent",
                "context": {"id": "own-1", "parent_id": "ctx-1"},
            },
            "owner": {
                "run_id": "owner",
                "context": {"id": "ctx-1"},
            },
        }
        client = _context_client(traces=traces, details=details)

        match = await resolve_context_trace(client, "automation", "uid", "ctx-1")

        assert match.run_id == "owner"
        assert match.match == "trace"
        assert match.scanned == 2
        assert match.trace_data == details["owner"]

    @pytest.mark.asyncio
    async def test_parent_scan_skips_non_connection_detail_failure(self):
        """A bad trace row is skipped while the scan continues."""
        traces = [
            {"run_id": "bad", "timestamp": {"start": "2026-11-08T16:00:02+00:00"}},
            {"run_id": "good", "timestamp": {"start": "2026-11-08T16:00:01+00:00"}},
        ]

        def _dispatch(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            if mtype == "trace/list":
                return {"success": True, "result": traces}
            if mtype == "trace/get":
                if message["run_id"] == "bad":
                    return {"success": False, "error": "The trace could not be found"}
                return {
                    "success": True,
                    "result": {
                        "run_id": "good",
                        "context": {"id": "own", "parent_id": "parent-1"},
                    },
                }
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_dispatch)

        match = await resolve_context_trace(client, "automation", "uid", "parent-1")

        assert match.run_id == "good"
        assert match.scanned == 2
        assert match.scan_errors == ("bad: The trace could not be found",)

    @pytest.mark.asyncio
    async def test_parent_scan_malformed_list_is_reported(self):
        """A non-list trace/list reply is reported in the not-found error."""

        def _dispatch(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            if mtype == "trace/list":
                return {"success": True, "result": "not a list"}
            return {"success": False, "error": f"unexpected {mtype}"}

        client = _make_pooled_client(_dispatch)

        with pytest.raises(ToolError) as exc_info:
            await resolve_context_trace(client, "automation", "uid", "parent-1")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error["scan_errors"] == ["trace/list returned no row list"]

    @pytest.mark.asyncio
    async def test_parent_scan_detail_transport_failure_is_classified(self):
        """A dead transport during a trace/get scan reports CONNECTION_FAILED."""

        def _dispatch(mtype, message):
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
            return {"success": False, "error": "Failed to connect to Home Assistant"}

        client = _make_pooled_client(_dispatch)

        with pytest.raises(ToolError) as exc_info:
            await resolve_context_trace(client, "automation", "uid", "parent-1")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "CONNECTION_FAILED"

    @pytest.mark.asyncio
    async def test_parent_context_prefers_a_run_over_not_triggered(self):
        """A real run wins over a not-triggered diagnostic with the same parent."""
        traces = [
            {
                "run_id": "diagnostic",
                "not_triggered": True,
                "timestamp": {"start": "2026-11-08T16:00:02+00:00"},
            },
            {
                "run_id": "run",
                "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
            },
        ]
        details = {
            "diagnostic": {
                "run_id": "diagnostic",
                "context": {"id": "own-1", "parent_id": "parent-1"},
            },
            "run": {
                "run_id": "run",
                "context": {"id": "own-2", "parent_id": "parent-1"},
            },
        }
        client = _context_client(traces=traces, details=details)

        match = await resolve_context_trace(client, "automation", "uid", "parent-1")

        assert match.run_id == "run"
        assert match.match == "parent"

    @pytest.mark.asyncio
    async def test_parent_context_falls_back_to_not_triggered(self):
        """A not-triggered diagnostic is returned when no run matches."""
        traces = [
            {
                "run_id": "diagnostic",
                "not_triggered": True,
                "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
            }
        ]
        details = {
            "diagnostic": {
                "run_id": "diagnostic",
                "context": {"id": "own", "parent_id": "parent-1"},
            }
        }
        client = _context_client(traces=traces, details=details)

        match = await resolve_context_trace(client, "automation", "uid", "parent-1")

        assert match.run_id == "diagnostic"
        assert match.match == "parent"

    @pytest.mark.asyncio
    async def test_direct_map_prefers_real_run_over_newer_diagnostic(self):
        """A direct-map diagnostic loses to a newer real run in the scan."""
        traces = [
            {
                "run_id": "diagnostic",
                "not_triggered": True,
                "timestamp": {"start": "2026-11-08T16:00:02+00:00"},
            },
            {
                "run_id": "run",
                "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
            },
        ]
        details = {
            "diagnostic": {
                "run_id": "diagnostic",
                "not_triggered": True,
                "context": {"id": "own-1", "parent_id": "parent-1"},
            },
            "run": {
                "run_id": "run",
                "context": {"id": "own-2", "parent_id": "parent-1"},
            },
        }
        contexts = {
            "parent-1": {
                "run_id": "diagnostic",
                "domain": "automation",
                "item_id": "uid",
            }
        }
        client = _context_client(
            contexts=contexts, traces=traces, details=details
        )

        match = await resolve_context_trace(
            client, "automation", "uid", "parent-1"
        )

        assert match.run_id == "run"
        assert match.match == "parent"
        assert any(
            other["run_id"] == "diagnostic" for other in match.other_matches
        )

    @pytest.mark.asyncio
    async def test_parent_context_not_found(self):
        """No matching parent id returns a structured not-found error."""
        client = _context_client(
            traces=[
                {"run_id": "r1", "timestamp": {"start": "2026-11-08T16:00:01+00:00"}}
            ],
            details={
                "r1": {
                    "run_id": "r1",
                    "context": {"id": "own", "parent_id": "other-parent"},
                }
            },
        )

        with pytest.raises(ToolError) as exc_info:
            await resolve_context_trace(client, "automation", "uid", "missing-parent")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error["scanned"] == 1
        assert error["scan_truncated"] is False

    @pytest.mark.asyncio
    async def test_scan_matches_the_requested_context_id(self):
        """A stale trace/contexts map still resolves the trace's own context."""
        client = _context_client(
            traces=[
                {"run_id": "r1", "timestamp": {"start": "2026-11-08T16:00:01+00:00"}}
            ],
            details={"r1": {"run_id": "r1", "context": {"id": "ctx-1"}}},
        )

        match = await resolve_context_trace(client, "automation", "uid", "ctx-1")

        assert match.run_id == "r1"
        assert match.match == "trace"

    @pytest.mark.asyncio
    async def test_parent_scan_reports_rows_without_a_run_id(self):
        """A stored row the scan cannot read is reported in the not-found error."""
        client = _context_client(
            traces=[
                {"timestamp": {"start": "2026-11-08T16:00:01+00:00"}},
                "junk",
            ]
        )

        with pytest.raises(ToolError) as exc_info:
            await resolve_context_trace(client, "automation", "uid", "parent-1")

        error = json.loads(str(exc_info.value))
        assert error["scan_errors"] == ["trace/list row without a run_id"]

    @pytest.mark.asyncio
    async def test_parent_scan_ignores_a_malformed_detail(self):
        """A trace/get result that is not a mapping cannot match a context."""
        client = _context_client(
            traces=[
                {"run_id": "r1", "timestamp": {"start": "2026-11-08T16:00:01+00:00"}}
            ],
            details={"r1": "junk"},
        )

        with pytest.raises(ToolError) as exc_info:
            await resolve_context_trace(client, "automation", "uid", "parent-1")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error["scanned"] == 1
        assert error["scan_errors"] == ["r1: trace/get returned no trace detail"]

    @pytest.mark.asyncio
    async def test_parent_scan_ignores_a_trace_without_context(self):
        """A stored trace with no context mapping cannot match any id."""
        client = _context_client(
            traces=[
                {"run_id": "r1", "timestamp": {"start": "2026-11-08T16:00:01+00:00"}}
            ],
            details={"r1": {"run_id": "r1"}},
        )

        with pytest.raises(ToolError) as exc_info:
            await resolve_context_trace(client, "automation", "uid", "parent-1")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error["scanned"] == 1
        assert error["scan_errors"] == []

    @pytest.mark.asyncio
    async def test_connection_failure_after_a_parent_match_keeps_it(self):
        """A later dropped connection returns the match the scan already read."""
        traces = [
            {"run_id": "good", "timestamp": {"start": "2026-11-08T16:00:02+00:00"}},
            {"run_id": "dead", "timestamp": {"start": "2026-11-08T16:00:01+00:00"}},
        ]

        def _dispatch(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            if mtype == "trace/list":
                return {"success": True, "result": traces}
            if message["run_id"] == "dead":
                return {
                    "success": False,
                    "error": "Failed to connect to Home Assistant",
                }
            return {
                "success": True,
                "result": {"run_id": "good", "context": {"parent_id": "parent-1"}},
            }

        client = _make_pooled_client(_dispatch)

        match = await resolve_context_trace(client, "automation", "uid", "parent-1")

        assert match.run_id == "good"
        assert match.scan_errors == (
            "dead: the scan stopped after a connection failure",
        )

    @pytest.mark.asyncio
    async def test_parent_context_scan_is_bounded(self):
        """An oversized trace list reports that the scan was truncated."""
        traces = [
            {
                "run_id": f"r{i}",
                "timestamp": {"start": f"2026-11-08T16:{i:02d}:00+00:00"},
            }
            for i in range(PARENT_CONTEXT_SCAN_LIMIT + 1)
        ]
        details = {
            f"r{i}": {
                "run_id": f"r{i}",
                "context": {"id": f"own{i}", "parent_id": f"parent{i}"},
            }
            for i in range(PARENT_CONTEXT_SCAN_LIMIT + 1)
        }
        client = _context_client(traces=traces, details=details)

        with pytest.raises(ToolError) as exc_info:
            await resolve_context_trace(client, "automation", "uid", "missing-parent")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error["scanned"] == PARENT_CONTEXT_SCAN_LIMIT
        assert error["scan_truncated"] is True

    @pytest.mark.asyncio
    async def test_trace_context_transport_failure_is_classified(self):
        """A dead transport during trace/contexts reports CONNECTION_FAILED."""

        def _dispatch(mtype, message):
            return {"success": False, "error": "Failed to connect to Home Assistant"}

        client = _make_pooled_client(_dispatch)

        with pytest.raises(ToolError) as exc_info:
            await resolve_context_trace(client, "automation", "uid", "ctx-1")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "CONNECTION_FAILED"

    @pytest.mark.asyncio
    async def test_parent_scan_transport_failure_is_classified(self):
        """A dead transport during trace/list reports CONNECTION_FAILED."""

        def _dispatch(mtype, message):
            if mtype == "trace/contexts":
                return {"success": True, "result": {}}
            return {"success": False, "error": "Failed to connect to Home Assistant"}

        client = _make_pooled_client(_dispatch)

        with pytest.raises(ToolError) as exc_info:
            await resolve_context_trace(client, "automation", "uid", "parent-1")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "CONNECTION_FAILED"


class TestResolveGlobalContext:
    """Global context resolution mirrors the per-item selection rule."""

    @pytest.mark.asyncio
    async def test_global_path_prefers_real_run_over_newer_diagnostic(self):
        """A newer not-triggered diagnostic does not beat a real run globally."""
        traces = [
            {
                "run_id": "diagnostic",
                "item_id": "uid",
                "not_triggered": True,
                "timestamp": {"start": "2026-11-08T16:00:02+00:00"},
            },
            {
                "run_id": "run",
                "item_id": "uid",
                "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
            },
        ]
        details = {
            "diagnostic": {
                "run_id": "diagnostic",
                "context": {"id": "own-1", "parent_id": "parent-1"},
            },
            "run": {
                "run_id": "run",
                "context": {"id": "own-2", "parent_id": "parent-1"},
            },
        }
        client = _context_client(contexts={}, traces=traces, details=details)

        match = await resolve_global_context(client, "parent-1", domain="automation")

        assert match.run_id == "run"
        assert match.match == "parent"
        assert match.scanned == 2
        assert any(
            other["run_id"] == "diagnostic" for other in match.other_matches
        )


class TestNotTriggeredPreference:
    """Shared selection rule: real runs win over not-triggered diagnostics."""

    @pytest.mark.asyncio
    async def test_public_path_prefers_real_run_over_newer_diagnostic(self):
        """A newer not-triggered diagnostic does not beat a real run on the
        public path."""
        traces = [
            {
                "run_id": "diagnostic",
                "not_triggered": True,
                "timestamp": {"start": "2026-11-08T16:00:02+00:00"},
            },
            {
                "run_id": "run",
                "timestamp": {"start": "2026-11-08T16:00:01+00:00"},
            },
        ]
        details = {
            "diagnostic": {
                "run_id": "diagnostic",
                "context": {"id": "own-1", "parent_id": "parent-1"},
            },
            "run": {
                "run_id": "run",
                "context": {"id": "own-2", "parent_id": "parent-1"},
            },
        }
        client = _context_client(contexts={}, traces=traces, details=details)

        match = await resolve_context_trace(client, "automation", "uid", "parent-1")

        assert match.run_id == "run"
        assert match.match == "parent"
        assert match.scanned == 2
        assert any(
            other["run_id"] == "diagnostic" for other in match.other_matches
        )


