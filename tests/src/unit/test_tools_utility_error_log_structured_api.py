"""``ha_get_logs(source='error_log')`` parameters, warnings and failures.

Covers how the tool takes ``structured``, ``top_n``, ``limit``, ``level`` and
``offset``, the warnings it adds when a parameter does not apply, how an empty
fetch is reported, and how a refused token surfaces. It also covers the window
and paging: every fetch asks for a bounded window (#2279), and ``offset`` /
``has_more`` / ``next_offset`` let a caller read further back, in raw and
structured mode. The summary's content is in
``test_tools_utility_error_log_structured.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.error_log_parsing import STRUCTURED_ERROR_LOG_WINDOW_LINES
from ha_mcp.tools.log_common import (
    DEFAULT_LOG_LIMIT,
    MAX_LIMIT,
    SUPERVISOR_SEARCH_WINDOW_LINES,
)

from ._error_log_core import (
    HEADER_LOOKBACK_LINES,
    assert_cursor_resumes_at,
    call_logs,
    error_log,
    expired_token_client,
    numbered_log,
    serve_log,
    structured,
)
from ._error_log_core import log_file as log_file
from ._error_log_samples import (
    EMPTY_LOG,
    SAMPLE_LOG,
    UNPARSEABLE_LOG,
)
from ._log_tool_calls import set_up_core_logs
from ._tool_errors import rejected_call_body

MAX_WINDOW = MAX_LIMIT + HEADER_LOOKBACK_LINES


@pytest.fixture
async def core_logs(hass: HomeAssistant, core_client: HomeAssistantClient) -> None:
    """Core's ``system_log`` and ``logger`` answering ``system`` and ``logger``."""
    await set_up_core_logs(hass)


