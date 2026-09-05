"""Unit tests for ``ha_get_logs(source="fault_log")`` (issue #2373).

The source reads ``home-assistant.log.fault`` through the component's
``read_file`` service behind the live tools-entry probe, so every test stubs
both where ``log_sources_fault`` imports them and drives ``LogTools.get_logs``
directly.
"""

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.log_common import DEFAULT_LOG_LIMIT
from ha_mcp.tools.log_sources_fault import (
    FAULT_LOG_PATH,
    FAULT_LOG_WINDOW_LINES,
    MIN_COMPONENT_VERSION_FAULT_LOG,
)
from ha_mcp.tools.tools_logs import LogTools

_READ_TARGET = "ha_mcp.tools.log_sources_fault.call_mcp_tools_service"
_GATE_TARGET = "ha_mcp.tools.log_sources_fault._assert_mcp_tools_available"

# Two faulthandler dumps, as HA Core's append-mode file accumulates them.
_SEGV_BLOCK = [
    "Fatal Python error: Segmentation fault",
    "",
    "Thread 0x00007f1 (most recent call first):",
    '  File "/usr/src/homeassistant/homeassistant/components/foo/__init__.py", line 10 in poll',
    "",
]
_ABORT_BLOCK = [
    "Fatal Python error: Aborted",
    "",
    "Current thread 0x00007f2 (most recent call first):",
    '  File "/usr/src/homeassistant/homeassistant/core.py", line 20 in run',
]
_TWO_CRASHES = "\n".join(_SEGV_BLOCK + _ABORT_BLOCK) + "\n"


@pytest.fixture(autouse=True)
def _tools_entry_present() -> Iterator[AsyncMock]:
    """The live tools-entry probe passes unless a test overrides it."""
    with patch(_GATE_TARGET, AsyncMock(return_value=None)) as gate:
        yield gate


def _call_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source": "fault_log",
        "limit": None,
        "search": None,
        "hours_back": 1,
        "entity_id": None,
        "end_time": None,
        "offset": 0,
        "compact": True,
        "level": None,
        "slug": None,
    }
    base.update(overrides)
    return base


def _read_file_ok(content: str, **extra: Any) -> dict[str, Any]:
    """A successful ``read_file`` reply in HA's ``call_service`` wrapping."""
    lines = content.split("\n")
    response = {
        "success": True,
        "path": FAULT_LOG_PATH,
        "content": content,
        "size": len(content),
        "modified": "2026-09-05T10:00:00",
        "lines_returned": len(lines),
        "total_lines": len(lines),
        "truncated": False,
    }
    response.update(extra)
    return {"changed_states": [], "service_response": response}


def _read_file_error(error: str) -> dict[str, Any]:
    return {
        "changed_states": [],
        "service_response": {"success": False, "error": error},
    }


def _parse_tool_error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(str(exc_info.value))
    return payload


