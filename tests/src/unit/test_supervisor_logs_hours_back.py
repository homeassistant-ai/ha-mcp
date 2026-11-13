"""``hours_back`` on ``ha_get_logs(source="supervisor"|"system_service")``.

The journal selects the window and serves it oldest-first: the first page
starts at its first entry (``Range: realtime=<since>``), and every later page
is read from a cursor Supervisor sent as ``X-First-Cursor``. The tests drive
the public tool, ``LogTools.get_logs``, over real HTTP against
``FakeGatewayd`` (``_journal_gatewayd.py``; the reads under it are in
``test_journal_window_reads.py``, tokens and ignored parameters in
``test_supervisor_logs_window_tokens.py``), which answers ranges the way
``journal-gatewayd.c`` does, so they check what the tool returns for what the
journal holds, not a copy of the selection. The server's client is the real
one on a real ``hass`` (``core_client``); it reaches the journal as the app
does (``in_app_client``) or at its ``/api/hassio/...`` path outside the app
(``hassio_path_client``).

The clock is frozen at ``NOW`` and moved a minute between pages, as the real
one moves.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.journal_window import EPOCH
from ha_mcp.client.rest_client import HomeAssistantClient

from ._gateway import gateway as gateway
from ._gateway import gateway_client as gateway_client
from ._journal_gatewayd import (
    NOW,
    FakeGatewayd,
    ask,
    at,
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
from ._journal_gatewayd import hassio_path_client as hassio_path_client
from ._journal_gatewayd import in_app_client as in_app_client
from ._journal_gatewayd import journal_client as journal_client
from ._journal_gatewayd import journal_proxy as journal_proxy

# Reaches before 1970, which the window defines as the whole journal.
_WHOLE_JOURNAL = 10**12


@pytest.mark.usefixtures("clock")
class TestPages:
    async def test_pages_run_from_the_window_start_to_its_end(
        self,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        journal_client: HomeAssistantClient,
    ):
        gatewayd.load(per_minute(3000))
        pages = await page_through(clock, journal_client, 50, 2)
        assert joined(pages) == [f"m{m}" for m in range(120, -1, -1)]
        assert [p["returned_lines"] for p in pages] == [50, 50, 21]
        assert {p["order"] for p in pages} == {"oldest"}
        assert gatewayd.ranges[0] == f"realtime={at(120)}::0:50"
        assert pages[-1]["has_more"] is False
        assert "next_offset" not in pages[-1]
        assert "window_complete" not in pages[0]

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
            limit=50,
            hours_back=2,
            between=lambda n: gatewayd.log(-n, f"new{n}"),
        )
        lines = joined(pages)
        assert lines[:121] == [f"m{m}" for m in range(120, -1, -1)]
        assert lines[121:] == [f"new{n}" for n in range(1, len(pages))]
        assert all(p["window_start"] == "2026-11-05T10:00:00+00:00" for p in pages)
        assert gatewayd.ranges[0] == f"realtime={at(120)}::0:50"
        assert "order='oldest'" in pages[0]["pagination_hint"]
        assert f"offset='window:{at(120)}:2:host:" in pages[0]["pagination_hint"]

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

    async def test_an_empty_journal_is_an_empty_page(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        # Supervisor sends no X-First-Cursor for an empty answer. Taking that
        # for a proxy that dropped the header would refuse a quiet service's
        # window instead of returning it empty.
        out = await ask(in_app_client, limit=50, hours_back=1)
        assert out["log"] == "" and out["has_more"] is False

    async def test_a_window_older_than_the_journal_is_empty(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load([(at(300), ["old"])])
        out = await ask(in_app_client, limit=50, hours_back=1)
        assert out["log"] == "" and out["has_more"] is False

    async def test_a_window_of_one_page_is_read_in_one_request(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(3000))
        out = await ask(in_app_client, limit=100, hours_back=1)
        assert out["log"].splitlines() == [f"m{m}" for m in range(60, -1, -1)]
        assert out["has_more"] is False
        assert len(gatewayd.requests) == 1

    @pytest.mark.parametrize("size", [250, 10_000, 100_000])
    async def test_the_requests_of_a_page_do_not_grow_with_the_window(
        self, size: int, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        # Every request counts against the client's one deadline, and a real
        # journal answers a deep skip in seconds. An entry an hour apart puts
        # the window's start exactly ``size`` entries back.
        gatewayd.load([(at(60 * m), [f"m{m}"]) for m in range(size + 49, -1, -1)])
        hours_back = size - 1
        sent = len(gatewayd.requests)
        first = await ask(in_app_client, limit=100, hours_back=hours_back)
        top = size - 1
        assert first["log"].splitlines() == [f"m{m}" for m in range(top, top - 100, -1)]
        assert len(gatewayd.requests) - sent <= 3  # the page, its end probe, reread
        sent = len(gatewayd.requests)
        second = await ask(in_app_client, limit=100, offset=first["next_offset"])
        assert second["log"].splitlines() == [
            f"m{m}" for m in range(top - 100, top - 200, -1)
        ]
        assert len(gatewayd.requests) - sent <= 4  # and the token's entry check


@pytest.mark.usefixtures("clock")
class TestOrderNewestWithAWindow:
    async def test_it_is_served_oldest_first_with_a_warning(
        self, gatewayd: FakeGatewayd, journal_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(3000))
        out = await ask(journal_client, limit=50, hours_back=2, order="newest")
        assert out["log"].splitlines() == [f"m{m}" for m in range(120, 70, -1)]
        assert out["order"] == "oldest"
        assert any("served oldest-first" in w for w in out["warnings"])
        follow = await ask(
            journal_client, limit=50, order="newest", offset=out["next_offset"]
        )
        assert follow["log"].splitlines()[0] == "m70"
        assert any("served oldest-first" in w for w in follow["warnings"])


@pytest.mark.usefixtures("clock")
class TestAVacuumedEntry:
    async def test_a_token_whose_entry_is_gone_is_refused_not_misread(
        self,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        # Gatewayd seeks a vacuumed cursor on to the next entry left, and the
        # skip counts from there: entries the read never returned would be
        # skipped, reported as success.
        gatewayd.load(per_minute(400))
        window = {"hours_back": 24, "limit": 5}
        first = await ask(in_app_client, **window)
        second = await ask(in_app_client, **window, offset=first["next_offset"])
        assert second["log"].splitlines() == [f"m{m}" for m in range(394, 389, -1)]
        gatewayd.head = 13  # the journal drops its oldest entries
        sent = len(gatewayd.requests)
        with pytest.raises(ToolError) as raised:
            await ask(in_app_client, **window, offset=second["next_offset"])
        message = str(raised.value)
        assert "no longer in the journal" in message
        assert "RESOURCE_NOT_FOUND" in message
        assert "Start a new read without 'offset'" in message
        assert "Ensure Supervisor is available" not in message
        assert len(gatewayd.requests) - sent == 1  # refused before any page


@pytest.mark.usefixtures("clock")
class TestTheWindowEnds:
    async def test_one_entry_pages_end(
        self,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load(per_minute(5))
        pages = await page_through(clock, in_app_client, 1, 1)
        assert joined(pages) == window_oracle(gatewayd, NOW - timedelta(hours=1))
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
        pages = await page_through(clock, in_app_client, 3, 1)
        assert joined(pages) == ["a", "b", "Traceback", " File", "Error"]
        assert pages[-1]["has_more"] is False


@pytest.mark.usefixtures("clock")
class TestFollowingTheHint:
    """An agent copies the printed call; the pages must equal the window.

    The oracle reads the fake's entries directly, so it shares no Range logic
    with the code under test. Entries are logged between calls: hours_back
    reaches back to the epoch only for the first call's seek, and
    continuation must come from the token alone.
    """

    @pytest.mark.parametrize("limit", [2, 7, 50])
    async def test_the_hinted_calls_read_the_window_exactly(
        self,
        limit: int,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        gatewayd.load(per_minute(60))
        gatewayd.entries[20] = (gatewayd.entries[20][0], ["Traceback", " File", "Err"])
        out = await ask(in_app_client, hours_back=_WHOLE_JOURNAL, limit=limit)
        expected = window_oracle(gatewayd, EPOCH)
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
        # The read reaches the journal's end, so it also reads what was
        # logged meanwhile, in order, after the window as it stood.
        assert lines[: len(expected)] == expected
        assert all(line.startswith("logged") for line in lines[len(expected) :])


@pytest.mark.usefixtures("clock")
class TestOutOfOrderStamps:
    """After a clock step an entry can be stamped before entries logged earlier.

    The window runs from where the journal's ``realtime=`` seek lands to the
    journal's end, whatever the stamps after it say: no ``until`` is sent, so
    an out-of-order stamp cannot end a read.
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
        pages = await page_through(clock, in_app_client, 50, 2)
        assert joined(pages) == expected
        assert {f"m{back - 1}" for back in early} <= set(expected)


