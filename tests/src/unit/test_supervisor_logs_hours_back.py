"""``hours_back`` on ``ha_get_logs(source="supervisor"|"system_service")``.

The journal selects the window: the first oldest-first page sends gatewayd's
own time range, ``Range: realtime=<since>::<skip>:<count>``, and every later
page continues from Supervisor's ``X-First-Cursor`` (``entries=<cursor>:…``),
which Supervisor and Core's hassio proxy pass through. ``FakeGatewayd``
answers ranges the way ``journal-gatewayd.c`` does: entries in journal order,
whose timestamps need not be in order; seek to a cursor, to the first entry
stamped at or after ``since``, to the tail (a negative skip) or to the head (a
skip of 0, ``-0`` included); a negative skip past the head clamps to the head,
a positive one past the end returns nothing; count by ENTRY; end at the first
entry stamped after ``until`` when one is given. So the tests check what the
tool asks for and what it does with the answer, not a copy of the selection.

A positive skip past the journal's end is answered two ways. systemd 258, and
Home Assistant OS 16 and later through its patch of gatewayd, return nothing.
Earlier versions (Supervised installs on Debian, HAOS 15 and before) return
the last entry: ``sd_journal_next_skip`` reports how far it got and gatewayd
prints where it stopped. Every test runs against both (``FakeGatewayd.clamps``).
"""

