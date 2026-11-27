"""Structured summary of ``ha_get_logs(source='error_log', structured=True)``.

The tool reads the log Core serves and answers with counted, deduplicated
issues grouped by integration. Covers parsing, grouping, ordering, the filters, the
count semantics and the warnings that keep a summary from reading as an
all-clear. The tool's other parameters and the paging are in
``test_tools_utility_error_log_structured_api.py`` and
``test_tools_utility_error_log_structured_pages.py``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ha_mcp.client.rest_client import HomeAssistantClient

from ._error_log_core import log_file as log_file
from ._error_log_core import structured
from ._error_log_samples import (
    ANSI_LOG,
    EMPTY_LOG,
    NESTED_PAREN_THREAD_LOG,
    OUT_OF_ORDER_LOG,
    SAMPLE_LOG,
    TRACEBACK_LOG,
    UNPARSEABLE_LOG,
    UNSORTED_LOG,
)

# The caps the summary applies; the tool schema documents the first.
DEFAULT_TOP_N = 20
MAX_MESSAGE_LEN = 200
MAX_COMPONENTS = 50
TRUNCATION_MARK = "…[truncated]"


@pytest.mark.parametrize(
    ("logger", "component"),
    [
        # Installed but not loaded: the lines of a setup that failed.
        ("homeassistant.components.zha", "zha"),
        ("homeassistant.components.zha.core.device", "zha"),
        ("homeassistant.components", "homeassistant.components"),
        ("homeassistant", "homeassistant"),
        (
            "custom_components.my_integration.sensor",
            "custom_components.my_integration.sensor",
        ),
        ("custom_components.my_integration", "custom_components.my_integration"),
    ],
)
async def test_an_issue_is_filed_under_the_integration_that_logged_it(
    log_file: Path,
    component_core_client: HomeAssistantClient,
    logger: str,
    component: str,
) -> None:
    log = f"2026-05-27 10:00:01.000 ERROR (MainThread) [{logger}] boom\n"
    result = await structured(log_file, component_core_client, log)
    assert result["top_issues"][0]["component"] == component
    assert list(result["by_component"]) == [component]


class TestParseErrorLogStructured:
    async def test_response_shape(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(log_file, component_core_client, SAMPLE_LOG)
        assert result["success"] is True
        assert result["source"] == "error_log"
        assert result["structured"] is True
        assert "summary" in result
        assert "top_issues" in result
        assert "by_component" in result

    async def test_summary_counts(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        summary = (await structured(log_file, component_core_client, SAMPLE_LOG))[
            "summary"
        ]
        assert summary["total_raw_lines"] == 8
        assert summary["parsed_entries"] == 8
        # Three "Device timeout" lines are one issue: 6 distinct in all.
        assert summary["unique_issues"] == 6

    async def test_deduplication_occurrence_count(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(log_file, component_core_client, SAMPLE_LOG)
        device_timeout = next(
            i for i in result["top_issues"] if i["message"] == "Device timeout"
        )
        assert device_timeout["count"] == 3
        assert device_timeout["logger"] == "homeassistant.components.zha.core.device"
        assert device_timeout["component"] == "zha"

    async def test_top_issues_sorted_by_count_desc(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        # UNSORTED_LOG emits the least frequent issues first, so this fails if
        # the sort is removed; SAMPLE_LOG's insertion order is already sorted.
        result = await structured(log_file, component_core_client, UNSORTED_LOG)
        counts = [i["count"] for i in result["top_issues"]]
        assert counts == sorted(counts, reverse=True)
        assert counts[0] > counts[-1], "fixture must have a non-flat count spread"
        assert result["top_issues"][0]["message"] == "frequent"

    async def test_equal_counts_are_broken_by_severity_then_recency(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        # Logs whose messages embed ids or ports tie at count 1 for everything;
        # a stable sort on count alone would turn "top N" into "the N oldest".
        log = textwrap.dedent("""\
            2026-05-27 10:00:00.000 ERROR (MainThread) [a.b.c] oldest
            2026-05-27 10:00:01.000 WARNING (MainThread) [a.b.c] middle
            2026-05-27 10:00:02.000 WARNING (MainThread) [a.b.c] newest
        """)
        result = await structured(log_file, component_core_client, log)
        assert [i["message"] for i in result["top_issues"]] == [
            "oldest",
            "newest",
            "middle",
        ]

    async def test_top_issues_first_and_last_seen(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(log_file, component_core_client, SAMPLE_LOG)
        device_timeout = next(
            i for i in result["top_issues"] if i["message"] == "Device timeout"
        )
        assert device_timeout["first_seen"] == "2026-05-27 10:00:01.123"
        assert device_timeout["last_seen"] == "2026-05-27 10:00:04.000"

    async def test_seen_bounds_survive_out_of_order_lines(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        # Last-write-wins would drag last_seen backwards on an unordered log.
        result = await structured(log_file, component_core_client, OUT_OF_ORDER_LOG)
        issue = result["top_issues"][0]
        assert issue["first_seen"] == "2026-05-27 10:00:01.000"
        assert issue["last_seen"] == "2026-05-27 10:00:09.000"

    async def test_by_component_contains_all_components(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(log_file, component_core_client, SAMPLE_LOG)
        assert "zha" in result["by_component"]
        assert "tuya" in result["by_component"]
        assert "homeassistant.loader" in result["by_component"]

    async def test_by_component_sorted_by_total_occurrences(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        # In UNSORTED_LOG the component with the largest single issue is not
        # the one with the largest total, so only the by_component sort can
        # produce this order.
        result = await structured(log_file, component_core_client, UNSORTED_LOG)
        totals = [v["total_occurrences"] for v in result["by_component"].values()]
        assert totals == sorted(totals, reverse=True)
        (first_name, first), (second_name, second) = list(
            result["by_component"].items()
        )[:2]
        assert (first_name, second_name) == ("chatty.sub.mod", "noisy.sub.mod")
        assert first["total_occurrences"] > second["total_occurrences"]
        assert first["issue_count"] > second["issue_count"]
        assert result["top_issues"][0]["component"] == second_name, (
            "the per-issue ranking must still favour the single biggest issue, "
            "otherwise the two sorts are not distinguishable"
        )

    async def test_modules_of_no_installed_integration_keep_their_logger_names(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        log = textwrap.dedent("""\
            2026-05-27 10:00:01.000 ERROR (MainThread) [custom_components.foo.sensor] a
            2026-05-27 10:00:02.000 ERROR (MainThread) [custom_components.foo.climate] b
            2026-05-27 10:00:03.000 ERROR (MainThread) [custom_components.foo.sensor] a
        """)
        result = await structured(log_file, component_core_client, log)
        assert (
            result["by_component"]["custom_components.foo.sensor"]["total_occurrences"]
            == 2
        )
        assert (
            result["by_component"]["custom_components.foo.climate"]["total_occurrences"]
            == 1
        )

    async def test_by_component_issue_count(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(log_file, component_core_client, SAMPLE_LOG)
        zha = result["by_component"]["zha"]
        assert zha["total_occurrences"] == 3
        assert zha["issue_count"] == 1

    async def test_level_filter_error_only(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(
            log_file, component_core_client, SAMPLE_LOG, level="ERROR"
        )
        assert result["top_issues"]
        for issue in result["top_issues"]:
            assert issue["level"] == "ERROR"
        assert "homeassistant.loader" not in result["by_component"]

    async def test_level_filter_warning(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(
            log_file, component_core_client, SAMPLE_LOG, level="WARNING"
        )
        assert result["summary"]["parsed_entries"] == 1
        assert result["top_issues"][0]["message"] == "Couldn't fetch data"

    async def test_search_filter_case_insensitive(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(
            log_file, component_core_client, SAMPLE_LOG, search="TIMEOUT"
        )
        assert result["summary"]["parsed_entries"] == 3
        assert all("timeout" in i["message"].lower() for i in result["top_issues"])

    async def test_search_filter_on_logger_name(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(
            log_file, component_core_client, SAMPLE_LOG, search="recorder"
        )
        assert result["summary"]["parsed_entries"] == 1
        assert result["top_issues"][0]["logger"] == "homeassistant.components.recorder"

    async def test_top_n_limits_top_issues(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(log_file, component_core_client, SAMPLE_LOG, top_n=2)
        assert len(result["top_issues"]) == 2
        assert result["summary"]["showing_top_n"] == 2

    async def test_top_n_larger_than_issues_returns_all(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(
            log_file, component_core_client, SAMPLE_LOG, top_n=100
        )
        assert len(result["top_issues"]) == result["summary"]["unique_issues"] == 6
        assert result["summary"]["showing_top_n"] == 6

    async def test_empty_log_returns_zero_counts(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(log_file, component_core_client, EMPTY_LOG)
        assert result["success"] is True
        assert result["summary"]["total_raw_lines"] == 0
        assert result["summary"]["parsed_entries"] == 0
        assert result["summary"]["unique_issues"] == 0
        assert result["top_issues"] == []
        assert result["by_component"] == {}

    async def test_window_reports_the_slice_that_was_read(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        # Without the window a caller cannot tell a 2-hour slice from the
        # instance's whole history.
        summary = (await structured(log_file, component_core_client, SAMPLE_LOG))[
            "summary"
        ]
        assert summary["window_start"] == "2026-05-27 10:00:01.123"
        assert summary["window_end"] == "2026-05-27 10:00:08.444"

    async def test_window_covers_filtered_out_entries_too(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(
            log_file, component_core_client, SAMPLE_LOG, level="WARNING"
        )
        assert result["summary"]["window_start"] == "2026-05-27 10:00:01.123"
        assert result["summary"]["window_end"] == "2026-05-27 10:00:08.444"

    async def test_window_is_none_when_nothing_parsed(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        summary = (await structured(log_file, component_core_client, UNPARSEABLE_LOG))[
            "summary"
        ]
        assert summary["window_start"] is None
        assert summary["window_end"] is None

    async def test_window_is_min_and_max_not_first_and_last_line(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        # The ascending fixtures cannot tell first/last-line bounds from min/max.
        summary = (await structured(log_file, component_core_client, OUT_OF_ORDER_LOG))[
            "summary"
        ]
        assert summary["window_start"] == "2026-05-27 10:00:01.000"
        assert summary["window_end"] == "2026-05-27 10:00:09.000"

    async def test_mixed_date_separators_do_not_corrupt_ordering(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        # " " sorts below every digit and "T" above them, so un-normalized the
        # T-form line wins every max() and loses every min() whatever its time.
        log = textwrap.dedent("""\
            2026-05-27T10:00:05.000 ERROR (MainThread) [a.b.c] boom
            2026-05-27 10:00:09.000 ERROR (MainThread) [a.b.c] boom
            2026-05-27T10:00:01.000 ERROR (MainThread) [a.b.c] boom
        """)
        result = await structured(log_file, component_core_client, log)
        issue = result["top_issues"][0]
        assert issue["count"] == 3
        assert issue["first_seen"] == "2026-05-27 10:00:01.000"
        assert issue["last_seen"] == "2026-05-27 10:00:09.000"
        assert result["summary"]["window_start"] == "2026-05-27 10:00:01.000"
        assert result["summary"]["window_end"] == "2026-05-27 10:00:09.000"

    async def test_nested_paren_thread_name_parses(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        # Since Python 3.10 a raw thread is named "Thread-N (fn)"; dropping its
        # lines would hide a whole component from top_issues and by_component.
        result = await structured(
            log_file, component_core_client, NESTED_PAREN_THREAD_LOG
        )
        assert result["summary"]["unparseable_lines"] == 0
        assert result["summary"]["parsed_entries"] == 1
        issue = result["top_issues"][0]
        assert issue["logger"] == "pychromecast.socket_client"
        assert issue["message"] == "Connection reset"

    async def test_logger_is_the_first_bracket_group_not_the_last(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        log = (
            "2026-05-27 10:00:01.123 ERROR (SyncWorker_3) [a.b] "
            "msg with (parens) [brackets] and a tail\n"
        )
        issue = (await structured(log_file, component_core_client, log))["top_issues"][
            0
        ]
        assert issue["logger"] == "a.b"
        assert issue["message"] == "msg with (parens) [brackets] and a tail"

    async def test_unparseable_lines_are_skipped(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(log_file, component_core_client, UNPARSEABLE_LOG)
        assert result["summary"]["parsed_entries"] == 0
        assert result["top_issues"] == []

    async def test_message_truncated_to_max_len(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        long_msg = "x" * (MAX_MESSAGE_LEN + 100)
        log = f"2026-05-27 10:00:01.000 ERROR (MainThread) [a.b.c] {long_msg}\n"
        message = (await structured(log_file, component_core_client, log))[
            "top_issues"
        ][0]["message"]
        # An unmarked cut is indistinguishable from a genuinely short message.
        assert message.endswith(TRUNCATION_MARK)
        assert len(message) == MAX_MESSAGE_LEN + len(TRUNCATION_MARK)
        assert message[:MAX_MESSAGE_LEN] == "x" * MAX_MESSAGE_LEN

    async def test_messages_differing_past_the_cap_stay_distinct(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        # Deduplicating on the capped display value would merge two unrelated
        # errors sharing a long prefix, invisibly: both render the same.
        shared = "x" * MAX_MESSAGE_LEN
        log = (
            f"2026-05-27 10:00:01.000 ERROR (MainThread) [a.b.c] {shared}first\n"
            f"2026-05-27 10:00:02.000 ERROR (MainThread) [a.b.c] {shared}second\n"
        )
        result = await structured(log_file, component_core_client, log)
        assert result["summary"]["unique_issues"] == 2
        assert [i["count"] for i in result["top_issues"]] == [1, 1]
        assert all(i["message"].endswith(TRUNCATION_MARK) for i in result["top_issues"])

    async def test_short_message_is_not_marked_truncated(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        log = "2026-05-27 10:00:01.000 ERROR (MainThread) [a.b.c] short\n"
        result = await structured(log_file, component_core_client, log)
        assert result["top_issues"][0]["message"] == "short"

    async def test_default_top_n_is_applied(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        lines = "\n".join(
            f"2026-05-27 10:00:{i:02d}.000 ERROR (MainThread) [a.b.c] Unique {i}"
            for i in range(DEFAULT_TOP_N + 5)
        )
        result = await structured(log_file, component_core_client, lines)
        assert len(result["top_issues"]) == DEFAULT_TOP_N


class TestSupervisorInstallRegressions:
    """Supervisor-backed installs (add-on / HAOS) serve ANSI-coloured journald."""

    async def test_ansi_coloured_lines_parse(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        # An ^-anchored regex matches no colour-wrapped line at all, turning a
        # log full of errors into "success: true, top_issues: []".
        summary = (await structured(log_file, component_core_client, ANSI_LOG))[
            "summary"
        ]
        assert summary["parsed_entries"] == 3
        assert summary["unparseable_lines"] == 0
        assert summary["unique_issues"] == 2

    async def test_ansi_reset_code_does_not_leak_into_message(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        # A trailing \x1b[0m in the message would split one recurring error
        # into several distinct ones.
        result = await structured(log_file, component_core_client, ANSI_LOG)
        timeout = next(
            i for i in result["top_issues"] if i["message"] == "Device timeout"
        )
        assert timeout["count"] == 2
        for issue in result["top_issues"]:
            assert "\x1b" not in issue["message"]

    async def test_total_format_drift_warns_instead_of_reading_as_clean(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(log_file, component_core_client, UNPARSEABLE_LOG)
        assert result["summary"]["parsed_entries"] == 0
        assert result["summary"]["unparseable_lines"] > 0
        # `warnings: list[str]` is the repo's single warning channel.
        assert isinstance(result["warnings"], list)
        assert "NOT evidence" in result["warnings"][0]
        assert "warning" not in result

    @pytest.mark.parametrize("text", ["", "\n\n   \n"])
    async def test_empty_fetch_warns_instead_of_reading_as_all_clear(
        self, log_file: Path, component_core_client: HomeAssistantClient, text: str
    ) -> None:
        # A running instance always logs something, so no lines means the
        # fetch produced nothing, not a healthy instance.
        result = await structured(log_file, component_core_client, text)
        assert "NOT evidence" in result["warnings"][0]
        assert "empty or failed fetch" in result["warnings"][0]

    async def test_blank_only_input_counts_blank_lines_not_unparseable_ones(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        summary = (await structured(log_file, component_core_client, "\n\n   \n"))[
            "summary"
        ]
        assert summary["blank_lines"] == 3
        assert summary["unparseable_lines"] == 0


class TestCountSemantics:
    """`unparseable`, `filtered out` and `blank` are different things."""

    async def test_traceback_lines_counted_as_unparseable(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        # 1 ERROR line + 4 traceback lines (header, File, source, exception).
        summary = (await structured(log_file, component_core_client, TRACEBACK_LOG))[
            "summary"
        ]
        assert summary["total_raw_lines"] == 5
        assert summary["parsed_entries"] == 1
        assert summary["unparseable_lines"] == 4

    async def test_every_input_line_is_accounted_for(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        # Every bucket is named, so the counters reconcile exactly; an unnamed
        # residual leaves a caller unable to tell what the gap was.
        log = SAMPLE_LOG + "\nTraceback (most recent call last):\n"
        s = (await structured(log_file, component_core_client, log, level="ERROR"))[
            "summary"
        ]
        assert (
            s["blank_lines"] + s["unparseable_lines"] + s["matched_lines"]
            == s["total_raw_lines"]
        )
        assert s["blank_lines"] == 1
        assert s["unparseable_lines"] == 1
        assert s["matched_lines"] == 8
        # matched_lines - parsed_entries is exactly what the filters removed.
        assert s["parsed_entries"] == 4

    async def test_filtered_out_lines_are_not_reported_as_unparseable(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        # A narrow filter must not read as "99% unparseable": a caller would
        # fall back to the raw log, the thing this feature exists to prevent.
        lines = [
            f"2026-05-27 10:00:{i % 60:02d}.000 INFO (MainThread) [a.b.c] tick"
            for i in range(200)
        ]
        lines.append("2026-05-27 11:00:00.000 ERROR (MainThread) [a.b.c] dead")
        result = await structured(
            log_file, component_core_client, "\n".join(lines), level="ERROR"
        )
        assert result["summary"]["unparseable_lines"] == 0
        assert result["summary"]["parsed_entries"] == 1
        assert "warnings" not in result

    async def test_all_filtered_out_warns_about_filters_not_parsing(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(
            log_file, component_core_client, SAMPLE_LOG, search="nothing-matches-this"
        )
        assert result["summary"]["parsed_entries"] == 0
        assert result["summary"]["unparseable_lines"] == 0
        assert "reflects the filters" in result["warnings"][0]

    async def test_filtered_empty_result_with_tracebacks_does_not_claim_format_drift(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        # Entries parse, tracebacks push the unparseable count high, and the
        # filter matches none: answering "no CRITICAL entries" with "no log
        # lines could be parsed" sends the agent back to the raw fetch.
        result = await structured(
            log_file, component_core_client, TRACEBACK_LOG * 5, level="CRITICAL"
        )
        s = result["summary"]
        assert s["unparseable_lines"] == 20
        assert s["matched_lines"] == 5
        assert s["parsed_entries"] == 0
        warning = result["warnings"][0]
        assert "reflects the filters" in warning
        assert "could not" not in warning and "NOT evidence" not in warning


class TestBoundedOutput:
    """Output stays bounded whatever the log holds."""

    async def test_by_component_is_capped(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        log = "\n".join(
            f"2026-05-27 10:00:00.000 ERROR (MainThread) [comp{i}.sub.mod] issue{i}"
            for i in range(MAX_COMPONENTS * 4)
        )
        result = await structured(log_file, component_core_client, log)
        assert len(result["by_component"]) == MAX_COMPONENTS
        assert result["summary"]["showing_components"] == MAX_COMPONENTS
        # The true total is still reported, so the cap never hides the scale.
        assert result["summary"]["components_affected"] == MAX_COMPONENTS * 4

    async def test_by_component_totals_survive_top_n_truncation(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        # Occurrences dropped from top_issues are still counted in by_component.
        log = "\n".join(
            f"2026-05-27 10:00:00.000 ERROR (MainThread) [one.sub.mod] issue{i}"
            for i in range(10)
        )
        result = await structured(log_file, component_core_client, log, top_n=2)
        assert len(result["top_issues"]) == 2
        bucket = result["by_component"]["one.sub.mod"]
        assert bucket["issue_count"] == 10
        assert bucket["total_occurrences"] == 10


async def test_same_message_at_different_levels_stays_distinct(
    log_file: Path, component_core_client: HomeAssistantClient
) -> None:
    # First-wins on level meant a later ERROR could be reported as WARNING.
    log = textwrap.dedent("""\
        2026-05-27 10:00:00.000 WARNING (MainThread) [a.b.c] same text
        2026-05-27 10:00:01.000 ERROR (MainThread) [a.b.c] same text
    """)
    result = await structured(log_file, component_core_client, log)
    assert result["summary"]["unique_issues"] == 2
    assert {i["level"] for i in result["top_issues"]} == {"WARNING", "ERROR"}
