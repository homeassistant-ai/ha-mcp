"""The world the ``ha_report_issue`` tests give a real Core: its error log,
a Supervisor for the add-on's own logs, and the server's startup log."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from homeassistant.const import KEY_DATA_LOGGING
from homeassistant.core import HomeAssistant

from ha_mcp.tools import tools_bug_report
from ha_mcp.utils.usage_logger import StartupLogCollector

# A signed token in the shape the redaction rules know; 108 characters.
JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)


@pytest.fixture
def write_error_log(hass: HomeAssistant, tmp_path: Path) -> Callable[[str], None]:
    """Write the file Core serves as ``home-assistant.log``. Asked for before
    ``core_client``: ``api`` only serves the log when Core knows the file at
    setup."""
    path = tmp_path / "home-assistant.log"
    path.write_text("", encoding="utf-8")
    hass.data[KEY_DATA_LOGGING] = str(path)

    def write(text: str) -> None:
        path.write_text(text, encoding="utf-8")

    return write


async def serve_supervisor(
    aiohttp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    own_logs: Callable[[web.Request], Awaitable[web.Response]],
) -> None:
    """Run as the add-on, with a Supervisor that answers ``/addons/self/logs``."""
    app = web.Application()
    app.router.add_get("/addons/self/logs", own_logs)
    supervisor = await aiohttp_client(app)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
    monkeypatch.setenv("SUPERVISOR_BASE_URL", str(supervisor.make_url("")).rstrip("/"))


def record_startup_log(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> StartupLogCollector:
    """A startup collector that caught ``message``, as the report's source.

    The process-wide collector only records the first minute after import, so
    the report reads this one instead.
    """
    collector = StartupLogCollector()
    collector.handle(
        logging.makeLogRecord(
            {
                "name": "FastMCP",
                "levelno": logging.INFO,
                "levelname": "INFO",
                "msg": message,
            }
        )
    )
    monkeypatch.setattr(tools_bug_report, "get_startup_logs", collector.get_logs)
    return collector


def url_body(url: str) -> str:
    """The issue body a new-issue link fills in."""
    return parse_qs(urlsplit(url).query)["body"][0]
