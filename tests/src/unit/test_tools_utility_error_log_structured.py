"""Unit tests for ha_get_logs(source='error_log', structured=True).

Covers:
- _get_component_prefix: dotted-segment extraction
- _parse_error_log_structured: parsing, dedup, grouping, filters, top_n
- ha_get_logs structured=True end-to-end: response shape, backward compat,
  warning when structured used with wrong source
"""

import textwrap
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_utility import (
    _DEFAULT_TOP_N,
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(log_text: str = _SAMPLE_LOG) -> MagicMock:
    client = MagicMock()
    client.get_error_log = AsyncMock(return_value=log_text)
    return client


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
        result = _parse_error_log_structured(_SAMPLE_LOG)
        counts = [i["count"] for i in result["top_issues"]]
        assert counts == sorted(counts, reverse=True)

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
        result = _parse_error_log_structured(_SAMPLE_LOG)
        totals = [v["total_occurrences"] for v in result["by_component"].values()]
        assert totals == sorted(totals, reverse=True)

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
        assert len(result["top_issues"][0]["message"]) == 200

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

    @pytest.mark.asyncio
    async def test_structured_string_true_is_accepted(self):
        """AI tools often pass booleans as strings."""
        client = _make_client(_SAMPLE_LOG)
        tools = _register_and_collect(client)
        result = await tools["ha_get_logs"](source="error_log", structured="true")
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
