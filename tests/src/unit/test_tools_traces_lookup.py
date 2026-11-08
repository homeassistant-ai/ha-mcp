"""Native context and logbook lookup tests for trace debugging."""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_traces import TraceTools
from ha_mcp.tools.trace_lookup import (
    LOGBOOK_ROW_LIMIT,
    PARENT_CONTEXT_SCAN_LIMIT,
    TraceLogbook,
    _logbook_window,
    fetch_trace_logbook,
    resolve_context_trace,
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
    async def test_own_context_wins_without_scanning(self):
        """A trace's own context id resolves from trace/contexts alone."""
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
        sent_types = [
            call.args[0]["type"]
            for call in client.send_websocket_message.await_args_list
        ]
        assert sent_types == ["trace/contexts"]

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
        assert match.scanned == 2
        assert match.truncated is False

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


class TestFetchTraceLogbook:
    @staticmethod
    def _trace():
        return {
            "run_id": "r1",
            "timestamp": {
                "start": "2026-11-08T16:00:00+00:00",
                "finish": "2026-11-08T16:00:01+00:00",
            },
            "context": {"id": "ctx-1"},
        }

    @pytest.mark.asyncio
    async def test_component_logbook_is_preferred(self):
        """The component's Core-processor rows win when advertised."""
        client = _make_pooled_client(lambda mtype, message: {})
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": [{"name": "Light"}]}
        )
        client.get_logbook = AsyncMock()

        with (
            patch(
                "ha_mcp.tools.trace_lookup.get_component_caps",
                AsyncMock(return_value={"logbook_events": True}),
            ),
            patch(
                "ha_mcp.tools.trace_lookup.component_supports",
                return_value=True,
            ),
        ):
            logbook = await fetch_trace_logbook(client, self._trace())

        assert logbook.transport == "component"
        assert logbook.entries == [{"name": "Light"}]
        assert logbook.warnings == []
        client.get_logbook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_component_absent_uses_rest(self):
        """Without the component, Core's REST logbook is the fallback."""
        client = _make_pooled_client(lambda mtype, message: {})
        client.get_logbook = AsyncMock(return_value=[{"name": "REST"}])

        with (
            patch(
                "ha_mcp.tools.trace_lookup.get_component_caps",
                AsyncMock(return_value={}),
            ),
            patch(
                "ha_mcp.tools.trace_lookup.component_supports",
                return_value=False,
            ),
        ):
            logbook = await fetch_trace_logbook(client, self._trace())

        assert logbook.transport == "rest"
        assert logbook.entries == [{"name": "REST"}]
        client.get_logbook.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_component_command_drops_caps_and_uses_rest(self):
        """An older component answer falls back and invalidates the cache."""
        client = _make_pooled_client(lambda mtype, message: {})
        client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error_code": "unknown_command"}
        )
        client.get_logbook = AsyncMock(return_value=[{"name": "REST"}])

        with (
            patch(
                "ha_mcp.tools.trace_lookup.get_component_caps",
                AsyncMock(return_value={"logbook_events": True}),
            ),
            patch(
                "ha_mcp.tools.trace_lookup.component_supports",
                return_value=True,
            ),
            patch("ha_mcp.tools.trace_lookup.invalidate_caps") as invalidate,
        ):
            logbook = await fetch_trace_logbook(client, self._trace())

        invalidate.assert_called_once_with(client)
        assert logbook.transport == "rest"

    @pytest.mark.asyncio
    async def test_component_failure_warns_and_uses_rest(self):
        """A component rejection does not drop the native REST fallback."""
        client = _make_pooled_client(lambda mtype, message: {})
        client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": "component exploded"}
        )
        client.get_logbook = AsyncMock(return_value=[{"name": "REST"}])

        with (
            patch(
                "ha_mcp.tools.trace_lookup.get_component_caps",
                AsyncMock(return_value={"logbook_events": True}),
            ),
            patch(
                "ha_mcp.tools.trace_lookup.component_supports",
                return_value=True,
            ),
        ):
            logbook = await fetch_trace_logbook(client, self._trace())

        assert logbook.transport == "rest"
        assert any("component exploded" in warning for warning in logbook.warnings)

    @pytest.mark.asyncio
    async def test_rest_non_list_reply_is_warned(self):
        """A malformed REST reply is a warning, not a row list."""
        client = _make_pooled_client(lambda mtype, message: {})
        client.get_logbook = AsyncMock(return_value={"not": "a list"})

        with (
            patch(
                "ha_mcp.tools.trace_lookup.get_component_caps",
                AsyncMock(return_value={}),
            ),
            patch(
                "ha_mcp.tools.trace_lookup.component_supports",
                return_value=False,
            ),
        ):
            logbook = await fetch_trace_logbook(client, self._trace())

        assert logbook.entries == []
        assert logbook.transport is None
        assert any("not a row list" in warning for warning in logbook.warnings)

    @pytest.mark.asyncio
    async def test_component_probe_failure_warns_and_uses_rest(self):
        """A failed component probe still leaves the REST fallback usable."""
        client = _make_pooled_client(lambda mtype, message: {})
        client.get_logbook = AsyncMock(return_value=[{"name": "REST"}])

        with patch(
            "ha_mcp.tools.trace_lookup.get_component_caps",
            AsyncMock(side_effect=RuntimeError("probe failed")),
        ):
            logbook = await fetch_trace_logbook(client, self._trace())

        assert logbook.transport == "rest"
        assert any("probe failed" in warning for warning in logbook.warnings)

    @pytest.mark.asyncio
    async def test_component_transport_failure_warns_and_uses_rest(self):
        """A component transport failure falls back to REST with a warning."""
        client = _make_pooled_client(lambda mtype, message: {})
        client.send_websocket_message = AsyncMock(
            side_effect=RuntimeError("component down")
        )
        client.get_logbook = AsyncMock(return_value=[{"name": "REST"}])

        with (
            patch(
                "ha_mcp.tools.trace_lookup.get_component_caps",
                AsyncMock(return_value={"logbook_events": True}),
            ),
            patch(
                "ha_mcp.tools.trace_lookup.component_supports",
                return_value=True,
            ),
        ):
            logbook = await fetch_trace_logbook(client, self._trace())

        assert logbook.transport == "rest"
        assert any("component down" in warning for warning in logbook.warnings)

    @pytest.mark.asyncio
    async def test_component_timeout_is_not_replayed_over_rest(self):
        """A timed-out recorder query must not be re-run on REST."""
        client = _make_pooled_client(lambda mtype, message: {})
        timeout = HomeAssistantCommandTimeout("component logbook timed out")
        wrapped = HomeAssistantConnectionError("no answer from Home Assistant")
        wrapped.__cause__ = timeout
        client.send_websocket_message = AsyncMock(side_effect=wrapped)
        client.get_logbook = AsyncMock(return_value=[{"name": "REST"}])

        with (
            patch(
                "ha_mcp.tools.trace_lookup.get_component_caps",
                AsyncMock(return_value={"logbook_events": True}),
            ),
            patch(
                "ha_mcp.tools.trace_lookup.component_supports",
                return_value=True,
            ),
        ):
            logbook = await fetch_trace_logbook(client, self._trace())

        assert logbook.entries == []
        assert logbook.transport is None
        assert any("timed out" in warning for warning in logbook.warnings)
        client.get_logbook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_component_non_list_reply_warns_and_uses_rest(self):
        """A component reply without a row list falls back to REST."""
        client = _make_pooled_client(lambda mtype, message: {})
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": "not a list"}
        )
        client.get_logbook = AsyncMock(return_value=[{"name": "REST"}])

        with (
            patch(
                "ha_mcp.tools.trace_lookup.get_component_caps",
                AsyncMock(return_value={"logbook_events": True}),
            ),
            patch(
                "ha_mcp.tools.trace_lookup.component_supports",
                return_value=True,
            ),
        ):
            logbook = await fetch_trace_logbook(client, self._trace())

        assert logbook.transport == "rest"
        assert any("not contain a row list" in warning for warning in logbook.warnings)

    def test_logbook_window_handles_invalid_and_missing_timestamps(self):
        """Malformed or missing trace timestamps still produce an ordered window."""
        invalid_start, invalid_finish = _logbook_window(
            {"timestamp": {"start": "not-a-date", "finish": "also-bad"}}
        )
        missing_start, missing_finish = _logbook_window({"timestamp": {}})
        string_timestamp_start, string_timestamp_finish = _logbook_window(
            {"timestamp": "not-a-dict"}
        )
        bad_start, good_finish = _logbook_window(
            {
                "timestamp": {
                    "start": "not-a-date",
                    "finish": "2026-11-08T16:00:00+00:00",
                }
            }
        )
        good_start, bad_finish = _logbook_window(
            {
                "timestamp": {
                    "start": "2026-11-08T16:00:00+00:00",
                    "finish": "also-bad",
                }
            }
        )
        reversed_start, reversed_finish = _logbook_window(
            {
                "timestamp": {
                    "start": "2026-11-08T16:00:05+00:00",
                    "finish": "2026-11-08T16:00:00+00:00",
                }
            }
        )
        naive_start, naive_finish = _logbook_window(
            {
                "timestamp": {
                    "start": "2026-11-08T16:00:00",
                    "finish": "2026-11-08T16:00:01",
                }
            }
        )

        for start, finish in (
            (invalid_start, invalid_finish),
            (missing_start, missing_finish),
            (string_timestamp_start, string_timestamp_finish),
            (bad_start, good_finish),
            (good_start, bad_finish),
            (reversed_start, reversed_finish),
            (naive_start, naive_finish),
        ):
            parsed_start = datetime.fromisoformat(start)
            parsed_finish = datetime.fromisoformat(finish)
            assert parsed_start < parsed_finish
            assert parsed_finish - parsed_start <= timedelta(minutes=2)

        assert datetime.fromisoformat(good_finish) == datetime.fromisoformat(
            "2026-11-08T16:00:02+00:00"
        )

    @pytest.mark.asyncio
    async def test_parent_context_rows_are_included(self):
        """The triggering context's rows are merged before the run's own rows."""
        client = _make_pooled_client(lambda mtype, message: {})
        client.send_websocket_message = AsyncMock(
            side_effect=lambda message: {
                "success": True,
                "result": [
                    {
                        "name": "Parent row",
                        "context_id": message["context_id"],
                        "message": "triggered",
                    }
                ],
            }
        )

        trace = {
            "run_id": "r1",
            "timestamp": {
                "start": "2026-11-08T16:00:00+00:00",
                "finish": "2026-11-08T16:00:01+00:00",
            },
            "context": {"id": "own-1", "parent_id": "parent-1"},
        }

        with (
            patch(
                "ha_mcp.tools.trace_lookup.get_component_caps",
                AsyncMock(return_value={"logbook_events": True}),
            ),
            patch(
                "ha_mcp.tools.trace_lookup.component_supports",
                return_value=True,
            ),
        ):
            logbook = await fetch_trace_logbook(client, trace)

        assert [row["context_id"] for row in logbook.entries] == [
            "parent-1",
            "own-1",
        ]
        assert logbook.total == 2
        assert logbook.truncated is False
        assert logbook.transport == "component"

    @pytest.mark.asyncio
    async def test_logbook_rows_are_capped(self):
        """The row cap reports the full total and a truncation flag."""
        row_count = LOGBOOK_ROW_LIMIT + 10
        client = _make_pooled_client(lambda mtype, message: {})
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [{"name": f"Row {i}"} for i in range(row_count)],
            }
        )

        with (
            patch(
                "ha_mcp.tools.trace_lookup.get_component_caps",
                AsyncMock(return_value={"logbook_events": True}),
            ),
            patch(
                "ha_mcp.tools.trace_lookup.component_supports",
                return_value=True,
            ),
        ):
            logbook = await fetch_trace_logbook(client, self._trace())

        assert len(logbook.entries) < logbook.total
        assert logbook.total == row_count
        assert logbook.truncated is True

    @pytest.mark.asyncio
    async def test_mixed_transports_are_reported(self):
        """One context from the component and one from REST reports mixed."""
        client = _make_pooled_client(lambda mtype, message: {})
        client.get_logbook = AsyncMock(return_value=[{"name": "Own row"}])
        trace = {
            "run_id": "r1",
            "timestamp": {
                "start": "2026-11-08T16:00:00+00:00",
                "finish": "2026-11-08T16:00:01+00:00",
            },
            "context": {"id": "own", "parent_id": "parent"},
        }

        with patch(
            "ha_mcp.tools.trace_lookup._fetch_component_logbook",
            AsyncMock(
                side_effect=[
                    ([{"name": "Parent row"}], [], True),
                    (None, [], True),
                ]
            ),
        ):
            logbook = await fetch_trace_logbook(client, trace)

        assert logbook.transport == "mixed"
        assert logbook.total == 2

    @pytest.mark.asyncio
    async def test_duplicate_logbook_rows_are_dropped(self):
        """The parent and own context queries may return the same row once."""
        client = _make_pooled_client(lambda mtype, message: {})
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {
                        "name": "Same row",
                        "context_id": "ctx",
                        "message": "triggered",
                        "when": "2026-11-08T16:00:00+00:00",
                    }
                ],
            }
        )
        trace = {
            "run_id": "r1",
            "timestamp": {
                "start": "2026-11-08T16:00:00+00:00",
                "finish": "2026-11-08T16:00:01+00:00",
            },
            "context": {"id": "ctx", "parent_id": "parent"},
        }

        with (
            patch(
                "ha_mcp.tools.trace_lookup.get_component_caps",
                AsyncMock(return_value={"logbook_events": True}),
            ),
            patch(
                "ha_mcp.tools.trace_lookup.component_supports",
                return_value=True,
            ),
        ):
            logbook = await fetch_trace_logbook(client, trace)

        assert len(logbook.entries) == 1
        assert logbook.total == 1

    @pytest.mark.asyncio
    async def test_rows_that_differ_by_device_are_kept(self):
        """Two device events with the same projection and time stay distinct."""
        client = _make_pooled_client(lambda mtype, message: {})
        client.send_websocket_message = AsyncMock(
            side_effect=lambda message: {
                "success": True,
                "result": [
                    {
                        "name": "Button",
                        "context_id": "ctx",
                        "message": "triggered",
                        "when": "2026-11-08T16:00:00+00:00",
                        "domain": "zha",
                        "device_id": "d1"
                        if message["context_id"] == "parent"
                        else "d2",
                    }
                ],
            }
        )
        trace = {
            "run_id": "r1",
            "timestamp": {
                "start": "2026-11-08T16:00:00+00:00",
                "finish": "2026-11-08T16:00:01+00:00",
            },
            "context": {"id": "own", "parent_id": "parent"},
        }

        with (
            patch(
                "ha_mcp.tools.trace_lookup.get_component_caps",
                AsyncMock(return_value={"logbook_events": True}),
            ),
            patch(
                "ha_mcp.tools.trace_lookup.component_supports",
                return_value=True,
            ),
        ):
            logbook = await fetch_trace_logbook(client, trace)

        assert [row["device_id"] for row in logbook.entries] == ["d1", "d2"]
        assert logbook.total == 2

    @pytest.mark.asyncio
    async def test_rest_failure_returns_a_warning_and_empty_entries(self):
        """A failed logbook read degrades to a warning, not a trace failure."""
        client = _make_pooled_client(lambda mtype, message: {})
        client.get_logbook = AsyncMock(side_effect=RuntimeError("logbook down"))

        with (
            patch(
                "ha_mcp.tools.trace_lookup.get_component_caps",
                AsyncMock(return_value={}),
            ),
            patch(
                "ha_mcp.tools.trace_lookup.component_supports",
                return_value=False,
            ),
        ):
            logbook = await fetch_trace_logbook(client, self._trace())

        assert logbook.entries == []
        assert logbook.transport is None
        assert any("logbook down" in warning for warning in logbook.warnings)

    @pytest.mark.asyncio
    async def test_missing_trace_context_returns_empty_entries(self):
        """A trace without a context id cannot query the logbook."""
        client = _make_pooled_client(lambda mtype, message: {})
        client.get_logbook = AsyncMock()

        logbook = await fetch_trace_logbook(client, {"run_id": "r1", "timestamp": {}})

        assert logbook.entries == []
        assert logbook.transport is None
        assert any("context" in warning.lower() for warning in logbook.warnings)
        client.get_logbook.assert_not_awaited()


