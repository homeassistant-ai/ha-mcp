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
A window therefore runs from ``since`` to the end of the journal, including
entries logged while it is read.

Two reads are offered:

- ``read_page`` is one page of the window, oldest-first or newest-first. The
  first page starts at the window's first entry (``realtime=``) or at the
  journal's newest (``entries=:-N:N``); every later page continues from
  Supervisor's ``X-First-Cursor`` for the page before it (``entries=<cursor>``),
  the way the frontend's log viewer loads more lines. The cursor and the
  window's start travel in an opaque token, so neither entries logged between
  calls nor the clock moving on can shift a page: each continues from an exact
  entry. A newest-first read ends at the window's start, which it finds from
  the realtime stamp each systemd cursor carries (``t=``).
- ``read_window`` is the whole window up to ``MAX_ENTRIES`` entries, for a
  search. Past the cap it reads the newest ``MAX_ENTRIES`` entries of the
  journal instead and reports that not every entry of the window was read.

Whether an entry lies ``k`` entries after a known one is asked with one-entry
probes (``_entry_after``). Gatewayd answers a skip past the journal's end in
two ways: systemd 258, and Home Assistant OS 16 and later through its own
patch, return nothing; earlier versions, which Supervised installs run, return
the last entry instead. So a probe that returns an entry is checked against
the one before it, which only the second kind of answer repeats. An entry
counts when Supervisor sends its ``X-First-Cursor``, or any body at all, never
by the text, since a blank entry prints a blank line.

