"""Unit tests for ha_get_logs(source='error_log', structured=True).

Covers:
- _get_component_prefix: dotted-segment extraction
- _parse_error_log_structured: parsing, dedup, grouping, filters, top_n
- ha_get_logs structured=True end-to-end: response shape, backward compat,
  warning when structured used with wrong source
"""

import asyncio
import inspect
import json
import textwrap
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError
from pydantic import TypeAdapter

from ha_mcp.client.rest_client import ErrorLogPage, HomeAssistantAuthError
from ha_mcp.tools.error_log_parsing import (
    _DEFAULT_TOP_N,
    _MAX_COMPONENTS,
    _MAX_MESSAGE_LEN,
    _TRUNCATION_MARK,
    STRUCTURED_ERROR_LOG_WINDOW_LINES,
    _get_component_prefix,
    _parse_error_log_structured,
)
from ha_mcp.tools.log_common import (
    DEFAULT_LOG_LIMIT,
    MAX_LIMIT,
    SUPERVISOR_SEARCH_WINDOW_LINES,
)
from ha_mcp.tools.tools_logs import register_logs_tools

# ---------------------------------------------------------------------------
# Sample log fixtures
# ---------------------------------------------------------------------------

_SAMPLE_LOG = textwrap.dedent("""\
    2026-05-27 10:00:01.123 ERROR (MainThread) [homeassistant.components.zha.core.device] Device timeout
    2026-05-27 10:00:02.456 ERROR (MainThread) [homeassistant.components.zha.core.device] Device timeout
    2026-05-27 10:00:03.789 WARNING (MainThread) [homeassistant.components.tuya] Couldn't fetch data
    2026-05-27 10:00:04.000 ERROR (MainThread) [homeassistant.components.zha.core.device] Device timeout
    2026-05-27 10:00:05.111 INFO (MainThread) [homeassistant.loader] Loaded component: zha
    2026-05-27 10:00:06.222 ERROR (MainThread) [homeassistant.components.tuya] Auth failed
    2026-05-27 10:00:07.333 DEBUG (MainThread) [homeassistant.core] State changed
    2026-05-27 10:00:08.444 CRITICAL (MainThread) [homeassistant.components.recorder] DB error
""")

_EMPTY_LOG = ""

_UNPARSEABLE_LOG = textwrap.dedent("""\
    Not a valid log line
    Another unparseable line
    2026-05-27 10:00:01 INVALIDLEVEL (thread) [logger] message
""")


def _many_small_issues_log() -> str:
    """`chatty` out-totals `noisy` while every single one of its issues is smaller.

    Both sorts are load-bearing against this fixture:

    * `top_issues` ranks per issue, so `noisy`'s one count-5 issue must come
      first — but `chatty`'s issues are emitted first, so insertion order alone
      gives the opposite answer.
    * `by_component` ranks per component *total*, so `chatty` (10 issues x2 = 20)
      must come first — but it is built by iterating the already-count-sorted
      issues, so insertion order alone again gives the opposite answer.
    """
    lines = [
        f"2026-05-27 10:{i:02d}:{repeat:02d}.000 ERROR (MainThread) "
        f"[chatty.sub.mod] issue {i}"
        for i in range(10)
        for repeat in range(2)
    ]
    lines += [
        f"2026-05-27 11:00:{i:02d}.000 ERROR (MainThread) [noisy.sub.mod] frequent"
        for i in range(5)
    ]
    return "\n".join(lines) + "\n"


_UNSORTED_LOG = _many_small_issues_log()

# Timestamps that neither ascend nor descend, so first-wins/last-wins and
# min/max disagree on both the per-issue bounds and the covered window.
_OUT_OF_ORDER_LOG = textwrap.dedent("""\
    2026-05-27 10:00:09.000 ERROR (MainThread) [a.b.c] boom
    2026-05-27 10:00:01.000 ERROR (MainThread) [a.b.c] boom
    2026-05-27 10:00:05.000 ERROR (MainThread) [a.b.c] boom
""")

# Since Python 3.10 an unnamed thread is called "Thread-N (target_fn)", so the
# thread field of a log line can itself contain parentheses.
_NESTED_PAREN_THREAD_LOG = (
    "2026-05-27 10:00:01.000 ERROR (Thread-4 (_read_loop)) "
    "[pychromecast.socket_client] Connection reset\n"
)

# What Supervisor-backed installs (add-on / HAOS / supervised) actually return:
# HA Core's journald stream, ANSI-coloured. Only container/pip installs read the
# plain file the other fixtures imitate.
_ANSI_LOG = (
    "\x1b[31m2026-05-27 10:00:01.123 ERROR (MainThread) "
    "[homeassistant.components.zha] Device timeout\x1b[0m\n"
    "\x1b[33m2026-05-27 10:00:02.456 WARNING (MainThread) "
    "[homeassistant.components.tuya] Slow response\x1b[0m\n"
    "\x1b[31m2026-05-27 10:00:03.789 ERROR (MainThread) "
    "[homeassistant.components.zha] Device timeout\x1b[0m\n"
)

# A real error followed by its traceback — the traceback lines are unparseable
# by design, which is what makes parsed_entries < total_raw_lines meaningful.
_TRACEBACK_LOG = textwrap.dedent("""\
    2026-05-27 10:00:01.123 ERROR (MainThread) [homeassistant.components.zha] Update failed
    Traceback (most recent call last):
      File "/usr/src/homeassistant/zha.py", line 42, in _async_update
        await self._device.read()
    ValueError: device did not respond
""")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(log_text: str = _SAMPLE_LOG, has_more: bool = False) -> MagicMock:
    """Client stub whose ``get_error_log`` answers with one page.

    ``has_more`` is the client's verdict, not a property of ``log_text``: on
    journald only the client can establish it (see ``_journald_error_log_page``),
    so the tool layer takes it as given.
    """
    client = MagicMock()
    client.get_error_log = AsyncMock(
        return_value=ErrorLogPage(text=log_text, has_more=has_more)
    )
    return client


def _get_tool_param_annotation(tool_name: str, param_name: str) -> Any:
    """Annotation of ``param_name`` on the REGISTERED tool, via real FastMCP.

    Mirrors ``test_json_string_param_coercion.py``: FastMCP builds its argument
    TypeAdapter from the registered fn's signature, so the annotation read here
    is exactly what validates inbound MCP traffic.
    """
    from fastmcp import FastMCP

    async def _inner() -> Any:
        mcp = FastMCP("test")
        register_logs_tools(mcp, _make_client())
        tool = await mcp.get_tool(tool_name)
        return inspect.signature(tool.fn).parameters[param_name].annotation

    return asyncio.run(_inner())


