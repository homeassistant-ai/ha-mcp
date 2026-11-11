"""``hours_back`` page reads of Supervisor logs, on every HAOS lane (#2974).

The window is read from the journal through Supervisor: directly inside the
app, and through Core's hassio proxy for the external, stdio and embedded
servers. Pages after the first continue from Supervisor's ``X-First-Cursor``,
which the proxy has to pass back, so this module runs on every HAOS lane
rather than only the in-app one.

``host`` is used because Supervisor formats it verbosely, so each entry's time
is checkable, and its journal reaches back to the boot.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from ..utilities.assertions import MCPAssertions

pytestmark = [pytest.mark.haos_only]

# hours_back covering the whole journal, so the window's start never moves
# past an entry between two calls.
_WHOLE_JOURNAL = 24 * 365


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


def _token_cursor(token: object) -> str:
    """The journal cursor in a ``window:<order>:<since>:<hours>:<cursor>:<skip>`` token.

    It is Supervisor's ``X-First-Cursor``: a proxy that dropped the header
    would fail here by name rather than through a generic error.
    """
    parts = str(token).split(":")
    assert len(parts) == 6 and parts[0] == "window", token
    return parts[4]


async def test_host_entries_all_fall_inside_the_window(mcp_client: Any) -> None:
    """The first oldest-first page of a 1-hour window starts inside it."""
    async with MCPAssertions(mcp_client) as mcp:
        result = await mcp.call_tool_success(
            "ha_get_logs", _host(hours_back=1, limit=500, order="oldest")
        )

    stamps = _entry_times(result["log"])
    assert stamps, f"Expected timestamped entries, got {result['log'][:200]!r}"
    assert min(stamps) >= datetime.fromisoformat(result["window_start"])
    # The window runs to the journal's end, so entries logged while the read
    # was in flight may follow read_at by a moment.
    assert max(stamps) <= datetime.fromisoformat(result["read_at"]) + timedelta(
        minutes=1
    )


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
    assert _token_cursor(first["next_offset"]).startswith("s="), first
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
    assert str(first["next_offset"]).startswith("window:newest:")
    assert _token_cursor(first["next_offset"]).startswith("s="), first
    first_stamps = _entry_times(first["log"])
    second_stamps = _entry_times(second["log"])
    assert first_stamps and second_stamps
    assert max(second_stamps) <= min(first_stamps)
    assert second["log"] != first["log"]


async def test_paging_ends_at_the_journal_end_without_repeating(
    mcp_client: Any,
) -> None:
    """Small pages of a quiet service's whole journal end, and equal one read.

    Gatewayd before systemd 258 answers a skip past the end with the last
    entry, which once made paging repeat it or never end. ``multicast`` logs
    little after boot, so its journal is a few pages of 2 entries and stays
    put across the calls.
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
    assert paged == whole["log"]
