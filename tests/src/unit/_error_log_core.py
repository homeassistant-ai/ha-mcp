"""``ha_get_logs`` over Core's own error log, for the structured error-log tests.

Core serves ``/api/error_log`` from the file in ``KEY_DATA_LOGGING``, so a test
writes the log text to that file and the tool reads it through the server's own
client. Core's ``api`` setup registers the view only when the file is known, so
``log_file`` must be requested before ``core_client``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from homeassistant.const import KEY_DATA_LOGGING
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_logs import register_logs_tools

# Lines the raw path reads behind the caller's limit to find a record's header.
HEADER_LOOKBACK_LINES = 200


@pytest.fixture
def log_file(hass: HomeAssistant, tmp_path: Path) -> Path:
    """The file Core serves as its error log, empty."""
    path = tmp_path / "home-assistant.log"
    path.write_text("", encoding="utf-8")
    hass.data[KEY_DATA_LOGGING] = str(path)
    return path


def numbered_log(count: int) -> str:
    """``count`` parseable log lines, each with its own message."""
    return "".join(
        f"2026-05-27 10:00:00.000 ERROR (MainThread) [a.b.c] issue {i}\n"
        for i in range(count)
    )


async def call_logs(client: HomeAssistantClient, **arguments: Any) -> dict[str, Any]:
    """``ha_get_logs`` called through FastMCP, so the schema validates the arguments."""
    mcp = FastMCP("error-log-structured")
    register_logs_tools(mcp, client)
    async with Client(mcp) as mcp_client:
        result = await mcp_client.call_tool("ha_get_logs", arguments)
    assert isinstance(result.structured_content, dict)
    return result.structured_content


async def serve_log(log_file: Path, text: str) -> None:
    """Make ``text`` the content of the log Core serves."""
    await asyncio.to_thread(log_file.write_text, text, "utf-8")


async def error_log(
    log_file: Path, client: HomeAssistantClient, text: str, **arguments: Any
) -> dict[str, Any]:
    """``ha_get_logs(source="error_log")`` over a log that holds ``text``."""
    await serve_log(log_file, text)
    return await call_logs(client, source="error_log", **arguments)


async def structured(
    log_file: Path, client: HomeAssistantClient, text: str, **arguments: Any
) -> dict[str, Any]:
    """The structured summary of a log that holds ``text``."""
    return await error_log(log_file, client, text, structured=True, **arguments)


@asynccontextmanager
async def expired_token_client(
    client: HomeAssistantClient,
) -> AsyncIterator[HomeAssistantClient]:
    """A client of the same Home Assistant whose token Core refuses."""
    stale = HomeAssistantClient(
        base_url=client.base_url, token="expired-token", verify_ssl=False
    )
    try:
        yield stale
    finally:
        await stale.close()
