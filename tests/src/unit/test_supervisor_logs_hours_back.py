"""``hours_back`` on ``ha_get_logs(source="supervisor"|"system_service")``.

The journal selects the window: the read sends gatewayd's own time range,
``Range: realtime=<since>:<until>:<skip>:<count>``, which Supervisor and Core's
hassio proxy pass through. ``FakeGatewayd`` answers those ranges the way
``journal-gatewayd.c`` does (seek, skip and count by ENTRY, stop after
``until``), so the tests check what the tool asks for and what it does with
the answer, not a copy of the selection.
"""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.journal_window import (
    newest_range,
    read_logs_window,
    read_window,
    window_range,
)
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.log_common import _collect_log_warnings
from ha_mcp.tools.tools_logs import LogTools

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
_RANGE = re.compile(r"^realtime=(\d*):(\d+):(-?\d+):(\d+)$")


def at(minutes_ago: int) -> int:
    return int((NOW - timedelta(minutes=minutes_ago)).timestamp())


class FakeGatewayd:
    """A journal of ``(epoch seconds, lines)`` entries, oldest first."""

    def __init__(self, entries: list[tuple[int, list[str]]]) -> None:
        self.entries = entries
        self.ranges: list[str] = []

    @classmethod
    def per_minute(cls, minutes: int) -> "FakeGatewayd":
        """One single-line entry a minute, the newest at ``NOW``."""
        return cls([(at(m), [f"m{m}"]) for m in range(minutes - 1, -1, -1)])

    def answer(self, range_header: str) -> str:
        self.ranges.append(range_header)
        match = _RANGE.match(range_header)
        assert match, f"unexpected Range {range_header!r}"
        since, until, skip, count = (
            int(match[1]) if match[1] else None,
            int(match[2]),
            int(match[3]),
            int(match[4]),
        )
        in_range = [e for e in self.entries if e[0] <= until]
        if since is not None:
            assert skip >= 0
            picked = [e for e in in_range if e[0] >= since][skip : skip + count]
        else:
            assert skip == -(count - 1)
            picked = in_range[-count:]
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
    def test_a_window_is_sent_as_gatewayd_seconds(self):
        since = datetime(2026, 11, 5, 11, 0, 30, 999000, tzinfo=UTC)
        assert window_range(since, NOW, 0, 50) == (
            f"realtime={int(since.timestamp())}:{int(NOW.timestamp())}:0:50"
        )

    def test_the_newest_read_always_names_its_entry_count(self):
        # systemd 256 rejected a Range without num_entries (systemd#37172).
        assert newest_range(NOW, 10).endswith(":-9:10")


@pytest.mark.asyncio
class TestReadWindow:
    async def test_the_journal_returns_exactly_the_window(self):
        journal = FakeGatewayd.per_minute(600)
        read = await read_window(
            journal.client(), "supervisor", NOW - timedelta(hours=2), NOW
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
            journal.client(), "core", NOW - timedelta(hours=1), NOW, max_entries=5
        )
        assert read.complete
        assert read.lines[-3:] == ["Traceback:", "  File x", "Error"]
        assert len(journal.ranges) == 2  # the probe found nothing past the cap

    async def test_an_oversized_window_returns_its_newest_entries(self):
        journal = FakeGatewayd.per_minute(600)
        read = await read_window(
            journal.client(),
            "host",
            NOW - timedelta(hours=5),
            NOW,
            max_entries=100,
        )
        assert not read.complete
        assert read.lines == [f"m{m}" for m in range(99, -1, -1)]

    async def test_entries_logged_after_until_are_not_read(self):
        journal = FakeGatewayd.per_minute(600)
        journal.entries.append((at(-5), ["after the window"]))
        read = await read_window(
            journal.client(), "host", NOW - timedelta(hours=5), NOW, max_entries=100
        )
        assert "after the window" not in read.lines

    async def test_an_empty_window_is_complete_and_empty(self):
        read = await read_window(
            FakeGatewayd([]).client(), "dns", NOW - timedelta(hours=1), NOW
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
            await read_window(client, "addons/core_ssh", NOW - timedelta(hours=1), NOW)
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
            await read_window(client, "supervisor", NOW - timedelta(hours=1), NOW)
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
            await read_logs_window(client, "supervisor", NOW - timedelta(hours=1), NOW)

    async def test_a_rejected_range_names_the_systemd_it_needs(self):
        client = MagicMock(timeout=30)
        client._raw_request = AsyncMock(
            side_effect=HomeAssistantAPIError("API error: 400 - bad", status_code=400)
        )
        with (
            patch(
                "ha_mcp.client.journal_window.is_running_in_addon", return_value=False
            ),
            pytest.raises(HomeAssistantAPIError, match="systemd 255") as raised,
        ):
            await read_logs_window(client, "host", NOW - timedelta(hours=1), NOW)
        assert raised.value.status_code == 400


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
        assert out["window_end"] == "2026-11-05T12:00:00+00:00"
        assert "warnings" not in out

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
