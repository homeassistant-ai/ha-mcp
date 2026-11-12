"""Reads of a time window of a Supervisor journal, and what they send (#2974).

``read_window`` and ``read_logs_window`` are the public reads under the
``hours_back`` search; ``window_range``, ``window_start`` and the page token
are the pieces they are built from. The reads run over real
HTTP against ``FakeGatewayd`` (``_journal_gatewayd.py``) with the server's
real client on a real ``hass``, as the app reaches Supervisor directly
(``in_app_client``) or through Core's hassio proxy (``proxied_client``).
The tool-level paging tests are in ``test_supervisor_logs_hours_back.py``.
"""

import re
from datetime import timedelta

import pytest

from ha_mcp.client.journal_window import (
    EPOCH,
    Order,
    WindowToken,
    parse_window_token,
    read_logs_page,
    read_logs_window,
    read_window,
    window_range,
    window_start,
)
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)

from ._gateway import gateway as gateway
from ._gateway import gateway_client as gateway_client
from ._journal_gatewayd import (
    NOW,
    SUPERVISOR_TOKEN,
    FakeGatewayd,
    ask,
    at,
    both_gatewayds,
    per_minute,
)
from ._journal_gatewayd import (
    clock as clock,
)
from ._journal_gatewayd import gatewayd as gatewayd
from ._journal_gatewayd import in_app_client as in_app_client
from ._journal_gatewayd import journal_client as journal_client
from ._journal_gatewayd import journal_proxy as journal_proxy
from ._journal_gatewayd import proxied_client as proxied_client


class TestRanges:
    def test_a_window_is_sent_as_gatewayd_seconds_with_no_end(self):
        since = NOW - timedelta(hours=1) + timedelta(seconds=30, milliseconds=999)
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


class TestTokens:
    @pytest.mark.parametrize(
        ("order", "skip", "before"),
        [("oldest", 5, ""), ("newest", 5, ""), ("newest", 0, "s=a;i=1;m=2")],
    )
    def test_a_token_round_trips(self, order: Order, skip: int, before: str):
        token = WindowToken(
            order, NOW, 2, "addons/core_ssh", "s=a;i=1;t=5", skip, before
        )
        assert parse_window_token(str(token)) == token

    @pytest.mark.parametrize(
        ("token", "reason"),
        [
            ("window:", "a window token reads"),
            ("window:sideways:1:1:host:c::0", "a window token reads"),
            ("window:oldest:x:1:host:c::0", "a window token reads"),
            ("window:oldest:1:x:host:c::0", "a window token reads"),
            ("window:oldest:1:1::c::0", "a window token reads"),  # no path
            ("window:oldest:1:1:host:::0", "a window token reads"),  # no cursor
            ("window:oldest:1:1:host:c:", "a window token reads"),  # no skip
            (
                "window:oldest:1:1:host:c::x",
                "a window token reads",
            ),  # skip not a number
            ("window:oldest:1:1:host:c::\u00b2", "a window token reads"),  # not decimal
            (
                "window:oldest:1:1:host:c:d:0",
                "a window token reads",
            ),  # oldest-first has no before
            (
                "window:newest:1:1:host:c::0",
                "a window token reads",
            ),  # neither a before nor a position
            ("window:newest:1:1:host:c:d:3", "a window token reads"),  # both
            ("window:oldest:99999999999999999999:1:host:c::0", "out of range"),
        ],
    )
    def test_a_malformed_token_is_refused(self, token: str, reason: str):
        # Each differs from a token that parses by this one defect.
        assert parse_window_token("window:oldest:1:1:host:c::0")
        with pytest.raises(ValueError, match=reason):
            parse_window_token(token)

    def test_a_string_that_is_no_window_token_is_refused(self):
        with pytest.raises(ValueError, match="not a window token"):
            parse_window_token("cursor:c:0")


