"""Record grouping for ``ha_get_logs(source="error_log")``.

Covers #2768: raw reads return whole records (header plus traceback frames),
``search`` selects a record by any line, ``level`` matches the header only,
``limit`` stays line-based without splitting records, and paging resumes before
the oldest returned record.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.client.rest_client import ErrorLogPage, HomeAssistantConnectionError
from ha_mcp.tools.error_log_parsing import (
    _split_log_records,
)
from ha_mcp.tools.log_common import MAX_LIMIT, SUPERVISOR_SEARCH_WINDOW_LINES
from ha_mcp.tools.tools_logs import LogTools

_HEADER = (
    "2026-05-27 10:00:01.123 ERROR (MainThread) "
    "[homeassistant.components.zha] Update failed"
)
_FRAMES = (
    "Traceback (most recent call last):\n"
    '  File "/usr/src/homeassistant/zha.py", line 42, in _async_update\n'
    "    await self._device.read()\n"
    "ValueError: device did not respond"
)
_ERROR_RECORD = f"{_HEADER}\n{_FRAMES}"
_WARNING_RECORD = (
    "2026-05-27 10:00:02.456 WARNING (MainThread) "
    "[homeassistant.components.tuya] Fetch failed\n"
    "ValueError: parse error: bad"
)
_CHAINED_RECORD = (
    f"{_HEADER}\n{_FRAMES}\n"
    "\nDuring handling of the above exception, another exception occurred:\n\n"
    "Traceback (most recent call last):\n"
    '  File "/usr/src/homeassistant/zha.py", line 50, in _async_update\n'
    "RuntimeError: retry exhausted"
)


def _kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source": "error_log",
        "limit": None,
        "search": None,
        "hours_back": 1,
        "entity_id": None,
        "device_id": None,
        "context_id": None,
        "end_time": None,
        "offset": 0,
        "compact": True,
        "level": None,
        "slug": None,
    }
    base.update(overrides)
    return base


def _client(text: str, *, has_more: bool = False) -> AsyncMock:
    client = AsyncMock()
    client.get_error_log = AsyncMock(
        return_value=ErrorLogPage(text=text, has_more=has_more)
    )
    return client


def _offset_aware_page(
    log_lines: list[str], *, entry_offsets: bool = False
) -> Callable[..., Awaitable[ErrorLogPage]]:
    """``get_error_log`` stub that honours ``lines``/``offset`` like a log file."""

    async def _page(lines: int, offset: int = 0) -> ErrorLogPage:
        end = max(0, len(log_lines) - offset)
        start = max(0, end - lines)
        return ErrorLogPage(
            text="\n".join(log_lines[start:end]),
            has_more=start > 0,
            entry_offsets=entry_offsets,
        )

    return _page


class TestSplitLogRecords:
    """The grouping primitive keeps chained tracebacks in one record."""

    def test_traceback_frames_belong_to_the_header(self) -> None:
        records = _split_log_records(_ERROR_RECORD.splitlines())
        assert len(records) == 1
        assert records[0].header == _HEADER
        assert records[0].level == "ERROR"
        assert records[0].line_count == 5

    def test_blank_lines_stay_inside_a_chained_exception(self) -> None:
        records = _split_log_records(_CHAINED_RECORD.splitlines())
        assert len(records) == 1
        assert any(not line.strip() for line in records[0].lines)
        assert "RuntimeError: retry exhausted" in records[0].lines

    def test_two_headers_split_records(self) -> None:
        records = _split_log_records(f"{_ERROR_RECORD}\n{_WARNING_RECORD}".splitlines())
        assert [record.level for record in records] == ["ERROR", "WARNING"]

    def test_leading_continuation_is_a_partial_record(self) -> None:
        records = _split_log_records(["  frame", "ValueError: x"])
        assert len(records) == 1
        assert records[0].header is None
        assert records[0].start_index == 0
        assert records[0].lines == ["  frame", "ValueError: x"]

    def test_lines_that_are_not_ha_log_lines_join_the_preceding_record(self) -> None:
        """s6-overlay restart banners have no HA header, so they attach.

        The Core journald stream carries them at every restart; treating them
        as a record's continuation lines is the documented behaviour the
        Supervisor path depends on.
        """
        banner = [
            "s6-rc: info: service legacy-services: stopping",
            "[12:00:00] INFO: Home Assistant Core finish process exit code 0",
        ]
        records = _split_log_records(_ERROR_RECORD.splitlines() + banner)
        assert len(records) == 1
        assert records[0].level == "ERROR"
        assert records[0].lines[-2:] == banner


class TestRecordFiltering:
    @pytest.mark.asyncio
    async def test_header_search_returns_the_frames(self) -> None:
        tools = LogTools(_client(_ERROR_RECORD))
        result = await tools.get_logs(**_kwargs(search="Update failed"))
        assert result["total_records"] == 1
        assert result["returned_records"] == 1
        assert "ValueError: device did not respond" in result["log"]
        assert result["log"].splitlines()[0] == _HEADER

    @pytest.mark.asyncio
    async def test_oldest_keeps_each_records_lines_in_order(self) -> None:
        tools = LogTools(_client(f"{_ERROR_RECORD}\n{_WARNING_RECORD}"))
        result = await tools.get_logs(**_kwargs(limit=10, order="oldest"))
        lines = result["log"].splitlines()
        assert lines[0] == _HEADER
        assert lines.index(_HEADER) < lines.index("ValueError: device did not respond")
        assert lines.index(_WARNING_RECORD.splitlines()[0]) > lines.index(
            "ValueError: device did not respond"
        )

    @pytest.mark.asyncio
    async def test_frame_search_returns_the_header_and_frames(self) -> None:
        tools = LogTools(_client(_ERROR_RECORD))
        result = await tools.get_logs(**_kwargs(search="ValueError"))
        assert result["returned_records"] == 1
        lines = result["log"].splitlines()
        assert lines[0] == _HEADER
        assert lines[-1] == "ValueError: device did not respond"

    @pytest.mark.asyncio
    async def test_level_matches_the_header_only(self) -> None:
        """A frame line containing "Error" must not satisfy level=ERROR."""
        tools = LogTools(_client(f"{_ERROR_RECORD}\n{_WARNING_RECORD}"))
        result = await tools.get_logs(**_kwargs(level="ERROR"))
        assert result["returned_records"] == 1
        assert _WARNING_RECORD.splitlines()[0] not in result["log"]
        assert "ValueError: parse error: bad" not in result["log"]

    @pytest.mark.asyncio
    async def test_newest_keeps_each_records_lines_in_order(self) -> None:
        tools = LogTools(_client(f"{_ERROR_RECORD}\n{_WARNING_RECORD}"))
        result = await tools.get_logs(**_kwargs(limit=10))
        assert result["log"].splitlines()[0] == _WARNING_RECORD.splitlines()[0]
        assert result["log"].index("ValueError: parse error: bad") > (
            result["log"].index(_WARNING_RECORD.splitlines()[0])
        )
        # The traceback record follows whole, header before its frames.
        assert result["log"].index(_HEADER) < result["log"].index(
            "ValueError: device did not respond"
        )

    @pytest.mark.asyncio
    async def test_system_search_matches_the_exception_text(self) -> None:
        client = AsyncMock()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {
                        "name": "a",
                        "message": ["m"],
                        "exception": "UndefinedError: template failed",
                        "timestamp": 1.0,
                    }
                ],
            }
        )
        tools = LogTools(client)
        result = await tools.get_logs(
            **_kwargs(source="system", search="UndefinedError")
        )
        assert result["returned_entries"] == 1


class TestRecordLimitAndPaging:
    @pytest.mark.asyncio
    async def test_limit_never_splits_a_record(self) -> None:
        older = (
            "2026-05-27 10:00:00.000 INFO (MainThread) [a.b.c] first\n"
            "  continuation one\n"
            "  continuation two"
        )
        newer = (
            "2026-05-27 10:00:03.000 ERROR (MainThread) [a.b.c] second\n"
            "  frame one\n"
            "  frame two"
        )
        tools = LogTools(_client(f"{older}\n{newer}", has_more=True))
        result = await tools.get_logs(**_kwargs(limit=3))
        assert result["returned_records"] == 1
        assert result["returned_lines"] == 3
        assert result["log"].startswith(newer.splitlines()[0])
        assert "frame two" in result["log"]
        # Resume just before the older record, not inside the newer one.
        assert result["next_offset"] == 3

    @pytest.mark.asyncio
    async def test_over_long_record_is_cut_with_a_larger_limit_hint(self) -> None:
        frames = "\n".join(f"  frame {i}" for i in range(8)) + "\nValueError: NEEDLE"
        record = f"{_HEADER}\n{frames}"
        tools = LogTools(_client(record))
        result = await tools.get_logs(**_kwargs(limit=3))
        assert result["returned_lines"] == 3
        assert result["returned_records"] == 1
        # The header names the logger and message; the exception line is the
        # match the caller filtered for. Both ends survive the cut.
        assert result["log"].splitlines()[0] == _HEADER
        assert result["log"].splitlines()[-1] == "ValueError: NEEDLE"
        warning = next(w for w in result["warnings"] if "newest record" in w)
        assert "10 lines" in warning
        assert "cut 7" in warning
        assert "from the middle" in warning
        assert "limit=10" in warning

    @pytest.mark.asyncio
    async def test_over_long_record_at_max_limit_names_the_per_call_cap(self) -> None:
        frames = "\n".join(f"  frame {i}" for i in range(MAX_LIMIT + 99))
        record = f"{_HEADER}\n{frames}"
        tools = LogTools(_client(record))
        result = await tools.get_logs(**_kwargs(limit=MAX_LIMIT))
        assert result["returned_lines"] == MAX_LIMIT
        warning = next(w for w in result["warnings"] if "newest record" in w)
        assert f"per-call maximum ({MAX_LIMIT})" in warning
        assert "100 lines were cut" in warning

    @pytest.mark.asyncio
    async def test_cut_record_with_limit_one_keeps_only_the_header(self) -> None:
        frames = "\n".join(f"  frame {i}" for i in range(4))
        record = f"{_HEADER}\n{frames}"
        tools = LogTools(_client(record))
        result = await tools.get_logs(**_kwargs(limit=1))
        assert result["returned_lines"] == 1
        assert result["log"] == _HEADER

    @pytest.mark.asyncio
    async def test_a_record_longer_than_the_window_returns_as_a_partial(
        self,
    ) -> None:
        """A tail whose header is more than a window away must not be withheld.

        Holding it back cannot help: the next window opens inside the same
        record again, and the full-window step would skip the lines the caller
        matched. It comes back as a partial record with a warning instead, and
        the counts include it.
        """
        frames = "\n".join(
            f"  frame {i}" for i in range(SUPERVISOR_SEARCH_WINDOW_LINES + 100)
        )
        record = f"{_HEADER}\n{frames}\nValueError: NEEDLE"
        client = AsyncMock()
        client.get_error_log = _offset_aware_page(record.splitlines())
        tools = LogTools(client)
        result = await tools.get_logs(**_kwargs(limit=100, search="NEEDLE"))

        assert result["returned_records"] == 1
        assert result["total_records"] == 1
        assert result["total_lines"] >= result["returned_lines"] > 0
        assert result["has_more"] is True
        assert result["next_offset"] == SUPERVISOR_SEARCH_WINDOW_LINES
        assert any(
            "longer than one fetch window" in warning for warning in result["warnings"]
        )
        assert any("partial record" in warning for warning in result["warnings"])

    @pytest.mark.asyncio
    async def test_an_entry_offset_read_returns_the_tail_as_a_partial(self) -> None:
        """A journald route must not hold back: its resume step counts entries.

        The reopen step is derived from lines, so on an entry-offset route it
        can skip the withheld record; the tail is returned here instead.
        """
        filler = [
            f"2026-05-27 10:00:00.000 WARNING (MainThread) [a.b.c] filler {i}"
            for i in range(3970)
        ]
        record = (
            _HEADER
            + "\n"
            + "\n".join(f"  frame {i}" for i in range(40))
            + "\nValueError: NEEDLE"
        )
        lines = filler[:1985] + record.splitlines() + filler[1985:]
        client = AsyncMock()
        client.get_error_log = _offset_aware_page(lines, entry_offsets=True)
        tools = LogTools(client)

        result = await tools.get_logs(**_kwargs(limit=20, search="NEEDLE"))

        assert result["returned_records"] == 1
        assert result["total_records"] == 1
        assert any("counts entries" in warning for warning in result["warnings"])
        assert result["has_more"] is True

    @pytest.mark.asyncio
    async def test_a_cut_leading_partial_is_held_back_with_consistent_counts(
        self,
    ) -> None:
        """A held-back tail must not be returned cut while counts drop it.

        `_select_error_log_page` builds a new record when it cuts, so an
        identity-based hold-back left the cut copy on the page: the response
        said `returned_records=1` while `total_records` and `total_lines`
        counted nothing. The next page must also still reach the header.
        """
        filler = [
            f"2026-05-27 10:00:00.000 WARNING (MainThread) [a.b.c] filler {i}"
            for i in range(3970)
        ]
        record = (
            _HEADER
            + "\n"
            + "\n".join(f"  frame {i}" for i in range(40))
            + "\nValueError: NEEDLE"
        )
        lines = filler[:1985] + record.splitlines() + filler[1985:]
        client = AsyncMock()
        client.get_error_log = _offset_aware_page(lines)
        tools = LogTools(client)

        first = await tools.get_logs(**_kwargs(limit=5, search="NEEDLE"))

        assert first["returned_records"] == first["returned_lines"] == 0
        assert first["total_records"] == first["total_lines"] == 0
        assert any("held back" in warning for warning in first["warnings"])
        assert first["has_more"] is True

        second = await tools.get_logs(
            **_kwargs(limit=5, search="NEEDLE", offset=first["next_offset"])
        )
        assert second["returned_records"] == 1
        assert second["log"].splitlines()[0] == _HEADER

    @pytest.mark.asyncio
    async def test_an_unmatched_leading_partial_is_not_held_back(self) -> None:
        """Only a matching leading tail may be withheld.

        Otherwise a search that hits a newer record would emit a held-back
        warning and re-anchor the step on a record it never matched, skipping
        matches that did not fit the page.
        """
        lines = [
            f"2026-05-27 10:00:00.000 WARNING (MainThread) [a.b.c] filler {i}"
            for i in range(2100)
        ]
        # A long record straddling the window's oldest edge: the window opens
        # inside it, so it forms a leading partial that matches nothing.
        long_start = 85
        for i in range(30):
            lines[long_start + i] = f"  frame {i}"
        lines[long_start] = _HEADER
        lines[2050] = (
            "2026-05-27 10:00:00.000 ERROR (MainThread) [a.b.c] target\n"
            "ValueError: TARGET"
        ).splitlines()[0]
        lines.insert(2051, "ValueError: TARGET")

        client = AsyncMock()
        client.get_error_log = _offset_aware_page(lines)
        tools = LogTools(client)
        result = await tools.get_logs(**_kwargs(limit=5, search="TARGET"))

        assert result["returned_records"] == 1
        assert not any("held back" in warning for warning in result.get("warnings", []))
        assert "TARGET" in result["log"]

    @pytest.mark.asyncio
    async def test_walking_a_filtered_read_keeps_matches_in_order(self) -> None:
        """A filtered walk must keep cross-page ordering, not just uniqueness."""
        records = [
            "2026-05-27 10:00:00.000 WARNING (MainThread) [a.b.c] filler\n"
            f"2026-05-27 10:00:00.000 ERROR (MainThread) [a.b.c] issue {i}\n"
            f"  frame {i}.0"
            for i in range(600)
        ]
        lines = "\n".join(records).splitlines()
        client = AsyncMock()
        client.get_error_log = _offset_aware_page(lines)
        tools = LogTools(client)

        seen: list[int] = []
        offset = 0
        for _ in range(6):
            page = await tools.get_logs(
                **_kwargs(limit=20, offset=offset, search="issue")
            )
            if page["returned_lines"]:
                assert page["log"].splitlines()[0].startswith("2026-")
            for line in page["log"].splitlines():
                match = re.search(r"issue (\d+)", line)
                if match:
                    seen.append(int(match.group(1)))
            if not page["has_more"]:
                break
            offset = page["next_offset"]

        assert seen, "the filtered walk returned no matches"
        assert len(seen) == len(set(seen)), "a filtered match repeated across pages"
        assert seen == sorted(seen, reverse=True), (
            "filtered paging must keep newest-first order across pages"
        )

    @pytest.mark.asyncio
    async def test_leading_partial_is_widened_to_reach_its_header(self) -> None:
        log = f"{_ERROR_RECORD}\n" + "\n".join(f"  frame {i}" for i in range(400))

        async def _tail_page(lines: int, offset: int = 0) -> ErrorLogPage:
            return ErrorLogPage(
                text="\n".join(log.splitlines()[-lines:]), has_more=True
            )

        client = AsyncMock()
        client.get_error_log = AsyncMock(side_effect=_tail_page)
        tools = LogTools(client)
        result = await tools.get_logs(**_kwargs(limit=5))

        assert client.get_error_log.await_count == 2
        first_lines = client.get_error_log.await_args_list[0].kwargs["lines"]
        second_lines = client.get_error_log.await_args_list[1].kwargs["lines"]
        assert second_lines > first_lines
        assert result["log"].splitlines()[0] == _HEADER

    @pytest.mark.asyncio
    async def test_partial_record_is_marked_when_the_header_is_unavailable(
        self,
    ) -> None:
        leading = "  frame\nValueError: device did not respond"
        client = AsyncMock()
        client.get_error_log = AsyncMock(
            return_value=ErrorLogPage(text=leading, has_more=False)
        )
        tools = LogTools(client)
        result = await tools.get_logs(**_kwargs(limit=5))
        assert any("partial record" in w for w in result["warnings"])
        assert result["log"].splitlines()[0] == "  frame"

    @pytest.mark.asyncio
    async def test_a_window_that_reaches_history_start_is_not_refetched(self) -> None:
        """A partial tail on a terminal window has no older history to widen into."""
        leading = "  frame\nValueError: device did not respond"
        client = AsyncMock()
        client.get_error_log = AsyncMock(
            return_value=ErrorLogPage(text=leading, has_more=False)
        )
        tools = LogTools(client)
        result = await tools.get_logs(**_kwargs(limit=5))

        assert client.get_error_log.await_count == 1
        assert any("partial record" in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_a_failed_widening_keeps_the_first_page(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        leading = "  frame\nValueError: device did not respond"
        client = AsyncMock()
        client.get_error_log = AsyncMock(
            side_effect=[
                ErrorLogPage(text=leading, has_more=True),
                HomeAssistantConnectionError("widen failed"),
            ]
        )
        tools = LogTools(client)
        with caplog.at_level("WARNING", logger="ha_mcp.tools.log_sources"):
            result = await tools.get_logs(**_kwargs(limit=5))

        assert client.get_error_log.await_count == 2
        assert any("partial record" in w for w in result["warnings"])
        assert any("wider read" in w and "failed" in w for w in result["warnings"])
        assert not any(
            "lies outside the fetched history" in w for w in result["warnings"]
        )
        assert result["log"].splitlines()[0] == "  frame"
        assert any(
            "wider read for a record header failed" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("filter_name", ["search", "level"])
    async def test_a_filtered_page_held_back_from_mid_record_returns_it_whole(
        self, filter_name: str
    ) -> None:
        """A filtered window that opens inside a record must not split it.

        The filtered window is already the search-window bound, so the widening
        refetch cannot grow it; the tail has to be left off this page and the
        next page re-opened at its last line, or the record's header is lost
        (and a `level` read drops the tail silently).
        """
        filler = [
            f"2026-05-27 10:00:00.000 WARNING (MainThread) [a.b.c] filler {i}"
            for i in range(3985)
        ]
        long_record = (
            _HEADER
            + "\n"
            + "\n".join(f"  frame {i}" for i in range(40))
            + "\nValueError: NEEDLE"
        )
        lines = filler[:1990] + long_record.splitlines() + filler[1990:]
        client = AsyncMock()
        client.get_error_log = _offset_aware_page(lines)
        tools = LogTools(client)

        first = await tools.get_logs(
            **_kwargs(
                limit=20,
                **{filter_name: "NEEDLE" if filter_name == "search" else "ERROR"},
            )
        )

        # The tail is held back, so page 1 cannot present it as a partial.
        assert first["returned_records"] == 0
        assert not any("partial record" in w for w in first.get("warnings", []))
        assert first["has_more"] is True

        second = await tools.get_logs(
            **_kwargs(
                limit=20,
                offset=first["next_offset"],
                **{filter_name: "NEEDLE" if filter_name == "search" else "ERROR"},
            )
        )
        assert second["returned_records"] == 1
        assert second["log"].splitlines()[0] == _HEADER
        assert second["log"].splitlines()[-1] == "ValueError: NEEDLE"

    @pytest.mark.asyncio
    async def test_walking_next_offset_never_repeats_a_record(self) -> None:
        """Following the hint pages backwards without repeats or split records."""
        records = [
            f"2026-05-27 10:00:00.000 ERROR (MainThread) [a.b.c] issue {i}\n"
            f"  frame {i}.0\n  frame {i}.1"
            for i in range(700)
        ]
        lines = "\n".join(records).splitlines()
        client = AsyncMock()
        client.get_error_log = _offset_aware_page(lines)
        tools = LogTools(client)

        seen: list[str] = []
        offset = 0
        last: dict[str, Any] = {}
        for _ in range(40):
            last = await tools.get_logs(**_kwargs(limit=100, offset=offset))
            if last["returned_lines"]:
                assert (
                    last["log"]
                    .splitlines()[0]
                    .startswith("2026-05-27 10:00:00.000 ERROR")
                ), "every page must open on a record header"
            seen.extend(
                line for line in last["log"].splitlines() if line.startswith("2026-")
            )
            if not last["has_more"]:
                break
            offset = last["next_offset"]

        assert len(seen) == len(set(seen)), "a record must not be returned twice"
        issues = [int(re.search(r"issue (\d+)", line).group(1)) for line in seen]
        assert issues == sorted(issues, reverse=True), (
            "newest-first paging must keep records in descending order"
        )

        if len(seen) < len(records):
            # The terminal window cannot page further; its hint has to make the
            # remainder reachable, and one call at the hinted limit must prove
            # that no record is silently lost.
            hint = last.get("pagination_hint") or ""
            match = re.search(r"limit=(\d+)", hint)
            assert match, f"terminal hint must name an actionable limit: {hint}"
            final = await tools.get_logs(
                **_kwargs(limit=int(match.group(1)), offset=offset)
            )
            final_seen = [
                line for line in final["log"].splitlines() if line.startswith("2026-")
            ]
            assert final["has_more"] is False
            assert len(set(seen) | set(final_seen)) == len(records), (
                "the terminal hint must make every remaining record reachable"
            )
