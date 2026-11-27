"""What ``HomeAssistantClient.get_error_log`` reads, on each route to Core's log.

Inside the app the client reads Core's journal straight from Supervisor.
Outside it, the client asks Core once whether ``hassio`` is loaded, then reads
the journal through ``/api/hassio/core/logs`` or Core's own ``/api/error_log``
file (``_error_log_routes_core.py``). Every route fetches the caller's window
and nothing more (#2279): an unconditional 20,000-line journald request hung
the call for 15+ minutes on a Supervisor-backed install.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import web

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)

from ._error_log_core import log_file as log_file
from ._error_log_core import serve_log
from ._error_log_routes_core import ErrorLogRoute
from ._error_log_routes_core import error_log_route as error_log_route
from ._error_log_routes_core import journald_route as journald_route
from ._error_log_routes_core import probing_route as probing_route
from ._error_log_routes_core import (
    supervised_journal_route as supervised_journal_route,
)
from ._gateway import Gateway
from ._gateway import gateway as gateway
from ._gateway import gateway_client as gateway_client
from ._journal_gatewayd import gatewayd as gatewayd
from ._journal_gatewayd import hassio_path_client as hassio_path_client
from ._journal_gatewayd import in_app_client as in_app_client
from ._journal_gatewayd import journal_proxy as journal_proxy

LINES = [f"line{i}" for i in range(10)]
# The client's deadline in seconds, shortened so a stalled read ends at once.
DEADLINE = 0.01


class Stall:
    """An upstream that takes a request and never answers it."""

    async def handle(self, request: web.Request) -> web.StreamResponse:
        await asyncio.Event().wait()
        raise AssertionError("a stalled upstream never answers")


@pytest.fixture
def stalled_error_log(gateway: Gateway) -> Gateway:
    """The gateway stalling every ``/api/error_log``; taken before ``gateway_client``."""
    gateway.routes.append(web.get("/api/error_log", Stall().handle))
    return gateway


@pytest.fixture
def stalled_probe(gateway: Gateway) -> Gateway:
    """The gateway stalling every ``/api/config``; taken before ``gateway_client``."""
    gateway.routes.append(web.get("/api/config", Stall().handle))
    return gateway


class TestTheWindow:
    @pytest.mark.parametrize(
        ("lines", "offset", "expected", "has_more"),
        [
            pytest.param(3, 2, LINES[5:8], True, id="ends-offset-lines-back"),
            pytest.param(500, 0, LINES, False, id="larger-than-the-log"),
            pytest.param(5, 5, LINES[:5], False, id="exactly-reaches-the-start"),
            pytest.param(4, 5, LINES[1:5], True, id="one-short-of-the-start"),
            # A journald Range cannot anchor one entry at the newest end.
            pytest.param(1, 0, LINES[8:], True, id="one-line-is-floored-to-two"),
        ],
    )
    async def test_every_route_serves_the_same_slice_of_the_log(
        self,
        error_log_route: ErrorLogRoute,
        lines: int,
        offset: int,
        expected: list[str],
        has_more: bool,
    ) -> None:
        """A caller paging back gets the same lines on every install.

        Off by one at the start either loops forever on an empty page or drops
        the oldest lines.
        """
        await error_log_route.serve(LINES)

        page = await error_log_route.client.get_error_log(lines=lines, offset=offset)

        assert page.text.splitlines() == expected
        assert page.has_more is has_more

    @pytest.mark.parametrize(
        ("lines", "offset", "expected"),
        [
            pytest.param(3, 10, [], id="past-the-start"),
            pytest.param(5, 8, LINES[:2], id="partial-last-page"),
        ],
    )
    async def test_a_container_page_at_the_start_holds_only_what_remains(
        self,
        log_file: Path,
        core_client: HomeAssistantClient,
        lines: int,
        offset: int,
        expected: list[str],
    ) -> None:
        """The page after the last one is empty, not a wrapped-around window."""
        await serve_log(log_file, "".join(f"{line}\n" for line in LINES))

        page = await core_client.get_error_log(lines=lines, offset=offset)

        assert page.text.splitlines() == expected
        assert page.has_more is False


class TestTheJournal:
    async def test_the_window_is_sent_as_a_range_with_no_lines_query(
        self, journald_route: ErrorLogRoute
    ) -> None:
        """Supervisor serves ``?lines=`` instead of the ``Range`` when both are sent."""
        await journald_route.client.get_error_log(lines=100, offset=250)

        (seen,) = journald_route.journal.requests
        assert seen.path.endswith("/core/logs")
        assert seen.range == "entries=:-349:100"
        assert seen.query == {"no_colors": "true"}

    async def test_a_short_window_ends_the_journal_without_a_probe(
        self, journald_route: ErrorLogRoute
    ) -> None:
        """Nothing shortens a window but the journal's start, so short means done."""
        await journald_route.serve(LINES[:2])

        page = await journald_route.client.get_error_log(lines=10)

        assert page.has_more is False
        assert len(journald_route.journal.ranges) == 1

    async def test_a_cursor_token_reads_the_entries_behind_its_resume_point(
        self, journald_route: ErrorLogRoute
    ) -> None:
        """The token's skip is where the page ends, not where it starts."""
        journal = journald_route.journal
        entries = [f"line{i}" for i in range(20)]
        await journald_route.serve(entries)

        page = await journald_route.client.get_error_log(
            lines=3, offset=f"cursor:{journal.cursor(5)}:7"
        )

        assert page.text.splitlines() == entries[9:12]
        assert page.has_more is True
        assert page.first_cursor == journal.cursor(9)

    async def test_a_cursor_page_clamped_to_the_head_stops_at_the_resume_point(
        self, journald_route: ErrorLogRoute
    ) -> None:
        """A block clamped to the journal's head must not run past the resume point.

        The resume point is seven entries in; gatewayd clamps the ten-entry
        block to the head and reads forward past it. Returning that block whole
        would repeat entries the previous page returned and hide none behind.
        """
        journal = journald_route.journal
        entries = [f"line{i}" for i in range(20)]
        await journald_route.serve(entries)

        page = await journald_route.client.get_error_log(
            lines=10, offset=f"cursor:{journal.cursor(2)}:5"
        )

        assert page.text.splitlines() == entries[:7]
        assert page.has_more is False

    async def test_a_cursor_page_before_the_head_returns_nothing(
        self, journald_route: ErrorLogRoute
    ) -> None:
        journal = journald_route.journal
        await journald_route.serve(LINES)

        page = await journald_route.client.get_error_log(
            lines=5, offset=f"cursor:{journal.cursor(0)}:0"
        )

        assert page.text == ""
        assert page.has_more is False


