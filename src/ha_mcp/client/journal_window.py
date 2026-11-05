"""Read a Supervisor journald log back to a point in time.

Supervisor addresses its logs by entry count, never by time, so ``hours_back``
is honoured by paging back from the newest entry behind each block's
``X-First-Cursor`` until a block reaches the cutoff. Pages use Supervisor's
verbose format because only it carries the journal's own timestamp on every
entry, whatever the app or service logs in its message.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .._version import is_running_in_addon
from .error_log_journald import (
    JournaldFetch,
    JournaldPage,
    cursor_token,
    first_cursor,
    journald_page,
)
from .rest_client import HomeAssistantConnectionError

# Small blocks: #2279 measured one 20,000-entry slice taking 15+ minutes.
PAGE_LINES = 1000
MAX_PAGES = 10

# journal_verbose_formatter: "<ISO timestamp> <hostname> <identifier>: <message>".
# A multi-line message continues on lines without it.
_JOURNAL_TIMESTAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?[+-]\d{2}:\d{2}) "
)
_VERBOSE_PARAMS = {"no_colors": "true", "verbose": "true"}


@dataclass(frozen=True)
class JournalWindow:
    """The entries read back to a cutoff, and how far the read got."""

    lines: list[str]
    reached_cutoff: bool
    journal_start: bool
    oldest: datetime | None
    pages: int


def entry_time(line: str) -> datetime | None:
    """The journal timestamp a verbose line starts with; None on a continuation."""
    match = _JOURNAL_TIMESTAMP.match(line)
    return datetime.fromisoformat(match.group(1)) if match else None


def oldest_time(lines: list[str]) -> datetime | None:
    """The timestamp of the first entry in ``lines`` that carries one."""
    for line in lines:
        stamp = entry_time(line)
        if stamp is not None:
            return stamp
    return None


def lines_since(lines: list[str], cutoff: datetime) -> list[str]:
    """Keep the entries at or after ``cutoff``, each with its continuation lines.

    Leading lines with no timestamp are the tail of an entry from an earlier
    block, of unknown time, and are dropped.
    """
    kept: list[str] = []
    keep = False
    for line in lines:
        stamp = entry_time(line)
        if stamp is not None:
            keep = stamp >= cutoff
        if keep:
            kept.append(line)
    return kept


async def read_since(
    fetch: JournaldFetch,
    cutoff: datetime,
    page_lines: int = PAGE_LINES,
    max_pages: int = MAX_PAGES,
) -> JournalWindow:
    """Page back from the newest entry until a block reaches ``cutoff``."""
    page = await journald_page(page_lines, 0, fetch)
    collected = page.text.splitlines()
    pages = 1
    while True:
        oldest = oldest_time(collected)
        if oldest is not None and oldest <= cutoff:
            return JournalWindow(collected, True, False, oldest, pages)
        if not page.has_more:
            return JournalWindow(collected, True, True, oldest, pages)
        if pages >= max_pages or not page.first_cursor:
            return JournalWindow(collected, False, False, oldest, pages)
        page = await journald_page(
            page_lines, cursor_token(page.first_cursor, 0), fetch
        )
        collected = page.text.splitlines() + collected
        pages += 1


def supervisor_fetch(client: Any, path: str) -> JournaldFetch:
    """A verbose block fetcher for ``/<path>/logs``, direct or via Core's proxy.

    ``path`` is ``addons/<slug>`` for an app or a system-service name.
    """
    if is_running_in_addon():

        async def direct(range_header: str) -> JournaldPage:
            response = await client._supervisor_logs_response(
                path,
                {"Accept": "text/plain", "Range": range_header},
                dict(_VERBOSE_PARAMS),
            )
            return JournaldPage(response.text, first_cursor(response))

        return direct

    async def proxied(range_header: str) -> JournaldPage:
        response = await client._raw_request(
            "GET",
            f"/hassio/{path}/logs",
            headers={"Accept": "text/plain", "Range": range_header},
            params=dict(_VERBOSE_PARAMS),
        )
        return JournaldPage(response.text, first_cursor(response))

    return proxied


async def read_logs_since(client: Any, path: str, cutoff: datetime) -> JournalWindow:
    """``read_since`` against Supervisor under one deadline for the whole walk.

    ``httpx`` timeouts are per I/O operation and each block costs two requests,
    so only an overall deadline bounds the read (as in ``get_error_log``).
    """
    try:
        async with asyncio.timeout(client.timeout):
            return await read_since(supervisor_fetch(client, path), cutoff)
    except TimeoutError as e:
        raise HomeAssistantConnectionError(
            f"Timeout reading /{path}/logs back to {cutoff.isoformat()} "
            f"after {client.timeout}s"
        ) from e
