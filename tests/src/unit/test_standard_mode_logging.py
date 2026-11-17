"""Regression tests for standard-mode console logging.

``ha_mcp.utils.usage_logger`` attaches its ``StartupLogCollector`` to the root
logger at import time, so every entry point reaches its logging setup with a
root handler already installed — and ``logging.basicConfig`` is a no-op (level
*and* handler) once the root logger has one. The standard-mode entry points
(``ha-mcp-web``, stdio via ``main``) were therefore leaving the root logger at
WARNING with no console handler, dropping every INFO line — including the
annotated ``GET -> 405 (NORMAL for most non-SSE connections)`` line that
``browser_landing`` emits in place of the raw uvicorn access line
``ProbeAccessLogFilter`` drops.

Forcing the reconfiguration is only safe because the collector is kept out of
the sweep: ``basicConfig(force=True)`` removes *and closes* every existing root
handler, which would otherwise silently take ``ha_report_issue``'s startup
diagnostics with it.

Each test runs ``ha-mcp-web`` as its own process, connected to the test's
Core, reads what it writes to stderr, and asks its ``ha_report_issue`` for
the startup log.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import aiohttp

from ha_mcp._vendor.fastmcp import Client
from ha_mcp.client.rest_client import HomeAssistantClient

from ._core_client import free_port

_LANDING_LOG_TEXT = "GET -> 405 (NORMAL for most non-SSE connections)"
# _log_startup_version's `logger.info(f"ha-mcp {version}")`, rendered through
# the console format.
_VERSION_LINE = "ha_mcp.__main__ INFO: ha-mcp "
# uvicorn's own logger, which logs whatever ha-mcp's level, says it serves.
_SERVING = "Uvicorn running on"
# A library that set up logging before ha-mcp starts.
_FOREIGN_ROOT_HANDLER = (
    "import logging\nlogging.getLogger().addHandler(logging.NullHandler())\n"
)


@dataclass
class WebServer:
    """A running ``ha-mcp-web`` and the stderr lines read from it so far."""

    process: asyncio.subprocess.Process
    url: str
    stderr: list[str] = field(default_factory=list)

    async def read_until(self, text: str) -> None:
        """Read stderr until a line contains ``text``."""
        assert self.process.stderr is not None
        async with asyncio.timeout(60):
            async for raw in self.process.stderr:
                self.stderr.append(raw.decode(errors="replace"))
                if text in self.stderr[-1]:
                    return
        raise AssertionError(f"ha-mcp-web exited without logging {text!r}")

    async def probe(self) -> None:
        """A browser's GET on the MCP path, which answers 405 by design."""
        async with (
            aiohttp.ClientSession() as session,
            session.get(self.url) as response,
        ):
            assert response.status == 405
        await self.read_until(_LANDING_LOG_TEXT)

    async def startup_log(self) -> list[str]:
        """The startup log messages ``ha_report_issue`` puts into a report."""
        async with Client(self.url) as client:
            result = await client.call_tool(
                "ha_report_issue", {"fields": "startup_logs"}
            )
        assert isinstance(result.structured_content, dict)
        return [entry["message"] for entry in result.structured_content["startup_logs"]]


@asynccontextmanager
async def ha_mcp_web(
    core_client: HomeAssistantClient, *, log_level: str = "INFO", prelude: str = ""
) -> AsyncIterator[WebServer]:
    """``ha-mcp-web`` with ``LOG_LEVEL=log_level``, serving once it says so.

    ``prelude`` runs in the process before the entry point does."""
    port = free_port()
    env = {
        **os.environ,
        "HOMEASSISTANT_URL": core_client.base_url,
        "HOMEASSISTANT_TOKEN": core_client.token,
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(port),
        "LOG_LEVEL": log_level,
    }
    env.pop("MCP_SECRET_PATH", None)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        f"{prelude}from ha_mcp.__main__ import main_web\nmain_web()\n",
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    server = WebServer(process, f"http://127.0.0.1:{port}/mcp")
    try:
        await server.read_until(_SERVING)
        yield server
    finally:
        if process.returncode is None:
            process.terminate()
        async with asyncio.timeout(30):
            await process.communicate()


class TestStandardModeConsoleLogging:
    """The logging ``ha-mcp-web`` sets up before it serves."""

    async def test_installs_stderr_console_handler_at_configured_level(
        self, core_client: HomeAssistantClient
    ) -> None:
        """stdio's stdout carries the MCP protocol, so console logs go to
        stderr, at the level ``LOG_LEVEL`` names and no lower."""
        async with ha_mcp_web(core_client) as server:
            lines = server.stderr

        assert any(_VERSION_LINE in line for line in lines), (
            "standard mode must install a console handler on the root logger; "
            "the startup collector alone makes basicConfig a no-op"
        )
        assert not any(" DEBUG: " in line for line in lines)

    async def test_annotated_probe_405_line_reaches_stderr(
        self, core_client: HomeAssistantClient
    ) -> None:
        async with ha_mcp_web(core_client) as server:
            await server.probe()

            assert any(_LANDING_LOG_TEXT in line for line in server.stderr)

    async def test_startup_collector_survives_and_keeps_recording(
        self, core_client: HomeAssistantClient
    ) -> None:
        """ha_report_issue's startup-log capture must survive logging setup and
        keep recording the lines logged after it."""
        async with ha_mcp_web(core_client) as server:
            await server.probe()
            messages = await server.startup_log()

        assert any(message.startswith("ha-mcp ") for message in messages)
        assert _LANDING_LOG_TEXT in messages

    async def test_debug_level_also_keeps_startup_collector(
        self, core_client: HomeAssistantClient
    ) -> None:
        """At DEBUG, the level the OAuth/OIDC deployments are debugged at, the
        collector must not be lost either."""
        async with ha_mcp_web(core_client, log_level="DEBUG") as server:
            await server.probe()
            messages = await server.startup_log()

        assert any(" DEBUG: " in line for line in server.stderr)
        assert any(_LANDING_LOG_TEXT in line for line in server.stderr)
        assert _LANDING_LOG_TEXT in messages

    async def test_force_reconfigures_past_a_foreign_root_handler(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Pins the ``force=True`` default itself (Patch76's #2039 review).

        The wrapper detaches only the COLLECTOR, so a foreign root handler —
        pytest's, a library's, or the setup's own console handler on a repeat
        call — is what the default actually guards: with one present,
        ``force=False`` makes ``basicConfig`` a silent no-op again (no console
        handler, root stays WARNING). The collector-only cases above stay
        green on a default flip; this one goes red.
        """
        async with ha_mcp_web(core_client, prelude=_FOREIGN_ROOT_HANDLER) as server:
            messages = await server.startup_log()

        assert any(_VERSION_LINE in line for line in server.stderr), (
            "with a foreign root handler present, only force=True installs the "
            "console handler"
        )
        # The force sweep removed the foreign handler; the collector was
        # shielded by the wrapper and stays live.
        assert any(message.startswith("ha-mcp ") for message in messages)
