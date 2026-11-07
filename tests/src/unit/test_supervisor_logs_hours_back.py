"""``hours_back`` on ``ha_get_logs(source="supervisor"|"system_service")``.

The journal selects the window: the read sends gatewayd's own time range,
``Range: realtime=<since>::<skip>:<count>``, which Supervisor and Core's hassio
proxy pass through. ``FakeGatewayd`` answers ranges the way
``journal-gatewayd.c`` does: entries in journal order, whose timestamps need
not be in order; seek to the first entry stamped at or after ``since``; skip
and count by ENTRY; end at the first entry stamped after ``until`` when one is
given; ``entries=:-N:N`` for the newest N. So the tests check what the tool
asks for and what it does with the answer, not a copy of the selection.
"""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.journal_window import (
    EPOCH,
    newest_range,
    read_logs_window,
    read_window,
    window_range,
    window_start,
)
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.log_common import _collect_log_warnings
from ha_mcp.tools.tools_logs import LogTools

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
_REALTIME = re.compile(r"^realtime=(\d*):(\d*):(-?\d+):(\d+)$")
_ENTRIES = re.compile(r"^entries=:(-?\d+):(\d+)$")


def at(minutes_ago: int) -> int:
    return int((NOW - timedelta(minutes=minutes_ago)).timestamp())


class FakeGatewayd:
    """A journal of ``(epoch seconds, lines)`` entries, in journal order."""

    def __init__(self, entries: list[tuple[int, list[str]]]) -> None:
        self.entries = entries
        self.ranges: list[str] = []

    @classmethod
    def per_minute(cls, minutes: int) -> "FakeGatewayd":
        """One single-line entry a minute, the newest at ``NOW``."""
        return cls([(at(m), [f"m{m}"]) for m in range(minutes - 1, -1, -1)])

    def answer(self, range_header: str) -> str:
        self.ranges.append(range_header)
        if match := _ENTRIES.match(range_header):
            skip, count = int(match[1]), int(match[2])
            start = max(len(self.entries) - 1 + skip, 0)
            picked = self.entries[start : start + count]
        else:
            match = _REALTIME.match(range_header)
            assert match, f"unexpected Range {range_header!r}"
            since = int(match[1]) if match[1] else 0
            until = int(match[2]) if match[2] else None
            skip, count = int(match[3]), int(match[4])
            assert skip >= 0
            start = next(
                (i for i, e in enumerate(self.entries) if e[0] >= since),
                len(self.entries),
            )
            picked = []
            for stamp, lines in self.entries[start + skip :]:
                if until is not None and stamp > until:
                    break  # gatewayd ends here; it does not skip the entry
                picked.append((stamp, lines))
                if len(picked) == count:
                    break
        return "\n".join(line for _, lines in picked for line in lines)

    def client(self, timeout: float = 30) -> MagicMock:
        client = MagicMock(timeout=timeout)

        async def raw_request(
            method: str, path: str, headers: dict[str, str], params: dict[str, Any]
        ) -> MagicMock:
            return MagicMock(text=self.answer(headers["Range"]))

        client._raw_request = AsyncMock(side_effect=raw_request)
        return client


def shown(text: str) -> list[str]:
    return text.splitlines()


class TestRanges:
    def test_a_window_is_sent_as_gatewayd_seconds_with_no_end(self):
        since = datetime(2026, 11, 5, 11, 0, 30, 999000, tzinfo=UTC)
        assert window_range(since, 0, 50) == f"realtime={int(since.timestamp())}::0:50"

    def test_the_newest_read_always_names_its_entry_count(self):
        # systemd 256 rejected a Range without num_entries (systemd#37172).
        assert newest_range(10) == "entries=:-9:10"


class TestWindowStart:
    def test_a_window_starts_hours_back_from_the_read(self):
        assert window_start(NOW, 2) == NOW - timedelta(hours=2)

    @pytest.mark.parametrize("hours_back", [600_000, 17_800_000, 10**12, 10**30])
    def test_a_window_reaching_past_1970_is_the_whole_journal(self, hours_back: int):
        # 600,000 h would be a negative time, which gatewayd does not take;
        # the larger values overflow datetime.
        assert window_start(NOW, hours_back) == EPOCH


@pytest.mark.asyncio
class TestReadWindow:
    async def test_the_journal_returns_exactly_the_window(self):
        journal = FakeGatewayd.per_minute(600)
        read = await read_window(
            journal.client(), "supervisor", NOW - timedelta(hours=2)
        )
        assert read.complete
        assert read.lines == [f"m{m}" for m in range(120, -1, -1)]
        assert len(journal.ranges) == 1  # a short window needs no probe

    async def test_a_multi_line_entry_at_the_cap_is_not_mistaken_for_overflow(self):
        # 5 entries, 7 lines: the line count reaches the cap, the entries do not.
        journal = FakeGatewayd(
            [(at(m), [f"m{m}"]) for m in (50, 40, 30, 20)]
            + [(at(10), ["Traceback:", "  File x", "Error"])]
        )
        read = await read_window(
            journal.client(), "core", NOW - timedelta(hours=1), max_entries=5
        )
        assert read.complete
        assert read.lines[-3:] == ["Traceback:", "  File x", "Error"]
        assert len(journal.ranges) == 2  # the probe found nothing past the cap

    async def test_an_oversized_window_returns_its_newest_entries(self):
        journal = FakeGatewayd.per_minute(600)
        read = await read_window(
            journal.client(), "host", NOW - timedelta(hours=5), max_entries=100
        )
        assert not read.complete
        assert read.lines == [f"m{m}" for m in range(99, -1, -1)]
        assert journal.ranges[-1] == "entries=:-99:100"

    async def test_an_entry_stamped_in_the_future_does_not_end_the_read(self):
        # After a clock correction an entry can carry a later time than the ones
        # logged after it. Gatewayd ends a read with an end time at such an
        # entry, so the read sends none, and nothing after it is lost.
        journal = FakeGatewayd.per_minute(120)
        journal.entries.insert(60, (at(-60), ["stamped an hour ahead"]))
        read = await read_window(journal.client(), "host", NOW - timedelta(hours=1))
        assert read.complete
        assert "stamped an hour ahead" in read.lines
        assert read.lines[-1] == "m0"
        assert all(re.match(r"^realtime=\d+::", r) for r in journal.ranges)

    async def test_an_empty_window_is_complete_and_empty(self):
        read = await read_window(
            FakeGatewayd([]).client(), "dns", NOW - timedelta(hours=1)
        )
        assert read.complete and read.lines == []