def _register_and_collect(client: Any) -> dict[str, Any]:
    collected: dict[str, Any] = {}

    def _tool(**_kwargs: Any) -> Any:
        def _wrap(fn: Any) -> Any:
            collected[fn.__name__] = fn
            return fn

        return _wrap

    mcp = SimpleNamespace(tool=_tool)
    register_logs_tools(mcp, client)
    return collected


# ---------------------------------------------------------------------------
# _get_component_prefix
# ---------------------------------------------------------------------------


class TestGetComponentPrefix:
    def test_three_segment_logger(self):
        assert (
            _get_component_prefix("homeassistant.components.zha")
            == "homeassistant.components.zha"
        )

    def test_deeper_logger_truncates_to_three(self):
        assert (
            _get_component_prefix("homeassistant.components.zha.core.device")
            == "homeassistant.components.zha"
        )

    def test_two_segment_logger_returns_as_is(self):
        assert (
            _get_component_prefix("homeassistant.components")
            == "homeassistant.components"
        )

    def test_single_segment_logger(self):
        assert _get_component_prefix("homeassistant") == "homeassistant"

    def test_custom_component_prefix_is_the_integration_not_the_module(self):
        # A custom integration's identity is custom_components.<domain>. Taking
        # three segments would make each of its modules its own component, so
        # one integration's totals split and consume several cap slots.
        assert (
            _get_component_prefix("custom_components.my_integration.sensor")
            == "custom_components.my_integration"
        )
        assert _get_component_prefix(
            "custom_components.my_integration.switch"
        ) == _get_component_prefix("custom_components.my_integration.sensor")

    def test_custom_component_without_module_is_unchanged(self):
        assert (
            _get_component_prefix("custom_components.my_integration")
            == "custom_components.my_integration"
        )


# ---------------------------------------------------------------------------
# _parse_error_log_structured
# ---------------------------------------------------------------------------


