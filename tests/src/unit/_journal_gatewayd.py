"""A journal served the way systemd-journal-gatewayd serves it, behind Supervisor.

``FakeGatewayd`` is an upstream of the server, not Home Assistant: Supervisor
passes a client's ``Range`` header to gatewayd and sends the first entry's
cursor back as ``X-First-Cursor``. It answers ranges the way
``journal-gatewayd.c`` does: entries in journal order, whose timestamps need
not be in order; seek to a cursor, to an entry stamped at or after ``since``
(by bisection, as ``journal_file_move_to_entry_by_realtime`` does through
``generic_array_bisect``; here over one entry array), to the tail (a negative
skip) or to the head (a skip of 0, ``-0`` included); a negative skip past the
head clamps to the head, a positive one past the end returns nothing (systemd
258, and Home Assistant OS through its own patch of gatewayd); count by ENTRY;
end at the first entry stamped after ``until`` when one is given.

The fixtures put the fake where the server's client reaches Supervisor: at
``SUPERVISOR_BASE_URL`` for the app (``in_app_client``), or at the client's
``/api/hassio/<path>/logs`` path outside the app (``hassio_path_client``);
``journal_client`` is both. The second does not run Core's hassio proxy:
that proxy imports ``aiohasupervisor``, which a unit test does not install, so
the path is a route of the reverse proxy in ``_gateway`` answered by the fake.
"""

from __future__ import annotations

import ast
import asyncio
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web
from freezegun.api import FrozenDateTimeFactory

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_logs import LogTools

if TYPE_CHECKING:
    from ._gateway import Gateway

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
SUPERVISOR_TOKEN = "supervisor-token-for-tests"
_REALTIME = re.compile(r"^realtime=(\d*):(\d*):(-?\d+):(\d+)$")
_ENTRIES = re.compile(r"^entries=([^:]*):(-?\d+):(\d+)$")

type Entry = tuple[int, list[str]]


def at(minutes_ago: int) -> int:
    return int((NOW - timedelta(minutes=minutes_ago)).timestamp())


def per_minute(minutes: int) -> list[Entry]:
    """One single-line entry a minute, the newest at ``NOW``."""
    return [(at(m), [f"m{m}"]) for m in range(minutes - 1, -1, -1)]


@dataclass(frozen=True)
class Seen:
    """One request that reached the fake."""

    path: str
    range: str | None
    authorization: str | None
    query: dict[str, str]


class FakeGatewayd:
    """A journal of ``(epoch seconds, lines)`` entries, in journal order."""

    def __init__(self, entries: list[Entry]) -> None:
        self.entries = entries
        # Every Range answered, and every request that reached ``handle``.
        self.ranges: list[str] = []
        self.requests: list[Seen] = []
        # Runs after each answered request (given its Range), to log entries
        # between requests.
        self.after_each: Callable[[str], None] | None = None
        # Supervisor sends the first entry's cursor whenever it returns one;
        # a proxy that drops the header is simulated by turning this off.
        self.send_cursor = True
        # Answer every request with a 400 and this message, in Supervisor's
        # {"result": "error", "message": ...} envelope.
        self.rejection: str | None = None
        # From this entry on the journal has a new seqnum id, as after a
        # journal reset; its seqnums start again at 1.
        self.reset_at: int | None = None
        # Entries before this index have been vacuumed away.
        self.head = 0
        # Never answer: the Supervisor is stuck.
        self.hang = False

    @classmethod
    def per_minute(cls, minutes: int) -> FakeGatewayd:
        return cls(per_minute(minutes))

    def load(self, entries: list[Entry]) -> FakeGatewayd:
        """Replace the journal's entries."""
        self.entries[:] = entries
        return self

    def log(self, minutes_ago: int, line: str) -> None:
        """Append an entry, as the journal does while pages are read."""
        self.entries.append((at(minutes_ago), [line]))

    def cursor(self, index: int) -> str:
        """A cursor in systemd's format, ``s=;i=;b=;m=;t=;x=``, all lowercase hex.

        ``i`` is the entry's seqnum in its seqnum id ``s``: the journal's write
        order, which restarts under a new id after ``reset_at``. ``m`` (the
        monotonic stamp) is unique here and finds the entry again. ``t`` is the
        realtime, which nothing reads.
        """
        reset = self.reset_at is not None and index >= self.reset_at
        seqnum = index - (self.reset_at or 0) + 1 if reset else index + 1
        seqnum_id = 2 if reset else 1
        stamp = self.entries[index][0] * 1_000_000
        return f"s={seqnum_id:032x};i={seqnum:x};b={7:032x};m={index:x};t={stamp:x};x=0"

    def _index(self, cursor: str) -> int:
        fields = dict(f.split("=", 1) for f in cursor.split(";"))
        return int(fields["m"], 16)

    def _start(self, range_header: str) -> tuple[int, int]:
        """Where gatewayd starts reading for ``range_header``, and how many."""
        if match := _ENTRIES.match(range_header):
            cursor, skip, count = match[1], int(match[2]), int(match[3])
            if cursor:
                # A cursor that was vacuumed seeks on to the next entry left.
                position = max(self._index(cursor), self.head)
            elif skip < 0:
                position = len(self.entries) - 1  # the tail
            else:
                position = self.head  # a skip of 0, "-0" included, seeks the head
            if skip < 0:
                return max(position + skip, self.head), count  # clamped to the head
            return position + skip, count
        match = _REALTIME.match(range_header)
        assert match, f"unexpected Range {range_header!r}"
        since = int(match[1]) if match[1] else 0
        skip, count = int(match[3]), int(match[4])
        assert skip >= 0
        return self.seek(since) + skip, count

    def seek(self, since: float) -> int:
        """Where a ``realtime=<since>`` seek lands, as gatewayd bisects for it.

        The last entry is tested first: when it is stamped before ``since``
        nothing is selected. Otherwise the range narrows to the left while the
        middle entry is stamped at or after ``since``. With stamps out of order
        that is not always the first such entry, as in gatewayd.
        """
        left, right = self.head, len(self.entries)
        if left == right or self.entries[right - 1][0] < since:
            return right
        right -= 1
        while left < right:
            middle = (left + right) // 2
            if self.entries[middle][0] >= since:
                right = middle
            else:
                left = middle + 1
        return left

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

    async def handle(self, request: web.Request) -> web.Response:
        """A ``/<path>/logs`` request, as Supervisor answers it.

        With a ``Range`` header the journal answers it; without one,
        Supervisor serves the newest ``?lines=`` entries (100 by default).
        """
        range_header = request.headers.get("Range")
        self.requests.append(
            Seen(
                request.path,
                range_header,
                request.headers.get("Authorization"),
                dict(request.query),
            )
        )
        if self.hang:
            await asyncio.Event().wait()
        if self.rejection is not None:
            return web.json_response(
                {"result": "error", "message": self.rejection}, status=400
            )
        if range_header is None:
            newest = int(request.query.get("lines", 100))
            tail = self.entries[-newest:]
            return web.Response(text="\n".join(line for _, e in tail for line in e))
        text, cursor = self.reply(range_header)
        headers = {"X-First-Cursor": cursor} if cursor and self.send_cursor else {}
        return web.Response(text=text, headers=headers)