The journal is in the order entries were written, and their realtime stamps
need not rise with it: a clock stepped at boot or by NTP leaves entries stamped
out of order. Oldest-first, the window starts where gatewayd's ``realtime=``
seek puts it, as with ``journalctl --since``. Newest-first, the window ends at
the first two consecutive entries stamped before its start, so one entry
stamped early does not end the read; the last page is cut where the stamps
cross the start.
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
    entries_before_resume,
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
    ``hours_back`` the value it was computed from. Oldest-first, the next page
    starts ``skip`` entries after ``cursor`` (the first entry of the page
    before). Newest-first, it ends just before ``cursor`` (the oldest entry of
    the page before), and ``skip`` is 0.
    """

    order: Order
    since: datetime
    hours_back: int
    cursor: str
    skip: int

    def __str__(self) -> str:
        return (
            f"{WINDOW_TOKEN_PREFIX}{self.order}:{_epoch(self.since)}:"
            f"{self.hours_back}:{self.cursor}:{self.skip}"
        )


def is_window_token(value: object) -> bool:
    """True when ``value`` looks like a token ``read_page`` returned."""
    return isinstance(value, str) and value.startswith(WINDOW_TOKEN_PREFIX)


def parse_window_token(token: str) -> WindowToken:
    """Split a page token; raises ``ValueError`` when it is malformed.

    systemd cursors separate their fields with ``;`` and never contain ``:``,
    but the skip is still split off from the right.
    """
    if not is_window_token(token):
        raise ValueError("not a window token")
    order, _, rest = token[len(WINDOW_TOKEN_PREFIX) :].partition(":")
    since, _, rest = rest.partition(":")
    hours_back, _, rest = rest.partition(":")
    cursor, separator, skip = rest.rpartition(":")
    if (
        order not in ("newest", "oldest")
        or not since.isdigit()
        or not hours_back.isdigit()
        or not separator
        or not cursor
        or not skip.isdigit()
    ):
        raise ValueError(
            "a window token reads "
            "'window:<order>:<since>:<hours_back>:<cursor>:<skip>' as returned "
            "in 'next_offset'"
        )
    try:
        start = datetime.fromtimestamp(int(since), UTC)
    except (OverflowError, ValueError, OSError) as e:
        raise ValueError(f"a window token's start is out of range: {since}") from e
    return WindowToken(
        "newest" if order == "newest" else "oldest",
        start,
        int(hours_back),
        cursor,
        int(skip),
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


def cursor_time(cursor: str) -> datetime | None:
    """The realtime stamp of the entry ``cursor`` names, from its ``t=`` field.

    A systemd cursor is ``s=…;i=…;b=…;m=…;t=…;x=…``, ``t`` being the entry's
    realtime in hexadecimal microseconds: the stamp gatewayd's ``realtime=``
    seek compares.
    """
    for field in cursor.split(";"):
        key, _, value = field.partition("=")
        if key == "t":
            try:
                return EPOCH + timedelta(microseconds=int(value, 16))
            except (ValueError, OverflowError):
                return None
    return None


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


async def _entry_after(fetch: JournaldFetch, cursor: str, k: int) -> str | None:
    """The cursor of the entry ``k`` (1 or more) entries after ``cursor``, if any.

    Past the journal's end, gatewayd before systemd 258 answers with the last
    entry rather than nothing, ``j < k`` entries after ``cursor``. The answer
    is checked by reading backwards from it, which entries logged meanwhile
    cannot change (they are appended), whereas a second forward probe could
    land on a newer last entry: the entry ``k`` back must be ``cursor`` and the
    one ``k - 1`` back must not be. Only ``j == k`` passes both; a backward
    skip past the head clamps to the head, which a cursor at the head would
    otherwise mistake for itself.
    """
    probe = (await fetch(f"entries={cursor}:{k}:1")).first_cursor
    if not probe or probe == cursor:
        return None
    nearer = await fetch(f"entries={probe}:{1 - k}:1")
    if nearer.first_cursor == cursor:
        return None
    back = await fetch(f"entries={probe}:{-k}:1")
    return probe if back.first_cursor == cursor else None


async def _entry_before(fetch: JournaldFetch, cursor: str) -> str | None:
    """The cursor of the entry just before ``cursor``; ``None`` at the head.

    A skip past the head clamps to the head in every version, so the head
    answers with itself.
    """
    probe = await fetch(cursor_range_header(cursor, 0, 1))
    if not probe.first_cursor or probe.first_cursor == cursor:
        return None
    return probe.first_cursor


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
        first = (await fetch(window_range(since, 0, 1))).first_cursor
        start = first and await _entry_after(fetch, first, skip)
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
    token = WindowToken("oldest", since, hours_back, cursor, count)
    return JournalPage(lines, True, str(token))


async def _newest_page(
    fetch: JournaldFetch,
    path: str,
    since: datetime,
    hours_back: int,
    count: int,
    resume: WindowToken | None,
) -> JournalPage:
    """``count`` entries back from the journal's end, or from ``resume``.

    The block is the ``count`` entries just behind the resume point, read the
    way the error log's journald paging reads one. When fewer precede it,
    gatewayd clamps the block's start to the journal's head and reads on past
    the resume point, so the block is cut to the entries before it. The window
    ends at the first two consecutive entries stamped before ``since``: a
    block whose oldest entry and the one before it are both that old holds the
    window's last entries, and the read ends there.
    """
    anchor = (resume.cursor, resume.skip) if resume else await _end_anchor(fetch)
    if anchor is None:
        return JournalPage([], False)
    cursor, skip = anchor
    block = await fetch(cursor_range_header(cursor, skip, count))
    if not _holds_an_entry(block):
        return JournalPage([], False)
    oldest = _cursor_of(block, path)
    older = await _entry_before(fetch, oldest)
    if _stamp(oldest, path) < since and (older is None or _stamp(older, path) < since):
        return JournalPage(await _window_part(fetch, path, since, count, anchor), False)
    lines = block.text.splitlines()
    if older is None:
        # The block starts at the head: keep only the entries before resume.
        # The block holds one at least, so the resume point is never probed.
        before = await entries_before_resume(cursor, skip, count, oldest, fetch, 1)
        if before < count:
            lines = (await fetch(f"entries={oldest}:0:{before}")).text.splitlines()
        return JournalPage(lines, False)
    if _stamp(older, path) < since:
        oldest_two = await _entry_before(fetch, older)
        if oldest_two is None or _stamp(oldest_two, path) < since:
            return JournalPage(lines, False)
    token = WindowToken("newest", since, hours_back, oldest, 0)
    return JournalPage(lines, True, str(token))


async def _end_anchor(fetch: JournaldFetch) -> tuple[str, int] | None:
    """A cursor and skip resuming just past the journal's newest entry.

    The newest entry cannot be addressed from the tail alone: gatewayd reads
    ``entries=:-<k>:…`` from the (k+1)-th newest entry, and ``-0`` is a skip of
    0, which seeks to the head. So this takes the cursor of the second-newest
    entry and checks whether an entry follows it; in a one-entry journal the
    first read clamps to that entry, and nothing follows it.
    """
    pair = await fetch("entries=:-1:2")
    if not pair.first_cursor:
        return None
    after = await fetch(f"entries={pair.first_cursor}:1:1")
    newest_follows = after.first_cursor not in (None, pair.first_cursor)
    return pair.first_cursor, 2 if newest_follows else 1


def _stamp(cursor: str, path: str) -> datetime:
    stamp = cursor_time(cursor)
    if stamp is None:
        raise HomeAssistantAPIError(
            f"The journal cursor for /{path}/logs carries no realtime stamp, "
            "so the window's start cannot be found"
        )
    return stamp


async def _window_part(
    fetch: JournaldFetch,
    path: str,
    since: datetime,
    count: int,
    anchor: tuple[str, int],
) -> list[str]:
    """The entries of the window among ``count`` back from ``anchor``.

    The ``n``-th entry back is probed alone; at or past the journal's head the
    probe clamps to the head, which is older than ``since`` here. Bisecting
    finds how many entries are in the window, as gatewayd's own ``realtime=``
    seek bisects on time, and those are read in one request. As between pages,
    the window ends at two consecutive entries stamped before ``since``, so an
    entry stamped early is in when the one before it is.
    """
    cursor, skip = anchor

    async def stamped_before(n: int) -> bool:
        probe = await fetch(cursor_range_header(cursor, skip + 1 - n, 1))
        return not probe.first_cursor or _stamp(probe.first_cursor, path) < since

    low, high = 0, count - 1
    while low < high:
        middle = (low + high + 1) // 2
        if await stamped_before(middle) and await stamped_before(middle + 1):
            high = middle - 1
        else:
            low = middle
    if not low:
        return []
    page = await fetch(cursor_range_header(cursor, skip, low))
    return page.text.splitlines()


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
    )


async def _within_timeout[T](
    client: Any, path: str, since: datetime, read: Coroutine[Any, Any, T]
) -> T:
    try:
        async with asyncio.timeout(client.timeout):
            return await read
    except TimeoutError as e:
        raise HomeAssistantConnectionError(
            f"Timeout reading /{path}/logs from {since.isoformat()} "
            f"after {client.timeout}s"
        ) from e