class TestParseErrorLogStructured:
    def test_response_shape(self):
        result = _parse_error_log_structured(_SAMPLE_LOG)
        assert result["success"] is True
        assert result["source"] == "error_log"
        assert result["structured"] is True
        assert "summary" in result
        assert "top_issues" in result
        assert "by_component" in result

    def test_summary_counts(self):
        result = _parse_error_log_structured(_SAMPLE_LOG)
        s = result["summary"]
        # 8 raw lines
        assert s["total_raw_lines"] == 8
        # All 8 lines match the HA log format
        assert s["parsed_entries"] == 8
        # unique (logger, message) pairs:
        # (zha.core.device, "Device timeout") ×3 → 1 unique
        # (tuya, "Couldn't fetch data") ×1 → 1
        # (loader, "Loaded...") ×1 → 1
        # (tuya, "Auth failed") ×1 → 1
        # (core, "State changed") ×1 → 1
        # (recorder, "DB error") ×1 → 1
        assert s["unique_issues"] == 6

    def test_deduplication_occurrence_count(self):
        result = _parse_error_log_structured(_SAMPLE_LOG)
        # "Device timeout" appears 3 times
        device_timeout = next(
            i for i in result["top_issues"] if i["message"] == "Device timeout"
        )
        assert device_timeout["count"] == 3
        assert device_timeout["logger"] == "homeassistant.components.zha.core.device"
        assert device_timeout["component"] == "homeassistant.components.zha"

    def test_top_issues_sorted_by_count_desc(self):
        # _UNSORTED_LOG emits the *least* frequent issues first, so this fails if
        # the sort is removed. Asserting on _SAMPLE_LOG would not: its insertion
        # order already equals its sorted order.
        result = _parse_error_log_structured(_UNSORTED_LOG)
        counts = [i["count"] for i in result["top_issues"]]
        assert counts == sorted(counts, reverse=True)
        assert counts[0] > counts[-1], "fixture must have a non-flat count spread"
        assert result["top_issues"][0]["message"] == "frequent"

    def test_equal_counts_are_broken_by_severity_then_recency(self):
        # Python's sort is stable, so count alone returns the OLDEST of a tied
        # run — and logs whose messages embed ids/ports tie at count 1 for
        # everything, turning "top N" into "the N oldest".
        log = textwrap.dedent("""\
            2026-05-27 10:00:00.000 ERROR (MainThread) [a.b.c] oldest
            2026-05-27 10:00:01.000 WARNING (MainThread) [a.b.c] middle
            2026-05-27 10:00:02.000 WARNING (MainThread) [a.b.c] newest
        """)
        messages = [
            i["message"] for i in _parse_error_log_structured(log)["top_issues"]
        ]
        # All three tie at count 1: ERROR outranks the WARNINGs on severity, and
        # the newer WARNING outranks the older one.
        assert messages == ["oldest", "newest", "middle"]

    def test_top_issues_first_and_last_seen(self):
        result = _parse_error_log_structured(_SAMPLE_LOG)
        device_timeout = next(
            i for i in result["top_issues"] if i["message"] == "Device timeout"
        )
        assert device_timeout["first_seen"] == "2026-05-27 10:00:01.123"
        assert device_timeout["last_seen"] == "2026-05-27 10:00:04.000"

    def test_seen_bounds_survive_out_of_order_lines(self):
        # first_seen/last_seen are the bounds of the occurrences, not the first
        # and last line that happened to arrive. Last-write-wins drags last_seen
        # backwards on an unordered log, and the recency tiebreaker then ranks
        # the issue by a timestamp it never actually ended at.
        issue = _parse_error_log_structured(_OUT_OF_ORDER_LOG)["top_issues"][0]
        assert issue["first_seen"] == "2026-05-27 10:00:01.000"
        assert issue["last_seen"] == "2026-05-27 10:00:09.000"

    def test_by_component_contains_all_components(self):
        result = _parse_error_log_structured(_SAMPLE_LOG)
        assert "homeassistant.components.zha" in result["by_component"]
        assert "homeassistant.components.tuya" in result["by_component"]
        assert "homeassistant.loader" in result["by_component"]

    def test_by_component_sorted_by_total_occurrences(self):
        # by_component is filled by iterating the already-count-sorted issues,
        # so insertion order follows the largest SINGLE issue. In _UNSORTED_LOG
        # the component with the largest single issue is not the one with the
        # largest total, so only the by_component sort can produce this order.
        result = _parse_error_log_structured(_UNSORTED_LOG)
        totals = [v["total_occurrences"] for v in result["by_component"].values()]
        assert totals == sorted(totals, reverse=True)
        (first_name, first), (second_name, second) = list(
            result["by_component"].items()
        )[:2]
        assert (first_name, second_name) == ("chatty.sub.mod", "noisy.sub.mod")
        assert first["total_occurrences"] > second["total_occurrences"]
        # The winner wins on volume with many small issues, not one big one.
        assert first["issue_count"] > second["issue_count"]
        top_issue = result["top_issues"][0]
        assert top_issue["component"] == second_name, (
            "the per-issue ranking must still favour the single biggest issue, "
            "otherwise the two sorts are not distinguishable"
        )

    def test_custom_component_modules_roll_up_into_one_bucket(self):
        # The prefix helper is unit-tested above, but nothing pinned that the
        # summary actually routes through it: a custom integration that logs
        # from several modules must appear as one component, not one per
        # module, or by_component fragments exactly where HACS users read it.
        log = textwrap.dedent("""\
            2026-05-27 10:00:01.000 ERROR (MainThread) [custom_components.foo.sensor] a
            2026-05-27 10:00:02.000 ERROR (MainThread) [custom_components.foo.climate] b
            2026-05-27 10:00:03.000 ERROR (MainThread) [custom_components.foo.sensor] a
        """)
        result = _parse_error_log_structured(log)
        assert list(result["by_component"]) == ["custom_components.foo"]
        bucket = result["by_component"]["custom_components.foo"]
        assert bucket["total_occurrences"] == 3
        assert bucket["issue_count"] == 2
        assert {i["component"] for i in result["top_issues"]} == {
            "custom_components.foo"
        }

    def test_by_component_issue_count(self):
        result = _parse_error_log_structured(_SAMPLE_LOG)
        zha = result["by_component"]["homeassistant.components.zha"]
        assert zha["total_occurrences"] == 3
        assert zha["issue_count"] == 1

    def test_level_filter_error_only(self):
        result = _parse_error_log_structured(_SAMPLE_LOG, level="ERROR")
        for issue in result["top_issues"]:
            assert issue["level"] == "ERROR"
        # Only ERROR-level loggers should appear in by_component
        # INFO, DEBUG, WARNING, CRITICAL filtered out
        assert "homeassistant.loader" not in result["by_component"]

    def test_level_filter_warning(self):
        result = _parse_error_log_structured(_SAMPLE_LOG, level="WARNING")
        assert result["summary"]["parsed_entries"] == 1
        assert result["top_issues"][0]["message"] == "Couldn't fetch data"

    def test_search_filter_case_insensitive(self):
        result = _parse_error_log_structured(_SAMPLE_LOG, search="timeout")
        assert result["summary"]["parsed_entries"] == 3
        assert all("timeout" in i["message"].lower() for i in result["top_issues"])

    def test_search_filter_on_logger_name(self):
        result = _parse_error_log_structured(_SAMPLE_LOG, search="recorder")
        assert result["summary"]["parsed_entries"] == 1
        assert result["top_issues"][0]["logger"] == "homeassistant.components.recorder"

    def test_top_n_limits_top_issues(self):
        result = _parse_error_log_structured(_SAMPLE_LOG, top_n=2)
        assert len(result["top_issues"]) == 2
        assert result["summary"]["showing_top_n"] == 2

    def test_top_n_larger_than_issues_returns_all(self):
        result = _parse_error_log_structured(_SAMPLE_LOG, top_n=100)
        assert len(result["top_issues"]) == result["summary"]["unique_issues"]
        assert result["summary"]["showing_top_n"] == result["summary"]["unique_issues"]

    def test_empty_log_returns_zero_counts(self):
        result = _parse_error_log_structured(_EMPTY_LOG)
        assert result["success"] is True
        assert result["summary"]["total_raw_lines"] == 0
        assert result["summary"]["parsed_entries"] == 0
        assert result["summary"]["unique_issues"] == 0
        assert result["top_issues"] == []
        assert result["by_component"] == {}

    def test_window_reports_the_slice_that_was_read(self):
        # Counts are bounded by the fetched window; without it the caller cannot
        # tell a 2-hour journald slice from the instance's whole history.
        summary = _parse_error_log_structured(_SAMPLE_LOG)["summary"]
        assert summary["window_start"] == "2026-05-27 10:00:01.123"
        assert summary["window_end"] == "2026-05-27 10:00:08.444"

    def test_window_covers_filtered_out_entries_too(self):
        # The window describes the log slice that was read, not the summary's
        # contents — a level filter must not shrink it.
        summary = _parse_error_log_structured(_SAMPLE_LOG, level="WARNING")["summary"]
        assert summary["window_start"] == "2026-05-27 10:00:01.123"
        assert summary["window_end"] == "2026-05-27 10:00:08.444"

    def test_window_is_none_when_nothing_parsed(self):
        summary = _parse_error_log_structured(_UNPARSEABLE_LOG)["summary"]
        assert summary["window_start"] is None
        assert summary["window_end"] is None

    def test_window_is_min_and_max_not_first_and_last_line(self):
        # The three window tests above all read ascending fixtures, where
        # first-wins/last-wins and min/max give the same answer — so none of
        # them would redden if the bounds regressed to "the line that arrived
        # first" and "the line that arrived last".
        summary = _parse_error_log_structured(_OUT_OF_ORDER_LOG)["summary"]
        assert summary["window_start"] == "2026-05-27 10:00:01.000"
        assert summary["window_end"] == "2026-05-27 10:00:09.000"

    def test_mixed_date_separators_do_not_corrupt_ordering(self):
        # The line regex accepts both "2026-05-27 10:00:00" and the ISO
        # "2026-05-27T10:00:00", and every timestamp comparison is a string
        # compare: " " (0x20) sorts below every digit, "T" (0x54) above them.
        # Un-normalized, the T-form line wins every max() and loses every
        # min() regardless of the time it carries, which corrupts the window,
        # the first/last_seen bounds and the recency tiebreaker at once.
        log = textwrap.dedent("""\
            2026-05-27T10:00:05.000 ERROR (MainThread) [a.b.c] boom
            2026-05-27 10:00:09.000 ERROR (MainThread) [a.b.c] boom
            2026-05-27T10:00:01.000 ERROR (MainThread) [a.b.c] boom
        """)
        result = _parse_error_log_structured(log)
        issue = result["top_issues"][0]
        assert issue["count"] == 3
        assert issue["first_seen"] == "2026-05-27 10:00:01.000"
        assert issue["last_seen"] == "2026-05-27 10:00:09.000"
        summary = result["summary"]
        assert summary["window_start"] == "2026-05-27 10:00:01.000"
        assert summary["window_end"] == "2026-05-27 10:00:09.000"

    def test_nested_paren_thread_name_parses(self):
        # threading.Thread(target=fn) is named "Thread-N (fn)" since 3.10, and
        # libraries that spawn raw threads for device I/O log under that name.
        # A thread group that cannot cross a nested ')' drops those lines into
        # unparseable_lines, where nothing discloses that a whole component is
        # structurally missing from top_issues and by_component.
        result = _parse_error_log_structured(_NESTED_PAREN_THREAD_LOG)
        assert result["summary"]["unparseable_lines"] == 0
        assert result["summary"]["parsed_entries"] == 1
        issue = result["top_issues"][0]
        assert issue["logger"] == "pychromecast.socket_client"
        assert issue["message"] == "Connection reset"

    def test_logger_is_the_first_bracket_group_not_the_last(self):
        # The other half of the same regex change: the thread group extends past
        # a nested ')' only because the following '[logger]' anchor demands it,
        # and it must still stop at the FIRST match. A greedy group would read
        # a later bracketed token in the message as the logger name instead.
        log = (
            "2026-05-27 10:00:01.123 ERROR (SyncWorker_3) [a.b] "
            "msg with (parens) [brackets] and a tail\n"
        )
        issue = _parse_error_log_structured(log)["top_issues"][0]
        assert issue["logger"] == "a.b"
        assert issue["message"] == "msg with (parens) [brackets] and a tail"

    def test_unparseable_lines_are_skipped(self):
        result = _parse_error_log_structured(_UNPARSEABLE_LOG)
        assert result["summary"]["parsed_entries"] == 0
        assert result["top_issues"] == []

    def test_message_truncated_to_max_len(self):
        long_msg = "x" * (_MAX_MESSAGE_LEN + 100)
        log = f"2026-05-27 10:00:01.000 ERROR (MainThread) [homeassistant.components.test] {long_msg}\n"
        result = _parse_error_log_structured(log)
        message = result["top_issues"][0]["message"]
        # Truncation is MARKED: an unmarked cut is indistinguishable from a
        # genuinely short message.
        assert message.endswith(_TRUNCATION_MARK)
        assert len(message) == _MAX_MESSAGE_LEN + len(_TRUNCATION_MARK)
        assert message[:_MAX_MESSAGE_LEN] == "x" * _MAX_MESSAGE_LEN

    def test_messages_differing_past_the_cap_stay_distinct(self):
        # The dedup key runs on the full message. Keying on the capped display
        # value merges two unrelated errors that share a long prefix into one
        # issue with a summed count — invisible in the output, since both
        # render as the same truncated string.
        shared = "x" * _MAX_MESSAGE_LEN
        log = (
            f"2026-05-27 10:00:01.000 ERROR (MainThread) [a.b.c] {shared}first\n"
            f"2026-05-27 10:00:02.000 ERROR (MainThread) [a.b.c] {shared}second\n"
        )
        result = _parse_error_log_structured(log)
        assert result["summary"]["unique_issues"] == 2
        assert [i["count"] for i in result["top_issues"]] == [1, 1]
        assert all(
            i["message"].endswith(_TRUNCATION_MARK) for i in result["top_issues"]
        )

    def test_short_message_is_not_marked_truncated(self):
        log = "2026-05-27 10:00:01.000 ERROR (MainThread) [homeassistant.components.test] short\n"
        result = _parse_error_log_structured(log)
        assert result["top_issues"][0]["message"] == "short"

    def test_default_top_n_is_applied(self):
        # Build a log with more unique issues than _DEFAULT_TOP_N
        lines = "\n".join(
            f"2026-05-27 10:00:{i:02d}.000 ERROR (MainThread) [homeassistant.components.test] Unique message {i}"
            for i in range(_DEFAULT_TOP_N + 5)
        )
        result = _parse_error_log_structured(lines)
        assert len(result["top_issues"]) == _DEFAULT_TOP_N


