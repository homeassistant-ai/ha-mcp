"""``hours_back`` page reads of Supervisor logs, on every HAOS lane (#2974).

The window is read from the journal through Supervisor: directly inside the
app, and through Core's hassio proxy for the external, stdio and embedded
servers. Pages after the first are read from an exact journal entry, a cursor
Supervisor sends as ``X-First-Cursor``,
which the proxy has to pass back, so this module runs on every HAOS lane
rather than only the in-app one.

``host`` is used because Supervisor formats it verbosely, so each entry's time
is checkable, and its journal reaches back to the boot.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from ha_mcp.client.journal_window import parse_window_token

from ..utilities.assertions import MCPAssertions

pytestmark = [pytest.mark.haos_only]

# An hours_back reaching before 1970, which the window defines as the whole
# journal, so its start never moves past an entry between two calls.
_WHOLE_JOURNAL = 10**12


def _entry_times(log_text: str) -> list[datetime]:
    """Timestamps of the verbose entries (``<date> <time+offset> <host> ...``)."""
    stamps = []
    for line in log_text.splitlines():
        parts = line.split(" ", 2)
        if len(parts) < 3:
            continue
        try:
            stamps.append(datetime.fromisoformat(f"{parts[0]} {parts[1]}"))
        except ValueError:
            continue  # a continuation line of a multi-line entry
    return stamps


def _host(**params: Any) -> dict[str, Any]:
    return {"source": "system_service", "slug": "host", **params}


async def test_host_entries_all_fall_inside_the_window(mcp_client: Any) -> None:
    """The first oldest-first page of a 1-hour window starts inside it."""
    async with MCPAssertions(mcp_client) as mcp:
        result = await mcp.call_tool_success(
            "ha_get_logs", _host(hours_back=1, limit=500, order="oldest")
        )

    stamps = _entry_times(result["log"])
    assert stamps, f"Expected timestamped entries, got {result['log'][:200]!r}"
    assert min(stamps) >= datetime.fromisoformat(result["window_start"])


async def test_oldest_first_pages_meet_without_a_gap(mcp_client: Any) -> None:
    """Two 50-entry pages are exactly the first 100 entries of the journal.

    A dropped Range would return Supervisor's newest 100 lines instead; a
    continuation that lost its entry would leave a gap or an overlap between
    the pages. The journal's head does not move between the calls.
    """
    window = _host(hours_back=_WHOLE_JOURNAL, order="oldest")
    async with MCPAssertions(mcp_client) as mcp:
        first = await mcp.call_tool_success("ha_get_logs", {**window, "limit": 50})
        second = await mcp.call_tool_success(
            "ha_get_logs", {**window, "limit": 50, "offset": first["next_offset"]}
        )
        both = await mcp.call_tool_success("ha_get_logs", {**window, "limit": 100})
        tail = await mcp.call_tool_success("ha_get_logs", _host(limit=500))

    assert first["has_more"] is True, first
    assert first["next_offset"], first
    assert second["log"]
    assert f"{first['log']}\n{second['log']}" == both["log"]
    first_stamps = _entry_times(first["log"])
    tail_stamps = _entry_times(tail["log"])
    assert first_stamps and tail_stamps
    assert min(first_stamps) < min(tail_stamps)


async def test_newest_first_pages_continue_back_in_time(mcp_client: Any) -> None:
    """The second newest-first page ends where the first one began."""
    window = _host(hours_back=_WHOLE_JOURNAL, limit=50)
    async with MCPAssertions(mcp_client) as mcp:
        first = await mcp.call_tool_success("ha_get_logs", window)
        second = await mcp.call_tool_success(
            "ha_get_logs", {**window, "offset": first["next_offset"]}
        )

    assert first["has_more"] is True, first
    # Read back by cursor; a fall to counting by position would be slow.
    assert parse_window_token(first["next_offset"]).before != "", first
    first_stamps = _entry_times(first["log"])
    second_stamps = _entry_times(second["log"])
    assert first_stamps and second_stamps
    assert max(second_stamps) <= min(first_stamps)
    assert second["log"] != first["log"]


async def test_paging_ends_at_the_journal_end_without_repeating(
    mcp_client: Any,
) -> None:
    """Small pages of a quiet service's whole journal end, and start with one read.

    Gatewayd before systemd 258 answers a skip past the end with the last
    entry, which once made paging repeat it or never end. ``multicast`` logs
    little after boot, so its journal is a few pages of 2 entries. Oldest-first
    paging follows the journal to its end, so an entry logged meanwhile may
    follow the single read's lines; they are a prefix of what was paged.
    """
    window = {
        "source": "system_service",
        "slug": "multicast",
        "hours_back": _WHOLE_JOURNAL,
        "order": "oldest",
    }
    async with MCPAssertions(mcp_client) as mcp:
        whole = await mcp.call_tool_success("ha_get_logs", {**window, "limit": 500})
        assert whole["has_more"] is False, "multicast logged more than expected"
        pages = [await mcp.call_tool_success("ha_get_logs", {**window, "limit": 2})]
        while pages[-1]["has_more"]:
            assert len(pages) < 300, "paging does not end"
            pages.append(
                await mcp.call_tool_success(
                    "ha_get_logs",
                    {**window, "limit": 2, "offset": pages[-1]["next_offset"]},
                )
            )

    paged = "\n".join(page["log"] for page in pages if page["log"])
    whole_lines = whole["log"].splitlines()
    assert whole_lines
    assert paged.splitlines()[: len(whole_lines)] == whole_lines


async def test_newest_first_pages_are_the_oldest_first_read_reversed(
    mcp_client: Any,
) -> None:
    """Small newest-first pages of a quiet service's journal run back to its start.

    ``multicast`` logs little after boot, so its whole journal is a few pages
    of 2 entries. Both orders start from the same first entry, so the pages
    read back to front are the single oldest-first read; an entry logged
    meanwhile can only follow it.
    """
    window = {
        "source": "system_service",
        "slug": "multicast",
        "hours_back": _WHOLE_JOURNAL,
    }
    async with MCPAssertions(mcp_client) as mcp:
        whole = await mcp.call_tool_success(
            "ha_get_logs", {**window, "order": "oldest", "limit": 500}
        )
        assert whole["has_more"] is False, "multicast logged more than expected"
        pages = [await mcp.call_tool_success("ha_get_logs", {**window, "limit": 2})]
        while pages[-1]["has_more"]:
            assert len(pages) < 300, "paging does not end"
            pages.append(
                await mcp.call_tool_success(
                    "ha_get_logs",
                    {**window, "limit": 2, "offset": pages[-1]["next_offset"]},
                )
            )

    assert parse_window_token(pages[0]["next_offset"]).before != "", pages[0]
    paged = [line for page in pages for line in page["log"].splitlines()]
    whole_lines = whole["log"].splitlines()
    assert whole_lines
    assert list(reversed(paged))[: len(whole_lines)] == whole_lines
