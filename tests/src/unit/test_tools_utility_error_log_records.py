"""Record grouping for ``ha_get_logs(source="error_log")``.

Covers #2768: raw reads return whole records (header plus traceback frames),
``search`` selects a record by any line, ``level`` matches the header only,
``limit`` stays line-based without splitting records, and paging resumes before
the oldest returned record.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.client.rest_client import ErrorLogPage
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
        records = _split_log_records("  frame\nValueError: x")
        assert len(records) == 1
        assert records[0].header is None
        assert records[0].start_index == 0


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
        frames = "\n".join(f"  frame {i}" for i in range(9))
        record = f"{_HEADER}\n{frames}"
        tools = LogTools(_client(record))
        result = await tools.get_logs(**_kwargs(limit=3))
        assert result["returned_lines"] == 3
        assert result["returned_records"] == 1
        assert result["log"].splitlines()[0] == _HEADER
        warning = next(w for w in result["warnings"] if "newest record" in w)
        assert "10 lines" in warning
        assert "cut 7" in warning
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
    async def test_leading_partial_is_widened_to_reach_its_header(self) -> None:
        leading = "  frame\nValueError: device did not respond"
        client = AsyncMock()
        client.get_error_log = AsyncMock(
            side_effect=[
                ErrorLogPage(text=leading, has_more=True),
                ErrorLogPage(text=_ERROR_RECORD, has_more=True),
            ]
        )
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
    async def test_a_no_match_window_steps_a_whole_window(self) -> None:
        client = _client(_ERROR_RECORD, has_more=True)
        tools = LogTools(client)
        result = await tools.get_logs(**_kwargs(search="nothing-matches"))
        assert result["returned_lines"] == 0
        assert result["next_offset"] == SUPERVISOR_SEARCH_WINDOW_LINES