async def _get(stub_reply: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    with patch(_READ_TARGET, AsyncMock(return_value=stub_reply)):
        return await LogTools(AsyncMock()).get_logs(**_call_kwargs(**overrides))


class TestHealthyInstall:
    @pytest.mark.asyncio
    async def test_empty_file_reports_no_crash(self) -> None:
        result = await _get(_read_file_ok(""))
        assert result["success"] is True
        assert result["source"] == "fault_log"
        assert result["path"] == FAULT_LOG_PATH
        assert result["crash_recorded"] is False
        assert result["log"] == ""
        assert result["returned_lines"] == 0
        assert result["has_more"] is False
        assert "No native crash recorded" in result["message"]
        assert result["modified"] == "2026-09-05T10:00:00"

    @pytest.mark.asyncio
    async def test_missing_file_reports_no_crash(self) -> None:
        result = await _get(
            _read_file_error("File does not exist: home-assistant.log.fault")
        )
        assert result["success"] is True
        assert result["crash_recorded"] is False
        assert result["total_lines"] == 0
        assert "No native crash recorded" in result["message"]


class TestRecordedCrash:
    @pytest.mark.asyncio
    async def test_newest_orders_blocks_and_keeps_lines_readable(self) -> None:
        result = await _get(_read_file_ok(_TWO_CRASHES))
        assert result["crash_recorded"] is True
        assert result["fatal_error_blocks_in_window"] == 2
        assert result["order"] == "newest"
        # Latest block first, each block in its original line order.
        assert result["log"].split("\n") == _ABORT_BLOCK + _SEGV_BLOCK
        assert result["returned_lines"] == len(_ABORT_BLOCK) + len(_SEGV_BLOCK)
        assert result["has_more"] is False
        assert result["total_lines"] == len(_TWO_CRASHES.split("\n"))
        assert result["window_lines"] == FAULT_LOG_WINDOW_LINES
        assert result["window_truncated"] is False

    @pytest.mark.asyncio
    async def test_oldest_keeps_file_order(self) -> None:
        result = await _get(_read_file_ok(_TWO_CRASHES), order="oldest")
        assert result["log"].split("\n") == _SEGV_BLOCK + _ABORT_BLOCK

    @pytest.mark.asyncio
    async def test_limit_and_offset_page_the_assembled_text(self) -> None:
        first = await _get(_read_file_ok(_TWO_CRASHES), limit=3)
        assert first["log"].split("\n") == _ABORT_BLOCK[:3]
        assert first["has_more"] is True
        assert first["next_offset"] == 3
        assert first["pagination_hint"] == (
            "ha_get_logs(source='fault_log', offset=3, limit=3, order='newest')"
        )

        second = await _get(_read_file_ok(_TWO_CRASHES), limit=3, offset=3)
        assert second["log"].split("\n") == (_ABORT_BLOCK + _SEGV_BLOCK)[3:6]
        assert second["offset"] == 3
        assert second["has_more"] is True

        last = await _get(_read_file_ok(_TWO_CRASHES), limit=3, offset=6)
        assert last["log"].split("\n") == (_ABORT_BLOCK + _SEGV_BLOCK)[6:9]
        assert last["has_more"] is False
        assert "next_offset" not in last

    @pytest.mark.asyncio
    async def test_header_survives_a_block_longer_than_limit(self) -> None:
        frames = [f'  File "/x.py", line {i} in f{i}' for i in range(150)]
        block = ["Fatal Python error: Segmentation fault", ""] + frames
        result = await _get(_read_file_ok("\n".join(block) + "\n"))
        assert result["limit"] == DEFAULT_LOG_LIMIT
        assert result["log"].split("\n")[0] == "Fatal Python error: Segmentation fault"
        assert result["returned_lines"] == DEFAULT_LOG_LIMIT
        assert result["has_more"] is True
        assert result["fatal_error_blocks_in_window"] == 1

    @pytest.mark.asyncio
    async def test_leading_partial_block_is_kept_oldest(self) -> None:
        # The component's tail cut an older block's header off the window.
        window = ['  File "/old.py", line 1 in tail', ""] + _ABORT_BLOCK
        result = await _get(
            _read_file_ok("\n".join(window) + "\n", truncated=True, total_lines=9000)
        )
        assert result["window_truncated"] is True
        assert result["total_lines"] == 9000
        assert result["fatal_error_blocks_in_window"] == 1
        assert result["log"].split("\n") == _ABORT_BLOCK + window[:2]

    @pytest.mark.asyncio
    async def test_search_keeps_whole_matching_blocks(self) -> None:
        # Only the abort block mentions core.py; it comes back intact, header
        # first, rather than as the single matching frame.
        result = await _get(_read_file_ok(_TWO_CRASHES), search="core.py")
        assert result["filters_applied"] == {"search": "core.py"}
        assert result["matched_blocks"] == 1
        assert result["log"].split("\n") == _ABORT_BLOCK
        # Counted on the window, before the search narrowed it.
        assert result["fatal_error_blocks_in_window"] == 2

    @pytest.mark.asyncio
    async def test_pagination_hint_keeps_the_search_filter(self) -> None:
        result = await _get(_read_file_ok(_TWO_CRASHES), search="fatal", limit=2)
        assert result["has_more"] is True
        assert result["pagination_hint"] == (
            "ha_get_logs(source='fault_log', offset=2, limit=2, "
            "order='newest', search='fatal')"
        )

    @pytest.mark.asyncio
    async def test_search_is_case_insensitive_across_blocks(self) -> None:
        result = await _get(_read_file_ok(_TWO_CRASHES), search="FATAL PYTHON")
        assert result["matched_blocks"] == 2
        assert result["log"].split("\n") == _ABORT_BLOCK + _SEGV_BLOCK

    @pytest.mark.asyncio
    async def test_always_reads_the_full_window(self) -> None:
        stub = AsyncMock(return_value=_read_file_ok(""))
        with patch(_READ_TARGET, stub):
            await LogTools(AsyncMock()).get_logs(**_call_kwargs(limit=5))
        stub.assert_awaited_once()
        _client, service, payload = stub.await_args.args
        assert service == "read_file"
        assert payload == {"path": FAULT_LOG_PATH, "tail_lines": FAULT_LOG_WINDOW_LINES}


class TestFailures:
    @pytest.mark.asyncio
    async def test_path_not_allowed_means_component_too_old(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await _get(
                _read_file_error(
                    "Path not allowed. Allowed paths: configuration.yaml, home-assistant.log"
                )
            )
        payload = _parse_tool_error(exc_info)
        assert payload["error"]["code"] == "COMPONENT_NOT_INSTALLED"
        assert MIN_COMPONENT_VERSION_FAULT_LOG in payload["error"]["message"]
        # create_error_response spreads ``context`` beside ``error``.
        assert payload["source"] == "fault_log"

    @pytest.mark.asyncio
    async def test_other_read_failure_is_service_call_failed(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await _get(_read_file_error("Permission denied: home-assistant.log.fault"))
        payload = _parse_tool_error(exc_info)
        assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "Permission denied" in payload["error"]["message"]
        suggestions = payload["error"]["suggestions"]
        assert any(FAULT_LOG_PATH in sug for sug in suggestions)
        assert any("readable" in sug for sug in suggestions)

    @pytest.mark.asyncio
    async def test_caller_token_gate_error_passes_through(self) -> None:
        gate_error = ToolError(
            json.dumps({"error": {"code": "COMPONENT_NOT_INSTALLED"}})
        )
        with (
            patch(_READ_TARGET, AsyncMock(side_effect=gate_error)),
            pytest.raises(ToolError) as exc_info,
        ):
            await LogTools(AsyncMock()).get_logs(**_call_kwargs())
        assert exc_info.value is gate_error

    @pytest.mark.asyncio
    async def test_live_tools_entry_probe_runs_before_the_read(
        self, _tools_entry_present: AsyncMock
    ) -> None:
        # Entry removed after the caller token was cached: the probe, not the
        # service call, must be what fails.
        entry_gone = ToolError(
            json.dumps({"error": {"code": "COMPONENT_NOT_INSTALLED"}})
        )
        _tools_entry_present.side_effect = entry_gone
        read = AsyncMock(return_value=_read_file_ok(""))
        with patch(_READ_TARGET, read), pytest.raises(ToolError) as exc_info:
            await LogTools(AsyncMock()).get_logs(**_call_kwargs())
        assert exc_info.value is entry_gone
        read.assert_not_awaited()


class TestParameterWarnings:
    @pytest.mark.asyncio
    async def test_level_is_reported_as_ignored_and_offset_is_not(self) -> None:
        result = await _get(_read_file_ok(""), level="ERROR", offset=5)
        joined = "\n".join(result["warnings"])
        assert "Parameter 'level' only applies" in joined
        assert "offset" not in joined
