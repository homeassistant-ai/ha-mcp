"""Unit tests for ha_get_logs(source='error_log', structured=True).

Covers:
- _get_component_prefix: dotted-segment extraction
- _parse_error_log_structured: parsing, dedup, grouping, filters, top_n
- ha_get_logs structured=True end-to-end: response shape, backward compat,
  warning when structured used with wrong source
"""

import asyncio
import inspect
import textwrap
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import TypeAdapter

from ha_mcp.tools.tools_utility import (
    _DEFAULT_TOP_N,
    _MAX_COMPONENTS,
    _TRUNCATION_MARK,
    _get_component_prefix,
    _parse_error_log_structured,
    register_utility_tools,
)

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

# Ordering fixture: emitted deliberately WORST-first so the sort is
# load-bearing. `noisy` has one issue seen 5 times (5 occurrences, 1 issue);
# `chatty` has three distinct issues seen once each (3 occurrences, 3 issues) —
# so ranking by total_occurrences and ranking by issue_count disagree, and the
# assertions can tell which one the code used.
_UNSORTED_LOG = textwrap.dedent("""\
    2026-05-27 10:00:00.000 ERROR (MainThread) [chatty.sub.mod] alpha
    2026-05-27 10:00:01.000 ERROR (MainThread) [chatty.sub.mod] beta
    2026-05-27 10:00:02.000 ERROR (MainThread) [chatty.sub.mod] gamma
    2026-05-27 10:00:03.000 ERROR (MainThread) [noisy.sub.mod] frequent
    2026-05-27 10:00:04.000 ERROR (MainThread) [noisy.sub.mod] frequent
    2026-05-27 10:00:05.000 ERROR (MainThread) [noisy.sub.mod] frequent
    2026-05-27 10:00:06.000 ERROR (MainThread) [noisy.sub.mod] frequent
    2026-05-27 10:00:07.000 ERROR (MainThread) [noisy.sub.mod] frequent
""")

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


def _make_client(log_text: str = _SAMPLE_LOG) -> MagicMock:
    client = MagicMock()
    client.get_error_log = AsyncMock(return_value=log_text)
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
        register_utility_tools(mcp, _make_client())
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
    register_utility_tools(mcp, client)
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

    def test_custom_component_prefix(self):
        assert (
            _get_component_prefix("custom_components.my_integration.sensor")
            == "custom_components.my_integration.sensor"
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
        # _UNSORTED_LOG deliberately emits the *least* frequent issue first, so
        # this fails if the sort is removed. Asserting on _SAMPLE_LOG would not:
        # its insertion order already equals its sorted order, so the assertion
        # held with the sort deleted.
        result = _parse_error_log_structured(_UNSORTED_LOG)
        counts = [i["count"] for i in result["top_issues"]]
        assert counts == sorted(counts, reverse=True)
        assert counts[0] > counts[-1], "fixture must have a non-flat count spread"
        assert result["top_issues"][0]["message"] == "frequent"

    def test_top_issues_first_and_last_seen(self):
        result = _parse_error_log_structured(_SAMPLE_LOG)
        device_timeout = next(
            i for i in result["top_issues"] if i["message"] == "Device timeout"
        )
        assert device_timeout["first_seen"] == "2026-05-27 10:00:01.123"
        assert device_timeout["last_seen"] == "2026-05-27 10:00:04.000"

    def test_by_component_contains_all_components(self):
        result = _parse_error_log_structured(_SAMPLE_LOG)
        assert "homeassistant.components.zha" in result["by_component"]
        assert "homeassistant.components.tuya" in result["by_component"]
        assert "homeassistant.loader" in result["by_component"]

    def test_by_component_sorted_by_total_occurrences(self):
        # In _UNSORTED_LOG the component that appears FIRST has several small
        # issues that out-total another component's single larger one, so the
        # ordering can only come from the sort, not from insertion order.
        result = _parse_error_log_structured(_UNSORTED_LOG)
        totals = [v["total_occurrences"] for v in result["by_component"].values()]
        assert totals == sorted(totals, reverse=True)
        first, second = list(result["by_component"].items())[:2]
        assert first[1]["total_occurrences"] > second[1]["total_occurrences"]
        # The winner wins on volume despite having fewer distinct issues.
        assert first[1]["issue_count"] < second[1]["issue_count"]

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

    def test_unparseable_lines_are_skipped(self):
        result = _parse_error_log_structured(_UNPARSEABLE_LOG)
        assert result["summary"]["parsed_entries"] == 0
        assert result["top_issues"] == []

    def test_message_truncated_to_max_len(self):
        long_msg = "x" * 300
        log = f"2026-05-27 10:00:01.000 ERROR (MainThread) [homeassistant.components.test] {long_msg}\n"
        result = _parse_error_log_structured(log)
        message = result["top_issues"][0]["message"]
        # Truncation is MARKED: an unmarked cut is indistinguishable from a
        # genuinely short message, and silently merges distinct errors that
        # happen to share their first 200 characters.
        assert message.endswith(_TRUNCATION_MARK)
        assert len(message) == 200 + len(_TRUNCATION_MARK)
        assert message[:200] == "x" * 200

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


# ---------------------------------------------------------------------------
# Regression tests for the review blockers (PR #2107)
#
# Each of these fails against the original implementation. They exist because
# the first version of this feature was validated only against plain-file
# fixtures, which is the one install type most users do NOT have.
# ---------------------------------------------------------------------------
class TestSupervisorInstallRegressions:
    """Supervisor-backed installs (add-on / HAOS) serve ANSI-coloured journald."""

    def test_ansi_coloured_lines_parse(self):
        # The original ^-anchored regex matched none of these, so a log full of
        # errors summarised as "success: true, top_issues: []".
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

    def test_empty_log_does_not_warn(self):
        # No input is genuinely nothing to report — distinct from format drift.
        assert "warnings" not in _parse_error_log_structured("")


class TestCountSemantics:
    """`unparseable` and `filtered out` are different things."""

    def test_traceback_lines_counted_as_unparseable(self):
        # 1 ERROR line + 4 traceback lines (header, File, source, exception).
        result = _parse_error_log_structured(_TRACEBACK_LOG)
        s = result["summary"]
        assert s["total_raw_lines"] == 5
        assert s["parsed_entries"] == 1
        assert s["unparseable_lines"] == 4
        assert s["parsed_entries"] < s["total_raw_lines"]

    def test_filtered_out_lines_are_not_reported_as_unparseable(self):
        # The bug this guards: with level='ERROR' on a healthy log, a caller
        # reading parsed_entries vs total_raw_lines concluded "99% unparseable"
        # and fell back to the raw log — the exact behaviour this feature exists
        # to prevent.
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
