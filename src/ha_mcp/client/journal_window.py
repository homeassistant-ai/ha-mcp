"""Read a time window of a Supervisor journald log, cut by the journal itself.

Supervisor passes a client's ``Range`` header to systemd-journal-gatewayd
unchanged, and gatewayd accepts a time range there:
``realtime=<since>:<until>[:<num_skip>:<num_entries>]``, in seconds since the
epoch. With ``since`` it seeks to the first entry at or after it and reads
forward, skipping and counting by entry. So the window is the journal's own
selection; nothing here parses log text or drops lines.

No ``until`` is sent. Gatewayd ends a read at the first entry stamped after
``until`` rather than skipping it, so after a clock correction one entry stamped
in the future would silently end the read and drop everything logged after it.
A window therefore runs from ``since`` to the end of the journal: oldest-first
follows that end, so it includes entries logged while it is read; newest-first
ends at the tail its first page saw.

Two reads are offered:

- ``read_page`` is one page of the window, oldest-first or newest-first. The
  window's first entry ``F`` is the one the journal selects
  (``realtime=<since>``). Oldest-first, the first page reads forward from
  there and every later page continues from Supervisor's ``X-First-Cursor``
  for the page before it (``entries=<cursor>``), the way the frontend's log
  viewer loads more lines. Newest-first, the first page is the journal's
  tail and every later one the entries just before the oldest of the page
  before; the page that reaches ``F`` is cut there. A page reaches ``F`` when
  its oldest entry is at or before ``F`` in the journal's write order, which
  the seqnum of the two cursors (``s=`` id, ``i=``) gives: the order sd-journal
  itself uses to merge the files of one seqnum id, not the realtime clock
  (``t=`` is never read). Cursors of different ids, or without a seqnum,
  cannot be ordered, and that read counts the window from ``F`` and pages by
  position instead. The cursors, the log they belong to, the window's start
  and the mode travel in an opaque token, so neither entries logged between
  calls nor the clock moving on can shift a page: each is read from an exact
  entry, never by a count from the journal's end.
- ``read_window`` is the whole window up to ``MAX_ENTRIES`` entries, for a
  search. Past the cap it reads the newest ``MAX_ENTRIES`` entries of the
  journal instead and reports that not every entry of the window was read.

Whether an entry lies ``k`` entries after a known one is asked with one-entry
probes (``_entry_after``). Gatewayd before systemd 258 answers a skip past the
journal's end with the last entry; systemd 258, and Home Assistant OS 16 and
later through its own patch, return nothing. So a probe that returns an entry
is checked against the one before it, which only the first kind of answer
repeats. An entry counts when Supervisor sends its ``X-First-Cursor``, or any
body at all, never by the text, since a blank entry prints a blank line.

In both orders the window starts where gatewayd's ``realtime=`` seek lands, as
with ``journalctl --since``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .._version import is_running_in_addon
from .error_log_journald import (
    JournaldFetch,
    JournaldPage,
    cursor_range_header,
    first_cursor,
    range_header,
)
from .rest_client import HomeAssistantAPIError, HomeAssistantConnectionError

# The most entries one search reads. It bounds the memory a search holds and
# keeps the read well inside the client's default 30 s timeout: on HAOS a
# 10,000-entry window read, its probe and the 10,000-entry tail took about 8 s
# together. Page reads are not capped: each reads one page.
MAX_ENTRIES = 10_000

# Nothing in a journal is older than the epoch, and gatewayd takes no negative
# time, so a window reaching further back starts here: the whole journal.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

WINDOW_TOKEN_PREFIX = "window:"

Order = Literal["newest", "oldest"]

_PARAMS = {"no_colors": "true"}


@dataclass(frozen=True)
class JournalWindow:
    """The entries of a time window, and whether all of them were read."""

    lines: list[str]
    complete: bool
    cap: int


@dataclass(frozen=True)
class JournalPage:
    """One page of a time window; ``next_token`` continues it while ``has_more``."""

    lines: list[str]
    has_more: bool
    next_token: str | None = None


@dataclass(frozen=True)
class WindowToken:
    """Where a page read resumes: the read's order and window, and an entry.

    ``since`` is the window's start as the first page computed it, and
    ``hours_back`` the value it was computed from. ``path`` names the log
    stream (``addons/<slug>`` or a system service), so a token cannot be
    carried to another stream, whose journal its cursor does not belong to.

    Oldest-first, the next page starts ``skip`` entries after ``cursor`` (the
    first entry of the page before); ``before`` is empty.

    Newest-first, ``cursor`` is the window's first entry, and the read is in
    one of two modes that never change mid-way. With ``before`` set (cursor
    mode) it is the oldest entry of the page before and ``skip`` is 0: the
    next page is the entries just before it. With ``before`` empty (position
    mode) ``skip`` is the number of entries after ``cursor`` at which the page
    before began: the next page is the entries just ahead of that.
    """

    order: Order
    since: datetime
    hours_back: int
    path: str
    cursor: str
    skip: int
    before: str = ""

    def __str__(self) -> str:
        return (
            f"{WINDOW_TOKEN_PREFIX}{self.order}:{_epoch(self.since)}:"
            f"{self.hours_back}:{self.path}:{self.cursor}:{self.before}:{self.skip}"
        )


def is_window_token(value: object) -> bool:
    """True when ``value`` looks like a token ``read_page`` returned."""
    return isinstance(value, str) and value.startswith(WINDOW_TOKEN_PREFIX)


def parse_window_token(token: str) -> WindowToken:
    """Split a page token; raises ``ValueError`` when it is malformed.

    systemd cursors separate their fields with ``;`` and never contain ``:``,
    nor does a log path (it holds ``/`` only), but the last two fields are
    still split off from the right.
    """
    if not is_window_token(token):
        raise ValueError("not a window token")
    order, _, rest = token[len(WINDOW_TOKEN_PREFIX) :].partition(":")
    since, _, rest = rest.partition(":")
    hours_back, _, rest = rest.partition(":")
    path, _, rest = rest.partition(":")
    rest, _, skip = rest.rpartition(":")
    cursor, _, before = rest.rpartition(":")
    if (
        order not in ("newest", "oldest")
        or not since.isdecimal()
        or not hours_back.isdecimal()
        or not path
        or not cursor
        or not skip.isdecimal()
        # Oldest-first has no ``before``; newest-first has exactly one of
        # ``before`` (and skip 0) and a position (skip 1 or more).
        or (order == "oldest" and before)
        or (order == "newest" and bool(before) == (int(skip) > 0))
    ):
        raise ValueError(
            "a window token reads 'window:<order>:<since>:<hours_back>:<path>:"
            "<cursor>:<before>:<skip>' as returned in 'next_offset'"
        )
    try:
        start = datetime.fromtimestamp(int(since), UTC)
    except (OverflowError, ValueError, OSError) as e:
        raise ValueError(f"a window token's start is out of range: {since}") from e
    return WindowToken(
        "newest" if order == "newest" else "oldest",
        start,
        int(hours_back),
        path,
        cursor,
        int(skip),
        before,
    )


def window_start(until: datetime, hours_back: int) -> datetime:
    """``until`` minus ``hours_back`` hours, in whole seconds, no earlier than the epoch.

    Any ``hours_back`` is accepted: one reaching before 1970, or past what
    ``datetime`` can hold, means the whole journal. The start is cut to the
    whole second gatewayd seeks to, so a token carrying it names the same
    start as the first page.
    """
    try:
        start = max(until - timedelta(hours=hours_back), EPOCH)
    except OverflowError:
        return EPOCH
    return start.replace(microsecond=0)


def _epoch(moment: datetime) -> str:
    """Whole seconds since the epoch, gatewayd's default unit for ``realtime=``."""
    return str(int(moment.timestamp()))


