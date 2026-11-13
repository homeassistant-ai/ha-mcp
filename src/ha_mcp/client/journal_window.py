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
A window therefore runs from ``since`` to the end of the journal, and follows
that end, so it includes entries logged while it is read.

Two reads are offered:

- ``read_page`` is one page of the window, oldest-first: the only order the
  journal serves a window in. The first page reads forward from the entry the
  journal selects (``realtime=<since>``), and every later page continues from
  Supervisor's ``X-First-Cursor`` for the page before it
  (``entries=<cursor>:<skip>:<count>``), the way the frontend's log viewer
  loads more lines. The cursor, the log it belongs to and the window's start
  travel in an opaque token, so neither entries logged between calls nor the
  clock moving on can shift a page. A token whose entry has been vacuumed is
  refused (``WindowRestart``): gatewayd would seek on to the next entry left
  and the skip would count from there.
- ``read_window`` is the whole window up to ``MAX_ENTRIES`` entries, for a
  search. Past the cap it reads the newest ``MAX_ENTRIES`` entries of the
  journal instead and reports that not every entry of the window was read.

Whether an entry lies ``k`` entries after a known one is asked with a
one-entry probe (``_entry_after``): past the journal's end gatewayd returns
nothing (systemd 258, and Home Assistant OS through its own patch). An entry
counts when Supervisor sends its ``X-First-Cursor``, or any body at all, never
by the text, since a blank entry prints a blank line.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .._version import is_running_in_addon
from .error_log_journald import (
    JournaldFetch,
    JournaldPage,
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


class WindowRestart(Exception):
    """A page token's entry is gone from the journal; the read must start again."""


@dataclass(frozen=True)
class WindowToken:
    """Where a page read resumes: the read's window, and an entry.

    ``since`` is the window's start as the first page computed it, and
    ``hours_back`` the value it was computed from. ``path`` names the log
    stream (``addons/<slug>`` or a system service), so a token cannot be
    carried to another stream, whose journal its cursor does not belong to.
    The next page starts ``skip`` entries after ``cursor``, the first entry of
    the page before.
    """

    since: datetime
    hours_back: int
    path: str
    cursor: str
    skip: int

    def __str__(self) -> str:
        return (
            f"{WINDOW_TOKEN_PREFIX}{_epoch(self.since)}:{self.hours_back}:"
            f"{self.path}:{self.cursor}:{self.skip}"
        )


def is_window_token(value: object) -> bool:
    """True when ``value`` looks like a token ``read_page`` returned."""
    return isinstance(value, str) and value.startswith(WINDOW_TOKEN_PREFIX)


def parse_window_token(token: str) -> WindowToken:
    """Split a page token; raises ``ValueError`` when it is malformed.

    systemd cursors separate their fields with ``;`` and never contain ``:``,
    nor does a log path (it holds ``/`` only), but the skip is still split off
    from the right.
    """
    if not is_window_token(token):
        raise ValueError("not a window token")
    since, _, rest = token[len(WINDOW_TOKEN_PREFIX) :].partition(":")
    hours_back, _, rest = rest.partition(":")
    path, _, rest = rest.partition(":")
    cursor, _, skip = rest.rpartition(":")
    if (
        not since.isdecimal()
        or not hours_back.isdecimal()
        or not path
        or not cursor
        or not skip.isdecimal()
    ):
        raise ValueError(
            "a window token reads 'window:<since>:<hours_back>:<path>:"
            "<cursor>:<skip>' as returned in 'next_offset'"
        )
    try:
        start = datetime.fromtimestamp(int(since), UTC)
    except (OverflowError, ValueError, OSError) as e:
        raise ValueError(f"a window token's start is out of range: {since}") from e
    return WindowToken(start, int(hours_back), path, cursor, int(skip))


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
    count: int,
    resume: WindowToken | None = None,
    skip: int = 0,
) -> JournalPage:
    """``count`` entries of the window from ``since``, oldest-first.

    The first page is read without ``resume`` and may start ``skip`` entries
    into the window. Each later page passes the token the page before
    returned. A multi-line entry is returned whole, so a page can hold more
    lines than ``count``. Every entry prints at least one line, so fewer lines
    than ``count`` ends the window.

    A first page that skips into the window starts from the window's first
    entry's cursor, so a skip past the window's end finds nothing rather than
    the journal's last entry.
    """
    fetch = _fetcher(client, path)
    if resume is not None:
        await _require_entry(fetch, path, resume.cursor)
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
    token = WindowToken(since, hours_back, path, cursor, count)
    return JournalPage(lines, True, str(token))


def _fetcher(client: Any, path: str) -> JournaldFetch:
    def fetch(range_: str) -> Coroutine[Any, Any, JournaldPage]:
        return _fetch(client, path, range_)

    return fetch


async def _require_entry(fetch: JournaldFetch, path: str, cursor: str) -> None:
    """Refuse a token whose entry has been vacuumed from the journal.

    Gatewayd seeks a missing cursor on to the next entry that is left
    (``sd_journal_seek_cursor``), and a skip would count from there.
    """
    if (await fetch(f"entries={cursor}:0:1")).first_cursor != cursor:
        raise WindowRestart(
            f"The entry this read continues from is no longer in the journal "
            f"of /{path}/logs (it was vacuumed); start a new read"
        )


async def _entry_after(fetch: JournaldFetch, cursor: str, k: int) -> str | None:
    """The cursor of the entry ``k`` (1 or more) entries after ``cursor``, if any."""
    return (await fetch(f"entries={cursor}:{k}:1")).first_cursor


async def read_logs_window(client: Any, path: str, since: datetime) -> JournalWindow:
    """``read_window`` under one deadline for all of its requests."""
    return await _within_timeout(client, path, since, read_window(client, path, since))


async def read_logs_page(
    client: Any,
    path: str,
    since: datetime,
    hours_back: int,
    count: int,
    resume: WindowToken | None = None,
    skip: int = 0,
) -> JournalPage:
    """``read_page`` under one deadline for all of its requests."""
    return await _within_timeout(
        client,
        path,
        since,
        read_page(client, path, since, hours_back, count, resume, skip),
    )


async def _within_timeout[T](
    client: Any,
    path: str,
    since: datetime,
    read: Coroutine[Any, Any, T],
) -> T:
    try:
        async with asyncio.timeout(client.timeout):
            return await read
    except TimeoutError as e:
        raise HomeAssistantConnectionError(
            f"Timeout reading /{path}/logs from {since.isoformat()} "
            f"after {client.timeout}s"
        ) from e
