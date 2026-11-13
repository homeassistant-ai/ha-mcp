"""Error-log summaries order Core 2026.11's offset timestamps by instant.

From 2026.11 every log timestamp carries its UTC offset. Compared as text,
the repeated hour when daylight saving ends sorts backwards, and the
offset-less lines logged before the upgrade cannot be placed against them.
"""

import asyncio
import textwrap
from pathlib import Path
from typing import Any

import pytest
from homeassistant.const import KEY_DATA_LOGGING
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_logs import register_logs_tools


@pytest.fixture
def log_file(hass: HomeAssistant, tmp_path: Path) -> Path:
    """The file Core serves as its error log; asked for before ``core_client``,
    whose ``api`` setup only serves the log when Core knows the file."""
    path = tmp_path / "home-assistant.log"
    path.write_text("", encoding="utf-8")
    hass.data[KEY_DATA_LOGGING] = str(path)
    return path


async def _structured_summary(
    log_file: Path, client: HomeAssistantClient, log: str
) -> dict[str, Any]:
    """The structured ``ha_get_logs`` summary of ``log`` as Core serves it."""
    await asyncio.to_thread(log_file.write_text, log, "utf-8")
    mcp = FastMCP("error-log-offsets")
    register_logs_tools(mcp, client)
    async with Client(mcp) as mcp_client:
        result = await mcp_client.call_tool(
            "ha_get_logs", {"source": "error_log", "structured": True}
        )
    assert isinstance(result.structured_content, dict)
    return result.structured_content


async def test_daylight_saving_end_keeps_the_later_instant_last(
    log_file: Path, core_client: HomeAssistantClient
) -> None:
    # 01:45 EDT is 05:45Z; the 01:15 EST line after it is 06:15Z.
    log = textwrap.dedent("""        2026-11-01 01:45:00.000-04:00 ERROR (MainThread) [a.b.c] boom
        2026-11-01 01:15:00.000-05:00 ERROR (MainThread) [a.b.c] boom
    """)
    result = await _structured_summary(log_file, core_client, log)
    assert result["summary"]["window_end"] == "2026-11-01 01:15:00.000-05:00"
    assert result["top_issues"][0]["last_seen"] == "2026-11-01 01:15:00.000-05:00"


async def test_lines_from_before_the_upgrade_take_the_logs_offset(
    log_file: Path, core_client: HomeAssistantClient
) -> None:
    # 08:00 before the upgrade, 09:00 after it, both local time at UTC+10.
    log = textwrap.dedent("""        2026-11-02 08:00:00.000 ERROR (MainThread) [a.b.c] boom
        2026-11-02 09:00:00.000+10:00 ERROR (MainThread) [a.b.c] boom
    """)
    summary = (await _structured_summary(log_file, core_client, log))["summary"]
    assert summary["window_start"] == "2026-11-02 08:00:00.000"
    assert summary["window_end"] == "2026-11-02 09:00:00.000+10:00"


async def test_a_utc_offset_counts_as_the_logs_offset(
    log_file: Path, core_client: HomeAssistantClient
) -> None:
    # +00:00 is a zero offset, not a missing one: the offset-less line is
    # 10:30Z, not the 09:30Z it would be with the later line's +01:00.
    log = textwrap.dedent("""\
        2026-11-02 10:00:00.000+00:00 ERROR (MainThread) [a.b.c] boom
        2026-11-02 10:30:00.000 ERROR (MainThread) [a.b.c] boom
        2026-11-02 10:15:00.000+01:00 ERROR (MainThread) [a.b.c] boom
    """)
    result = await _structured_summary(log_file, core_client, log)
    assert result["summary"]["window_end"] == "2026-11-02 10:30:00.000"
    # 09:15Z, though it sorts between the other two as text
    assert result["summary"]["window_start"] == "2026-11-02 10:15:00.000+01:00"
    assert result["top_issues"][0]["first_seen"] == "2026-11-02 10:15:00.000+01:00"
