"""Native logbook lookup tests for trace debugging."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_traces import TraceTools
from ha_mcp.tools.trace_lookup import (
    LOGBOOK_ROW_LIMIT,
    TraceLogbook,
    _logbook_window,
    fetch_trace_logbook,
)

from .test_tools_traces import _make_pooled_client


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
    async def test_direct_component_timeout_is_not_replayed_over_rest(self):
        """The direct timeout shape also skips the REST replay."""
        client = _make_pooled_client(lambda mtype, message: {})
        client.send_websocket_message = AsyncMock(
            side_effect=HomeAssistantCommandTimeout("component logbook timed out")
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

        assert logbook.entries == []
        assert logbook.transport is None
        assert any("timed out" in warning for warning in logbook.warnings)
        client.get_logbook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_parent_timeout_stops_further_context_reads(self):
        """A timed-out component read stops the loop and names the skipped context."""
        client = _make_pooled_client(lambda mtype, message: {})
        client.get_logbook = AsyncMock()
        trace = {
            "run_id": "r1",
            "timestamp": {
                "start": "2026-11-08T16:00:00+00:00",
                "finish": "2026-11-08T16:00:01+00:00",
            },
            "context": {"id": "own-1", "parent_id": "parent-1"},
        }

        with patch(
            "ha_mcp.tools.trace_lookup._fetch_component_logbook",
            AsyncMock(
                return_value=(
                    None,
                    ["The component logbook read timed out: boom"],
                    False,
                )
            ),
        ) as component:
            logbook = await fetch_trace_logbook(client, trace)

        assert component.await_count == 1
        assert logbook.entries == []
        assert logbook.transport is None
        assert any("parent-1" in warning for warning in logbook.warnings)
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

        # A missing finish means the trace is still running, so the read must
        # reach the present instead of stopping one minute after the start.
        running_start = datetime.fromisoformat(good_start)
        running_finish = datetime.fromisoformat(bad_finish)
        assert running_finish > running_start + timedelta(minutes=1)

    def test_logbook_window_reaches_now_for_a_running_trace(self):
        """A running trace's logbook window ends at the current time."""
        start = datetime.now(UTC) - timedelta(minutes=5)
        _, finish = _logbook_window({"timestamp": {"start": start.isoformat()}})

        assert datetime.fromisoformat(finish) >= datetime.now(UTC) - timedelta(
            seconds=5
        )

    @pytest.mark.asyncio
    async def test_parent_context_rows_are_included(self):
        """The run's rows are merged first and the triggering context follows."""
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
            "own-1",
            "parent-1",
        ]
        assert logbook.total == 2
        assert logbook.truncated is False
        assert logbook.transport == "component"

    @pytest.mark.asyncio
    async def test_parent_context_read_reaches_back_for_delayed_triggers(self):
        """A `for:` delay can put the triggering event before the run start."""
        client = _make_pooled_client(lambda mtype, message: {})
        calls = []

        async def _fake(client, context_id, start, end):
            calls.append(
                (
                    context_id,
                    datetime.fromisoformat(start),
                    datetime.fromisoformat(end),
                )
            )
            return [], "component", [], False

        trace = {
            "run_id": "r1",
            "timestamp": {
                "start": "2026-11-08T16:00:00+00:00",
                "finish": "2026-11-08T16:00:01+00:00",
            },
            "context": {"id": "own-1", "parent_id": "parent-1"},
        }

        with patch(
            "ha_mcp.tools.trace_lookup._fetch_logbook_context",
            side_effect=_fake,
        ):
            await fetch_trace_logbook(client, trace)

        assert [call[0] for call in calls] == ["own-1", "parent-1"]
        assert calls[1][1] == calls[0][1] - timedelta(minutes=5)
        assert calls[1][2] == calls[0][2]

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

        assert len(logbook.entries) == LOGBOOK_ROW_LIMIT
        assert logbook.total == row_count
        assert logbook.truncated is True

    @pytest.mark.asyncio
    async def test_run_rows_survive_the_cap_before_parent_rows(self):
        """The run's own rows fill the cap before the triggering context's rows."""
        client = _make_pooled_client(lambda mtype, message: {})
        client.send_websocket_message = AsyncMock(
            side_effect=lambda message: {
                "success": True,
                "result": [
                    {
                        "name": f"{message['context_id']} row {i}",
                        "context_id": message["context_id"],
                    }
                    for i in range(
                        LOGBOOK_ROW_LIMIT if message["context_id"] == "own-1" else 10
                    )
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

        assert logbook.total == LOGBOOK_ROW_LIMIT + 10
        assert logbook.truncated is True
        assert {row["context_id"] for row in logbook.entries} == {"own-1"}

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

        assert [row["device_id"] for row in logbook.entries] == ["d2", "d1"]
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

    @pytest.mark.asyncio
    async def test_empty_segment_section_string_skips_the_read(self):
        """A section string with no names is an explicit filter, not no filter."""
        client = self._client()

        with patch(
            "ha_mcp.tools.tools_traces.fetch_trace_logbook",
            AsyncMock(return_value=TraceLogbook([{"name": "Light"}], "rest", [])),
        ) as fetch:
            result = await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test",
                run_id="r1",
                detailed=True,
                sections=" , ",
            )

        fetch.assert_not_awaited()
        assert "logbook_entries" not in result