import ast
import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.journal_window import (
    EPOCH,
    WindowToken,
    cursor_time,
    parse_window_token,
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

from ._logbook_fallback import call_kwargs

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
_REALTIME = re.compile(r"^realtime=(\d*):(\d*):(-?\d+):(\d+)$")
_ENTRIES = re.compile(r"^entries=([^:]*):(-?\d+):(\d+)$")


def at(minutes_ago: int) -> int:
    return int((NOW - timedelta(minutes=minutes_ago)).timestamp())


class FakeGatewayd:
    """A journal of ``(epoch seconds, lines)`` entries, in journal order.

    ``clamps`` selects the pre-258 answer to a skip past the end: the last
    entry instead of nothing. The autouse ``_gatewayd_version`` fixture sets it.
    """

    clamps = False

    def __init__(self, entries: list[tuple[int, list[str]]]) -> None:
        self.entries = entries
        self.ranges: list[str] = []
        # Runs after each answered request (given its Range), to log entries
        # between requests.
        self.after_each: Callable[[str], None] | None = None

    @classmethod
    def per_minute(cls, minutes: int) -> "FakeGatewayd":
        """One single-line entry a minute, the newest at ``NOW``."""
        return cls([(at(m), [f"m{m}"]) for m in range(minutes - 1, -1, -1)])

    def log(self, minutes_ago: int, line: str) -> None:
        """Append an entry, as the journal does while pages are read."""
        self.entries.append((at(minutes_ago), [line]))

    def cursor(self, index: int) -> str:
        """A systemd-shaped cursor; ``t`` is the entry's realtime in hex µs."""
        stamp = self.entries[index][0] * 1_000_000
        return f"s=fake;i={index:x};b=boot;m={index:x};t={stamp:x};x=0"

    def _index(self, cursor: str) -> int:
        fields = dict(f.split("=", 1) for f in cursor.split(";"))
        return int(fields["i"], 16)

    def _start(self, range_header: str) -> tuple[int, int]:
        """Where gatewayd starts reading for ``range_header``, and how many."""
        if match := _ENTRIES.match(range_header):
            cursor, skip, count = match[1], int(match[2]), int(match[3])
            if cursor:
                position = self._index(cursor)
            elif skip < 0:
                position = len(self.entries) - 1  # the tail
            else:
                position = 0  # a skip of 0, "-0" included, seeks the head
            if skip < 0:
                return max(position + skip, 0), count  # clamped to the head
            return self._past_end(position + skip), count
        match = _REALTIME.match(range_header)
        assert match, f"unexpected Range {range_header!r}"
        since = int(match[1]) if match[1] else 0
        skip, count = int(match[3]), int(match[4])
        assert skip >= 0
        first = next(
            (i for i, e in enumerate(self.entries) if e[0] >= since),
            len(self.entries),
        )
        if first == len(self.entries):
            return first, count  # nothing at or after since, in every version
        return self._past_end(first + skip), count

    def _past_end(self, start: int) -> int:
        """Before systemd 258 a forward skip past the end stops on the last entry."""
        if self.clamps and self.entries and start >= len(self.entries):
            return len(self.entries) - 1
        return start

    def answer(self, range_header: str) -> str:
        return self.reply(range_header)[0]

    def reply(self, range_header: str) -> tuple[str, str | None]:
        """The body for ``range_header`` and its first entry's cursor."""
        self.ranges.append(range_header)
        start, count = self._start(range_header)
        # request_parse_range_skip_and_n_entries: num_entries <= 0 is EINVAL,
        # which gatewayd answers with 400.
        assert count > 0, f"gatewayd rejects a count of 0: {range_header!r}"
        match = _REALTIME.match(range_header)
        until = int(match[2]) if match and match[2] else None
        picked: list[int] = []
        for index in range(start, len(self.entries)):
            if until is not None and self.entries[index][0] > until:
                break  # gatewayd ends here; it does not skip the entry
            picked.append(index)
            if len(picked) == count:
                break
        text = "\n".join(line for i in picked for line in self.entries[i][1])
        cursor = self.cursor(picked[0]) if picked else None
        if self.after_each:
            self.after_each(range_header)
        return text, cursor

    def client(self, timeout: float = 30) -> MagicMock:
        client = MagicMock(timeout=timeout)

        async def raw_request(
            method: str, path: str, headers: dict[str, str], params: dict[str, Any]
        ) -> MagicMock:
            text, cursor = self.reply(headers["Range"])
            # Supervisor sends the first entry's cursor whenever it returns one.
            return MagicMock(
                text=text, headers={"X-First-Cursor": cursor} if cursor else {}
            )

        client._raw_request = AsyncMock(side_effect=raw_request)
        return client


def shown(text: str) -> list[str]:
    return text.splitlines()


class TestRanges:
    def test_a_window_is_sent_as_gatewayd_seconds_with_no_end(self):
        since = datetime(2026, 11, 5, 11, 0, 30, 999000, tzinfo=UTC)
        assert window_range(since, 0, 50) == f"realtime={int(since.timestamp())}::0:50"


class TestWindowStart:
    def test_a_window_starts_hours_back_from_the_read(self):
        assert window_start(NOW, 2) == NOW - timedelta(hours=2)

    def test_a_window_starts_on_the_whole_second_gatewayd_seeks_to(self):
        read_at = NOW + timedelta(microseconds=999_000)
        assert window_start(read_at, 1) == NOW - timedelta(hours=1)

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
        assert read.lines[0] == "m50"  # nothing from before the window

    async def test_an_oversized_window_returns_its_newest_entries(self):
        journal = FakeGatewayd.per_minute(600)
        read = await read_window(
            journal.client(), "host", NOW - timedelta(hours=5), max_entries=100
        )
        assert not read.complete
        assert read.lines == [f"m{m}" for m in range(99, -1, -1)]
        assert journal.ranges[-1] == "entries=:-99:100"

    @pytest.mark.parametrize("blank", ["   ", ""])
    async def test_a_blank_entry_past_the_cap_still_counts_as_more(self, blank: str):
        # The probe asks whether an entry exists past the cap. A blank or empty
        # message there is still an entry; judging by its text would call the
        # window whole and drop the newer entries after it.
        journal = FakeGatewayd.per_minute(150)
        journal.entries[100] = (journal.entries[100][0], [blank])
        journal.entries[-1] = (journal.entries[-1][0], ["newest needle"])
        read = await read_window(
            journal.client(), "core", NOW - timedelta(hours=5), max_entries=100
        )
        assert not read.complete
        assert read.lines[-1] == "newest needle"

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


def tools_on(journal: FakeGatewayd) -> LogTools:
    return LogTools(journal.client())


@pytest.fixture(autouse=True, params=[False, True], ids=["empty-past-end", "clamps"])
def _gatewayd_version(request: pytest.FixtureRequest) -> Any:
    with patch.object(FakeGatewayd, "clamps", request.param):
        yield request.param


@pytest.fixture(autouse=True)
def _through_core() -> Any:
    with patch("ha_mcp.client.journal_window.is_running_in_addon", return_value=False):
        yield


class TestTokens:
    def test_a_token_round_trips(self):
        token = WindowToken("newest", NOW, 2, "s=a;i=1;t=5", 0)
        assert parse_window_token(str(token)) == token

    @pytest.mark.parametrize(
        "token",
        [
            "window:",
            "window:sideways:1:c:0",
            "window:oldest:x:c:0",
            "window:oldest:1:c",
        ],
    )
    def test_a_malformed_token_is_refused(self, token: str):
        with pytest.raises(ValueError):
            parse_window_token(token)

    def test_a_cursor_carries_its_entry_time(self):
        stamp = NOW + timedelta(microseconds=250)
        micros = (stamp - EPOCH) // timedelta(microseconds=1)
        assert cursor_time(f"s=a;i=1;b=b;m=1;t={micros:x};x=2") == stamp
        assert cursor_time("s=a;i=1") is None


async def page_through(
    journal: FakeGatewayd,
    order: str,
    limit: int,
    hours_back: int,
    between: Any = None,
) -> list[dict[str, Any]]:
    """Every page of a window, each read a minute after the one before.

    ``between(n)`` runs before page ``n`` (from 1), to log entries meanwhile.
    """
    tools = tools_on(journal)
    pages = [
        await tools._get_system_service_log(
            service="host", limit=limit, hours_back=hours_back, now=NOW, order=order
        )
    ]
    while pages[-1]["has_more"]:
        assert len(pages) < 100, "paging does not end"
        if between:
            between(len(pages))
        pages.append(
            await tools._get_system_service_log(
                service="host",
                limit=limit,
                hours_back=hours_back,
                now=NOW + timedelta(minutes=len(pages)),
                order=order,
                offset=pages[-1]["next_offset"],
            )
        )
    return pages


def joined(pages: list[dict[str, Any]]) -> list[str]:
    return [line for page in pages for line in shown(page["log"])]


@pytest.mark.asyncio
class TestOldestFirstPages:
    async def test_the_first_page_starts_at_the_window_start(self):
        journal = FakeGatewayd.per_minute(3000)
        out = await tools_on(journal)._get_system_service_log(
            service="supervisor", limit=500, hours_back=2, now=NOW, order="oldest"
        )
        assert journal.ranges[0] == f"realtime={at(120)}::0:500"
        assert shown(out["log"]) == [f"m{m}" for m in range(120, -1, -1)]
        assert out["has_more"] is False
        assert out["window_start"] == "2026-11-05T10:00:00+00:00"
        assert out["read_at"] == "2026-11-05T12:00:00+00:00"
        assert "window_complete" not in out
        assert "warnings" not in out

    async def test_pages_continue_from_an_entry_while_the_clock_moves_on(self):
        # Each page is read a minute later, and the journal grows meanwhile.
        # Recomputing the window's start would move it forward a minute per
        # page and skip entries; the token keeps the first page's start and
        # continues from the exact entry the page before ended on.
        journal = FakeGatewayd.per_minute(3000)
        pages = await page_through(
            journal,
            "oldest",
            limit=50,
            hours_back=2,
            between=lambda n: journal.log(-n, f"new{n}"),
        )
        lines = joined(pages)
        assert lines[:121] == [f"m{m}" for m in range(120, -1, -1)]
        assert lines[121:] == [f"new{n}" for n in range(1, len(pages))]
        assert all(p["window_start"] == "2026-11-05T10:00:00+00:00" for p in pages)
        assert journal.ranges[0] == f"realtime={at(120)}::0:50"
        token = parse_window_token(pages[0]["next_offset"])
        assert (token.order, token.skip) == ("oldest", 50)
        assert f"entries={token.cursor}:50:50" in journal.ranges
        assert "offset='window:oldest:" in pages[0]["pagination_hint"]

    async def test_an_integer_offset_skips_into_the_window(self):
        journal = FakeGatewayd.per_minute(3000)
        out = await tools_on(journal)._get_system_service_log(
            service="host", limit=50, offset=100, hours_back=2, now=NOW, order="oldest"
        )
        # The skip is taken from the window's first entry, so one past the
        # window's end finds nothing on either gatewayd (see the next test).
        first = journal.cursor(len(journal.entries) - 121)
        assert journal.ranges[0] == f"realtime={at(120)}::0:1"
        assert f"entries={first}:100:1" in journal.ranges
        assert shown(out["log"]) == [f"m{m}" for m in range(20, -1, -1)]
        assert out["has_more"] is False
        assert "next_offset" not in out
        assert "pagination_hint" not in out

    async def test_a_full_last_page_asks_the_journal_whether_more_follows(self):
        journal = FakeGatewayd.per_minute(100)
        out = await tools_on(journal)._get_supervisor_log(
            slug="core_ssh", limit=50, offset=50, hours_back=24, now=NOW, order="oldest"
        )
        assert f"entries={journal.cursor(50)}:50:1" in journal.ranges
        assert out["has_more"] is False
        assert out["returned_lines"] == 50

    @pytest.mark.parametrize("offset", [120, 121, 500])
    async def test_an_offset_at_or_past_the_windows_end_is_an_empty_page(
        self, offset: int
    ):
        journal = FakeGatewayd.per_minute(3000)
        out = await tools_on(journal)._get_system_service_log(
            service="host",
            limit=50,
            offset=offset,
            hours_back=2,
            now=NOW,
            order="oldest",
        )
        expected = ["m0"] if offset == 120 else []
        assert shown(out["log"]) == expected
        assert out["has_more"] is False

    async def test_a_page_counts_entries_and_returns_each_one_whole(self):
        journal = FakeGatewayd(
            [
                (at(30), ["Traceback:", "  File x", "Error"]),
                (at(20), ["m20"]),
                (at(10), ["m10"]),
            ]
        )
        out = await tools_on(journal)._get_system_service_log(
            service="core", limit=2, hours_back=1, now=NOW, order="oldest"
        )
        assert shown(out["log"]) == ["Traceback:", "  File x", "Error", "m20"]
        assert parse_window_token(out["next_offset"]).skip == 2


@pytest.mark.asyncio
class TestNewestFirstPages:
    async def test_pages_run_back_to_the_window_start_and_stop_there(self):
        journal = FakeGatewayd.per_minute(3000)
        pages = await page_through(journal, "newest", limit=50, hours_back=2)
        assert joined(pages) == [f"m{m}" for m in range(121)]
        assert [p["returned_lines"] for p in pages] == [50, 50, 21]
        assert pages[-1]["has_more"] is False
        assert "next_offset" not in pages[-1]
        assert "window_complete" not in pages[0]

    async def test_new_entries_do_not_shift_a_page(self):
        journal = FakeGatewayd.per_minute(3000)
        pages = await page_through(
            journal,
            "newest",
            limit=50,
            hours_back=2,
            between=lambda n: journal.log(-n, f"new{n}"),
        )
        assert joined(pages) == [f"m{m}" for m in range(121)]

    async def test_a_page_that_reaches_the_window_start_exactly_has_no_more(self):
        journal = FakeGatewayd.per_minute(3000)
        pages = await page_through(journal, "newest", limit=121, hours_back=2)
        assert len(pages) == 1
        assert joined(pages) == [f"m{m}" for m in range(121)]

    async def test_a_whole_journal_window_stops_at_the_head(self):
        journal = FakeGatewayd.per_minute(120)
        pages = await page_through(journal, "newest", limit=50, hours_back=24)
        assert joined(pages) == [f"m{m}" for m in range(120)]

    async def test_a_one_entry_page_is_the_newest_entry(self):
        # gatewayd reads entries=:-0:1 from the head, so the newest entry has
        # to be reached from a cursor.
        journal = FakeGatewayd.per_minute(10)
        pages = await page_through(journal, "newest", limit=1, hours_back=1)
        assert joined(pages) == [f"m{m}" for m in range(10)]
        assert not any(r.startswith("entries=:-0:") for r in journal.ranges)

    async def test_a_one_entry_journal(self):
        journal = FakeGatewayd([(at(5), ["only"])])
        pages = await page_through(journal, "newest", limit=50, hours_back=1)
        assert joined(pages) == ["only"]

    async def test_an_empty_journal(self):
        pages = await page_through(FakeGatewayd([]), "newest", limit=50, hours_back=1)
        assert pages[0]["log"] == "" and pages[0]["has_more"] is False

    async def test_a_window_older_than_the_journal_is_empty(self):
        journal = FakeGatewayd([(at(300), ["old"])])
        pages = await page_through(journal, "newest", limit=50, hours_back=1)
        assert joined(pages) == []

    async def test_lines_are_shown_newest_first(self):
        journal = FakeGatewayd(
            [(at(30), ["Traceback:", "  File x", "Error"]), (at(20), ["m20"])]
        )
        out = await tools_on(journal)._get_system_service_log(
            service="core", limit=5, hours_back=1, now=NOW
        )
        assert shown(out["log"]) == ["m20", "Error", "  File x", "Traceback:"]

    async def test_a_token_from_the_other_order_is_refused(self):
        journal = FakeGatewayd.per_minute(300)
        first = await tools_on(journal)._get_system_service_log(
            service="host", limit=50, hours_back=2, now=NOW, order="oldest"
        )
        with pytest.raises(ToolError, match="order='oldest'"):
            await tools_on(journal)._get_system_service_log(
                service="host",
                limit=50,
                hours_back=2,
                now=NOW,
                order="newest",
                offset=first["next_offset"],
            )


def window_oracle(
    journal: FakeGatewayd, since: datetime, order: str = "oldest"
) -> list[str]:
    """The window straight from the fake's entries, without any Range.

    Every entry from the first one stamped at or after ``since`` (where
    gatewayd's seek lands on a journal whose stamps rise) to the end.
    """
    start = next(
        (i for i, e in enumerate(journal.entries) if e[0] >= since.timestamp()),
        len(journal.entries),
    )
    lines = [line for _, entry in journal.entries[start:] for line in entry]
    return lines if order == "oldest" else list(reversed(lines))


def hinted_call(hint: str) -> dict[str, Any]:
    """The keyword arguments of the ``ha_get_logs(...)`` call a hint prints."""
    call = ast.parse(hint[hint.index("ha_get_logs(") :].strip(), mode="eval").body
    assert isinstance(call, ast.Call)
    return {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords if kw.arg}


@pytest.mark.asyncio
class TestTheWindowEnds:
    """Paging stops at the window's end on both gatewayd versions."""

    @pytest.mark.parametrize("order", ["oldest", "newest"])
    async def test_one_entry_pages_end(self, order: str):
        journal = FakeGatewayd.per_minute(5)
        pages = await page_through(journal, order, limit=1, hours_back=1)
        assert joined(pages) == window_oracle(journal, NOW - timedelta(hours=1), order)
        assert len(pages) == 5

    async def test_a_traceback_as_the_windows_last_entry_ends_paging(self):
        journal = FakeGatewayd(
            [
                (at(30), ["a"]),
                (at(20), ["b"]),
                (at(10), ["Traceback", " File", "Error"]),
            ]
        )
        pages = await page_through(journal, "oldest", limit=3, hours_back=1)
        assert joined(pages) == ["a", "b", "Traceback", " File", "Error"]
        assert pages[-1]["has_more"] is False

    async def test_a_window_of_exactly_the_cap_is_complete(self):
        journal = FakeGatewayd.per_minute(300)
        read = await read_window(
            journal.client(), "core", NOW - timedelta(minutes=99), max_entries=100
        )
        assert read.complete
        assert read.lines == [f"m{m}" for m in range(99, -1, -1)]


@pytest.mark.asyncio
class TestFollowingTheHint:
    """An agent copies the printed call; the pages must equal the window.

    The oracle reads the fake's entries directly, so it shares no Range logic
    with the code under test. Entries are logged between calls, and the calls
    run on the real clock, so the window start would drift if it were
    recomputed: hours_back reaches back to the epoch only for the first call's
    seek; continuation must come from the token alone.
    """

    @pytest.mark.parametrize("order", ["oldest", "newest"])
    @pytest.mark.parametrize("limit", [1, 7, 50])
    async def test_the_hinted_calls_read_the_window_exactly(
        self, order: str, limit: int
    ):
        journal = FakeGatewayd.per_minute(180)
        journal.entries[60] = (journal.entries[60][0], ["Traceback", " File", "Err"])
        tools = tools_on(journal)
        out = await tools.get_logs(
            **call_kwargs(
                source="system_service",
                slug="host",
                hours_back=10**12,
                limit=limit,
                order=order,
            )
        )
        expected = window_oracle(journal, EPOCH, order)
        pages = [out]
        while out["has_more"]:
            assert len(pages) < 400, "paging does not end"
            if len(pages) <= 3:
                # A few, not one per page: oldest-first follows the journal's
                # end, so logging as fast as it reads would never let it end.
                journal.log(-len(pages), f"logged meanwhile {len(pages)}")
            out = await tools.get_logs(
                **call_kwargs(**hinted_call(out["pagination_hint"]))
            )
            pages.append(out)
        lines = joined(pages)
        if order == "oldest":
            # Oldest-first reaches the journal's end, so it also reads what
            # was logged meanwhile, in order, after the window as it stood.
            assert lines[: len(expected)] == expected
            assert all(line.startswith("logged") for line in lines[len(expected) :])
        else:
            assert lines == expected


@pytest.mark.asyncio
class TestOutOfOrderStamps:
    """After a clock step, an entry can be stamped before entries logged earlier."""

    @pytest.mark.parametrize("back", [50, 51, 25])
    async def test_one_early_stamp_does_not_end_a_newest_first_read(self, back: int):
        # ``back`` 50 puts the early entry at a page's oldest position, 51 just
        # before it, 25 inside a page.
        journal = FakeGatewayd.per_minute(120)
        stamp, lines = journal.entries[-back]
        journal.entries[-back] = (stamp - 3 * 3600, lines)
        newest = await page_through(journal, "newest", limit=50, hours_back=2)
        oldest = await page_through(journal, "oldest", limit=50, hours_back=2)
        assert sorted(joined(newest)) == sorted(joined(oldest))
        assert len(joined(newest)) == 120

    @pytest.mark.parametrize("back", [6, 30, 59])
    async def test_one_early_stamp_inside_the_last_page_does_not_cut_it(
        self, back: int
    ):
        journal = FakeGatewayd.per_minute(120)
        stamp, lines = journal.entries[-back]
        journal.entries[-back] = (stamp - 3 * 3600, lines)
        newest = await page_through(journal, "newest", limit=50, hours_back=1)
        oldest = await page_through(journal, "oldest", limit=50, hours_back=1)
        assert sorted(joined(newest)) == sorted(joined(oldest))
        assert len(joined(newest)) == 61

    async def test_two_early_stamps_in_a_row_end_it(self):
        journal = FakeGatewayd.per_minute(120)
        for back in (50, 51):
            stamp, lines = journal.entries[-back]
            journal.entries[-back] = (stamp - 3 * 3600, lines)
        newest = await page_through(journal, "newest", limit=50, hours_back=2)
        assert joined(newest) == [f"m{m}" for m in range(49)]


@pytest.mark.asyncio
class TestEntriesLoggedDuringARead:
    """The journal grows between any two requests, not only between pages."""

    @pytest.mark.parametrize("order", ["oldest", "newest"])
    @pytest.mark.parametrize("limit", [1, 3, 10])
    async def test_no_entry_is_skipped_or_repeated(self, order: str, limit: int):
        # Multi-line entries let a page reach ``limit`` lines with fewer
        # entries, so its end probe runs past the journal's end, where an
        # entry logged between two probes once made a clamping gatewayd's
        # answers disagree and skipped what followed.
        journal = FakeGatewayd(
            [
                (at(m), ["tb", " at", "err"] if m % 4 == 0 else [f"e{m}"])
                for m in range(30, 0, -1)
            ]
        )
        logged = iter(range(40))

        def log_one(_range: str) -> None:
            if (n := next(logged, None)) is not None:
                lines = [f"mid{n}", f"mid{n}+", f"mid{n}++"] if n % 2 else [f"mid{n}"]
                journal.entries.append((at(0), lines))

        journal.after_each = log_one
        pages = await page_through(journal, order, limit=limit, hours_back=1)
        journal.after_each = None
        lines = joined(pages)
        if order == "oldest":
            # No gap and no repeat; only what was logged after the last page
            # was read can be missing from its end.
            expected = window_oracle(journal, NOW - timedelta(hours=1))
            assert lines == expected[: len(lines)]
            assert all(line.startswith("mid") for line in expected[len(lines) :])
            assert "e1" in lines
        else:
            # Newest-first starts at the journal's end as the first request
            # found it; what was logged after that is not part of the read.
            expected = window_oracle(journal, NOW - timedelta(hours=1), "newest")
            assert lines == expected[len(expected) - len(lines) :]
            assert lines[-1] == "e30"

    async def test_an_entry_logged_between_end_probes_is_not_skipped(self):
        # The last page holds 4 entries in 12 lines, so its end probe (10
        # entries on) runs past the end. Before systemd 258 that answers with
        # the last entry; an entry logged right after it made a second forward
        # probe answer differently, which once read as "more follows" and
        # continued from past the end, skipping what had been logged.
        journal = FakeGatewayd(
            [(at(m), [f"e{m}"]) for m in range(40, 20, -1)]
            + [(at(m), [f"tb{m}", " at", "err"]) for m in range(20, 16, -1)]
        )
        last_page = journal.cursor(20)  # tb20, where the third page starts
        logging = False
        logged = iter(range(6))

        def log_from_the_end_probe(range_: str) -> None:
            nonlocal logging
            logging = logging or range_ == f"entries={last_page}:10:1"
            if logging and (n := next(logged, None)) is not None:
                journal.entries.append((at(0), [f"mid{n}"]))

        journal.after_each = log_from_the_end_probe
        pages = await page_through(journal, "oldest", limit=10, hours_back=1)
        journal.after_each = None
        lines = joined(pages)
        expected = window_oracle(journal, NOW - timedelta(hours=1))
        assert lines == expected[: len(lines)]
        assert all(line.startswith("mid") for line in expected[len(lines) :])


@pytest.mark.asyncio
class TestTokenAndHoursBack:
    async def test_a_different_hours_back_with_a_token_is_flagged(self):
        journal = FakeGatewayd.per_minute(300)
        tools = tools_on(journal)
        first = await tools._get_system_service_log(
            service="host", limit=50, hours_back=2, now=NOW, order="oldest"
        )
        second = await tools._get_system_service_log(
            service="host",
            limit=50,
            hours_back=9,
            now=NOW,
            order="oldest",
            offset=first["next_offset"],
        )
        assert second["hours_back"] == 2
        assert second["window_start"] == first["window_start"]
        assert "hours_back=9 does not match" in second["warnings"][0]
        assert "hours_back=2" in second["pagination_hint"]

    async def test_a_token_alone_continues_its_window(self):
        journal = FakeGatewayd.per_minute(300)
        tools = tools_on(journal)
        first = await tools._get_system_service_log(
            service="host", limit=50, hours_back=2, now=NOW, order="oldest"
        )
        second = await tools._get_system_service_log(
            service="host",
            limit=50,
            now=NOW,
            order="oldest",
            offset=first["next_offset"],
        )
        assert second["hours_back"] == 2
        assert "warnings" not in second
        assert shown(second["log"])[0] == "m70"


class TestTheFakeMatchesGatewayd:
    """Each Range shape the code sends, answered as journal-gatewayd.c would.

    Derived from systemd v256 ``request_reader_entries`` and ``request_handler
    _entries`` (seek, then ``sd_journal_previous_skip(-n_skip + 1)`` or
    ``sd_journal_next_skip(n_skip + 1)``), and the v258 guard against a skip
    past the end. Keeps the fake honest when it changes.
    """

    @pytest.mark.parametrize(
        ("range_", "empty_world", "clamp_world"),
        [
            # no cursor, negative skip: from the tail, the (k+1)-th newest
            ("entries=:-1:2", ["m1", "m0"], ["m1", "m0"]),
            ("entries=:-4:2", ["m4", "m3"], ["m4", "m3"]),
            # a skip of 0, "-0" included, seeks the head
            ("entries=:-0:1", ["m4"], ["m4"]),
            ("entries=:0:2", ["m4", "m3"], ["m4", "m3"]),
            # past the head: clamped to the head in every version
            ("entries=:-9:1", ["m4"], ["m4"]),
            ("entries=<2>:-9:1", ["m4"], ["m4"]),
            # from a cursor
            ("entries=<2>:0:1", ["m2"], ["m2"]),
            ("entries=<2>:-1:1", ["m3"], ["m3"]),
            ("entries=<2>:2:1", ["m0"], ["m0"]),
            # past the end: nothing from 258, the last entry before it
            ("entries=<2>:3:1", [], ["m0"]),
            ("entries=:7:1", [], ["m0"]),
            (f"realtime={at(2)}::3:1", [], ["m0"]),
            # a window after every entry is empty in every version
            (f"realtime={at(-1)}::0:1", [], []),
        ],
    )
    @pytest.mark.parametrize("clamps", [False, True])
    def test_each_range(
        self,
        range_: str,
        empty_world: list[str],
        clamp_world: list[str],
        clamps: bool,
    ):
        journal = FakeGatewayd.per_minute(5)  # m4 (head) ... m0 (newest)
        journal.clamps = clamps
        range_ = range_.replace("<2>", journal.cursor(2))
        assert shown(journal.answer(range_)) == (clamp_world if clamps else empty_world)


@pytest.mark.asyncio
class TestSearchedWindow:
    @pytest.mark.parametrize("hours_back", [600_000, 10**12])
    async def test_a_huge_hours_back_reads_the_whole_journal(self, hours_back: int):
        journal = FakeGatewayd.per_minute(300)
        out = await tools_on(journal)._get_system_service_log(
            service="host", limit=500, search="m", hours_back=hours_back, now=NOW
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
        assert out["window_complete"] is True
        assert out["has_more"] is False
        assert "pagination_hint" not in out

    async def test_an_oversized_search_points_to_the_page_read(self):
        with patch("ha_mcp.client.journal_window.MAX_ENTRIES", 100):
            out = await tools_on(FakeGatewayd.per_minute(3000))._get_system_service_log(
                service="host", search="m", hours_back=24, now=NOW
            )
        assert out["window_complete"] is False
        assert out["has_more"] is True
        assert "more than 100 entries" in out["warnings"][0]
        hint = out["pagination_hint"]
        assert "cannot be searched" in hint
        assert "smaller hours_back" not in hint
        assert "hours_back=24, order='oldest', limit=100)" in hint
        assert shown(out["log"])[0] == "m0"  # the newest are the ones searched

    async def test_unreturned_search_matches_are_reported(self):
        journal = FakeGatewayd.per_minute(600)
        for i in (-10, -20, -30):
            journal.entries[i] = (journal.entries[i][0], ["needle"])
        out = await tools_on(journal)._get_supervisor_log(
            slug="core_ssh", search="needle", limit=2, hours_back=2, now=NOW
        )
        assert out["returned_lines"] == 2
        assert out["has_more"] is True
        assert "1 more matching" in out["pagination_hint"]


@pytest.mark.asyncio
class TestErrorsAndDefaults:
    @pytest.mark.parametrize(
        ("source", "slug"), [("supervisor", "core_ssh"), ("system_service", "host")]
    )
    async def test_a_rejection_unrelated_to_the_range_keeps_supervisors_words(
        self, source: str, slug: str
    ):
        client = MagicMock(timeout=30)
        client._raw_request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 400 - App is not installed", status_code=400
            )
        )
        with pytest.raises(ToolError) as raised:
            await LogTools(client).get_logs(
                **call_kwargs(source=source, slug=slug, hours_back=1)
            )
        message = str(raised.value)
        assert "App is not installed" in message
        assert "systemd" not in message

    async def test_a_page_without_a_cursor_says_so(self):
        client = MagicMock(timeout=30)
        client._raw_request = AsyncMock(return_value=MagicMock(text="a\nb", headers={}))
        with pytest.raises(ToolError, match="X-First-Cursor"):
            await LogTools(client)._get_system_service_log(
                service="host", limit=2, hours_back=1, now=NOW, order="oldest"
            )

    async def test_without_hours_back_the_newest_lines_are_read_as_before(self):
        client = MagicMock()
        client._get_system_service_logs = AsyncMock(return_value="a\nb\nc")
        out = await LogTools(client)._get_system_service_log(
            service="supervisor", limit=2
        )
        assert out["log"] == "c\nb"
        assert "window_complete" not in out
        client._raw_request.assert_not_called()