# ---------------------------------------------------------------------------
# ha_get_logs end-to-end (structured mode)
# ---------------------------------------------------------------------------


class TestHaGetLogsStructured:
    @pytest.mark.asyncio
    async def test_structured_true_returns_parsed_response(self):
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="error_log", structured=True)
        assert result["success"] is True
        assert result["structured"] is True
        assert "top_issues" in result
        assert "by_component" in result

    @pytest.mark.asyncio
    async def test_structured_false_returns_raw_log(self):
        """Backward compat: structured=False must return the existing 'log' string."""
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="error_log", structured=False)
        assert "log" in result
        assert "top_issues" not in result

    @pytest.mark.asyncio
    async def test_structured_default_is_false(self):
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="error_log")
        assert "log" in result

    @pytest.mark.parametrize(
        ("raw_value", "expected"), [("true", True), ("false", False)]
    )
    def test_structured_string_is_coerced_by_the_schema(self, raw_value, expected):
        """AI tools often pass booleans as strings; the schema must coerce them.

        Asserted at the annotation level rather than by calling the collected
        function: ``_register_and_collect`` stores the undecorated fn, so no
        pydantic validation runs there and *any* non-empty string is truthy —
        ``structured="false"`` would have passed a `structured is True`
        assertion while meaning the opposite. FastMCP builds its argument
        TypeAdapter from this same signature, so this is what the transport
        enforces.
        """
        annotation = _get_tool_param_annotation("ha_get_logs", "structured")
        assert TypeAdapter(annotation).validate_python(raw_value) is expected

    @pytest.mark.asyncio
    async def test_structured_bool_reaches_the_parser(self):
        """The coerced True actually selects the structured branch."""
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="error_log", structured=True)
        assert result["structured"] is True

    @pytest.mark.asyncio
    async def test_top_n_parameter_is_passed_through(self):
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](
            source="error_log", structured=True, top_n=2
        )
        assert len(result["top_issues"]) <= 2

    @pytest.mark.asyncio
    async def test_level_filter_applies_in_structured_mode(self):
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](
            source="error_log", structured=True, level="ERROR"
        )
        for issue in result["top_issues"]:
            assert issue["level"] == "ERROR"

    @pytest.mark.asyncio
    async def test_search_filter_applies_in_structured_mode(self):
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](
            source="error_log", structured=True, search="timeout"
        )
        assert all("timeout" in i["message"].lower() for i in result["top_issues"])

    @pytest.mark.asyncio
    async def test_structured_on_non_error_log_source_emits_warning(self):
        """structured=True on source='system' should warn and be ignored."""
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="system", structured=True)
        assert "warnings" in result
        assert any("structured" in w for w in result["warnings"])
        # Must still return normal system-log response shape
        assert "entries" in result

    @pytest.mark.asyncio
    async def test_structured_on_logbook_emits_warning(self):
        client = _make_client()
        client.get_logbook = AsyncMock(return_value=[])
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="logbook", structured=True)
        assert "warnings" in result
        assert any("structured" in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_top_n_without_structured_emits_warning(self):
        """top_n on the raw path does nothing — the obvious caller mistake.

        The warning must name the piece that is missing. On this call the
        source is already the one the sentence asks for, so a tail blaming
        the source tells the caller their correct argument is the problem
        and leaves the real omission unstated.
        """
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="error_log", top_n=5)
        assert "log" in result
        warning = next(w for w in result["warnings"] if "top_n" in w)
        assert "ignored because structured=False" in warning
        assert "ignored for source=" not in warning

    @pytest.mark.asyncio
    async def test_top_n_on_other_source_emits_warning(self):
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="system", top_n=5)
        # Here the source IS the problem, so the tail must still name it.
        warning = next(w for w in result["warnings"] if "top_n" in w)
        assert "ignored for source='system'" in warning

    @pytest.mark.asyncio
    async def test_top_n_in_structured_mode_does_not_warn(self):
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](
            source="error_log", structured=True, top_n=5
        )
        assert not any("top_n" in w for w in result.get("warnings", []))

    @pytest.mark.asyncio
    async def test_invalid_top_n_names_top_n_not_limit(self):
        """The validation error must name the parameter the caller passed."""
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        with pytest.raises(ToolError) as exc_info:
            await tools["ha_get_logs"](source="error_log", structured=True, top_n=0)
        payload = json.loads(str(exc_info.value))
        assert "top_n must be at least 1" in payload["error"]["message"]

    @pytest.mark.asyncio
    async def test_limit_zero_is_accepted_in_structured_mode(self):
        """`limit` is coerced only on the raw path, and this pins why.

        The coercion sits below the structured early-return because the
        summary ranks the whole fetched window — `limit` has no meaning
        there, so validating it would reject a value that changes nothing.
        Moving it back above the return is invisible to every other test.
        """
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](
            source="error_log", structured=True, limit=0
        )
        assert result["structured"] is True
        assert result["summary"]["parsed_entries"] == 8
        # Accepted, but not silently: the caller still learns it did nothing.
        assert any("limit" in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_limit_zero_is_still_rejected_on_the_raw_path(self):
        """The counterpart: where `limit` does apply, 0 is still invalid."""
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        with pytest.raises(ToolError):
            await tools["ha_get_logs"](source="error_log", limit=0)

    @pytest.mark.asyncio
    async def test_level_critical_is_accepted(self):
        """CRITICAL is in VALID_LOG_LEVELS; CRITICAL lines must be isolatable."""
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](
            source="error_log", structured=True, level="CRITICAL"
        )
        assert [i["message"] for i in result["top_issues"]] == ["DB error"]

    @pytest.mark.asyncio
    async def test_auth_error_surfaces_as_structured_tool_error(self):
        """A 401 must not escape raw.

        ``HomeAssistantAuthError`` is a sibling of ``HomeAssistantAPIError``,
        not a subclass, so an ``except (HomeAssistantAPIError, ...)`` tuple lets
        it through to FastMCP without a structured ``code``.
        """
        client = _make_client()
        client.get_error_log = AsyncMock(
            side_effect=HomeAssistantAuthError("401 Unauthorized")
        )
        tools = _register_and_collect(client)
        with pytest.raises(ToolError) as exc_info:
            await tools["ha_get_logs"](source="error_log")
        payload = json.loads(str(exc_info.value))
        assert payload["success"] is False
        assert payload["error"]["code"]
        assert payload.get("source") == "error_log"

    @pytest.mark.asyncio
    async def test_expired_token_on_a_supervised_install_reaches_the_auth_handler(
        self,
    ):
        """A 401 raised by the install-class probe must reach the auth handler.

        Driven through the real ``get_error_log`` rather than a stubbed one,
        because the defect lived between the two: the probe swallowed the
        401 and answered "not supervised", so the caller requested
        ``/api/error_log`` — unregistered under SUPERVISOR — and the dead
        token came back as a 404 with connection advice.
        """
        from ha_mcp.client.rest_client import HomeAssistantClient

        with patch.object(HomeAssistantClient, "__init__", lambda self, **kw: None):
            real = HomeAssistantClient()
        real._supervised_detected = None
        real._request = AsyncMock(
            side_effect=HomeAssistantAuthError("401 Unauthorized")
        )
        real._raw_request = AsyncMock()
        real.timeout = 30

        client = _make_client()
        client.get_error_log = real.get_error_log
        tools = _register_and_collect(client)
        with (
            patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False),
            pytest.raises(ToolError) as exc_info,
        ):
            await tools["ha_get_logs"](source="error_log")

        payload = json.loads(str(exc_info.value))
        assert payload["success"] is False
        assert payload["error"]["code"]
        # The container fallback must never have been attempted.
        real._raw_request.assert_not_called()