@pytest.fixture
def gatewayd() -> FakeGatewayd:
    """An empty journal."""
    return FakeGatewayd([])


@pytest.fixture
async def in_app_client(
    core_client: HomeAssistantClient,
    gatewayd: FakeGatewayd,
    aiohttp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> HomeAssistantClient:
    """The server's client as the app runs it: Supervisor is reached directly."""
    app = web.Application()
    app.router.add_get("/{path:.+}/logs", gatewayd.handle)
    server = await aiohttp_client(app)
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", SUPERVISOR_TOKEN)
    monkeypatch.setenv("SUPERVISOR_BASE_URL", str(server.make_url("")).rstrip("/"))
    return core_client


@pytest.fixture
async def journal_proxy(gateway: Gateway, gatewayd: FakeGatewayd) -> Gateway:
    """``gateway`` answering the client's ``/api/hassio/<path>/logs`` from the fake journal.

    Takes effect for ``gateway_client`` only when requested before it.
    """
    gateway.routes.append(web.get("/api/hassio/{path:.+}/logs", gatewayd.handle))
    return gateway


@pytest.fixture
async def hassio_path_client(
    journal_proxy: Gateway,
    gateway_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[HomeAssistantClient]:
    """The server's client outside the app, reading ``/api/hassio/<path>/logs``."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    yield gateway_client


@pytest.fixture(params=["in-app", "hassio-path"])
def journal_client(request: pytest.FixtureRequest) -> HomeAssistantClient:
    """The client on each route the journal is reached by."""
    name = "in_app_client" if request.param == "in-app" else "hassio_path_client"
    client: HomeAssistantClient = request.getfixturevalue(name)
    return client


@pytest.fixture
def clock(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    """The clock, stopped at ``NOW``; a test moves it with ``tick``."""
    freezer.move_to(NOW)
    return freezer


async def ask(client: HomeAssistantClient, **overrides: Any) -> dict[str, Any]:
    """``ha_get_logs`` on a system service, ``host`` unless told otherwise."""
    kwargs: dict[str, Any] = {
        "source": "system_service",
        "slug": "host",
        "limit": None,
        "search": None,
        "hours_back": None,
        "entity_id": None,
        "device_id": None,
        "context_id": None,
        "end_time": None,
        "offset": 0,
        "compact": False,
        "level": None,
        "order": None,
    }
    kwargs.update(overrides)
    return await LogTools(client).get_logs(**kwargs)


async def page_through(
    clock: FrozenDateTimeFactory,
    client: HomeAssistantClient,
    limit: int,
    hours_back: int,
    between: Callable[[int], None] | None = None,
) -> list[dict[str, Any]]:
    """Every page of a window, the first read at ``NOW``, each later a minute after the one before.

    ``between(n)`` runs before page ``n`` (from 1), to log entries meanwhile.
    """
    window = {"hours_back": hours_back, "limit": limit}
    clock.move_to(NOW)
    pages = [await ask(client, **window)]
    while pages[-1]["has_more"]:
        assert len(pages) < 100, "paging does not end"
        if between:
            between(len(pages))
        clock.tick(timedelta(minutes=1))
        pages.append(await ask(client, **window, offset=pages[-1]["next_offset"]))
    return pages


def joined(pages: list[dict[str, Any]]) -> list[str]:
    return [line for page in pages for line in page["log"].splitlines()]


def window_oracle(journal: FakeGatewayd, since: datetime) -> list[str]:
    """The window straight from the fake's entries, without any Range.

    Every entry from where gatewayd's seek lands for ``since`` to the end.
    """
    start = journal.seek(since.timestamp())
    return [line for _, entry in journal.entries[start:] for line in entry]


def hinted_call(hint: str) -> dict[str, Any]:
    """The keyword arguments of the ``ha_get_logs(...)`` call a hint prints."""
    call = ast.parse(hint[hint.index("ha_get_logs(") :].strip(), mode="eval").body
    assert isinstance(call, ast.Call)
    return {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords if kw.arg}