class TestTheFakeMatchesGatewayd:
    """Each Range shape the code sends, answered as journal-gatewayd.c would.

    Derived from systemd v256 ``request_reader_entries`` and ``request_handler
    _entries`` (seek, then ``sd_journal_previous_skip(-n_skip + 1)`` or
    ``sd_journal_next_skip(n_skip + 1)``), and the v258 guard against a skip
    past the end. Keeps the fake honest when it changes.
    """

    @pytest.mark.parametrize(
        ("range_", "answer"),
        [
            # no cursor, negative skip: from the tail, the (k+1)-th newest
            ("entries=:-1:2", ["m1", "m0"]),
            ("entries=:-4:2", ["m4", "m3"]),
            # a skip of 0, "-0" included, seeks the head
            ("entries=:-0:1", ["m4"]),
            ("entries=:0:2", ["m4", "m3"]),
            # past the head: clamped to the head in every version
            ("entries=:-9:1", ["m4"]),
            ("entries=<2>:-9:1", ["m4"]),
            # from a cursor
            ("entries=<2>:0:1", ["m2"]),
            ("entries=<2>:-1:1", ["m3"]),
            ("entries=<2>:2:1", ["m0"]),
            # a window after every entry is empty in every version
            (f"realtime={at(-1)}::0:1", []),
        ],
    )
    def test_each_range(self, range_: str, answer: list[str], gatewayd: FakeGatewayd):
        gatewayd.load(per_minute(5))  # m4 (head) ... m0 (newest)
        range_ = range_.replace("<2>", gatewayd.cursor(2))
        assert gatewayd.answer(range_).splitlines() == answer

    @pytest.mark.parametrize(
        ("range_", "answer_before_258"),
        [
            ("entries=<2>:3:1", ["m0"]),
            ("entries=:7:1", ["m0"]),
            (f"realtime={at(2)}::3:1", ["m0"]),
        ],
    )
    @both_gatewayds
    def test_a_skip_past_the_end_is_answered_both_ways(
        self, range_: str, answer_before_258: list[str], gatewayd: FakeGatewayd
    ):
        gatewayd.load(per_minute(5))
        range_ = range_.replace("<2>", gatewayd.cursor(2))
        answered = gatewayd.answer(range_).splitlines()
        assert answered == (answer_before_258 if gatewayd.clamps else [])