# ---------------------------------------------------------------------------
# Supervisor-backed installs
#
# The plain-file fixtures above imitate container/pip installs. Add-on, HAOS and
# supervised installs read HA Core's journald stream instead, which is the one
# most users have — and the format these tests pin.
# ---------------------------------------------------------------------------
class TestSupervisorInstallRegressions:
    """Supervisor-backed installs (add-on / HAOS) serve ANSI-coloured journald."""

    def test_ansi_coloured_lines_parse(self):
        # An ^-anchored regex matches no colour-wrapped line at all, which turns
        # a log full of errors into "success: true, top_issues: []".
        result = _parse_error_log_structured(_ANSI_LOG)
        assert result["summary"]["parsed_entries"] == 3
        assert result["summary"]["unparseable_lines"] == 0
        assert result["summary"]["unique_issues"] == 2

    def test_ansi_reset_code_does_not_leak_into_message(self):
        # A trailing \x1b[0m inside `message` would split the dedup key, so the
        # same recurring error would be counted as several distinct ones.
        result = _parse_error_log_structured(_ANSI_LOG)
        timeout = next(
            i for i in result["top_issues"] if i["message"] == "Device timeout"
        )
        assert timeout["count"] == 2
        for issue in result["top_issues"]:
            assert "\x1b" not in issue["message"]

    def test_total_format_drift_warns_instead_of_reading_as_clean(self):
        result = _parse_error_log_structured(_UNPARSEABLE_LOG)
        assert result["summary"]["parsed_entries"] == 0
        assert result["summary"]["unparseable_lines"] > 0
        # Top-level `warnings: list[str]` — the repo's single warning channel.
        # A singular `warning` string put one payload on two channels.
        assert isinstance(result["warnings"], list)
        assert "NOT evidence" in result["warnings"][0]
        assert "warning" not in result

    def test_empty_fetch_warns_instead_of_reading_as_all_clear(self):
        # A running instance always logs something, so zero lines means the
        # fetch produced nothing (empty journald window, proxy answering 200
        # with no body) — not a healthy instance.
        result = _parse_error_log_structured("")
        assert "NOT evidence" in result["warnings"][0]
        assert "empty or failed fetch" in result["warnings"][0]

    def test_blank_only_input_warns_like_an_empty_fetch(self):
        result = _parse_error_log_structured("\n\n   \n")
        assert result["summary"]["blank_lines"] == 3
        assert result["summary"]["unparseable_lines"] == 0
        assert "empty or failed fetch" in result["warnings"][0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", ["", "\n\n   \n"])
    async def test_empty_fetch_on_the_default_path_warns_too(self, raw):
        """The same fetch must not be blessed by one mode and refused by the other.

        `structured=False` is the default, so this is the path most agents
        are on: an empty fetch there used to answer success with an empty
        `log`, total_lines 0 and a note about matching filters — which
        reads back to the user as "no errors in the log".
        """
        client = _make_client(raw)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="error_log")
        assert result["success"] is True
        # Whitespace-only counts as lines, so `total_lines` alone does not
        # discriminate the two inputs — "no content arrived" does.
        assert result["log"].strip() == ""
        assert "empty or failed fetch" in result["warnings"][0]
        assert "NOT evidence" in result["warnings"][0]

    @pytest.mark.asyncio
    async def test_empty_filter_result_is_not_reported_as_an_empty_fetch(self):
        """An empty *filter result* is a different thing and keeps its own story.

        The fetch worked and the filter simply excluded everything, which
        `filters_applied` already discloses — warning about a failed fetch
        here would send the caller after an infrastructure problem that
        does not exist.
        """
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](
            source="error_log", search="nothing-matches-this"
        )
        assert result["total_lines"] == 0
        assert result["filters_applied"] == {"search": "nothing-matches-this"}
        assert not any("empty or failed fetch" in w for w in result.get("warnings", []))


