"""The window and paging of ``ha_get_logs(source='error_log')`` over Core's log.

Every fetch asks for a bounded window (#2279), and ``offset`` / ``has_more`` /
``next_offset`` let a caller read further back, in raw and structured mode.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.error_log_parsing import STRUCTURED_ERROR_LOG_WINDOW_LINES
from ha_mcp.tools.log_common import (
    DEFAULT_LOG_LIMIT,
    MAX_LIMIT,
    SUPERVISOR_SEARCH_WINDOW_LINES,
)

from ._error_log_core import HEADER_LOOKBACK_LINES, error_log, numbered_log
from ._error_log_core import log_file as log_file
from ._error_log_samples import EMPTY_LOG, SAMPLE_LOG

MAX_WINDOW = MAX_LIMIT + HEADER_LOOKBACK_LINES


class TestFetchWindowIsBounded:
    """Every error_log fetch asks for a bounded window (#2279).

    The unconditional 20,000-line request these replace made Supervisor
    assemble a journald slice for 15+ minutes before the tool call returned.
    """

    async def test_structured_reads_the_bounded_summary_window(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file, core_client, numbered_log(2500), structured=True
        )
        assert STRUCTURED_ERROR_LOG_WINDOW_LINES < 20000
        assert result["window_lines"] == STRUCTURED_ERROR_LOG_WINDOW_LINES
        assert result["summary"]["total_raw_lines"] == STRUCTURED_ERROR_LOG_WINDOW_LINES

    async def test_raw_unfiltered_window_reaches_past_the_limit(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(log_file, core_client, numbered_log(1000), limit=25)
        assert result["window_lines"] > 25
        assert result["window_lines"] <= MAX_WINDOW
        assert result["total_lines"] == result["window_lines"]

    async def test_raw_default_limit_reaches_past_the_default(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(log_file, core_client, numbered_log(1000))
        assert result["window_lines"] > DEFAULT_LOG_LIMIT
        assert result["window_lines"] <= MAX_WINDOW

    @pytest.mark.parametrize(
        "filter_kwargs",
        [{"search": "issue"}, {"level": "ERROR"}, {"search": "x", "level": "ERROR"}],
    )
    async def test_raw_client_side_filters_widen_the_window(
        self,
        log_file: Path,
        core_client: HomeAssistantClient,
        filter_kwargs: dict[str, str],
    ) -> None:
        """`search` and `level` both filter the fetched text client-side, so both
        need history behind the caller's limit to find matches in."""
        result = await error_log(
            log_file, core_client, numbered_log(3000), limit=10, **filter_kwargs
        )
        assert result["window_lines"] == SUPERVISOR_SEARCH_WINDOW_LINES

    async def test_limit_is_clamped_before_it_sizes_the_window(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file, core_client, numbered_log(1000), limit=MAX_LIMIT + 5000
        )
        assert result["window_lines"] > MAX_LIMIT
        assert result["window_lines"] <= MAX_WINDOW

    async def test_raw_offset_skips_the_newest_lines(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file, core_client, numbered_log(1000), limit=5, offset=400
        )
        assert result["offset"] == 400
        # 1000 lines, newest 400 skipped: the newest one read is "issue 599".
        assert result["log"].splitlines()[0].endswith("issue 599")

    async def test_structured_offset_skips_the_newest_lines(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file, core_client, numbered_log(1000), structured=True, offset=400
        )
        assert result["offset"] == 400
        assert result["summary"]["total_raw_lines"] == 600


class TestErrorLogPagination:
    """`offset`/`has_more`/`next_offset`: the logbook pagination contract,
    applied to the bounded log window so an agent can read further back."""

    async def test_has_more_when_history_remains_behind_the_window(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(log_file, core_client, numbered_log(400), limit=20)
        assert result["offset"] == 0
        assert result["has_more"] is True
        assert result["next_offset"] == 20
        assert "offset=20" in result["pagination_hint"]

    async def test_a_window_that_holds_the_whole_log_does_not_claim_more_history(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        """A log exactly as long as the window is full, yet nothing precedes it."""
        window = 20 + HEADER_LOOKBACK_LINES
        result = await error_log(log_file, core_client, numbered_log(window), limit=20)
        assert result["window_lines"] == window
        assert result["has_more"] is False
        assert "next_offset" not in result
        assert "offset=" not in result["pagination_hint"]

    async def test_terminal_window_with_unreturned_matches_hints_a_larger_limit(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        """Matches the limit slice left inside the LAST window must stay reachable.

        Paging cannot reach them: no older history exists behind the window,
        so a next_offset here would loop. A larger limit retrieves them from
        the already-addressed window exactly.
        """
        result = await error_log(
            log_file, core_client, numbered_log(50), limit=10, search="issue"
        )
        assert result["has_more"] is False
        assert "next_offset" not in result
        assert "40 more matching lines remain" in result["pagination_hint"]
        assert "limit=50" in result["pagination_hint"]

    async def test_terminal_window_past_the_cap_names_the_shortfall(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        """One response cannot return this window, so the hint must say so.

        The old wording promised a single call would retrieve every remaining
        line while capping the suggested limit at ``MAX_LIMIT``, so the caller
        came back with the same unsatisfiable request.
        """
        result = await error_log(
            log_file,
            core_client,
            numbered_log(MAX_LIMIT + 100),
            limit=MAX_LIMIT,
            search="issue",
        )
        assert result["has_more"] is False
        hint = result["pagination_hint"]
        assert f"returns {MAX_LIMIT} of them" in hint
        assert "narrow" in hint
        assert "in one response" not in hint

    async def test_next_offset_advances_from_the_current_offset(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file, core_client, numbered_log(400), limit=20, offset=60
        )
        assert result["offset"] == 60
        assert result["next_offset"] == 80

    async def test_window_lines_reports_the_size_requested(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        """A degraded (short) window has to be visible as such.

        Without it, a proxy that served 100 lines for a 2000-line ask is
        indistinguishable from a log that only holds 100.
        """
        result = await error_log(log_file, core_client, numbered_log(20), limit=200)
        assert result["window_lines"] > 200
        assert result["window_lines"] <= MAX_WINDOW
        assert result["total_lines"] == 20

    async def test_filtered_window_resumes_where_the_shown_block_starts(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        """A filter that drops most of a window must not skip what it dropped.

        Window of 2000, limit 10: the tool shows the 10 newest matches, so the
        next page has to resume just before the oldest one shown. Stepping a
        whole window instead jumps over every match in between, silently, and
        exactly when a search is what the caller asked for.
        """
        window = SUPERVISOR_SEARCH_WINDOW_LINES
        # Skipping the newest 200 of 2300 lines leaves lines 100..2099.
        result = await error_log(
            log_file,
            core_client,
            numbered_log(window + 300),
            limit=10,
            search="issue 7",
            offset=200,
        )
        # Only "issue 700".."issue 799" lie inside that window.
        assert result["total_lines"] == 100
        assert result["returned_lines"] == 10
        # The oldest match shown is "issue 790", 690 lines into the window.
        assert result["next_offset"] == 200 + window - 690
        assert "search='issue 7'" in result["pagination_hint"]

    async def test_a_match_on_the_oldest_line_steps_exactly_one_window(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        """The step never exceeds the window that produced it."""
        window = SUPERVISOR_SEARCH_WINDOW_LINES
        log = numbered_log(window + 50).replace("issue 50\n", "needle\n")
        result = await error_log(log_file, core_client, log, limit=5, search="needle")
        assert result["returned_lines"] == 1
        assert result["next_offset"] == window

    async def test_a_fully_filtered_window_steps_the_whole_window(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        """Nothing shown means nothing to resume before."""
        result = await error_log(
            log_file,
            core_client,
            numbered_log(SUPERVISOR_SEARCH_WINDOW_LINES + 20),
            limit=20,
            search="nothing-matches-this",
        )
        assert result["returned_lines"] == 0
        assert result["next_offset"] == SUPERVISOR_SEARCH_WINDOW_LINES

    async def test_structured_pages_by_the_whole_window(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        window = STRUCTURED_ERROR_LOG_WINDOW_LINES
        result = await error_log(
            log_file,
            core_client,
            numbered_log(2 * window + 100),
            structured=True,
            offset=8,
        )
        assert result["structured"] is True
        assert result["offset"] == 8
        assert result["has_more"] is True
        assert result["window_lines"] == window
        # The summary consumes the whole window, so the next page is a whole
        # window further back.
        assert result["next_offset"] == 8 + window
        assert "structured=True" in result["pagination_hint"]
        assert f"offset={8 + window}" in result["pagination_hint"]

    async def test_structured_end_of_history_reports_no_next_offset(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(log_file, core_client, SAMPLE_LOG, structured=True)
        assert result["has_more"] is False
        assert "next_offset" not in result
        assert result["window_lines"] == STRUCTURED_ERROR_LOG_WINDOW_LINES

    async def test_note_explains_what_offset_counts(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        """The window is not the whole log; the response has to say so, or an
        agent reads `total_lines` as the instance's total."""
        result = await error_log(log_file, core_client, SAMPLE_LOG)
        assert "offset" in result["note"]
        assert "window" in result["note"]

    @pytest.mark.parametrize("structured", [False, True])
    async def test_last_page_says_it_reached_the_start_of_history(
        self, log_file: Path, core_client: HomeAssistantClient, structured: bool
    ) -> None:
        """The clamp can hand back entries an earlier page already showed, so
        the terminal page says so rather than reading as fresh history."""
        result = await error_log(
            log_file, core_client, numbered_log(100), structured=structured, offset=50
        )
        assert result["has_more"] is False
        assert "start of the available history" in result["note"]

    async def test_first_page_does_not_claim_to_be_the_last(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(log_file, core_client, SAMPLE_LOG)
        assert "start of the available history" not in result["note"]


class TestEmptyWindowAtAnOffset:
    """An empty window means different things at the newest edge and deep in
    history, and the wrong one sends the caller after an outage that is not
    happening (or blesses a failed fetch as a clean log)."""

    @pytest.mark.parametrize("structured", [False, True])
    async def test_empty_page_past_the_end_is_benign(
        self, log_file: Path, core_client: HomeAssistantClient, structured: bool
    ) -> None:
        result = await error_log(
            log_file, core_client, EMPTY_LOG, structured=structured, offset=900
        )
        joined = " ".join(result["warnings"])
        assert "offset=900" in joined
        # The failed-fetch alarm must not fire on a routine last page.
        assert "NOT evidence" not in joined

    @pytest.mark.parametrize("structured", [False, True])
    async def test_empty_fetch_at_offset_zero_still_warns_loudly(
        self, log_file: Path, core_client: HomeAssistantClient, structured: bool
    ) -> None:
        """A running instance always logs something."""
        result = await error_log(
            log_file, core_client, EMPTY_LOG, structured=structured
        )
        joined = " ".join(result["warnings"])
        assert "NOT evidence" in joined
        assert "offset=" not in joined