@pytest.mark.usefixtures("clock")
class TestReadWindow:
    async def test_the_journal_returns_exactly_the_window(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(600))
        read = await read_window(in_app_client, "supervisor", NOW - timedelta(hours=2))
        assert read.complete
        assert read.lines == [f"m{m}" for m in range(120, -1, -1)]
        assert len(gatewayd.ranges) == 1  # a short window needs no probe

    @both_gatewayds
    async def test_a_multi_line_entry_at_the_cap_is_not_mistaken_for_overflow(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        # 5 entries, 7 lines: the line count reaches the cap, the entries do not.
        gatewayd.load(
            [(at(m), [f"m{m}"]) for m in (50, 40, 30, 20)]
            + [(at(10), ["Traceback:", "  File x", "Error"])]
        )
        read = await read_window(
            in_app_client, "core", NOW - timedelta(hours=1), max_entries=5
        )
        assert read.complete
        assert read.lines[-3:] == ["Traceback:", "  File x", "Error"]
        assert read.lines[0] == "m50"  # nothing from before the window

    @both_gatewayds
    async def test_an_oversized_window_returns_its_newest_entries(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(600))
        read = await read_window(
            in_app_client, "host", NOW - timedelta(hours=5), max_entries=100
        )
        assert not read.complete
        assert read.lines == [f"m{m}" for m in range(99, -1, -1)]

    @pytest.mark.parametrize("blank", ["   ", ""])
    @both_gatewayds
    async def test_a_blank_entry_past_the_cap_still_counts_as_more(
        self, blank: str, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        # The probe asks whether an entry exists past the cap. A blank or empty
        # message there is still an entry; judging by its text would call the
        # window whole and drop the newer entries after it.
        gatewayd.load(per_minute(150))
        gatewayd.entries[100] = (gatewayd.entries[100][0], [blank])
        gatewayd.entries[-1] = (gatewayd.entries[-1][0], ["newest needle"])
        read = await read_window(
            in_app_client, "core", NOW - timedelta(hours=5), max_entries=100
        )
        assert not read.complete
        assert read.lines[-1] == "newest needle"

    async def test_an_entry_stamped_in_the_future_does_not_end_the_read(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        # After a clock correction an entry can carry a later time than the ones
        # logged after it. Gatewayd ends a read with an end time at such an
        # entry, so the read sends none, and nothing after it is lost.
        gatewayd.load(per_minute(120))
        gatewayd.entries.insert(60, (at(-60), ["stamped an hour ahead"]))
        read = await read_window(in_app_client, "host", NOW - timedelta(hours=1))
        assert read.complete
        assert "stamped an hour ahead" in read.lines
        assert read.lines[-1] == "m0"
        assert all(re.match(r"^realtime=\d+::", r) for r in gatewayd.ranges)

    async def test_an_empty_window_is_complete_and_empty(
        self, in_app_client: HomeAssistantClient
    ):
        read = await read_window(in_app_client, "dns", NOW - timedelta(hours=1))
        assert read.complete and read.lines == []

    @both_gatewayds
    async def test_a_window_of_exactly_the_cap_is_complete(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(300))
        read = await read_window(
            in_app_client, "core", NOW - timedelta(minutes=99), max_entries=100
        )
        assert read.complete
        assert read.lines == [f"m{m}" for m in range(99, -1, -1)]


class TestRoutes:
    async def test_inside_the_app_the_range_goes_straight_to_supervisor(
        self, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        await read_window(in_app_client, "addons/core_ssh", NOW - timedelta(hours=1))
        seen = gatewayd.requests[0]
        assert seen.path == "/addons/core_ssh/logs"
        assert seen.range is not None and seen.range.startswith("realtime=")
        assert seen.query == {"no_colors": "true"}
        assert seen.authorization == f"Bearer {SUPERVISOR_TOKEN}"

    async def test_outside_the_app_the_range_goes_through_core(
        self, gatewayd: FakeGatewayd, proxied_client: HomeAssistantClient
    ):
        await read_window(proxied_client, "supervisor", NOW - timedelta(hours=1))
        seen = gatewayd.requests[0]
        assert seen.path == "/api/hassio/supervisor/logs"
        assert seen.range is not None and seen.range.startswith("realtime=")
        assert seen.authorization == f"Bearer {proxied_client.token}"

    @pytest.mark.usefixtures("clock")
    async def test_the_first_page_reads_the_same_on_both_routes(
        self, gatewayd: FakeGatewayd, journal_client: HomeAssistantClient
    ):
        gatewayd.load(per_minute(3000))
        out = await ask(journal_client, limit=500, hours_back=2, order="oldest")
        assert gatewayd.ranges[0] == f"realtime={at(120)}::0:500"
        assert out["log"].splitlines() == [f"m{m}" for m in range(120, -1, -1)]
        assert out["has_more"] is False
        assert out["window_start"] == "2026-11-05T10:00:00+00:00"
        assert out["read_at"] == "2026-11-05T12:00:00+00:00"
        assert "window_complete" not in out
        assert "warnings" not in out


class TestTheDeadline:
    """Real time: ``asyncio.timeout`` never fires under a frozen clock."""

    @pytest.mark.parametrize("read", ["window", "newest page", "oldest page"])
    async def test_a_stuck_supervisor_cannot_hang_the_read(
        self, read: str, gatewayd: FakeGatewayd, in_app_client: HomeAssistantClient
    ):
        # Nothing is ever answered, so any read ends at the deadline, however
        # many requests it would have made.
        gatewayd.load(per_minute(20))
        gatewayd.hang = True
        client = HomeAssistantClient(
            base_url=in_app_client.base_url,
            token=in_app_client.token,
            timeout=1,
            verify_ssl=False,
        )
        since = NOW - timedelta(hours=1)
        try:
            with pytest.raises(
                HomeAssistantConnectionError, match=r"(?i)timeout|timed out"
            ):
                if read == "window":
                    await read_logs_window(client, "supervisor", since)
                else:
                    order: Order = "newest" if read == "newest page" else "oldest"
                    await read_logs_page(client, "supervisor", since, 1, order, 5)
        finally:
            await client.close()