class TestTheInstallClass:
    async def test_the_install_class_is_asked_once_per_client(
        self, probing_route: ErrorLogRoute
    ) -> None:
        """Core's answer, ``hassio`` loaded or not, holds for the client's life.

        The log itself is read anew on every call.
        """
        assert probing_route.gateway is not None
        texts = []
        for line in ("first", "second", "third"):
            await probing_route.serve([line])
            texts.append((await probing_route.client.get_error_log(lines=10)).text)

        assert texts == ["first", "second", "third"]
        assert probing_route.gateway.requests.count("GET /api/config") == 1

    async def test_a_failed_probe_reads_cores_own_log_and_asks_again(
        self,
        log_file: Path,
        gateway_client: HomeAssistantClient,
        gateway: Gateway,
    ) -> None:
        """A Core that fails to say its install class is asked again next call."""
        await serve_log(log_file, "line0\n")
        gateway.outages = [500]

        first = await gateway_client.get_error_log(lines=10)
        second = await gateway_client.get_error_log(lines=10)

        assert first.text == second.text == "line0"
        assert gateway.requests.count("GET /api/config") == 2
        assert gateway.requests.count("GET /api/error_log") == 2


@pytest.mark.timeout(10)
class TestTheDeadline:
    """``httpx.Timeout`` is per I/O operation, so an upstream trickling bytes
    never trips it (#2279); one deadline over the whole call must."""

    async def test_a_stalled_journal_ends_at_the_deadline(
        self, journald_route: ErrorLogRoute, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = journald_route.client
        await journald_route.serve(LINES)
        # The install class is asked here, before anything stalls.
        await client.get_error_log(lines=5)
        journald_route.journal.hang = True
        monkeypatch.setattr(client, "timeout", DEADLINE)

        with pytest.raises(HomeAssistantConnectionError, match=r"after 0\.01s"):
            await client.get_error_log(lines=5)

    async def test_a_stalled_container_log_ends_at_the_deadline(
        self,
        stalled_error_log: Gateway,
        gateway_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The container route transfers the whole file, and is bounded too."""
        # A cursor token is refused once Core says it has no hassio, before the
        # log is read: the install class is known before anything stalls.
        with pytest.raises(HomeAssistantAPIError):
            await gateway_client.get_error_log(lines=5, offset="cursor:c:0")
        monkeypatch.setattr(gateway_client, "timeout", DEADLINE)

        with pytest.raises(HomeAssistantConnectionError, match=r"after 0\.01s"):
            await gateway_client.get_error_log(lines=5)

    async def test_a_stalled_install_class_probe_ends_at_the_deadline(
        self,
        stalled_probe: Gateway,
        gateway_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A connection already open: a 10 ms deadline must land on the stalled
        # read, not on the connection being made.
        await gateway_client.get_states()
        monkeypatch.setattr(gateway_client, "timeout", DEADLINE)

        with pytest.raises(HomeAssistantConnectionError, match=r"after 0\.01s"):
            await gateway_client.get_error_log(lines=5)

        assert "GET /api/error_log" not in stalled_probe.requests