class TestCountSemantics:
    """`unparseable`, `filtered out` and `blank` are different things."""

    def test_traceback_lines_counted_as_unparseable(self):
        # 1 ERROR line + 4 traceback lines (header, File, source, exception).
        result = _parse_error_log_structured(_TRACEBACK_LOG)
        s = result["summary"]
        assert s["total_raw_lines"] == 5
        assert s["parsed_entries"] == 1
        assert s["unparseable_lines"] == 4
        assert s["parsed_entries"] < s["total_raw_lines"]

    def test_every_input_line_is_accounted_for(self):
        # Every bucket is named, so the four counters reconcile exactly; an
        # unnamed residual leaves a caller unable to tell what the gap was.
        log = _SAMPLE_LOG + "\nTraceback (most recent call last):\n"
        s = _parse_error_log_structured(log, level="ERROR")["summary"]
        assert (
            s["blank_lines"] + s["unparseable_lines"] + s["matched_lines"]
            == s["total_raw_lines"]
        )
        assert s["blank_lines"] == 1
        assert s["unparseable_lines"] == 1
        assert s["matched_lines"] == 8
        # matched_lines - parsed_entries is exactly what the filters removed.
        assert s["parsed_entries"] == 4

    def test_filtered_out_lines_are_not_reported_as_unparseable(self):
        # A caller reading parsed_entries against total_raw_lines must not read
        # a narrow filter as "99% unparseable" and fall back to the raw log —
        # the exact behaviour this feature exists to prevent.
        lines = [
            f"2026-05-27 10:00:{i % 60:02d}.000 INFO (MainThread) [homeassistant.core] tick"
            for i in range(200)
        ]
        lines.append(
            "2026-05-27 11:00:00.000 ERROR (MainThread) [homeassistant.components.zha] dead"
        )
        result = _parse_error_log_structured("\n".join(lines), level="ERROR")
        assert result["summary"]["unparseable_lines"] == 0
        assert result["summary"]["parsed_entries"] == 1
        assert "warnings" not in result

    def test_all_filtered_out_warns_about_filters_not_parsing(self):
        result = _parse_error_log_structured(_SAMPLE_LOG, search="nothing-matches-this")
        assert result["summary"]["parsed_entries"] == 0
        assert result["summary"]["unparseable_lines"] == 0
        assert "reflects the filters" in result["warnings"][0]

    def test_filtered_empty_result_with_tracebacks_does_not_claim_format_drift(self):
        # The arrangement that reaches every real log and that _SAMPLE_LOG (zero
        # unparseable lines) cannot produce: entries parse fine, tracebacks push
        # the unparseable count high, and the filter matches none of the entries.
        # Keying the warning on unparseable_lines answers "no CRITICAL entries"
        # with "no log lines could be parsed", sending the agent back to the
        # 20,000-line raw fetch this mode exists to avoid.
        log = _TRACEBACK_LOG * 5
        result = _parse_error_log_structured(log, level="CRITICAL")
        s = result["summary"]
        assert s["unparseable_lines"] == 20
        assert s["matched_lines"] == 5
        assert s["parsed_entries"] == 0
        assert s["unparseable_lines"] > s["matched_lines"], (
            "fixture must be mostly unparseable, like a real log with tracebacks"
        )
        warning = result["warnings"][0]
        assert "reflects the filters" in warning
        assert "could not" not in warning and "NOT evidence" not in warning


class TestBoundedOutput:
    """`bounded output regardless of input size` must hold for the whole payload."""

    def test_by_component_is_capped(self):
        log = "\n".join(
            f"2026-05-27 10:00:00.000 ERROR (MainThread) [comp{i}.sub.mod] issue{i}"
            for i in range(_MAX_COMPONENTS * 4)
        )
        result = _parse_error_log_structured(log)
        assert len(result["by_component"]) == _MAX_COMPONENTS
        assert result["summary"]["showing_components"] == _MAX_COMPONENTS
        # The true total is still reported, so the cap never hides the scale.
        assert result["summary"]["components_affected"] == _MAX_COMPONENTS * 4

    def test_by_component_totals_survive_top_n_truncation(self):
        # "nothing is hidden by the cap": occurrences dropped from top_issues
        # must still be counted in by_component.
        log = "\n".join(
            f"2026-05-27 10:00:00.000 ERROR (MainThread) [one.sub.mod] issue{i}"
            for i in range(10)
        )
        result = _parse_error_log_structured(log, top_n=2)
        assert len(result["top_issues"]) == 2
        bucket = result["by_component"]["one.sub.mod"]
        assert bucket["issue_count"] == 10
        assert bucket["total_occurrences"] == 10