def window_range(since: datetime, skip: int, count: int) -> str:
    """``count`` entries from the ``skip``-th entry at or after ``since``.

    ``num_entries`` is always given: systemd 256 answers 400 to a Range whose
    count is empty (systemd#37172, made optional again by systemd#37883).
    """
    return f"realtime={_epoch(since)}::{skip}:{count}"


async def _fetch(client: Any, path: str, range_header: str) -> JournaldPage:
    """One ``/<path>/logs`` read with ``range_header``, direct or via Core's proxy.

    ``path`` is ``addons/<slug>`` for an app or a system-service name. Core's
    hassio proxy forwards ``Range`` and Supervisor's ``X-First-Cursor`` on its
    log paths.
    """
    headers = {"Accept": "text/plain", "Range": range_header}
    if is_running_in_addon():
        response = await client._supervisor_logs_response(path, headers, dict(_PARAMS))
    else:
        response = await client._raw_request(
            "GET", f"/hassio/{path}/logs", headers=headers, params=dict(_PARAMS)
        )
    return JournaldPage(str(response.text), first_cursor(response))


def _holds_an_entry(page: JournaldPage) -> bool:
    return page.first_cursor is not None or page.text != ""


def _cursor_of(page: JournaldPage, path: str) -> str:
    """The page's first cursor, which paging cannot do without."""
    if not page.first_cursor:
        raise HomeAssistantAPIError(
            f"Supervisor sent no X-First-Cursor for /{path}/logs, so the "
            "window cannot be paged from an exact entry"
        )
    return page.first_cursor


