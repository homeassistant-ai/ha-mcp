"""Unit tests for ``ha_get_logs(source="fault_log")`` (issue #2373).

The source tails ``home-assistant.log.fault`` through the component's
``read_file`` service, so every test stubs ``call_mcp_tools_service`` where
``log_sources_fault`` imports it and drives ``LogTools.get_logs`` directly.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.log_common import (
    DEFAULT_LOG_LIMIT,
    MAX_LIMIT,
    SUPERVISOR_SEARCH_WINDOW_LINES,
)
from ha_mcp.tools.log_sources_fault import (
    FAULT_LOG_PATH,
    MIN_COMPONENT_VERSION_FAULT_LOG,
)
from ha_mcp.tools.tools_logs import LogTools

_PATCH_TARGET = "ha_mcp.tools.log_sources_fault.call_mcp_tools_service"

# Two faulthandler dumps, as HA Core's append-mode file accumulates them.
_TWO_CRASHES = (
    "Fatal Python error: Segmentation fault\n"
    "\n"
    "Thread 0x00007f1 (most recent call first):\n"
    '  File "/usr/src/homeassistant/homeassistant/components/foo/__init__.py", line 10 in poll\n'
    "\n"
    "Fatal Python error: Aborted\n"
    "\n"
    "Current thread 0x00007f2 (most recent call first):\n"
    '  File "/usr/src/homeassistant/homeassistant/core.py", line 20 in run\n'
)


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


class TestHealthyInstall:
    @pytest.mark.asyncio
    async def test_empty_file_reports_no_crash(self) -> None:
        with patch(_PATCH_TARGET, AsyncMock(return_value=_read_file_ok(""))):
            result = await LogTools(AsyncMock()).get_logs(**_call_kwargs())
        assert result["success"] is True
        assert result["source"] == "fault_log"
        assert result["path"] == FAULT_LOG_PATH
        assert result["crash_recorded"] is False
        assert result["log"] == ""
        assert result["returned_lines"] == 0
        assert "No native crash recorded" in result["message"]
        assert result["modified"] == "2026-09-05T10:00:00"

    @pytest.mark.asyncio
    async def test_missing_file_reports_no_crash(self) -> None:
        stub = AsyncMock(
            return_value=_read_file_error(
                "File does not exist: home-assistant.log.fault"
            )
        )
        with patch(_PATCH_TARGET, stub):
            result = await LogTools(AsyncMock()).get_logs(**_call_kwargs())
        assert result["success"] is True
        assert result["crash_recorded"] is False
        assert result["total_lines"] == 0
        assert "No native crash recorded" in result["message"]


class TestRecordedCrash:
    @pytest.mark.asyncio
    async def test_newest_first_window_and_crash_count(self) -> None:
        with patch(_PATCH_TARGET, AsyncMock(return_value=_read_file_ok(_TWO_CRASHES))):
            result = await LogTools(AsyncMock()).get_logs(**_call_kwargs(limit=3))
        assert result["crash_recorded"] is True
        assert result["fatal_error_blocks_in_window"] == 2
        assert result["order"] == "newest"
        assert result["limit"] == 3
        assert result["returned_lines"] == 3
        # Most-recent three lines of the file (the blank separator included),
        # newest first.
        assert result["log"] == (
            '  File "/usr/src/homeassistant/homeassistant/core.py", line 20 in run\n'
            "Current thread 0x00007f2 (most recent call first):\n"
        )
        assert result["total_lines"] == len(_TWO_CRASHES.split("\n"))
        assert result["window_lines"] == 3

    @pytest.mark.asyncio
    async def test_oldest_keeps_chronological_order(self) -> None:
        with patch(_PATCH_TARGET, AsyncMock(return_value=_read_file_ok(_TWO_CRASHES))):
            result = await LogTools(AsyncMock()).get_logs(
                **_call_kwargs(limit=2, order="oldest")
            )
        assert result["log"].splitlines() == [
            "Current thread 0x00007f2 (most recent call first):",
            '  File "/usr/src/homeassistant/homeassistant/core.py", line 20 in run',
        ]

    @pytest.mark.asyncio
    async def test_search_widens_window_and_filters(self) -> None:
        stub = AsyncMock(return_value=_read_file_ok(_TWO_CRASHES))
        with patch(_PATCH_TARGET, stub):
            result = await LogTools(AsyncMock()).get_logs(
                **_call_kwargs(search="fatal python error")
            )
        stub.assert_awaited_once()
        _client, service, payload = stub.await_args.args
        assert service == "read_file"
        assert payload == {
            "path": FAULT_LOG_PATH,
            "tail_lines": SUPERVISOR_SEARCH_WINDOW_LINES,
        }
        assert result["filters_applied"] == {"search": "fatal python error"}
        assert result["matched_lines"] == 2
        assert result["log"].splitlines() == [
            "Fatal Python error: Aborted",
            "Fatal Python error: Segmentation fault",
        ]
        # Counted on the fetched window, before the search narrowed it.
        assert result["fatal_error_blocks_in_window"] == 2

    @pytest.mark.asyncio
    async def test_default_and_capped_limit_drive_tail_lines(self) -> None:
        stub = AsyncMock(return_value=_read_file_ok(""))
        with patch(_PATCH_TARGET, stub):
            await LogTools(AsyncMock()).get_logs(**_call_kwargs())
            await LogTools(AsyncMock()).get_logs(**_call_kwargs(limit=MAX_LIMIT * 4))
        first, second = (call.args[2] for call in stub.await_args_list)
        assert first["tail_lines"] == DEFAULT_LOG_LIMIT
        assert second["tail_lines"] == MAX_LIMIT


class TestFailures:
    @pytest.mark.asyncio
    async def test_path_not_allowed_means_component_too_old(self) -> None:
        stub = AsyncMock(
            return_value=_read_file_error(
                "Path not allowed. Allowed patterns: configuration.yaml, home-assistant.log"
            )
        )
        with (
            patch(_PATCH_TARGET, stub),
            pytest.raises(ToolError) as exc_info,
        ):
            await LogTools(AsyncMock()).get_logs(**_call_kwargs())
        payload = _parse_tool_error(exc_info)
        assert payload["error"]["code"] == "COMPONENT_NOT_INSTALLED"
        assert MIN_COMPONENT_VERSION_FAULT_LOG in payload["error"]["message"]
        # create_error_response spreads ``context`` beside ``error``.
        assert payload["source"] == "fault_log"

    @pytest.mark.asyncio
    async def test_other_read_failure_is_service_call_failed(self) -> None:
        stub = AsyncMock(
            return_value=_read_file_error("Permission denied: home-assistant.log.fault")
        )
        with (
            patch(_PATCH_TARGET, stub),
            pytest.raises(ToolError) as exc_info,
        ):
            await LogTools(AsyncMock()).get_logs(**_call_kwargs())
        payload = _parse_tool_error(exc_info)
        assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "Permission denied" in payload["error"]["message"]

    @pytest.mark.asyncio
    async def test_tools_entry_gate_error_passes_through(self) -> None:
        gate_error = ToolError(
            json.dumps({"error": {"code": "COMPONENT_NOT_INSTALLED"}})
        )
        with (
            patch(_PATCH_TARGET, AsyncMock(side_effect=gate_error)),
            pytest.raises(ToolError) as exc_info,
        ):
            await LogTools(AsyncMock()).get_logs(**_call_kwargs())
        assert exc_info.value is gate_error


class TestParameterWarnings:
    @pytest.mark.asyncio
    async def test_level_and_offset_are_reported_as_ignored(self) -> None:
        with patch(_PATCH_TARGET, AsyncMock(return_value=_read_file_ok(""))):
            result = await LogTools(AsyncMock()).get_logs(
                **_call_kwargs(level="ERROR", offset=5)
            )
        joined = "\n".join(result["warnings"])
        assert "Parameter 'level' only applies" in joined
        assert "Parameter 'offset' only applies" in joined