@pytest.fixture
async def logbook(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core's logbook on a recorder, as the tool reads it."""
    assert await async_setup_component(hass, "logbook", {})
    await async_wait_recording_done(hass)


class TestStructuredParameter:
    async def test_structured_true_returns_parsed_response(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file, component_core_client, SAMPLE_LOG, structured=True
        )
        assert result["success"] is True
        assert result["structured"] is True
        assert "top_issues" in result
        assert "by_component" in result
        assert "log" not in result

    async def test_structured_false_returns_raw_log(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file, component_core_client, SAMPLE_LOG, structured=False
        )
        assert "Device timeout" in result["log"]
        assert "top_issues" not in result

    async def test_structured_default_is_false(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(log_file, component_core_client, SAMPLE_LOG)
        assert "Device timeout" in result["log"]
        assert "top_issues" not in result

    @pytest.mark.parametrize(
        ("raw_value", "summary"), [("true", True), ("false", False)]
    )
    async def test_structured_string_is_coerced_by_the_schema(
        self,
        log_file: Path,
        component_core_client: HomeAssistantClient,
        raw_value: str,
        summary: bool,
    ) -> None:
        """AI tools often pass booleans as strings; "false" must not read as true."""
        result = await error_log(
            log_file, component_core_client, SAMPLE_LOG, structured=raw_value
        )
        assert ("top_issues" in result) is summary
        assert ("log" in result) is not summary

    async def test_structured_on_non_error_log_source_emits_warning(
        self, core_logs: None, core_client: HomeAssistantClient
    ) -> None:
        """structured=True on source='system' warns and is ignored."""
        logging.getLogger("homeassistant.components.zha").error("Device timeout")
        result = await call_logs(core_client, source="system", structured=True)
        assert any("structured" in w for w in result["warnings"])
        assert [e["message"] for e in result["entries"]] == [["Device timeout"]]

    async def test_structured_on_logbook_emits_warning(
        self, logbook: None, core_client: HomeAssistantClient
    ) -> None:
        result = await call_logs(core_client, source="logbook", structured=True)
        assert any("structured" in w for w in result["warnings"])
        assert "top_issues" not in result


class TestTopNParameter:
    async def test_top_n_without_structured_emits_warning(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """top_n on the raw path does nothing, the obvious caller mistake.

        The warning must name the piece that is missing: the source is already
        right, so blaming it would tell the caller their correct argument is
        the problem.
        """
        result = await error_log(log_file, component_core_client, SAMPLE_LOG, top_n=5)
        assert "Device timeout" in result["log"]
        warning = next(w for w in result["warnings"] if "top_n" in w)
        assert "ignored because structured=False" in warning
        assert "ignored for source=" not in warning

    async def test_top_n_on_other_source_emits_warning(
        self, core_logs: None, core_client: HomeAssistantClient
    ) -> None:
        result = await call_logs(core_client, source="system", top_n=5)
        # Here the source IS the problem, so the tail must still name it.
        warning = next(w for w in result["warnings"] if "top_n" in w)
        assert "ignored for source='system'" in warning

    async def test_top_n_in_structured_mode_does_not_warn(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(log_file, component_core_client, SAMPLE_LOG, top_n=5)
        assert len(result["top_issues"]) == 5
        assert not any("top_n" in w for w in result.get("warnings", []))

    async def test_a_top_n_below_one_is_refused_by_name(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """The refusal must name the parameter the caller passed, not `limit`."""
        await serve_log(log_file, SAMPLE_LOG)
        with pytest.raises(ToolError, match="top_n") as refused:
            await call_logs(
                component_core_client, source="error_log", structured=True, top_n=0
            )
        assert "limit" not in str(refused.value)


class TestLimitAndLevel:
    async def test_limit_zero_is_accepted_in_structured_mode(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """`limit` is validated only on the raw path, and this pins why.

        The summary ranks the whole fetched window, so `limit` has no meaning
        there and validating it would reject a value that changes nothing.
        """
        result = await structured(log_file, component_core_client, SAMPLE_LOG, limit=0)
        assert result["structured"] is True
        assert result["summary"]["parsed_entries"] == 8
        # Accepted, but not silently: the caller still learns it did nothing.
        assert any("limit" in w for w in result["warnings"])

    async def test_limit_zero_is_still_rejected_on_the_raw_path(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """The counterpart: where `limit` does apply, 0 is still invalid."""
        await serve_log(log_file, SAMPLE_LOG)
        with pytest.raises(ToolError):
            await call_logs(component_core_client, source="error_log", limit=0)

    async def test_level_critical_is_accepted(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """CRITICAL is a valid level, so CRITICAL lines can be isolated."""
        result = await structured(
            log_file, component_core_client, SAMPLE_LOG, level="CRITICAL"
        )
        assert [i["message"] for i in result["top_issues"]] == ["DB error"]


# The component probe uses a real WebSocket. After refusing its token, Core
# closes it and aiohttp re-arms the server heartbeat on the client's close reply.
@pytest.mark.parametrize("expected_lingering_timers", [True])
class TestRefusedToken:
    async def test_auth_error_surfaces_as_structured_tool_error(
        self, log_file: Path, core_client: HomeAssistantClient
    ) -> None:
        """A 401 must not escape raw.

        ``HomeAssistantAuthError`` is a sibling of ``HomeAssistantAPIError``, not
        a subclass, so an ``except (HomeAssistantAPIError, ...)`` tuple lets it
        through to FastMCP without a structured ``code``.
        """
        await serve_log(log_file, SAMPLE_LOG)
        async with expired_token_client(core_client) as stale:
            body = await rejected_call_body(call_logs(stale, source="error_log"))
        assert body["success"] is False
        assert body["error"]["code"]
        assert body.get("source") == "error_log"

    async def test_expired_token_on_a_supervised_install_reaches_the_auth_handler(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """A 401 on the install-class probe must reach the auth handler.

        With no log file Core registers no ``/api/error_log``, as under the
        Supervisor. The probe used to swallow the 401 and answer "not
        supervised", so the call fell through to that missing route and the
        dead token came back as a 404 with connection advice.
        """
        async with expired_token_client(core_client) as stale:
            body = await rejected_call_body(call_logs(stale, source="error_log"))
        assert body["success"] is False
        assert body["error"]["code"] == "AUTH_INVALID_TOKEN"
        assert "404" not in str(body)


class TestWarningsAreMergedNotClobbered:
    """Three layers can warn on one structured call; none may overwrite another.

    The parser (format drift), the structured builder (ignored limit/order) and
    ``get_logs`` (wrong-source params) each append to the same top-level list.
    An overwrite silently drops the "this summary is NOT an all-clear" notice,
    the one warning that must never be lost.
    """

    async def test_drift_warning_survives_ignored_param_warning(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(
            log_file, component_core_client, UNPARSEABLE_LOG, limit=5, order="oldest"
        )
        joined = " ".join(result["warnings"])
        assert "NOT evidence" in joined
        assert "do not apply when structured=True" in joined

    async def test_wrong_source_param_warning_does_not_clobber_drift(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await structured(
            log_file, component_core_client, UNPARSEABLE_LOG, entity_id="light.kitchen"
        )
        joined = " ".join(result["warnings"])
        assert "only apply to source='logbook'" in joined
        assert "NOT evidence" in joined


class TestEmptyFetch:
    @pytest.mark.parametrize("raw", [EMPTY_LOG, "\n\n   \n"])
    async def test_empty_fetch_on_the_default_path_warns_too(
        self, log_file: Path, component_core_client: HomeAssistantClient, raw: str
    ) -> None:
        """The same fetch must not be blessed by one mode and refused by the other.

        `structured=False` is the default, so most agents are on this path: an
        empty fetch there used to answer success with an empty `log`, which
        reads back to the user as "no errors in the log".
        """
        result = await error_log(log_file, component_core_client, raw)
        assert result["success"] is True
        assert result["log"].strip() == ""
        assert "empty or failed fetch" in result["warnings"][0]
        assert "NOT evidence" in result["warnings"][0]

    async def test_empty_filter_result_is_not_reported_as_an_empty_fetch(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """An empty *filter result* keeps its own story.

        The fetch worked and the filter excluded everything, which
        `filters_applied` already discloses; warning about a failed fetch would
        send the caller after an infrastructure problem that does not exist.
        """
        result = await error_log(
            log_file, component_core_client, SAMPLE_LOG, search="nothing-matches-this"
        )
        assert result["total_lines"] == 0
        assert result["filters_applied"] == {"search": "nothing-matches-this"}
        assert not any("empty or failed fetch" in w for w in result.get("warnings", []))


class TestOffsetParameterIncompatibilityWarning:
    """`offset` applies to source='logbook' and source='error_log'. Anywhere
    else it must be flagged rather than silently dropped."""

    @pytest.mark.parametrize("source", ["system", "logger"])
    async def test_offset_on_an_unsupported_source_warns(
        self, core_logs: None, core_client: HomeAssistantClient, source: str
    ) -> None:
        result = await call_logs(core_client, source=source, offset=50)
        warnings = result.get("warnings", [])
        assert any("offset" in w and source in w for w in warnings), warnings

    @pytest.mark.parametrize("structured_mode", [False, True])
    async def test_offset_on_error_log_does_not_warn(
        self,
        log_file: Path,
        component_core_client: HomeAssistantClient,
        structured_mode: bool,
    ) -> None:
        result = await error_log(
            log_file,
            component_core_client,
            numbered_log(100),
            structured=structured_mode,
            offset=50,
        )
        assert not any("offset" in w for w in result.get("warnings", []))

    async def test_zero_offset_never_warns(
        self, core_logs: None, core_client: HomeAssistantClient
    ) -> None:
        """The default stays silent: a warning on every `system` call would
        train agents to ignore the channel."""
        result = await call_logs(core_client, source="system")
        assert not any("offset" in w for w in result.get("warnings", []))


# -- window and paging ------------------------------------------------------


class TestFetchWindowIsBounded:
    """Every error_log fetch asks for a bounded window (#2279).

    The unconditional 20,000-line request these replace made Supervisor
    assemble a journald slice for 15+ minutes before the tool call returned.
    """

    async def test_structured_reads_the_bounded_summary_window(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file, component_core_client, numbered_log(2500), structured=True
        )
        assert STRUCTURED_ERROR_LOG_WINDOW_LINES < 20000
        assert result["window_lines"] == STRUCTURED_ERROR_LOG_WINDOW_LINES
        assert result["summary"]["total_raw_lines"] == STRUCTURED_ERROR_LOG_WINDOW_LINES

    async def test_raw_unfiltered_window_reaches_past_the_limit(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file, component_core_client, numbered_log(1000), limit=25
        )
        assert result["window_lines"] > 25
        assert result["window_lines"] <= MAX_WINDOW
        assert result["total_lines"] == result["window_lines"]

    async def test_raw_default_limit_reaches_past_the_default(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(log_file, component_core_client, numbered_log(1000))
        assert result["window_lines"] > DEFAULT_LOG_LIMIT
        assert result["window_lines"] <= MAX_WINDOW

    @pytest.mark.parametrize(
        "filter_kwargs",
        [{"search": "issue"}, {"level": "ERROR"}, {"search": "x", "level": "ERROR"}],
    )
    async def test_raw_client_side_filters_widen_the_window(
        self,
        log_file: Path,
        component_core_client: HomeAssistantClient,
        filter_kwargs: dict[str, str],
    ) -> None:
        """`search` and `level` both filter the fetched text client-side, so both
        need history behind the caller's limit to find matches in."""
        result = await error_log(
            log_file,
            component_core_client,
            numbered_log(3000),
            limit=10,
            **filter_kwargs,
        )
        assert result["window_lines"] == SUPERVISOR_SEARCH_WINDOW_LINES

    async def test_limit_is_clamped_before_it_sizes_the_window(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file, component_core_client, numbered_log(1000), limit=MAX_LIMIT + 5000
        )
        assert result["window_lines"] > MAX_LIMIT
        assert result["window_lines"] <= MAX_WINDOW

    async def test_raw_offset_skips_the_newest_lines(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file, component_core_client, numbered_log(1000), limit=5, offset=400
        )
        assert result["offset"] == 400
        # 1000 lines, newest 400 skipped: the newest one read is "issue 599".
        assert result["log"].splitlines()[0].endswith("issue 599")

    async def test_structured_offset_skips_the_newest_lines(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file,
            component_core_client,
            numbered_log(1000),
            structured=True,
            offset=400,
        )
        assert result["offset"] == 400
        assert result["summary"]["total_raw_lines"] == 600


class TestErrorLogPagination:
    """`offset`/`has_more`/`next_offset`: the logbook pagination contract,
    applied to the bounded log window so an agent can read further back."""

    async def test_has_more_when_history_remains_behind_the_window(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file, component_core_client, numbered_log(400), limit=20
        )
        assert result["offset"] == 0
        assert result["has_more"] is True
        await assert_cursor_resumes_at(component_core_client, result, 20)

    async def test_a_window_that_holds_the_whole_log_does_not_claim_more_history(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """A log exactly as long as the window is full, yet nothing precedes it."""
        window = 20 + HEADER_LOOKBACK_LINES
        result = await error_log(
            log_file, component_core_client, numbered_log(window), limit=20
        )
        assert result["window_lines"] == window
        assert result["has_more"] is False
        assert "next_offset" not in result
        assert "offset=" not in result["pagination_hint"]

    async def test_terminal_window_with_unreturned_matches_hints_a_larger_limit(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """Matches the limit slice left inside the LAST window must stay reachable.

        Paging cannot reach them: no older history exists behind the window,
        so a next_offset here would loop. A larger limit retrieves them from
        the already-addressed window exactly.
        """
        result = await error_log(
            log_file, component_core_client, numbered_log(50), limit=10, search="issue"
        )
        assert result["has_more"] is False
        assert "next_offset" not in result
        assert "40 more matching lines remain" in result["pagination_hint"]
        assert "limit=50" in result["pagination_hint"]

    async def test_terminal_window_past_the_cap_names_the_shortfall(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """One response cannot return this window, so the hint must say so.

        The old wording promised a single call would retrieve every remaining
        line while capping the suggested limit at ``MAX_LIMIT``, so the caller
        came back with the same unsatisfiable request.
        """
        result = await error_log(
            log_file,
            component_core_client,
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
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file, component_core_client, numbered_log(400), limit=20, offset=60
        )
        assert result["offset"] == 60
        await assert_cursor_resumes_at(component_core_client, result, 80)

    async def test_window_lines_reports_the_size_requested(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """A degraded (short) window has to be visible as such.

        Without it, a proxy that served 100 lines for a 2000-line ask is
        indistinguishable from a log that only holds 100.
        """
        result = await error_log(
            log_file, component_core_client, numbered_log(20), limit=200
        )
        assert result["window_lines"] > 200
        assert result["window_lines"] <= MAX_WINDOW
        assert result["total_lines"] == 20

    async def test_filtered_window_resumes_where_the_shown_block_starts(
        self, log_file: Path, component_core_client: HomeAssistantClient
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
            component_core_client,
            numbered_log(window + 300),
            limit=10,
            search="issue 7",
            offset=200,
        )
        # Only "issue 700".."issue 799" lie inside that window.
        assert result["total_lines"] == 100
        assert result["returned_lines"] == 10
        # The oldest match shown is "issue 790", 690 lines into the window.
        await assert_cursor_resumes_at(
            component_core_client, result, 200 + window - 690
        )
        assert "search='issue 7'" in result["pagination_hint"]

    async def test_a_match_on_the_oldest_line_steps_exactly_one_window(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """The step never exceeds the window that produced it."""
        window = SUPERVISOR_SEARCH_WINDOW_LINES
        log = numbered_log(window + 50).replace("issue 50\n", "needle\n")
        result = await error_log(
            log_file, component_core_client, log, limit=5, search="needle"
        )
        assert result["returned_lines"] == 1
        await assert_cursor_resumes_at(component_core_client, result, window)

    async def test_a_fully_filtered_window_steps_the_whole_window(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """Nothing shown means nothing to resume before."""
        result = await error_log(
            log_file,
            component_core_client,
            numbered_log(SUPERVISOR_SEARCH_WINDOW_LINES + 20),
            limit=20,
            search="nothing-matches-this",
        )
        assert result["returned_lines"] == 0
        await assert_cursor_resumes_at(
            component_core_client, result, SUPERVISOR_SEARCH_WINDOW_LINES
        )

    async def test_structured_pages_by_the_whole_window(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        window = STRUCTURED_ERROR_LOG_WINDOW_LINES
        result = await error_log(
            log_file,
            component_core_client,
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
        await assert_cursor_resumes_at(component_core_client, result, 8 + window)
        assert "structured=True" in result["pagination_hint"]

    async def test_structured_end_of_history_reports_no_next_offset(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file, component_core_client, SAMPLE_LOG, structured=True
        )
        assert result["has_more"] is False
        assert "next_offset" not in result
        assert result["window_lines"] == STRUCTURED_ERROR_LOG_WINDOW_LINES

    async def test_note_explains_what_offset_counts(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """The window is not the whole log; the response has to say so, or an
        agent reads `total_lines` as the instance's total."""
        result = await error_log(log_file, component_core_client, SAMPLE_LOG)
        assert "offset" in result["note"]
        assert "window" in result["note"]

    @pytest.mark.parametrize("structured", [False, True])
    async def test_last_page_says_it_reached_the_start_of_history(
        self,
        log_file: Path,
        component_core_client: HomeAssistantClient,
        structured: bool,
    ) -> None:
        """The clamp can hand back entries an earlier page already showed, so
        the terminal page says so rather than reading as fresh history."""
        result = await error_log(
            log_file,
            component_core_client,
            numbered_log(100),
            structured=structured,
            offset=50,
        )
        assert result["has_more"] is False
        assert "start of the available history" in result["note"]

    async def test_first_page_does_not_claim_to_be_the_last(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(log_file, component_core_client, SAMPLE_LOG)
        assert "start of the available history" not in result["note"]


class TestEmptyWindowAtAnOffset:
    """An empty window means different things at the newest edge and deep in
    history, and the wrong one sends the caller after an outage that is not
    happening (or blesses a failed fetch as a clean log)."""

    @pytest.mark.parametrize("structured", [False, True])
    async def test_empty_page_past_the_end_is_benign(
        self,
        log_file: Path,
        component_core_client: HomeAssistantClient,
        structured: bool,
    ) -> None:
        result = await error_log(
            log_file,
            component_core_client,
            EMPTY_LOG,
            structured=structured,
            offset=900,
        )
        joined = " ".join(result["warnings"])
        assert "offset=900" in joined
        # The failed-fetch alarm must not fire on a routine last page.
        assert "NOT evidence" not in joined

    @pytest.mark.parametrize("structured", [False, True])
    async def test_empty_fetch_at_offset_zero_still_warns_loudly(
        self,
        log_file: Path,
        component_core_client: HomeAssistantClient,
        structured: bool,
    ) -> None:
        """A running instance always logs something."""
        result = await error_log(
            log_file, component_core_client, EMPTY_LOG, structured=structured
        )
        joined = " ".join(result["warnings"])
        assert "NOT evidence" in joined
        assert "offset=" not in joined