async def read_window(
    client: Any,
    path: str,
    since: datetime,
    max_entries: int | None = None,
) -> JournalWindow:
    """The entries of ``/<path>/logs`` from ``since`` to the end of the journal.

    Every entry prints at least one line, so fewer lines than ``max_entries``
    means the window is whole and needs no probe. An entry logged between the
    read and the probe can make the probe report more than there was; that
    errs towards calling a window incomplete, never the other way.
    """
    if max_entries is None:
        max_entries = MAX_ENTRIES
    page = await _fetch(client, path, window_range(since, 0, max_entries))
    lines = page.text.splitlines()
    if len(lines) < max_entries:
        return JournalWindow(lines, True, max_entries)
    first = _cursor_of(page, path)
    if await _entry_after(_fetcher(client, path), first, max_entries) is None:
        return JournalWindow(lines, True, max_entries)
    page = await _fetch(client, path, range_header(max_entries))
    return JournalWindow(page.text.splitlines(), False, max_entries)


async def read_page(
    client: Any,
    path: str,
    since: datetime,
    hours_back: int,
    order: Order,
    count: int,
    resume: WindowToken | None = None,
    skip: int = 0,
) -> JournalPage:
    """``count`` entries of the window from ``since``, in ``order``.

    The first page is read without ``resume``; oldest-first it may start
    ``skip`` entries into the window. Each later page passes the token the
    page before returned. A multi-line entry is returned whole, so a page can
    hold more lines than ``count``; lines are in journal order.
    """
    fetch = _fetcher(client, path)
    if order == "oldest":
        return await _oldest_page(fetch, path, since, hours_back, count, resume, skip)
    return await _newest_page(fetch, path, since, hours_back, count, resume)


def _fetcher(client: Any, path: str) -> JournaldFetch:
    def fetch(range_: str) -> Coroutine[Any, Any, JournaldPage]:
        return _fetch(client, path, range_)

    return fetch


async def _entry_after(
    fetch: JournaldFetch, cursor: str, k: int, at_head: bool = True
) -> str | None:
    """The cursor of the entry ``k`` (1 or more) entries after ``cursor``, if any.

    Past the journal's end, gatewayd before systemd 258 answers with the last
    entry rather than nothing, ``j < k`` entries after ``cursor``. The answer
    is checked by reading backwards from it, which entries logged meanwhile
    cannot change (they are appended), whereas a second forward probe could
    land on a newer last entry: the entry ``k`` back must be ``cursor`` and the
    one ``k - 1`` back must not be. Only ``j == k`` passes both; a backward
    skip past the head clamps to the head, which a cursor at the head would
    otherwise mistake for itself. A caller that knows ``cursor`` is not the
    head passes ``at_head=False``, which drops the one-nearer check.
    """
    probe = (await fetch(f"entries={cursor}:{k}:1")).first_cursor
    if not probe or probe == cursor:
        return None
    if k == 1:
        # One entry on, a skip past the end can only have answered with the
        # last entry, which is ``cursor`` itself and was refused above.
        return probe
    if at_head:
        nearer = await fetch(f"entries={probe}:{1 - k}:1")
        if nearer.first_cursor == cursor:
            return None
    back = await fetch(f"entries={probe}:{-k}:1")
    return probe if back.first_cursor == cursor else None


