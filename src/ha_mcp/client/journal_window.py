"""Read a time window of a Supervisor journald log, cut by the journal itself.

Supervisor passes a client's ``Range`` header to systemd-journal-gatewayd
unchanged, and gatewayd accepts a time range there:
``realtime=<since>:<until>[:<num_skip>:<num_entries>]``, in seconds since the
epoch. With ``since`` it seeks to the first entry at or after it and reads
forward, skipping and counting by entry. So the window is the journal's own
selection; nothing here parses timestamps or drops lines.

No ``until`` is sent. Gatewayd ends a read at the first entry stamped after
``until`` rather than skipping it, so after a clock correction one entry stamped
in the future would silently end the read and drop everything logged after it.
A window therefore runs from ``since`` to the end of the journal, including
entries logged while it is read.

Two reads are offered. ``read_page`` is one page of the window counted from its
first entry, which the journal serves with ``num_skip``/``num_entries``; it
reaches any depth of the window. ``read_window`` is the whole window up to
``MAX_ENTRIES`` entries, for callers that need all of it at once (a search, the
newest lines first). Past the cap it reads the newest ``MAX_ENTRIES`` entries
of the journal instead and reports itself incomplete.

Whether more entries follow a read is asked with a one-entry probe past it. It
counts as an entry when Supervisor sends the entry's ``X-First-Cursor``, or any
body at all, never by the text, since a blank entry prints a blank line.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .._version import is_running_in_addon
from .error_log_journald import first_cursor, range_header
from .rest_client import HomeAssistantConnectionError

# The most entries one whole-window read fetches. A single gatewayd read must
# finish inside the client timeout, and #2279 measured one 20,000-entry slice
# taking 15+ minutes, so the cap is half that. ``read_page`` is not capped: it
# reads one page at a time.
MAX_ENTRIES = 10_000

# Nothing in a journal is older than the epoch, and gatewayd takes no negative
# time, so a window reaching further back starts here: the whole journal.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_PARAMS = {"no_colors": "true"}


@dataclass(frozen=True)
class JournalWindow:
    """The entries of a time window, and whether all of them were read."""

    lines: list[str]
    complete: bool
    cap: int


@dataclass(frozen=True)
class JournalPage:
    """One page of a time window, and whether entries follow it."""

    lines: list[str]
    has_more: bool


def window_start(until: datetime, hours_back: int) -> datetime:
    """``until`` minus ``hours_back`` hours, no earlier than the epoch.

    Any ``hours_back`` is accepted: one reaching before 1970, or past what
    ``datetime`` can hold, means the whole journal.
    """
    try:
        return max(until - timedelta(hours=hours_back), EPOCH)
    except OverflowError:
        return EPOCH


def _epoch(moment: datetime) -> str:
    """Whole seconds since the epoch, gatewayd's default unit for ``realtime=``."""
    return str(int(moment.timestamp()))


def window_range(since: datetime, skip: int, count: int) -> str:
    """``count`` entries from the ``skip``-th entry at or after ``since``.

    ``num_entries`` is always given: systemd 256 answers 400 to a Range whose
    count is empty (systemd#37172, made optional again by systemd#37883).
    """
    return f"realtime={_epoch(since)}::{skip}:{count}"


async def _fetch(client: Any, path: str, range_header: str) -> tuple[str, bool]:
    """One ``/<path>/logs`` read with ``range_header``, direct or via Core's proxy.

    ``path`` is ``addons/<slug>`` for an app or a system-service name. Core's
    hassio proxy forwards ``Range`` and Supervisor's ``X-First-Cursor`` on its
    log paths. Returns the body and whether it holds at least one entry.
    """
    headers = {"Accept": "text/plain", "Range": range_header}
    if is_running_in_addon():
        response = await client._supervisor_logs_response(path, headers, dict(_PARAMS))
    else:
        response = await client._raw_request(
            "GET", f"/hassio/{path}/logs", headers=headers, params=dict(_PARAMS)
        )
    text = str(response.text)
    return text, first_cursor(response) is not None or text != ""


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
    text, _ = await _fetch(client, path, window_range(since, 0, max_entries))
    lines = text.splitlines()
    if len(lines) < max_entries:
        return JournalWindow(lines, True, max_entries)
    _, more = await _fetch(client, path, window_range(since, max_entries, 1))
    if not more:
        return JournalWindow(lines, True, max_entries)
    text, _ = await _fetch(client, path, range_header(max_entries))
    return JournalWindow(text.splitlines(), False, max_entries)


async def read_page(
    client: Any, path: str, since: datetime, skip: int, count: int
) -> JournalPage:
    """``count`` entries of ``/<path>/logs`` from the ``skip``-th at or after ``since``.

    Every entry prints at least one line, so fewer lines than ``count`` means
    the page reached the window's end and needs no probe; a multi-line entry
    is returned whole, so a page can hold more lines than ``count``.
    """
    text, _ = await _fetch(client, path, window_range(since, skip, count))
    lines = text.splitlines()
    if len(lines) < count:
        return JournalPage(lines, False)
    _, more = await _fetch(client, path, window_range(since, skip + count, 1))
    return JournalPage(lines, more)


async def read_logs_window(client: Any, path: str, since: datetime) -> JournalWindow:
    """``read_window`` under one deadline for all of its requests."""
    return await _within_timeout(client, path, since, read_window(client, path, since))


async def read_logs_page(
    client: Any, path: str, since: datetime, skip: int, count: int
) -> JournalPage:
    """``read_page`` under one deadline for both of its requests."""
    return await _within_timeout(
        client, path, since, read_page(client, path, since, skip, count)
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
