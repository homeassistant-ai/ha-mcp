"""``hours_back`` on ``ha_get_logs(source="supervisor"|"system_service")``.

The journal selects the window: both orders start from its first entry
(``Range: realtime=<since>``), and every later page is read from a cursor
Supervisor sent as ``X-First-Cursor``, which Supervisor and Core's hassio
proxy pass through. The tests drive the public tool,
``LogTools.get_logs``, over real HTTP against ``FakeGatewayd``
(``_journal_gatewayd.py``; the reads under it are in
``test_journal_window_reads.py``, tokens and ignored parameters in
``test_supervisor_logs_window_tokens.py``), which answers ranges the way
``journal-gatewayd.c`` does, so they check what the tool returns for what the
journal holds, not a copy of the selection. The server's client is the real
one on a real ``hass`` (``core_client``); it reaches the journal as the app
does (``in_app_client``) or through Core's hassio proxy (``proxied_client``).

A test that reads past the journal's end runs against both answers to a skip
past it (``both_gatewayds``). The clock is frozen at ``NOW`` and moved a minute
between pages, as the real one moves.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.journal_window import (
    EPOCH,
    Order,
    WindowToken,
    parse_window_token,
)
from ha_mcp.client.rest_client import HomeAssistantClient

from ._gateway import gateway as gateway
from ._gateway import gateway_client as gateway_client
from ._journal_gatewayd import (
    NOW,
    FakeGatewayd,
    ask,
    at,
    both_gatewayds,
    hinted_call,
    joined,
    page_through,
    per_minute,
    window_oracle,
)
from ._journal_gatewayd import (
    clock as clock,
)
from ._journal_gatewayd import gatewayd as gatewayd
from ._journal_gatewayd import in_app_client as in_app_client
from ._journal_gatewayd import journal_client as journal_client
from ._journal_gatewayd import journal_proxy as journal_proxy
from ._journal_gatewayd import proxied_client as proxied_client

# Reaches before 1970, which the window defines as the whole journal.
_WHOLE_JOURNAL = 10**12


@pytest.mark.usefixtures("clock")
@both_gatewayds
class TestOldestFirstPages:
    async def test_pages_continue_from_an_entry_while_the_clock_moves_on(
        self,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        # Each page is read a minute later, and the journal grows meanwhile.
        # Recomputing the window's start would move it forward a minute per
        # page and skip entries; the token keeps the first page's start and
        # continues from the exact entry the page before ended on.
        gatewayd.load(per_minute(3000))
        pages = await page_through(
            clock,
            in_app_client,
            "oldest",
            limit=50,
            hours_back=2,
            between=lambda n: gatewayd.log(-n, f"new{n}"),
        )
        lines = joined(pages)
        assert lines[:121] == [f"m{m}" for m in range(120, -1, -1)]
        assert lines[121:] == [f"new{n}" for n in range(1, len(pages))]
        assert all(p["window_start"] == "2026-11-05T10:00:00+00:00" for p in pages)
        assert gatewayd.ranges[0] == f"realtime={at(120)}::0:50"
        assert "offset='window:oldest:" in pages[0]["pagination_hint"]

    async def test_an_integer_offset_skips_into_the_window(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(3000))
        out = await ask(
            in_app_client, limit=50, offset=100, hours_back=2, order="oldest"
        )
        assert gatewayd.ranges[0] == f"realtime={at(120)}::0:1"
        assert out["log"].splitlines() == [f"m{m}" for m in range(20, -1, -1)]
        assert out["has_more"] is False
        assert "next_offset" not in out
        assert "pagination_hint" not in out

    async def test_a_full_last_page_asks_the_journal_whether_more_follows(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(100))
        out = await ask(
            in_app_client,
            source="supervisor",
            slug="core_ssh",
            limit=50,
            offset=50,
            hours_back=24,
            order="oldest",
        )
        assert out["log"].splitlines() == [f"m{m}" for m in range(49, -1, -1)]
        assert out["has_more"] is False

    @pytest.mark.parametrize("offset", [120, 121, 500])
    async def test_an_offset_at_or_past_the_windows_end_is_an_empty_page(
        self, offset: int, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(3000))
        out = await ask(
            in_app_client, limit=50, offset=offset, hours_back=2, order="oldest"
        )
        expected = ["m0"] if offset == 120 else []
        assert out["log"].splitlines() == expected
        assert out["has_more"] is False

    async def test_an_offset_into_an_empty_window_is_an_empty_page(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        # Every entry is older than the window, so there is no first entry to
        # count from; the journal's last entry must not answer instead.
        gatewayd.load([(at(300), ["old"])])
        out = await ask(in_app_client, limit=50, offset=5, hours_back=1, order="oldest")
        assert out["log"] == ""
        assert out["has_more"] is False

    async def test_a_page_counts_entries_and_returns_each_one_whole(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(
            [
                (at(30), ["Traceback:", "  File x", "Error"]),
                (at(20), ["m20"]),
                (at(10), ["m10"]),
            ]
        )
        out = await ask(
            in_app_client, slug="core", limit=2, hours_back=1, order="oldest"
        )
        assert out["log"].splitlines() == ["Traceback:", "  File x", "Error", "m20"]
        rest = await ask(in_app_client, slug="core", offset=out["next_offset"])
        assert rest["log"] == "m10"


@pytest.mark.usefixtures("clock")
@both_gatewayds
class TestNewestFirstPages:
    async def test_pages_run_back_to_the_window_start_and_stop_there(
        self,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        journal_client: HomeAssistantClient,
    ):
        gatewayd.load(per_minute(3000))
        pages = await page_through(clock, journal_client, "newest", 50, 2)
        assert joined(pages) == [f"m{m}" for m in range(121)]
        assert [p["returned_lines"] for p in pages] == [50, 50, 21]
        assert gatewayd.ranges[0] == f"realtime={at(120)}::0:50"
        assert pages[-1]["has_more"] is False
        assert "next_offset" not in pages[-1]
        assert "window_complete" not in pages[0]

    async def test_a_page_that_reaches_the_window_start_exactly_has_no_more(
        self,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load(per_minute(3000))
        pages = await page_through(clock, in_app_client, "newest", 121, 2)
        assert len(pages) == 1
        assert joined(pages) == [f"m{m}" for m in range(121)]

    async def test_a_whole_journal_window_stops_at_the_head(
        self,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load(per_minute(120))
        pages = await page_through(clock, in_app_client, "newest", 50, 24)
        assert joined(pages) == [f"m{m}" for m in range(120)]

    async def test_a_one_entry_page_is_the_newest_entry(
        self,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load(per_minute(10))
        pages = await page_through(clock, in_app_client, "newest", 1, 1)
        assert joined(pages) == [f"m{m}" for m in range(10)]

    async def test_a_one_entry_journal(
        self,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load([(at(5), ["only"])])
        pages = await page_through(clock, in_app_client, "newest", 50, 1)
        assert joined(pages) == ["only"]

    @pytest.mark.parametrize("size", [250, 10_000, 100_000])
    async def test_the_requests_of_a_page_do_not_grow_with_the_window(
        self, size: int, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        # Every request counts against the client's one deadline, and a real
        # journal answers a deep skip in seconds. An entry an hour apart puts
        # the window's start exactly ``size`` entries back, 50 older ones
        # before it.
        gatewayd.load([(at(60 * m), [f"m{m}"]) for m in range(size + 49, -1, -1)])
        hours_back = size - 1
        sent = len(gatewayd.requests)
        first = await ask(in_app_client, limit=100, hours_back=hours_back)
        assert first["log"].splitlines() == [f"m{m}" for m in range(100)]
        assert len(gatewayd.requests) - sent <= 6
        sent = len(gatewayd.requests)
        middle = await ask(in_app_client, limit=100, offset=first["next_offset"])
        assert middle["log"].splitlines() == [f"m{m}" for m in range(100, 200)]
        assert len(gatewayd.requests) - sent <= 3  # the first entry's check, the page
        # The page before the window's first entry is cut there: 60 entries.
        start = parse_window_token(first["next_offset"]).since
        last = WindowToken(
            "newest",
            start,
            hours_back,
            "host",
            gatewayd.cursor(50),
            0,
            gatewayd.cursor(110),
        )
        sent = len(gatewayd.requests)
        out = await ask(in_app_client, limit=100, offset=str(last))
        assert out["returned_lines"] == 60 and out["has_more"] is False
        # The window's first entry is checked, then bisected (log2(limit)),
        # verified and read.
        assert len(gatewayd.requests) - sent <= 12

    @pytest.mark.parametrize("hours_back", [2, _WHOLE_JOURNAL])
    @pytest.mark.parametrize("limit", [7, 50])
    async def test_the_pages_are_the_oldest_first_read_reversed(
        self,
        hours_back: int,
        limit: int,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        # Entries logged between the newest-first pages are not part of the
        # read and do not move a page: no gap, no overlap, no new line.
        gatewayd.load(per_minute(150))
        gatewayd.entries[40] = (gatewayd.entries[40][0], ["Traceback", " File", "Err"])
        since = EPOCH if hours_back == _WHOLE_JOURNAL else NOW - timedelta(hours=2)
        expected = window_oracle(gatewayd, since)
        oldest = await page_through(clock, in_app_client, "oldest", limit, hours_back)
        newest = await page_through(
            clock,
            in_app_client,
            "newest",
            limit,
            hours_back,
            between=lambda n: gatewayd.log(-n, f"new{n}"),
        )
        assert joined(oldest) == expected
        assert joined(newest) == list(reversed(expected))


@pytest.mark.usefixtures("clock")
class TestNewestFirstWindowsThatNeverReachTheEnd:
    """Nothing here probes past the journal's end, so one gatewayd answers for both."""

    async def test_an_empty_journal(
        self,
        clock: FrozenDateTimeFactory,
        in_app_client: HomeAssistantClient,
    ):
        pages = await page_through(clock, in_app_client, "newest", 50, 1)
        assert pages[0]["log"] == "" and pages[0]["has_more"] is False

    async def test_a_window_older_than_the_journal_is_empty(
        self,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load([(at(300), ["old"])])
        pages = await page_through(clock, in_app_client, "newest", 50, 1)
        assert joined(pages) == []

    async def test_lines_are_shown_newest_first(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(
            [(at(30), ["Traceback:", "  File x", "Error"]), (at(20), ["m20"])]
        )
        out = await ask(in_app_client, slug="core", limit=5, hours_back=1)
        assert out["log"].splitlines() == ["m20", "Error", "  File x", "Traceback:"]

    async def test_a_window_of_one_page_is_read_in_one_request(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(3000))
        out = await ask(in_app_client, limit=100, hours_back=1)
        assert out["log"].splitlines() == [f"m{m}" for m in range(61)]
        assert out["has_more"] is False
        assert len(gatewayd.requests) == 1


@pytest.mark.usefixtures("clock")
@both_gatewayds
class TestTheWindowEnds:
    """Paging stops at the window's end on both gatewayd versions."""

    @pytest.mark.parametrize("order", ["oldest", "newest"])
    async def test_one_entry_pages_end(
        self,
        order: Order,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load(per_minute(5))
        pages = await page_through(clock, in_app_client, order, 1, 1)
        assert joined(pages) == window_oracle(gatewayd, NOW - timedelta(hours=1), order)
        assert len(pages) == 5

    async def test_a_traceback_as_the_windows_last_entry_ends_paging(
        self,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load(
            [
                (at(30), ["a"]),
                (at(20), ["b"]),
                (at(10), ["Traceback", " File", "Error"]),
            ]
        )
        pages = await page_through(clock, in_app_client, "oldest", 3, 1)
        assert joined(pages) == ["a", "b", "Traceback", " File", "Error"]
        assert pages[-1]["has_more"] is False


@pytest.mark.usefixtures("clock")
@both_gatewayds
class TestFollowingTheHint:
    """An agent copies the printed call; the pages must equal the window.

    The oracle reads the fake's entries directly, so it shares no Range logic
    with the code under test. Entries are logged between calls: hours_back
    reaches back to the epoch only for the first call's seek, and
    continuation must come from the token alone.
    """

    @pytest.mark.parametrize("order", ["oldest", "newest"])
    @pytest.mark.parametrize("limit", [2, 7, 50])
    async def test_the_hinted_calls_read_the_window_exactly(
        self,
        order: Order,
        limit: int,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load(per_minute(60))
        gatewayd.entries[20] = (gatewayd.entries[20][0], ["Traceback", " File", "Err"])
        out = await ask(
            in_app_client, hours_back=_WHOLE_JOURNAL, limit=limit, order=order
        )
        expected = window_oracle(gatewayd, EPOCH, order)
        pages = [out]
        while out["has_more"]:
            assert len(pages) < 400, "paging does not end"
            if len(pages) <= 3:
                # A few, not one per page: oldest-first follows the journal's
                # end, so logging as fast as it reads would never let it end.
                gatewayd.log(-len(pages), f"logged meanwhile {len(pages)}")
            out = await ask(in_app_client, **hinted_call(out["pagination_hint"]))
            pages.append(out)
        lines = joined(pages)
        if order == "oldest":
            # Oldest-first reaches the journal's end, so it also reads what
            # was logged meanwhile, in order, after the window as it stood.
            assert lines[: len(expected)] == expected
            assert all(line.startswith("logged") for line in lines[len(expected) :])
        else:
            assert lines == expected


@pytest.mark.usefixtures("clock")
@both_gatewayds
class TestOutOfOrderStamps:
    """After a clock step an entry can be stamped before entries logged earlier.

    The window is what the journal's ``realtime=`` seek selects, in both
    orders: from the first entry it lands on to the journal's end, whatever
    the stamps after it say.
    """

    @staticmethod
    def stamp_early(journal: FakeGatewayd, back: int) -> None:
        stamp, lines = journal.entries[-back]
        journal.entries[-back] = (stamp - 3 * 3600, lines)

    @pytest.mark.parametrize("early", [[25], [50], [51], [50, 51], [100]])
    async def test_early_stamps_inside_the_window_do_not_end_a_read(
        self,
        early: list[int],
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load(per_minute(150))
        for back in early:
            self.stamp_early(gatewayd, back)
        expected = window_oracle(gatewayd, NOW - timedelta(hours=2))
        assert len(expected) == 121  # the window holds the early ones too
        oldest = await page_through(clock, in_app_client, "oldest", 50, 2)
        newest = await page_through(clock, in_app_client, "newest", 50, 2)
        assert joined(oldest) == expected
        assert joined(newest) == list(reversed(expected))

    async def test_a_late_stamp_before_the_window_starts_it_there(
        self,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        # The seek lands on the first entry stamped at or after the start,
        # which here is an entry the clock step stamped late.
        gatewayd.load(per_minute(150))
        gatewayd.entries[-125] = (at(0), gatewayd.entries[-125][1])
        expected = window_oracle(gatewayd, NOW - timedelta(hours=2))
        assert len(expected) == 125
        oldest = await page_through(clock, in_app_client, "oldest", 50, 2)
        newest = await page_through(clock, in_app_client, "newest", 50, 2)
        assert joined(oldest) == expected
        assert joined(newest) == list(reversed(expected))


@pytest.mark.usefixtures("clock")
@both_gatewayds
class TestAJournalReset:
    """A new seqnum id in the journal's tail leaves its cursors unordered.

    When the first page's oldest entry is in another id than the window's first
    (a reset at 100), the pages count the window from its first entry and read
    by position. When it is in the same id (a reset at 149, only the newest
    entry), they read back by cursor. Both are the oldest-first read reversed,
    with nothing logged meanwhile moving a page.
    """

    @pytest.mark.parametrize(("reset_at", "by_position"), [(100, True), (149, False)])
    @pytest.mark.parametrize("limit", [7, 50])
    async def test_the_pages_are_the_oldest_first_read_reversed(
        self,
        reset_at: int,
        by_position: bool,
        limit: int,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load(per_minute(150))
        gatewayd.reset_at = reset_at
        expected = window_oracle(gatewayd, NOW - timedelta(hours=2))
        oldest = await page_through(clock, in_app_client, "oldest", limit, 2)
        newest = await page_through(
            clock,
            in_app_client,
            "newest",
            limit,
            2,
            between=lambda n: gatewayd.log(-n, f"new{n}"),
        )
        assert joined(oldest) == expected
        assert joined(newest) == list(reversed(expected))
        # The page's oldest entry is in the new id (so unordered against the
        # window's first) for a reset at 100, and in the old one for 149. The
        # token says which mode the read is in, and the read keeps it.
        modes = {parse_window_token(p["next_offset"]).before == "" for p in newest[:-1]}
        assert modes == {by_position}

    async def test_a_reset_before_the_window_does_not_change_the_mode(
        self,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load(per_minute(150))
        gatewayd.reset_at = 10  # the window starts at entry 29, after it
        newest = await page_through(clock, in_app_client, "newest", 50, 2)
        token = parse_window_token(newest[0]["next_offset"])
        assert token.before != "" and token.skip == 0
        assert joined(newest) == [f"m{m}" for m in range(121)]


@pytest.mark.usefixtures("clock")
class TestAJournalThatChangesUnderANewestFirstRead:
    """What cursors cannot prove is an error naming the way out, never a short page."""

    async def test_another_seqnum_id_inside_the_window_is_refused(
        self,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        # Entries 305-315 come from a second journal file with an id of its
        # own. The page whose oldest entry is one of them cannot tell whether
        # it reaches the window's first; it must not guess and drop the rest.
        gatewayd.load(per_minute(400))
        gatewayd.other_id = (305, 316)
        oldest = await page_through(clock, in_app_client, "oldest", 45, 24)
        assert len(joined(oldest)) == 400
        with pytest.raises(ToolError, match="order='oldest'"):
            await page_through(clock, in_app_client, "newest", 45, 24)

    async def test_a_vacuumed_first_entry_is_refused_not_repeated(
        self,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load(per_minute(400))
        window = {"hours_back": 24, "limit": 100, "order": "newest"}
        first = await ask(in_app_client, **window)
        second = await ask(in_app_client, **window, offset=first["next_offset"])
        gatewayd.head = 150  # the journal drops its oldest entries
        sent = len(gatewayd.requests)
        with pytest.raises(ToolError, match="no longer in the journal"):
            await ask(in_app_client, **window, offset=second["next_offset"])
        assert len(gatewayd.requests) - sent == 1  # refused before any page


@pytest.mark.usefixtures("clock")
@both_gatewayds
class TestEntriesLoggedDuringARead:
    """The journal grows between any two requests, not only between pages."""

    @pytest.mark.parametrize("order", ["oldest", "newest"])
    @pytest.mark.parametrize("limit", [1, 3, 10])
    async def test_no_entry_is_skipped_or_repeated(
        self,
        order: Order,
        limit: int,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        # Multi-line entries let a page reach ``limit`` lines with fewer
        # entries, so its end probe runs past the journal's end, where an
        # entry logged between two probes once made a clamping gatewayd's
        # answers disagree and skipped what followed.
        gatewayd.load(
            [
                (at(m), ["tb", " at", "err"] if m % 4 == 0 else [f"e{m}"])
                for m in range(30, 0, -1)
            ]
        )
        logged = iter(range(40))

        def log_one(_range: str) -> None:
            if (n := next(logged, None)) is not None:
                lines = [f"mid{n}", f"mid{n}+", f"mid{n}++"] if n % 2 else [f"mid{n}"]
                gatewayd.entries.append((at(0), lines))

        gatewayd.after_each = log_one
        pages = await page_through(clock, in_app_client, order, limit, 1)
        gatewayd.after_each = None
        lines = joined(pages)
        if order == "oldest":
            # No gap and no repeat; only what was logged after the last page
            # was read can be missing from its end.
            expected = window_oracle(gatewayd, NOW - timedelta(hours=1))
            assert lines == expected[: len(lines)]
            assert all(line.startswith("mid") for line in expected[len(lines) :])
            assert "e1" in lines
        else:
            # Newest-first ends at the tail its first page saw; what was logged
            # after that is not part of the read, and the rest is read whole.
            expected = window_oracle(gatewayd, NOW - timedelta(hours=1))
            assert lines == list(reversed(expected[: len(lines)]))
            assert all(line.startswith("mid") for line in expected[len(lines) :])
            assert lines[-1] == "e30"

    async def test_an_entry_logged_between_end_probes_is_not_skipped(
        self,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        # The last page holds 4 entries in 12 lines, so its end probe (10
        # entries on) runs past the end. Before systemd 258 that answers with
        # the last entry; an entry logged right after it made a second forward
        # probe answer differently, which once read as "more follows" and
        # continued from past the end, skipping what had been logged.
        gatewayd.load(
            [(at(m), [f"e{m}"]) for m in range(40, 20, -1)]
            + [(at(m), [f"tb{m}", " at", "err"]) for m in range(20, 16, -1)]
        )
        last_page = gatewayd.cursor(20)  # tb20, where the third page starts
        logging = False
        logged = iter(range(6))

        def log_from_the_end_probe(range_: str) -> None:
            nonlocal logging
            logging = logging or last_page in range_
            if logging and (n := next(logged, None)) is not None:
                gatewayd.entries.append((at(0), [f"mid{n}"]))

        gatewayd.after_each = log_from_the_end_probe
        pages = await page_through(clock, in_app_client, "oldest", 10, 1)
        gatewayd.after_each = None
        lines = joined(pages)
        expected = window_oracle(gatewayd, NOW - timedelta(hours=1))
        assert lines == expected[: len(lines)]
        assert all(line.startswith("mid") for line in expected[len(lines) :])

    @pytest.mark.parametrize("per_request", [1, 2, 3])
    async def test_entries_logged_after_a_short_page_was_read_are_not_skipped(
        self,
        per_request: int,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        # The window's last page holds 9 entries in 11 lines, so it reaches
        # the 10-line count with fewer than 10 entries. Entries logged after
        # it was read can make the end probe find a tenth entry on; the token
        # then skips 10, and the page has to hold the entries it skips.
        gatewayd.load(
            [(at(m), [f"e{m}"]) for m in range(40, 32, -1)]
            + [(at(30), ["Traceback", " File", "Error"])]
        )
        logged = iter(range(12))

        def log_some(_range: str) -> None:
            for _ in range(per_request):
                if (n := next(logged, None)) is not None:
                    gatewayd.entries.append((at(0), [f"mid{n}", f"mid{n}+"]))

        gatewayd.after_each = log_some
        pages = await page_through(clock, in_app_client, "oldest", 10, 1)
        gatewayd.after_each = None
        lines = joined(pages)
        expected = window_oracle(gatewayd, NOW - timedelta(hours=1))
        assert lines == expected[: len(lines)]
        assert all(line.startswith("mid") for line in expected[len(lines) :])


class TestSearchedWindow:
    @pytest.mark.usefixtures("clock")
    @pytest.mark.parametrize("hours_back", [600_000, 10**12])
    async def test_a_huge_hours_back_reads_the_whole_journal(
        self,
        hours_back: int,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load(per_minute(300))
        out = await ask(in_app_client, limit=500, search="m", hours_back=hours_back)
        assert out["success"] is True
        assert out["window_start"] == "1970-01-01T00:00:00+00:00"
        assert out["total_lines"] == 300
        assert gatewayd.ranges[0] == "realtime=0::0:10000"

    @pytest.mark.usefixtures("clock")
    async def test_app_log_search_spans_the_whole_window(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(600))
        gatewayd.entries[-100] = (at(99), ["needle in the window"])
        gatewayd.entries[-300] = (at(299), ["needle before the window"])
        out = await ask(
            in_app_client,
            source="supervisor",
            slug="core_ssh",
            search="needle",
            hours_back=2,
        )
        assert out["log"] == "needle in the window"
        assert out["window_complete"] is True
        assert out["has_more"] is False
        assert "pagination_hint" not in out

    @pytest.mark.usefixtures("clock")
    @both_gatewayds
    async def test_an_oversized_search_points_to_the_page_read(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(3000))
        with patch("ha_mcp.client.journal_window.MAX_ENTRIES", 100):
            out = await ask(in_app_client, search="m", hours_back=24)
        assert out["window_complete"] is False
        assert out["has_more"] is True
        assert "more than 100 entries" in out["warnings"][0]
        hint = out["pagination_hint"]
        assert "cannot be searched" in hint
        assert "smaller hours_back" not in hint
        assert "hours_back=24, order='oldest', limit=100)" in hint
        assert out["log"].splitlines()[0] == "m0"  # the newest are the ones searched

    @pytest.mark.usefixtures("clock")
    async def test_unreturned_search_matches_are_reported(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(600))
        for i in (-10, -20, -30):
            gatewayd.entries[i] = (gatewayd.entries[i][0], ["needle"])
        out = await ask(
            in_app_client,
            source="supervisor",
            slug="core_ssh",
            search="needle",
            limit=2,
            hours_back=2,
        )
        assert out["returned_lines"] == 2
        assert out["has_more"] is True
        assert "1 more matching" in out["pagination_hint"]


@pytest.mark.usefixtures("clock")
class TestErrorsAndDefaults:
    @pytest.mark.parametrize(
        ("source", "slug"), [("supervisor", "core_ssh"), ("system_service", "host")]
    )
    async def test_a_rejection_unrelated_to_the_range_keeps_supervisors_words(
        self,
        source: str,
        slug: str,
        gatewayd: FakeGatewayd,
        journal_client: HomeAssistantClient,
    ):
        gatewayd.rejection = "App is not installed"
        with pytest.raises(ToolError) as raised:
            await ask(journal_client, source=source, slug=slug, hours_back=1)
        message = str(raised.value)
        assert "App is not installed" in message
        assert "systemd" not in message

    @pytest.mark.parametrize(
        ("order", "offset"), [("oldest", 0), ("newest", 0), ("oldest", 5)]
    )
    async def test_a_page_without_a_cursor_says_so(
        self,
        order: Order,
        offset: int,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        # A proxy that drops X-First-Cursor leaves a page with text and nothing
        # to continue from; reading that as an empty window would end paging
        # with no error.
        gatewayd.load(per_minute(10))
        gatewayd.send_cursor = False
        with pytest.raises(ToolError, match="X-First-Cursor"):
            await ask(in_app_client, limit=2, hours_back=1, order=order, offset=offset)

    async def test_without_hours_back_the_newest_lines_are_read_as_before(
        self, gatewayd: FakeGatewayd, journal_client: HomeAssistantClient
    ):
        gatewayd.load([(at(3), ["a"]), (at(2), ["b"]), (at(1), ["c"])])
        out = await ask(journal_client, slug="supervisor", limit=2)
        assert out["log"] == "c\nb"
        assert "window_complete" not in out
        assert [(s.range, s.query["lines"]) for s in gatewayd.requests] == [(None, "2")]


@pytest.mark.usefixtures("clock")
class TestGetLogsPassesTheWindowOn:
    @pytest.mark.parametrize(
        ("source", "slug", "path"),
        [
            ("supervisor", "core_ssh", "addons/core_ssh"),
            ("system_service", "host", "host"),
        ],
    )
    async def test_hours_back_and_offset_reach_the_journal(
        self,
        source: str,
        slug: str,
        path: str,
        gatewayd: FakeGatewayd,
        journal_client: HomeAssistantClient,
    ):
        # A window reaching before 1970 starts at the epoch, so the request
        # does not depend on the clock get_logs reads.
        gatewayd.load(per_minute(600))
        out = await ask(
            journal_client,
            source=source,
            slug=slug,
            hours_back=_WHOLE_JOURNAL,
            offset=500,
            limit=50,
            order="oldest",
        )
        assert gatewayd.ranges[0] == "realtime=0::0:1"
        assert out["log"].splitlines()[0] == "m99"
        assert "warnings" not in out
        assert gatewayd.requests[0].path.endswith(f"/{path}/logs")


class TestTheLogbookDefault:
    """Real time: the logbook read runs Core's own transports, which wait."""

    async def test_the_logbook_reads_one_hour_when_hours_back_is_omitted(
        self, core_client: HomeAssistantClient
    ):
        # The spy calls through, so Core answers; the request is what is
        # asserted. This Core has no logbook set up and answers 404, which
        # the tool reports as an error after making the request.
        with (
            patch.object(
                core_client, "get_logbook", wraps=core_client.get_logbook
            ) as spy,
            pytest.raises(ToolError),
        ):
            await ask(
                core_client,
                source="logbook",
                slug=None,
                end_time="2026-11-05T12:00:00+00:00",
            )
        assert spy.await_args is not None
        assert spy.await_args.kwargs["start_time"] == "2026-11-05T11:00:00+00:00"
