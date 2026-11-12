"""``ha_get_logs(source="fault_log")`` (issue #2373), on a real ``hass``.

The source reads ``home-assistant.log.fault`` through the File & YAML Tools
entry's ``read_file`` service behind the live tools-entry probe. Core sets that
entry up, the test writes the file into Core's config directory (this test's
``tmp_path``), and the tool runs on ``core_client``, so every reply is the
component's own.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

import custom_components.ha_mcp_tools as component
from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import DOMAIN, TOOLS_ENTRY_TITLE
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.log_common import DEFAULT_LOG_LIMIT
from ha_mcp.tools.log_sources_fault import (
    FAULT_LOG_PATH,
    FAULT_LOG_WINDOW_LINES,
    MIN_COMPONENT_VERSION_FAULT_LOG,
)
from ha_mcp.tools.tools_filesystem import call_mcp_tools_service
from ha_mcp.tools.tools_logs import LogTools

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


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Core's config directory, where the fault log lives, is this test's own."""
    return hass_tmp_config_dir


@pytest.fixture
async def tools_entry(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> MockConfigEntry:
    """The File & YAML Tools entry, set up by Core, which serves ``read_file``."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TOOLS_ENTRY_TITLE,
        version=HaMcpToolsConfigFlow.VERSION,
        minor_version=HaMcpToolsConfigFlow.MINOR_VERSION,
        data={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _fault_file(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(FAULT_LOG_PATH))


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


def _parse_tool_error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(str(exc_info.value))
    return payload


async def _get(client: HomeAssistantClient, **overrides: Any) -> dict[str, Any]:
    return await LogTools(client).get_logs(**_call_kwargs(**overrides))


@pytest.mark.usefixtures("tools_entry")
class TestHealthyInstall:
    async def test_empty_file_reports_no_crash(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        path = await _write_fault_log(hass, "")
        mtime = datetime(2026, 9, 5, 10, 0).timestamp()
        await hass.async_add_executor_job(os.utime, path, (mtime, mtime))

        result = await _get(core_client)

        assert result["success"] is True
        assert result["source"] == "fault_log"
        assert result["path"] == FAULT_LOG_PATH
        assert result["crash_recorded"] is False
        assert result["log"] == ""
        assert result["returned_lines"] == 0
        assert result["has_more"] is False
        assert "No native crash recorded" in result["message"]
        # The file's modification time is passed through.
        assert datetime.fromisoformat(result["modified"]).timestamp() == mtime

    async def test_missing_file_reports_no_crash(
        self, core_client: HomeAssistantClient
    ) -> None:
        result = await _get(core_client)

        assert result["success"] is True
        assert result["crash_recorded"] is False
        assert result["total_lines"] == 0
        assert "No native crash recorded" in result["message"]


@pytest.mark.usefixtures("tools_entry")
class TestRecordedCrash:
    async def test_newest_orders_blocks_and_keeps_lines_readable(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await _write_fault_log(hass, _TWO_CRASHES)

        result = await _get(core_client)

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
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await _write_fault_log(hass, _TWO_CRASHES)

        result = await _get(core_client, order="oldest")

        assert result["log"].split("\n") == _SEGV_BLOCK + _ABORT_BLOCK

    async def test_limit_and_offset_page_the_assembled_text(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await _write_fault_log(hass, _TWO_CRASHES)

        first = await _get(core_client, limit=3)
        assert first["log"].split("\n") == _ABORT_BLOCK[:3]
        assert first["has_more"] is True
        assert first["next_offset"] == 3
        assert first["pagination_hint"] == (
            "ha_get_logs(source='fault_log', offset=3, limit=3, order='newest')"
        )

        second = await _get(core_client, limit=3, offset=3)
        assert second["log"].split("\n") == (_ABORT_BLOCK + _SEGV_BLOCK)[3:6]
        assert second["offset"] == 3
        assert second["has_more"] is True

        last = await _get(core_client, limit=3, offset=6)
        assert last["log"].split("\n") == (_ABORT_BLOCK + _SEGV_BLOCK)[6:9]
        assert last["has_more"] is False
        assert "next_offset" not in last

    async def test_header_survives_a_block_longer_than_limit(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        frames = [f'  File "/x.py", line {i} in f{i}' for i in range(150)]
        block = ["Fatal Python error: Segmentation fault", "", *frames]
        await _write_fault_log(hass, "\n".join(block) + "\n")

        result = await _get(core_client)

        assert result["limit"] == DEFAULT_LOG_LIMIT
        assert result["log"].split("\n")[0] == "Fatal Python error: Segmentation fault"
        assert result["returned_lines"] == DEFAULT_LOG_LIMIT
        assert result["has_more"] is True
        assert result["fatal_error_blocks_in_window"] == 1

    async def test_leading_partial_block_is_kept_oldest(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        # The older block's header lies beyond the window the read tails, so
        # the window opens with that block's frames.
        frames = [
            f'  File "/old.py", line {i} in tail' for i in range(FAULT_LOG_WINDOW_LINES)
        ]
        older = ["Fatal Python error: Segmentation fault", "", *frames]
        content = "\n".join(older + _ABORT_BLOCK) + "\n"
        await _write_fault_log(hass, content)

        result = await _get(core_client, limit=len(_ABORT_BLOCK) + 2)

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
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await _write_fault_log(hass, _TWO_CRASHES)

        # Only the abort block mentions core.py; it comes back intact, header
        # first, rather than as the single matching frame.
        result = await _get(core_client, search="core.py")

        assert result["filters_applied"] == {"search": "core.py"}
        assert result["matched_blocks"] == 1
        assert result["log"].split("\n") == _ABORT_BLOCK
        # Counted on the window, before the search narrowed it.
        assert result["fatal_error_blocks_in_window"] == 2

    async def test_pagination_hint_keeps_the_search_filter(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await _write_fault_log(hass, _TWO_CRASHES)

        result = await _get(core_client, search="fatal", limit=2)

        assert result["has_more"] is True
        assert result["pagination_hint"] == (
            "ha_get_logs(source='fault_log', offset=2, limit=2, "
            "order='newest', search='fatal')"
        )

    async def test_search_is_case_insensitive_across_blocks(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await _write_fault_log(hass, _TWO_CRASHES)

        result = await _get(core_client, search="FATAL PYTHON")

        assert result["matched_blocks"] == 2
        assert result["log"].split("\n") == _ABORT_BLOCK + _SEGV_BLOCK

    async def test_always_reads_the_full_window(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await _write_fault_log(hass, _TWO_CRASHES)

        with patch.object(
            core_client, "call_service", wraps=core_client.call_service
        ) as spy:
            await _get(core_client, limit=5)

        reads = [c for c in spy.await_args_list if c.args[:2] == (DOMAIN, "read_file")]
        assert len(reads) == 1
        payload = reads[0].args[2]
        assert payload["path"] == FAULT_LOG_PATH
        assert payload["tail_lines"] == FAULT_LOG_WINDOW_LINES


class TestFailures:
    @pytest.mark.usefixtures("tools_entry")
    async def test_path_not_allowed_means_component_too_old(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A component older than MIN_COMPONENT_VERSION_FAULT_LOG: its read
        # allowlist does not carry the fault log yet.
        await _write_fault_log(hass, _TWO_CRASHES)
        monkeypatch.setattr(
            component,
            "ALLOWED_READ_FILES",
            [f for f in component.ALLOWED_READ_FILES if f != FAULT_LOG_PATH],
        )

        with pytest.raises(ToolError) as exc_info:
            await _get(core_client)

        payload = _parse_tool_error(exc_info)
        assert payload["error"]["code"] == "COMPONENT_NOT_INSTALLED"
        assert MIN_COMPONENT_VERSION_FAULT_LOG in payload["error"]["message"]
        # create_error_response spreads ``context`` beside ``error``.
        assert payload["source"] == "fault_log"

    @pytest.mark.usefixtures("tools_entry")
    async def test_other_read_failure_is_service_call_failed(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        # A file the component cannot decode is a read failure with a cause
        # other than the allowlist or a missing file.
        await _write_fault_log(hass, b"Fatal Python error: \xff\xfe\n")

        with pytest.raises(ToolError) as exc_info:
            await _get(core_client)

        payload = _parse_tool_error(exc_info)
        assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "Cannot read binary file" in payload["error"]["message"]
        suggestions = payload["error"]["suggestions"]
        assert any(FAULT_LOG_PATH in sug for sug in suggestions)
        assert any("readable" in sug for sug in suggestions)

    async def test_live_tools_entry_probe_runs_before_the_read(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools_entry: MockConfigEntry,
    ) -> None:
        # The client cached its caller token while the entry was loaded; then
        # the entry was removed. The probe, not the service call, must be what
        # fails, with the actionable "add the entry" error.
        await _write_fault_log(hass, _TWO_CRASHES)
        await call_mcp_tools_service(core_client, "read_file", {"path": FAULT_LOG_PATH})
        assert await hass.config_entries.async_unload(tools_entry.entry_id)

        with (
            patch.object(
                core_client, "call_service", wraps=core_client.call_service
            ) as spy,
            pytest.raises(ToolError) as exc_info,
        ):
            await _get(core_client)

        assert _parse_tool_error(exc_info)["error"]["code"] == (
            "COMPONENT_NOT_INSTALLED"
        )
        assert not [
            c for c in spy.await_args_list if c.args[:2] == (DOMAIN, "read_file")
        ]


@pytest.mark.usefixtures("tools_entry")
class TestParameterWarnings:
    async def test_level_is_reported_as_ignored_and_offset_is_not(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await _write_fault_log(hass, "")

        result = await _get(core_client, level="ERROR", offset=5)

        joined = "\n".join(result["warnings"])
        assert "Parameter 'level' only applies" in joined
        assert "offset" not in joined