async def _oldest_page(
    fetch: JournaldFetch,
    path: str,
    since: datetime,
    hours_back: int,
    count: int,
    resume: WindowToken | None,
    skip: int,
) -> JournalPage:
    """Every entry prints at least one line, so fewer lines than ``count`` ends the window.

    A first page that skips into the window starts from the window's first
    entry's cursor, so a skip past the window's end finds nothing rather than
    the journal's last entry.
    """
    if resume is not None:
        page = await fetch(f"entries={resume.cursor}:{resume.skip}:{count}")
    elif skip:
        first_page = await fetch(window_range(since, 0, 1))
        if not _holds_an_entry(first_page):
            return JournalPage([], False)
        start = await _entry_after(fetch, _cursor_of(first_page, path), skip)
        if not start:
            return JournalPage([], False)
        page = await fetch(f"entries={start}:0:{count}")
    else:
        page = await fetch(window_range(since, 0, count))
    lines = page.text.splitlines()
    if len(lines) < count:
        return JournalPage(lines, False)
    cursor = _cursor_of(page, path)
    if await _entry_after(fetch, cursor, count) is None:
        return JournalPage(lines, False)
    # Entries logged since the page was read can make the probe find one past
    # a page that held fewer than ``count`` entries (multi-line entries reach
    # the line count first). The token skips ``count`` entries, so the page is
    # read again now that ``count`` entries are known to follow its first.
    lines = (await fetch(f"entries={cursor}:0:{count}")).text.splitlines()
    token = WindowToken("oldest", since, hours_back, path, cursor, count)
    return JournalPage(lines, True, str(token))


async def _window_size(fetch: JournaldFetch, first: str, start: int) -> int:
    """How many entries run from ``first`` to the journal's end, ``first`` included.

    Doubling the distance from ``start`` (the page size, which the first read
    has just shown the window to reach) finds one that no entry lies at, and
    bisecting narrows it, so a window of ``n`` entries costs about
    ``2 * log2(n / start)`` probes. Entries logged meanwhile are appended
    after those counted, so positions from ``first`` stay valid.
    """
    head = (await fetch(f"entries={first}:-1:1")).first_cursor
    at_head = head in (None, first)

    async def exists(k: int) -> bool:
        return await _entry_after(fetch, first, k, at_head) is not None

    present, absent = 0, start
    while await exists(absent):
        present, absent = absent, absent * 2
    if absent == start and start > 1:
        # A window of exactly ``start`` entries is the common short case.
        if await exists(start - 1):
            present = start - 1
    while absent - present > 1:
        middle = (present + absent) // 2
        if await exists(middle):
            present = middle
        else:
            absent = middle
    return present + 1


def _seqnum(cursor: str) -> tuple[str, int] | None:
    """The seqnum id and seqnum a systemd cursor names (``s=``, ``i=``), if any."""
    fields = dict(f.partition("=")[::2] for f in cursor.split(";"))
    try:
        return fields["s"], int(fields["i"], 16)
    except (KeyError, ValueError):
        return None


def _reaches(oldest: str, first: str, *, older_id_reaches: bool) -> bool | None:
    """Whether ``oldest`` is at or before ``first`` in the journal; ``None`` if unknown.

    Entries of one seqnum id are numbered in the order they were written,
    which is what sd-journal compares when it merges journal files
    (``journal_file_compare_locations``). It is not the realtime clock, which
    steps and leaves stamps out of order, so ``t=`` is never read. A different
    id is not comparable, except that ids only change forwards: when ``first``
    and the entry after ``oldest`` share an id, a different one is older.
    """
    a, b = _seqnum(oldest), _seqnum(first)
    if a is None or b is None:
        return None
    if a[0] != b[0]:
        return True if older_id_reaches else None
    return a[1] <= b[1]