class TestWarningsAreMergedNotClobbered:
    """Three layers can warn on one structured call; none may overwrite another.

    ``_parse_error_log_structured`` (format drift), ``_build_structured_error_log``
    (ignored limit/order) and ``get_logs`` (wrong-source params) each append to
    the same top-level list. An overwrite here silently drops the "this summary
    is NOT an all-clear" notice, which is the one warning that must never be lost.
    """

    @pytest.mark.asyncio
    async def test_drift_warning_survives_ignored_param_warning(self):
        client = _make_client(_UNPARSEABLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](
            source="error_log", structured=True, limit=5, order="oldest"
        )
        joined = " ".join(result["warnings"])
        assert "NOT evidence" in joined
        assert "do not apply when structured=True" in joined

    @pytest.mark.asyncio
    async def test_wrong_source_param_warning_does_not_clobber_drift(self):
        client = _make_client(_UNPARSEABLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](
            source="error_log", structured=True, entity_id="light.kitchen"
        )
        joined = " ".join(result["warnings"])
        assert "only apply to source='logbook'" in joined
        assert "NOT evidence" in joined


def _numbered_log(count: int) -> str:
    """`count` parseable log lines — enough to fill an exact fetch window."""
    return "".join(
        f"2026-05-27 10:00:00.000 ERROR (MainThread) [a.b.c] issue {i}\n"
        for i in range(count)
    )


class TestFetchWindowIsBounded:
    """Every error_log fetch asks for a bounded window (#2279).

    The unconditional 20,000-line request these replace made Supervisor
    assemble a journald slice for 15+ minutes before the tool call returned.
    """

    @pytest.mark.asyncio
    async def test_structured_reads_the_bounded_summary_window(self):
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        await tools["ha_get_logs"](source="error_log", structured=True)
        assert client.get_error_log.await_args.kwargs == {
            "lines": STRUCTURED_ERROR_LOG_WINDOW_LINES,
            "offset": 0,
        }
        assert STRUCTURED_ERROR_LOG_WINDOW_LINES < 20000

    @pytest.mark.asyncio
    async def test_raw_unfiltered_reads_exactly_the_limit(self):
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        await tools["ha_get_logs"](source="error_log", limit=25)
        assert client.get_error_log.await_args.kwargs["lines"] == 25

    @pytest.mark.asyncio
    async def test_raw_default_limit_reads_the_default_window(self):
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        await tools["ha_get_logs"](source="error_log")
        assert client.get_error_log.await_args.kwargs["lines"] == DEFAULT_LOG_LIMIT

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "filter_kwargs",
        [{"search": "timeout"}, {"level": "ERROR"}, {"search": "x", "level": "ERROR"}],
    )
    async def test_raw_client_side_filters_widen_the_window(self, filter_kwargs):
        """`search` and `level` both filter the fetched text client-side, so
        both need history behind the caller's limit to find matches in — the
        same widening `_get_supervisor_log` applies for `search`."""
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        await tools["ha_get_logs"](source="error_log", limit=10, **filter_kwargs)
        assert (
            client.get_error_log.await_args.kwargs["lines"]
            == SUPERVISOR_SEARCH_WINDOW_LINES
        )

    @pytest.mark.asyncio
    async def test_limit_is_clamped_before_it_sizes_the_window(self):
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        await tools["ha_get_logs"](source="error_log", limit=MAX_LIMIT + 5000)
        assert client.get_error_log.await_args.kwargs["lines"] == MAX_LIMIT

    @pytest.mark.asyncio
    @pytest.mark.parametrize("structured", [False, True])
    async def test_offset_reaches_the_client(self, structured):
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        kwargs = {"source": "error_log", "structured": structured, "offset": 400}
        await tools["ha_get_logs"](**kwargs)
        assert client.get_error_log.await_args.kwargs["offset"] == 400