@pytest.mark.asyncio
class TestGetLogsPassesTheWindowOn:
    @pytest.mark.parametrize(
        ("source", "slug"), [("supervisor", "core_ssh"), ("system_service", "host")]
    )
    async def test_hours_back_and_offset_reach_the_journal(
        self, source: str, slug: str
    ):
        # A window reaching before 1970 starts at the epoch, so the request
        # does not depend on the clock get_logs reads.
        journal = FakeGatewayd.per_minute(600)
        out = await tools_on(journal).get_logs(
            **call_kwargs(
                source=source,
                slug=slug,
                hours_back=10**12,
                offset=500,
                limit=50,
                order="oldest",
            )
        )
        assert journal.ranges[0] == "realtime=0::0:1"
        assert f"entries={journal.cursor(0)}:500:1" in journal.ranges
        assert shown(out["log"])[0] == "m99"
        assert "warnings" not in out

    @pytest.mark.parametrize(
        ("source", "slug"), [("supervisor", "core_ssh"), ("system_service", "host")]
    )
    async def test_a_token_reaches_the_journal(self, source: str, slug: str):
        journal = FakeGatewayd.per_minute(60)
        token = str(WindowToken("oldest", EPOCH, 2, journal.cursor(10), 5))
        await tools_on(journal).get_logs(
            **call_kwargs(
                source=source, slug=slug, hours_back=2, offset=token, order="oldest"
            )
        )
        assert journal.ranges[0] == f"entries={journal.cursor(10)}:5:100"

    async def test_a_malformed_token_is_refused(self):
        with pytest.raises(ToolError, match="Invalid 'offset' token"):
            await tools_on(FakeGatewayd([])).get_logs(
                **call_kwargs(
                    source="system_service",
                    slug="host",
                    hours_back=2,
                    offset="window:x",
                )
            )

    async def test_the_logbook_reads_one_hour_when_hours_back_is_omitted(self):
        fetch = AsyncMock(return_value=([], "rest"))
        with (
            patch.object(LogTools, "_fetch_logbook_events", fetch),
            patch(
                "ha_mcp.tools.response_helpers.fetch_ha_timezone",
                AsyncMock(return_value=("UTC", False)),
            ),
        ):
            await LogTools(MagicMock()).get_logs(
                **call_kwargs(source="logbook", end_time="2026-11-05T12:00:00+00:00")
            )
        assert fetch.await_args.kwargs["start_time"] == "2026-11-05T11:00:00+00:00"


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

    @pytest.mark.parametrize(
        ("hours_back", "order", "search"),
        [(None, "oldest", None), (2, "newest", None), (2, "oldest", "needle")],
    )
    def test_offset_where_the_journal_cannot_page_is_flagged(
        self, hours_back: int | None, order: str, search: str | None
    ):
        warnings = _collect_log_warnings(
            "system_service",
            None,
            None,
            None,
            None,
            None,
            "host",
            order,
            50,
            hours_back=hours_back,
            search=search,
        )
        assert any("'offset'" in w for w in warnings)

    def test_offset_on_an_oldest_first_window_is_not_flagged(self):
        warnings = _collect_log_warnings(
            "supervisor",
            None,
            None,
            None,
            None,
            None,
            "core_ssh",
            "oldest",
            50,
            hours_back=2,
        )
        assert not any("offset" in w for w in warnings)
