"""``ha_get_logs(source="fault_log")`` (issue #2373), on a real ``hass``."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.__main__ import FAULT_LOG_FILENAME
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.log_common import DEFAULT_LOG_LIMIT
from ha_mcp.tools.log_sources_fault import (
    FAULT_LOG_WINDOW_LINES,
)
from ha_mcp.tools.tools_logs import LogTools

from ._tool_errors import error_body

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


def _fault_file(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(FAULT_LOG_FILENAME))


async def _write_fault_log(hass: HomeAssistant, content: str | bytes) -> Path:
    path = _fault_file(hass)
    data = content.encode() if isinstance(content, str) else content
    await hass.async_add_executor_job(path.write_bytes, data)
    return path


def _call_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source": "fault_log",
        "limit": None,
        "search": None,
        "hours_back": None,
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


async def _get(client: HomeAssistantClient, **overrides: Any) -> dict[str, Any]:
    return await LogTools(client).get_logs(**_call_kwargs(**overrides))


class TestHealthyInstall:
    async def test_empty_file_reports_no_crash(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        path = await _write_fault_log(hass, "")
        mtime = datetime(2026, 9, 5, 10, 0).timestamp()
        await hass.async_add_executor_job(os.utime, path, (mtime, mtime))

        result = await _get(component_core_client)

        assert result["success"] is True
        assert result["source"] == "fault_log"
        assert result["path"] == FAULT_LOG_FILENAME
        assert result["crash_recorded"] is False
        assert result["log"] == ""
        assert result["returned_lines"] == 0
        assert result["has_more"] is False
        assert "No native crash recorded" in result["message"]
        # The file's modification time is passed through.
        assert datetime.fromisoformat(result["modified"]).timestamp() == mtime

    async def test_missing_file_reports_no_crash(
        self, component_core_client: HomeAssistantClient
    ) -> None:
        result = await _get(component_core_client)

        assert result["success"] is True
        assert result["crash_recorded"] is False
        assert result["total_lines"] == 0
        assert "No native crash recorded" in result["message"]


async def test_crash_read_uses_shared_component_command(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await _write_fault_log(hass, _TWO_CRASHES)
    with patch.object(
        component_core_client,
        "call_service",
        AsyncMock(wraps=component_core_client.call_service),
    ) as service:
        result = await _get(component_core_client, search="core.py")

    assert result["log"].splitlines() == _ABORT_BLOCK
    assert not result.get("warnings")
    service.assert_not_awaited()


class TestRecordedCrash:
    async def test_newest_orders_blocks_and_keeps_lines_readable(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        await _write_fault_log(hass, _TWO_CRASHES)

        result = await _get(component_core_client)

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

    async def test_oldest_keeps_file_order(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        await _write_fault_log(hass, _TWO_CRASHES)

        result = await _get(component_core_client, order="oldest")

        assert result["log"].split("\n") == _SEGV_BLOCK + _ABORT_BLOCK

    async def test_limit_and_offset_page_the_assembled_text(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        await _write_fault_log(hass, _TWO_CRASHES)

        first = await _get(component_core_client, limit=3)
        assert first["log"].split("\n") == _ABORT_BLOCK[:3]
        assert first["has_more"] is True
        assert first["next_offset"] == 3
        assert first["pagination_hint"] == (
            "ha_get_logs(source='fault_log', offset=3, limit=3, order='newest')"
        )

        second = await _get(component_core_client, limit=3, offset=3)
        assert second["log"].split("\n") == (_ABORT_BLOCK + _SEGV_BLOCK)[3:6]
        assert second["offset"] == 3
        assert second["has_more"] is True

        last = await _get(component_core_client, limit=3, offset=6)
        assert last["log"].split("\n") == (_ABORT_BLOCK + _SEGV_BLOCK)[6:9]
        assert last["has_more"] is False
        assert "next_offset" not in last

    async def test_header_survives_a_block_longer_than_limit(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        frames = [f'  File "/x.py", line {i} in f{i}' for i in range(150)]
        block = ["Fatal Python error: Segmentation fault", "", *frames]
        await _write_fault_log(hass, "\n".join(block) + "\n")

        result = await _get(component_core_client)

        assert result["limit"] == DEFAULT_LOG_LIMIT
        assert result["log"].split("\n")[0] == "Fatal Python error: Segmentation fault"
        assert result["returned_lines"] == DEFAULT_LOG_LIMIT
        assert result["has_more"] is True
        assert result["fatal_error_blocks_in_window"] == 1

    async def test_leading_partial_block_is_kept_oldest(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        # The older block's header lies beyond the window the read tails, so
        # the window opens with that block's frames.
        frames = [
            f'  File "/old.py", line {i} in tail' for i in range(FAULT_LOG_WINDOW_LINES)
        ]
        older = ["Fatal Python error: Segmentation fault", "", *frames]
        content = "\n".join(older + _ABORT_BLOCK) + "\n"
        await _write_fault_log(hass, content)

        result = await _get(component_core_client, limit=len(_ABORT_BLOCK) + 2)

        assert result["window_truncated"] is True
        assert result["total_lines"] == len(content.split("\n"))
        assert result["fatal_error_blocks_in_window"] == 1
        # The partial block is kept as the oldest one, after the newer whole
        # block and in its own line order.
        lines = result["log"].split("\n")
        assert lines[: len(_ABORT_BLOCK)] == _ABORT_BLOCK
        partial = lines[len(_ABORT_BLOCK) :]
        start = frames.index(partial[0])
        assert partial == frames[start : start + 2]

    async def test_search_keeps_whole_matching_blocks(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        await _write_fault_log(hass, _TWO_CRASHES)

        # Only the abort block mentions core.py; it comes back intact, header
        # first, rather than as the single matching frame.
        result = await _get(component_core_client, search="core.py")

        assert result["filters_applied"] == {"search": "core.py"}
        assert result["matched_blocks"] == 1
        assert result["log"].split("\n") == _ABORT_BLOCK
        # Counted on the window, before the search narrowed it.
        assert result["fatal_error_blocks_in_window"] == 2

    async def test_pagination_hint_keeps_the_search_filter(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        await _write_fault_log(hass, _TWO_CRASHES)

        result = await _get(component_core_client, search="fatal", limit=2)

        assert result["has_more"] is True
        assert result["pagination_hint"] == (
            "ha_get_logs(source='fault_log', offset=2, limit=2, "
            "order='newest', search='fatal')"
        )

    async def test_search_is_case_insensitive_across_blocks(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        await _write_fault_log(hass, _TWO_CRASHES)

        result = await _get(component_core_client, search="FATAL PYTHON")

        assert result["matched_blocks"] == 2
        assert result["log"].split("\n") == _ABORT_BLOCK + _SEGV_BLOCK


class TestFailures:
    async def test_absent_component_explains_unavailable_read(
        self, core_client: HomeAssistantClient
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await _get(core_client)
        assert error_body(exc_info)["error"]["code"] == "COMPONENT_NOT_INSTALLED"

    async def test_other_read_failure_is_service_call_failed(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A file Home Assistant cannot open is a read failure with a cause
        # other than the allowlist or a missing file. Simulated, because root
        # ignores mode bits.
        fault_file = await _write_fault_log(hass, "Fatal Python error: Aborted\n")
        real_open = Path.open

        def refusing_open(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == fault_file:
                raise PermissionError(13, "Permission denied")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", refusing_open)

        with pytest.raises(ToolError) as exc_info:
            await _get(component_core_client)

        payload = error_body(exc_info)
        assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "Permission denied" in payload["error"]["message"]
        suggestion = payload["error"]["suggestion"]
        assert FAULT_LOG_FILENAME in suggestion
        assert "readable" in suggestion


class TestParameterWarnings:
    async def test_level_is_reported_as_ignored_and_offset_is_not(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        await _write_fault_log(hass, "")

        result = await _get(component_core_client, level="ERROR", offset=5)

        joined = "\n".join(result["warnings"])
        assert "Parameter 'level' only applies" in joined
        assert "offset" not in joined
