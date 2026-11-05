"""``hours_back`` on ``ha_get_logs(source="supervisor"|"system_service")``.

Supervisor serves these logs by entry count, so ``hours_back`` is honoured by
paging back from the newest entry. The journal is ``journald_fetch``, which
mirrors gatewayd's ``Range`` semantics, holding one verbose entry per minute.
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.journal_window import (
    entry_time,
    lines_since,
    read_logs_since,
    read_since,
    supervisor_fetch,
)
from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools.log_common import _collect_log_warnings
from ha_mcp.tools.tools_logs import LogTools

from ._error_log_test_helpers import journald_fetch

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def verbose(minutes_ago: int, message: str = "") -> str:
    stamp = (NOW - timedelta(minutes=minutes_ago)).isoformat(
        sep=" ", timespec="milliseconds"
    )
    return f"{stamp} homeassistant supervisor[42]: {message or f'm{minutes_ago}'}"


def journal(minutes: int) -> list[str]:
    """One entry a minute, oldest first, the newest at ``NOW``."""
    return [verbose(m) for m in range(minutes - 1, -1, -1)]


class TestEntryTime:
    def test_an_entry_in_another_timezone_is_placed_correctly(self):
        line = "2026-03-01 08:15:42.250-06:00 host supervisor[1]: x"
        assert entry_time(line) == datetime(2026, 3, 1, 14, 15, 42, 250000, tzinfo=UTC)

    def test_a_traceback_line_is_not_mistaken_for_an_entry(self):
        assert entry_time('  File "x.py", line 3, in y') is None


class TestLinesSince:
    def test_entries_before_the_window_are_left_out(self):
        lines = [verbose(m) for m in (90, 61, 60, 30, 0)]
        kept = lines_since(lines, NOW - timedelta(minutes=60))
        assert all(entry_time(line) >= NOW - timedelta(minutes=60) for line in kept)
        assert len(kept) == 3

    def test_a_traceback_stays_with_its_entry(self):
        lines = [verbose(90, "old"), "  old trace", verbose(10, "new"), "  new trace"]
        kept = lines_since(lines, NOW - timedelta(minutes=60))
        assert kept == [verbose(10, "new"), "  new trace"]

    def test_a_trace_cut_from_an_earlier_block_is_not_shown_alone(self):
        lines = ["  tail of an entry from an earlier block", verbose(5)]
        assert lines_since(lines, NOW - timedelta(hours=1)) == [verbose(5)]


@pytest.mark.asyncio
class TestReadSince:
    async def test_a_window_longer_than_one_block_is_read_whole(self):
        read = await read_since(
            journald_fetch(journal(3000)), NOW - timedelta(minutes=250), page_lines=100
        )
        assert read.reached_cutoff
        assert read.oldest <= NOW - timedelta(minutes=250)
        assert read.lines == journal(len(read.lines))  # contiguous: no gap, no repeat

    async def test_a_short_journal_ends_the_read_without_repeating_entries(self):
        read = await read_since(
            journald_fetch(journal(250)), NOW - timedelta(hours=24), page_lines=100
        )
        assert read.journal_start
        assert read.lines == journal(250)

    async def test_a_window_past_the_page_limit_is_reported_incomplete(self):
        read = await read_since(
            journald_fetch(journal(3000)),
            NOW - timedelta(hours=24),
            page_lines=100,
            max_pages=4,
        )
        assert not read.reached_cutoff and not read.journal_start
        assert read.oldest > NOW - timedelta(hours=24)

    async def test_an_empty_journal_returns_nothing(self):
        read = await read_since(journald_fetch([]), NOW - timedelta(hours=1))
        assert read.lines == [] and read.oldest is None


@pytest.mark.asyncio
class TestSupervisorFetch:
    async def test_inside_the_app_the_read_asks_supervisor_for_timestamps(self):
        client = MagicMock()
        client._supervisor_logs_response = AsyncMock(
            return_value=MagicMock(text="", headers={})
        )
        with patch(
            "ha_mcp.client.journal_window.is_running_in_addon", return_value=True
        ):
            await supervisor_fetch(client, "addons/core_ssh")("entries=:-9:10")
        path, headers, params = client._supervisor_logs_response.call_args.args
        assert path == "addons/core_ssh"
        assert headers["Range"] == "entries=:-9:10"
        assert "verbose" in params

    async def test_outside_the_app_the_read_goes_through_core(self):
        client = MagicMock()
        client._raw_request = AsyncMock(return_value=MagicMock(text="", headers={}))
        with patch(
            "ha_mcp.client.journal_window.is_running_in_addon", return_value=False
        ):
            await supervisor_fetch(client, "supervisor")("entries=:-9:10")
        args, kwargs = client._raw_request.call_args
        assert args == ("GET", "/hassio/supervisor/logs")
        assert kwargs["headers"]["Range"] == "entries=:-9:10"
        assert "verbose" in kwargs["params"]

    async def test_a_stalled_supervisor_cannot_hang_the_read(self):
        client = MagicMock(timeout=0.01)

        async def stall(*_args: Any, **_kwargs: Any) -> None:
            await asyncio.sleep(1)

        client._raw_request = stall
        with (
            patch(
                "ha_mcp.client.journal_window.is_running_in_addon", return_value=False
            ),
            pytest.raises(HomeAssistantConnectionError, match="Timeout reading"),
        ):
            await read_logs_since(client, "supervisor", NOW)


@pytest.fixture
def journal_of() -> Iterator[Any]:
    """Serve the tool layer's Supervisor reads from a list of entries."""

    def _serve(entries: list[str]) -> LogTools:
        patcher.start().return_value = journald_fetch(entries)
        return LogTools(MagicMock(timeout=30))

    patcher = patch("ha_mcp.client.journal_window.supervisor_fetch")
    yield _serve
    patcher.stop()