class TestTraceToolLogbook:
    @staticmethod
    def _client():
        def _dispatch(mtype, message):
            if mtype == "config/entity_registry/get":
                return {"success": True, "result": {"unique_id": "uid_1"}}
            if mtype == "trace/get":
                return {
                    "success": True,
                    "result": {
                        "run_id": "r1",
                        "state": "stopped",
                        "timestamp": {
                            "start": "2026-11-08T16:00:00+00:00",
                            "finish": "2026-11-08T16:00:01+00:00",
                        },
                        "context": {"id": "ctx-1"},
                        "trace": {},
                    },
                }
            return {"success": False, "error": f"unexpected {mtype}"}

        return _make_pooled_client(_dispatch)

    @pytest.mark.asyncio
    async def test_detailed_true_adds_logbook_and_transport(self):
        client = self._client()

        with patch(
            "ha_mcp.tools.tools_traces.fetch_trace_logbook",
            AsyncMock(
                return_value=TraceLogbook(
                    [{"name": "Light", "message": "turned on"}],
                    "component",
                    [],
                )
            ),
        ):
            result = await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test",
                run_id="r1",
                detailed=True,
            )

        assert result["logbook_entries"] == [{"name": "Light", "message": "turned on"}]
        assert result["logbook_transport"] == "component"
        assert "warnings" not in result

    @pytest.mark.asyncio
    async def test_logbook_warnings_reach_the_tool_response(self):
        client = self._client()

        with patch(
            "ha_mcp.tools.tools_traces.fetch_trace_logbook",
            AsyncMock(
                return_value=TraceLogbook(
                    [], None, ["Could not read the trace logbook"]
                )
            ),
        ):
            result = await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test",
                run_id="r1",
                detailed=True,
            )

        assert result["logbook_entries"] == []
        assert result["warnings"] == ["Could not read the trace logbook"]

    @pytest.mark.asyncio
    async def test_sections_logbook_fetches_without_detailed(self):
        client = self._client()

        with patch(
            "ha_mcp.tools.tools_traces.fetch_trace_logbook",
            AsyncMock(return_value=TraceLogbook([{"name": "Light"}], "rest", [])),
        ) as fetch:
            result = await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test",
                run_id="r1",
                sections="logbook",
            )

        fetch.assert_awaited_once()
        assert result["logbook_entries"] == [{"name": "Light"}]
        assert result["logbook_transport"] == "rest"

    @pytest.mark.asyncio
    async def test_sections_without_logbook_skip_the_read(self):
        """An explicit section list without logbook never queries the recorder."""
        client = self._client()

        with patch(
            "ha_mcp.tools.tools_traces.fetch_trace_logbook",
            AsyncMock(return_value=TraceLogbook([{"name": "Light"}], "rest", [])),
        ) as fetch:
            result = await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test",
                run_id="r1",
                detailed=True,
                sections="context",
            )

        fetch.assert_not_awaited()
        assert "logbook_entries" not in result
        assert result["context"] == {"id": "ctx-1"}
