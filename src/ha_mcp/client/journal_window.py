"""Read a time window of a Supervisor journald log, cut by the journal itself.

Supervisor passes a client's ``Range`` header to systemd-journal-gatewayd
unchanged, and gatewayd (systemd 255 and later; HAOS ships 256) accepts a time
range there: ``realtime=<since>:<until>[:<num_skip>:<num_entries>]``, in seconds
since the epoch. With ``since`` it seeks to the first entry at or after it and
reads forward. So the window is the journal's own selection; nothing here
parses timestamps or drops lines.

No ``until`` is sent. Gatewayd ends a read at the first entry stamped after
``until`` rather than skipping it, so after a clock correction one entry stamped
in the future would silently end the read and drop everything logged after it.
A window therefore runs from ``since`` to the end of the journal; ``until`` is
only the moment the read starts, reported as the window's end.

A window is capped at ``MAX_ENTRIES`` entries. A one-entry probe past the cap
says whether the window holds more; when it does, the newest ``MAX_ENTRIES``
entries of the journal are read instead (``entries=:-N:N``, the same tail read
the error log uses), because the tool shows the newest lines of what it reads,
and the read reports itself incomplete.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .._version import is_running_in_addon
from .rest_client import HomeAssistantAPIError, HomeAssistantConnectionError

# #2279 measured one 20,000-entry slice taking 15+ minutes; the live test of
# #2974 read 5,836 host entries in one window without trouble.
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

    ``num_entries`` is always given: from systemd 256 until a 2025 fix,
    gatewayd rejected a Range without it (systemd#37172).
    """
    return f"realtime={_epoch(since)}::{skip}:{count}"


def newest_range(count: int) -> str:
    """The ``count`` newest entries of the journal."""
    return f"entries=:{-(count - 1)}:{count}"


async def _fetch(client: Any, path: str, range_header: str) -> str:
    """One ``/<path>/logs`` read with ``range_header``, direct or via Core's proxy.

    ``path`` is ``addons/<slug>`` for an app or a system-service name. Core's
    hassio proxy forwards ``Range`` on its log paths.
    """
    headers = {"Accept": "text/plain", "Range": range_header}
    if is_running_in_addon():
        response = await client._supervisor_logs_response(path, headers, dict(_PARAMS))
    else:
        response = await client._raw_request(
            "GET", f"/hassio/{path}/logs", headers=headers, params=dict(_PARAMS)
        )
    return str(response.text)


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
    text = await _fetch(client, path, window_range(since, 0, max_entries))
    lines = text.splitlines()
    if len(lines) < max_entries:
        return JournalWindow(lines, True, max_entries)
    beyond = await _fetch(client, path, window_range(since, max_entries, 1))
    if not beyond.strip():
        return JournalWindow(lines, True, max_entries)
    text = await _fetch(client, path, newest_range(max_entries))
    return JournalWindow(text.splitlines(), False, max_entries)


async def read_logs_window(client: Any, path: str, since: datetime) -> JournalWindow:
    """``read_window`` under one deadline for all of its requests."""
    try:
        async with asyncio.timeout(client.timeout):
            return await read_window(client, path, since)
    except TimeoutError as e:
        raise HomeAssistantConnectionError(
            f"Timeout reading /{path}/logs from {since.isoformat()} "
            f"after {client.timeout}s"
        ) from e
    except HomeAssistantAPIError as e:
        # Supervisor reports gatewayd refusing a Range as a 500 (seen live for a
        # negative time); gatewayd itself answers 400.
        if e.status_code in (400, 500):
            raise HomeAssistantAPIError(
                f"{e} -- the journal could not serve the time range. A time "
                "window needs systemd 255 or later on the host (Home Assistant "
                "OS ships 256); without hours_back the newest lines can still "
                "be read",
                status_code=e.status_code,
                response_data=e.response_data,
            ) from e
        raise