@pytest.mark.usefixtures("clock")
class TestAJournalReset:
    async def test_a_new_seqnum_id_does_not_change_the_pages(
        self,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        # Pages continue from cursors alone; a reset's new seqnum id in the
        # window must not end or reorder them.
        gatewayd.load(per_minute(150))
        gatewayd.reset_at = 100
        pages = await page_through(clock, in_app_client, 7, 2)
        assert joined(pages) == window_oracle(gatewayd, NOW - timedelta(hours=2))


@pytest.mark.usefixtures("clock")
class TestEntriesLoggedDuringARead:
    """The journal grows between any two requests, not only between pages."""

    @pytest.mark.parametrize("limit", [1, 3, 10])
    async def test_no_entry_is_skipped_or_repeated(
        self,
        limit: int,
        clock: FrozenDateTimeFactory,
        gatewayd: FakeGatewayd,
        in_app_client: HomeAssistantClient,
    ):
        # Multi-line entries let a page reach ``limit`` lines with fewer
        # entries, so its end probe and the token's skip run across entries
        # logged after the page was read.
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
        pages = await page_through(clock, in_app_client, limit, 1)
        gatewayd.after_each = None
        lines = joined(pages)
        # No gap and no repeat; only what was logged after the last page was
        # read can be missing from its end.
        expected = window_oracle(gatewayd, NOW - timedelta(hours=1))
        assert lines == expected[: len(lines)]
        assert all(line.startswith("mid") for line in expected[len(lines) :])
        assert "e1" in lines

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
        pages = await page_through(clock, in_app_client, 10, 1)
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

    @pytest.mark.parametrize("offset", [0, 5])
    async def test_a_page_without_a_cursor_says_so(
        self,
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
            await ask(in_app_client, limit=2, hours_back=1, offset=offset)

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