@pytest.mark.asyncio
class TestToolLayer:
    async def test_system_service_logs_cover_the_requested_hours(self, journal_of):
        tools = journal_of(journal(3000))
        out = await tools._get_system_service_log(
            service="supervisor", limit=500, hours_back=2, now=NOW
        )
        stamps = [entry_time(line) for line in out["log"].splitlines()]
        assert min(stamps) >= NOW - timedelta(hours=2)
        assert min(stamps) < NOW - timedelta(hours=2) + timedelta(minutes=1)
        assert out["window_complete"] is True

    async def test_app_log_search_spans_the_whole_window(self, journal_of):
        entries = journal(600)
        entries[-100] = verbose(99, "needle in the window")
        entries[-300] = verbose(299, "needle before the window")
        tools = journal_of(entries)
        out = await tools._get_supervisor_log(
            slug="core_ssh", search="needle", hours_back=2, now=NOW
        )
        assert "needle in the window" in out["log"]
        assert "before the window" not in out["log"]

    async def test_an_unreached_cutoff_is_not_reported_as_a_quiet_window(self):
        incomplete = await read_since(
            journald_fetch(journal(3000)),
            NOW - timedelta(hours=100),
            page_lines=100,
            max_pages=2,
        )
        tools = LogTools(MagicMock())
        with patch(
            "ha_mcp.tools.log_sources_supervisor.read_logs_since",
            AsyncMock(return_value=incomplete),
        ):
            out = await tools._get_system_service_log(
                service="host", hours_back=100, now=NOW
            )
        assert out["window_complete"] is False
        assert "100-hour cutoff" in out["warnings"][0]

    async def test_a_journal_shorter_than_the_window_says_where_it_begins(
        self, journal_of
    ):
        tools = journal_of(journal(50))
        out = await tools._get_system_service_log(service="dns", hours_back=24, now=NOW)
        assert out["window_complete"] is True
        assert "journal begins at" in out["warnings"][0]

    async def test_without_hours_back_the_newest_lines_are_read_as_before(self):
        client = MagicMock()
        client._get_system_service_logs = AsyncMock(return_value="a\nb\nc")
        out = await LogTools(client)._get_system_service_log(
            service="supervisor", limit=2
        )
        assert out["log"] == "c\nb"
        assert "window_complete" not in out


class TestIgnoredParameterWarning:
    def test_hours_back_on_a_source_that_ignores_it_is_flagged(self):
        warnings = _collect_log_warnings(
            "error_log", None, None, None, None, None, None, "newest", 0, hours_back=3
        )
        assert any("'hours_back' only applies" in w for w in warnings)

    @pytest.mark.parametrize("source", ["logbook", "supervisor", "system_service"])
    def test_hours_back_where_it_applies_is_not_flagged(self, source: str):
        warnings = _collect_log_warnings(
            source, None, None, None, None, None, None, "newest", 0, hours_back=3
        )
        assert not any("hours_back" in w for w in warnings)