@pytest.mark.asyncio
class TestRoutes:
    async def test_inside_the_app_the_range_goes_straight_to_supervisor(self):
        client = MagicMock(timeout=30)
        client._supervisor_logs_response = AsyncMock(return_value=MagicMock(text=""))
        with patch(
            "ha_mcp.client.journal_window.is_running_in_addon", return_value=True
        ):
            await read_window(client, "addons/core_ssh", NOW - timedelta(hours=1))
        path, headers, params = client._supervisor_logs_response.call_args.args
        assert path == "addons/core_ssh"
        assert headers["Range"].startswith("realtime=")
        assert params == {"no_colors": "true"}

    async def test_outside_the_app_the_range_goes_through_core(self):
        client = MagicMock(timeout=30)
        client._raw_request = AsyncMock(return_value=MagicMock(text=""))
        with patch(
            "ha_mcp.client.journal_window.is_running_in_addon", return_value=False
        ):
            await read_window(client, "supervisor", NOW - timedelta(hours=1))
        args, kwargs = client._raw_request.call_args
        assert args == ("GET", "/hassio/supervisor/logs")
        assert kwargs["headers"]["Range"].startswith("realtime=")

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
            await read_logs_window(client, "supervisor", NOW - timedelta(hours=1))

    @pytest.mark.parametrize("status", [400, 500])
    async def test_a_rejected_range_names_the_systemd_it_needs(self, status: int):
        client = MagicMock(timeout=30)
        client._raw_request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                f"API error: {status} - bad", status_code=status
            )
        )
        with (
            patch(
                "ha_mcp.client.journal_window.is_running_in_addon", return_value=False
            ),
            pytest.raises(HomeAssistantAPIError, match="systemd 255") as raised,
        ):
            await read_logs_window(client, "host", NOW - timedelta(hours=1))
        assert raised.value.status_code == status


def tools_on(journal: FakeGatewayd) -> LogTools:
    return LogTools(journal.client())


@pytest.fixture(autouse=True)
def _through_core() -> Any:
    with patch("ha_mcp.client.journal_window.is_running_in_addon", return_value=False):
        yield


@pytest.mark.asyncio
class TestToolLayer:
    async def test_system_service_logs_cover_the_requested_hours(self):
        out = await tools_on(FakeGatewayd.per_minute(3000))._get_system_service_log(
            service="supervisor", limit=500, hours_back=2, now=NOW, order="oldest"
        )
        assert shown(out["log"])[0] == "m120"
        assert out["total_lines"] == 121
        assert out["window_complete"] is True
        assert out["window_start"] == "2026-11-05T10:00:00+00:00"
        assert out["read_at"] == "2026-11-05T12:00:00+00:00"
        assert "window_end" not in out
        assert "warnings" not in out

    @pytest.mark.parametrize("hours_back", [600_000, 10**12])
    async def test_a_huge_hours_back_reads_the_whole_journal(self, hours_back: int):
        journal = FakeGatewayd.per_minute(300)
        out = await tools_on(journal)._get_system_service_log(
            service="host", limit=500, hours_back=hours_back, now=NOW
        )
        assert out["success"] is True
        assert out["window_start"] == "1970-01-01T00:00:00+00:00"
        assert out["total_lines"] == 300
        assert journal.ranges[0] == "realtime=0::0:10000"

    async def test_app_log_search_spans_the_whole_window(self):
        journal = FakeGatewayd.per_minute(600)
        journal.entries[-100] = (at(99), ["needle in the window"])
        journal.entries[-300] = (at(299), ["needle before the window"])
        out = await tools_on(journal)._get_supervisor_log(
            slug="core_ssh", search="needle", hours_back=2, now=NOW
        )
        assert out["log"] == "needle in the window"

    async def test_a_partial_window_is_not_reported_as_a_whole_one(self):
        with patch("ha_mcp.client.journal_window.MAX_ENTRIES", 100):
            out = await tools_on(FakeGatewayd.per_minute(3000))._get_system_service_log(
                service="host", hours_back=24, now=NOW
            )
        assert out["window_complete"] is False
        assert "more than 100 entries" in out["warnings"][0]
        assert shown(out["log"])[0] == "m0"  # the newest are the ones read

    async def test_without_hours_back_the_newest_lines_are_read_as_before(self):
        client = MagicMock()
        client._get_system_service_logs = AsyncMock(return_value="a\nb\nc")
        out = await LogTools(client)._get_system_service_log(
            service="supervisor", limit=2
        )
        assert out["log"] == "c\nb"
        assert "window_complete" not in out
        client._raw_request.assert_not_called()


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