class TestErrorLogPagination:
    """`offset`/`has_more`/`next_offset` — the logbook pagination contract,
    applied to the bounded log window so an agent can read further back.

    ``has_more`` is passed through from the client verbatim: a full window
    proves nothing on journald, where an over-shot offset clamps to the oldest
    entry and comes back full forever, so the tool layer must not re-derive it
    from what it received.
    """

    @pytest.mark.asyncio
    async def test_has_more_is_taken_from_the_client(self):
        client = _make_client(_numbered_log(20), has_more=True)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="error_log", limit=20)
        assert result["offset"] == 0
        assert result["has_more"] is True
        assert result["next_offset"] == 20
        assert "offset=20" in result["pagination_hint"]

    @pytest.mark.asyncio
    async def test_a_full_window_alone_does_not_claim_more_history(self):
        """The clamp case: the window is saturated but the client says done."""
        client = _make_client(_numbered_log(20), has_more=False)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="error_log", limit=20)
        assert result["has_more"] is False
        assert "next_offset" not in result
        assert "pagination_hint" not in result

    @pytest.mark.asyncio
    async def test_terminal_window_with_unreturned_matches_hints_a_larger_limit(self):
        """Matches the limit slice left inside the LAST window must stay reachable.

        Paging cannot reach them: no older history exists behind the window,
        and on journald a deeper fetch clamps back to this same oldest window
        every time, so a next_offset here would loop. A larger limit retrieves
        them from the already-addressed window exactly.
        """
        client = _make_client(_numbered_log(50), has_more=False)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](
            source="error_log", limit=10, search="issue"
        )
        assert result["has_more"] is False
        assert "next_offset" not in result
        assert "40 more matching lines remain" in result["pagination_hint"]
        assert "limit=50" in result["pagination_hint"]

    @pytest.mark.asyncio
    async def test_next_offset_advances_from_the_current_offset(self):
        client = _make_client(_numbered_log(20), has_more=True)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="error_log", limit=20, offset=60)
        assert result["offset"] == 60
        assert result["next_offset"] == 80

    @pytest.mark.asyncio
    async def test_window_lines_reports_the_size_requested(self):
        """A degraded (short) window has to be visible as such.

        Without it, a proxy that stripped the Range header and served 100 lines
        for a 2000-line ask is indistinguishable from a log that only holds 100.
        """
        client = _make_client(_numbered_log(20), has_more=True)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="error_log", limit=200)
        assert result["window_lines"] == 200
        assert result["total_lines"] == 20

    @pytest.mark.asyncio
    async def test_filtered_window_resumes_where_the_shown_block_starts(self):
        """A filter that drops most of a window must not skip what it dropped.

        Window of 2000, limit 10: the tool shows the 10 newest matches, so the
        next page has to resume just before the oldest one shown. Stepping a
        whole window instead jumps over every match in between — silently, and
        exactly when a search is what the caller asked for.
        """
        client = _make_client(
            _numbered_log(SUPERVISOR_SEARCH_WINDOW_LINES), has_more=True
        )
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](
            source="error_log", limit=10, search="issue 7"
        )
        # "issue 7", "issue 70".."issue 79", "issue 700".."issue 799".
        assert result["total_lines"] == 111
        assert result["returned_lines"] == 10
        # Oldest match shown is "issue 790" (index 790 in the window), so the
        # next page resumes 2000 - 790 lines back.
        assert result["next_offset"] == SUPERVISOR_SEARCH_WINDOW_LINES - 790
        assert "search='issue 7'" in result["pagination_hint"]

    @pytest.mark.asyncio
    async def test_step_never_exceeds_the_window_size(self):
        """A window longer than requested must not push the step past it.

        On journald the step is spent in ENTRY units while it is derived from
        LINES, and a multi-line entry makes a window come back longer than the
        entry count asked for — an uncapped step would then page over entries
        that were never shown. Here 20 lines answer a 5-line ask.
        """
        client = _make_client(_numbered_log(20), has_more=True)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="error_log", limit=5)
        assert result["window_lines"] == 5
        assert result["next_offset"] == 5

    @pytest.mark.asyncio
    async def test_a_fully_filtered_window_steps_the_whole_window(self):
        """Nothing shown means nothing to resume before."""
        client = _make_client(_numbered_log(20), has_more=True)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](
            source="error_log", limit=20, search="nothing-matches-this"
        )
        assert result["returned_lines"] == 0
        assert result["next_offset"] == SUPERVISOR_SEARCH_WINDOW_LINES

    @pytest.mark.asyncio
    async def test_structured_pages_by_the_whole_window(self):
        window = "ha_mcp.tools.error_log_parsing.STRUCTURED_ERROR_LOG_WINDOW_LINES"
        with patch(window, 4):
            client = _make_client(_numbered_log(4), has_more=True)
            tools = _register_and_collect(client)
            result = await tools["ha_get_logs"](
                source="error_log", structured=True, offset=8
            )
        assert result["structured"] is True
        assert result["offset"] == 8
        assert result["has_more"] is True
        assert result["window_lines"] == 4
        # The summary consumes the whole window, so the next page is a whole
        # window further back.
        assert result["next_offset"] == 12
        assert "structured=True" in result["pagination_hint"]
        assert "offset=12" in result["pagination_hint"]

    @pytest.mark.asyncio
    async def test_structured_end_of_history_reports_no_next_offset(self):
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="error_log", structured=True)
        assert result["has_more"] is False
        assert "next_offset" not in result
        assert result["window_lines"] == STRUCTURED_ERROR_LOG_WINDOW_LINES

    @pytest.mark.asyncio
    async def test_note_explains_what_offset_counts(self):
        """The window is not the whole log — the response has to say so, or an
        agent reads `total_lines` as the instance's total."""
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="error_log")
        assert "offset" in result["note"]
        assert "window" in result["note"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("structured", [False, True])
    async def test_last_page_says_it_reached_the_start_of_history(self, structured):
        """The clamp can hand back entries an earlier page already showed, so
        the terminal page says so rather than reading as fresh history."""
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](
            source="error_log", structured=structured, offset=5000
        )
        assert result["has_more"] is False
        assert "start of the available history" in result["note"]

    @pytest.mark.asyncio
    async def test_first_page_does_not_claim_to_be_the_last(self):
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="error_log")
        assert "start of the available history" not in result["note"]


class TestEmptyWindowAtAnOffset:
    """An empty window means different things at the newest edge and deep in
    history, and the wrong one sends the caller after an outage that is not
    happening (or blesses a failed fetch as a clean log)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("structured", [False, True])
    async def test_empty_page_past_the_end_is_benign(self, structured):
        client = _make_client("")
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](
            source="error_log", structured=structured, offset=900
        )
        joined = " ".join(result["warnings"])
        assert "offset=900" in joined
        # The failed-fetch alarm must not fire on a routine last page.
        assert "NOT evidence" not in joined

    @pytest.mark.asyncio
    @pytest.mark.parametrize("structured", [False, True])
    async def test_empty_fetch_at_offset_zero_still_warns_loudly(self, structured):
        """Unchanged: a running instance always logs something."""
        client = _make_client("")
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="error_log", structured=structured)
        joined = " ".join(result["warnings"])
        assert "NOT evidence" in joined
        assert "offset=" not in joined


class TestOffsetParameterIncompatibilityWarning:
    """`offset` applies to source='logbook' and source='error_log'. Anywhere
    else it must be flagged rather than silently dropped — same shape as the
    existing level/entity_id/slug incompatibility warnings."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["system", "logger"])
    async def test_offset_on_an_unsupported_source_warns(self, source):
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source=source, offset=50)
        warnings = result.get("warnings", [])
        assert any("offset" in w and source in w for w in warnings), warnings

    @pytest.mark.asyncio
    @pytest.mark.parametrize("structured", [False, True])
    async def test_offset_on_error_log_does_not_warn(self, structured):
        client = _make_client()
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](
            source="error_log", structured=structured, offset=50
        )
        assert not any("offset" in w for w in result.get("warnings", []))

    @pytest.mark.asyncio
    async def test_zero_offset_never_warns(self):
        """The default must stay silent — a warning on every `system` call
        would train agents to ignore the channel."""
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="system")
        assert not any("offset" in w for w in result.get("warnings", []))


class TestDedupKey:
    def test_same_message_at_different_levels_stays_distinct(self):
        # First-wins on level meant a later ERROR could be reported as WARNING.
        log = textwrap.dedent("""\
            2026-05-27 10:00:00.000 WARNING (MainThread) [a.b.c] same text
            2026-05-27 10:00:01.000 ERROR (MainThread) [a.b.c] same text
        """)
        result = _parse_error_log_structured(log)
        assert result["summary"]["unique_issues"] == 2
        assert {i["level"] for i in result["top_issues"]} == {"WARNING", "ERROR"}