async def _newest_entry(fetch: JournaldFetch) -> str | None:
    """The cursor of the journal's newest entry.

    Gatewayd reads ``entries=:-<k>:...`` from the (k+1)-th newest entry and
    ``-0`` is a skip of 0, which seeks the head, so the newest is reached from
    the second newest. A one-entry journal answers with that entry itself, or
    with nothing after it.
    """
    pair = await fetch("entries=:-1:2")
    if not pair.first_cursor:
        return None
    after = await fetch(f"entries={pair.first_cursor}:1:1")
    if after.first_cursor in (None, pair.first_cursor):
        return pair.first_cursor
    return after.first_cursor


def _no_order(path: str) -> HomeAssistantAPIError:
    return HomeAssistantAPIError(
        f"A journal cursor for /{path}/logs carries no seqnum, so the window's "
        "start cannot be found"
    )


async def _newest_page(
    fetch: JournaldFetch,
    path: str,
    since: datetime,
    hours_back: int,
    count: int,
    resume: WindowToken | None,
) -> JournalPage:
    """The window's last ``count`` entries, then the ones before each page.

    The window starts at its first entry ``F``, as the journal selects it
    (``realtime=``). The first page is the journal's tail; each later page is
    the ``count`` entries just before the oldest entry of the page before, so
    entries logged later cannot move a page. A page reaches ``F`` when its
    oldest entry is at or before ``F`` in the journal's write order (see
    ``_reaches``), and that page is cut at ``F``.

    A window of fewer lines than ``count`` is its first read. Cursors that
    cannot be ordered (a journal reset gives them different seqnum ids, or a
    cursor carries no seqnum) read the window by position from ``F`` instead,
    counting its entries once. The token records the mode, so a read never
    changes it.
    """
    if resume is None:
        first_page = await fetch(window_range(since, 0, count))
        if not _holds_an_entry(first_page):
            return JournalPage([], False)
        lines = first_page.text.splitlines()
        if len(lines) < count:
            # Every entry prints a line: fewer than ``count`` is the whole window.
            return JournalPage(lines, False)
        first = _cursor_of(first_page, path)
        return await _first_newest_page(fetch, path, since, hours_back, count, first)
    if resume.before:
        return await _earlier_page(fetch, path, since, hours_back, count, resume)
    return await _position_page(
        fetch, path, since, hours_back, count, resume.cursor, resume.skip
    )


async def _first_newest_page(
    fetch: JournaldFetch,
    path: str,
    since: datetime,
    hours_back: int,
    count: int,
    first: str,
) -> JournalPage:
    if count == 1:
        newest = await _newest_entry(fetch)
        block = await fetch(f"entries={newest}:0:1") if newest else None
    else:
        block = await fetch(range_header(count))
    if block is None or not _holds_an_entry(block):
        return JournalPage([], False)
    oldest = _cursor_of(block, path)
    reaches = _reaches(oldest, first, older_id_reaches=False)
    if reaches is None:
        end = await _window_size(fetch, first, count)
        return await _position_page(fetch, path, since, hours_back, count, first, end)
    if reaches:
        # The window holds ``count`` entries or fewer, though its lines reach
        # ``count`` (multi-line entries).
        size = await _window_size(fetch, first, count)
        if size > count:
            # Entries logged since the tail was read: page the grown window.
            return await _position_page(
                fetch, path, since, hours_back, count, first, size
            )
        return JournalPage(
            (await fetch(f"entries={first}:0:{size}")).text.splitlines(), False
        )
    token = WindowToken("newest", since, hours_back, path, first, 0, oldest)
    return JournalPage(block.text.splitlines(), True, str(token))


