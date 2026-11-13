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


async def test_order_newest_with_a_window_is_served_oldest_first(
    mcp_client: Any,
) -> None:
    """The journal serves a window oldest-first; 'newest' says so, not reverses it."""
    window = _host(hours_back=_WHOLE_JOURNAL, limit=50)
    async with MCPAssertions(mcp_client) as mcp:
        newest = await mcp.call_tool_success(
            "ha_get_logs", {**window, "order": "newest"}
        )
        oldest = await mcp.call_tool_success(
            "ha_get_logs", {**window, "order": "oldest"}
        )

    assert newest["order"] == "oldest", newest
    assert any("served oldest-first" in w for w in newest.get("warnings", []))
    assert newest["log"] == oldest["log"]


async def test_paging_ends_at_the_journal_end_without_repeating(
    mcp_client: Any,
) -> None:
    """Small pages of a quiet service's whole journal end, without a repeat.

    ``multicast`` logs little after boot, so its journal is a few pages of 2
    entries. Oldest-first paging follows the journal to its end, so an entry
    logged meanwhile may follow the first single read's lines: they are a
    prefix of what was paged. A second single read after paging holds every
    entry paged, so the paged lines are a prefix of it, which a repeated
    entry would break.
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
        after = await mcp.call_tool_success("ha_get_logs", {**window, "limit": 500})
        assert after["has_more"] is False, "multicast logged more than expected"

    paged = [line for page in pages for line in page["log"].splitlines()]
    whole_lines = whole["log"].splitlines()
    assert whole_lines
    assert paged[: len(whole_lines)] == whole_lines
    assert after["log"].splitlines()[: len(paged)] == paged