async def _earlier_page(
    fetch: JournaldFetch,
    path: str,
    since: datetime,
    hours_back: int,
    count: int,
    resume: WindowToken,
) -> JournalPage:
    """The ``count`` entries before ``resume.before``, cut at the window's first."""
    first, later = resume.cursor, resume.before
    if (await fetch(f"entries={first}:0:1")).first_cursor != first:
        # Gatewayd would read from the journal's head instead, and no page
        # would ever reach the window's first entry.
        raise HomeAssistantAPIError(
            f"The first entry of the window is no longer in the journal of "
            f"/{path}/logs (it was vacuumed); start a new read"
        )
    block = await fetch(cursor_range_header(later, 0, count))
    if not _holds_an_entry(block):
        return JournalPage([], False)
    oldest = _cursor_of(block, path)
    before, boundary = _seqnum(oldest), _seqnum(later)
    if before is None or boundary is None:
        raise _no_order(path)
    if before[0] == boundary[0] and before[1] >= boundary[1]:
        raise HomeAssistantAPIError(
            f"The journal of /{path}/logs changed under this read (a page did "
            "not end before the one it follows); start a new read"
        )
    reaches = _reaches(oldest, first, older_id_reaches=True)
    if reaches is None:
        raise _no_order(path)
    if not reaches:
        token = WindowToken("newest", since, hours_back, path, first, 0, oldest)
        return JournalPage(block.text.splitlines(), True, str(token))
    # A different seqnum id than the window's first entry is taken as an older
    # one (a reset before it); the check below is what proves it.
    size = await _entries_between(fetch, path, first, later, count)
    if (await fetch(f"entries={first}:{size}:1")).first_cursor != later:
        raise HomeAssistantAPIError(
            f"The journal of /{path}/logs holds entries of another seqnum id "
            "inside this window, so a newest-first read cannot find where it "
            "starts; read it with order='oldest'"
        )
    cut = await fetch(f"entries={first}:0:{size}")
    return JournalPage(cut.text.splitlines(), False)


async def _entries_between(
    fetch: JournaldFetch, path: str, first: str, later: str, count: int
) -> int:
    """How many entries run from ``first`` up to, not including, ``later``.

    At most ``count``. The entry ``j`` after ``first`` is at or after
    ``later`` once ``j`` reaches that number (past the journal's end it is
    nothing, or the last entry, which is after ``later`` too), so bisecting
    ``j`` finds it with one-entry probes.
    """
    boundary = _seqnum(later)
    if boundary is None:
        raise _no_order(path)

    async def at_or_after(j: int) -> bool:
        probe = (await fetch(f"entries={first}:{j}:1")).first_cursor
        if not probe:
            return True
        found = _seqnum(probe)
        if found is None:
            raise _no_order(path)
        return found[0] != boundary[0] or found[1] >= boundary[1]

    low, high = 0, count
    while high - low > 1:
        middle = (low + high) // 2
        if await at_or_after(middle):
            high = middle
        else:
            low = middle
    return high


async def _position_page(
    fetch: JournaldFetch,
    path: str,
    since: datetime,
    hours_back: int,
    count: int,
    first: str,
    end: int,
) -> JournalPage:
    """The entries from ``end - count`` up to position ``end``, counted from ``first``."""
    start = max(end - count, 0)
    page = await fetch(f"entries={first}:{start}:{end - start}")
    lines = page.text.splitlines()
    if start == 0:
        return JournalPage(lines, False)
    token = WindowToken("newest", since, hours_back, path, first, start)
    return JournalPage(lines, True, str(token))


async def read_logs_window(client: Any, path: str, since: datetime) -> JournalWindow:
    """``read_window`` under one deadline for all of its requests."""
    return await _within_timeout(client, path, since, read_window(client, path, since))


async def read_logs_page(
    client: Any,
    path: str,
    since: datetime,
    hours_back: int,
    order: Order,
    count: int,
    resume: WindowToken | None = None,
    skip: int = 0,
) -> JournalPage:
    """``read_page`` under one deadline for all of its requests."""
    return await _within_timeout(
        client,
        path,
        since,
        read_page(client, path, since, hours_back, order, count, resume, skip),
        " (a newest-first read of a large window can be slow; order='oldest' "
        "reads it without finding its start)"
        if order == "newest"
        else "",
    )


async def _within_timeout[T](
    client: Any,
    path: str,
    since: datetime,
    read: Coroutine[Any, Any, T],
    hint: str = "",
) -> T:
    try:
        async with asyncio.timeout(client.timeout):
            return await read
    except TimeoutError as e:
        raise HomeAssistantConnectionError(
            f"Timeout reading /{path}/logs from {since.isoformat()} "
            f"after {client.timeout}s{hint}"
        ) from e
