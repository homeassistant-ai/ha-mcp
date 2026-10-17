"""Cross-seam contract test for the ``ha_mcp_tools/info`` ``webhook_auth`` field.

Pipes the REAL component ``_do_info(hass)`` into ``ha_report_issue``'s
server-entry line (#2584), so a name or shape drift between the component's
additive field and the report's reader fails here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_bug_report import BugReportTools

from .test_component_ws_search import FakeConfigEntry, FakeHass, wsapi

DOMAIN = "ha_mcp_tools"
SERVER_ROW = {"entry_id": "a1", "title": "HA-MCP Server", "state": "loaded"}


def _report(hass: FakeHass, info_drop: str | None = None) -> BugReportTools:
    """``ha_report_issue``'s probes against a component serving ``hass``.

    ``info_drop`` removes that key from the real ``info`` reply, standing in
    for a component older than the field.
    """

    async def _send(message: dict[str, Any]) -> dict[str, Any]:
        if message["type"] == "config_entries/get":
            return {"success": True, "result": [SERVER_ROW]}
        assert message["type"] == "ha_mcp_tools/info"
        result = wsapi._do_info(hass)
        if info_drop is not None:
            del result[info_drop]
        return {"success": True, "result": result}

    client = MagicMock(base_url="http://ha.local:8123", token="tok")
    client.send_websocket_message = AsyncMock(side_effect=_send)
    return BugReportTools(client)


def _server(options: dict[str, Any] | None = None) -> FakeConfigEntry:
    return FakeConfigEntry(DOMAIN, data={"entry_type": "server"}, options=options)


@pytest.mark.asyncio
async def test_configured_mode_reaches_the_report() -> None:
    tools = FakeConfigEntry(DOMAIN, data={"entry_type": "tools"})
    hass = FakeHass(config_entries=[tools, _server({"webhook_auth": "ha_auth"})])

    line = await _report(hass)._detect_server_entry_status()

    assert line == "added (loaded), webhook auth: ha_auth"


@pytest.mark.asyncio
async def test_changed_mode_reaches_the_next_report() -> None:
    """The mode is an entry option the user can change while a long-lived
    server keeps its client; a later report must not repeat the first answer."""
    server = _server({"webhook_auth": "ha_auth"})
    report = _report(FakeHass(config_entries=[server]))
    await report._detect_server_entry_status()

    server.options = {"webhook_auth": "legacy"}

    line = await report._detect_server_entry_status()
    assert line == "added (loaded), webhook auth: legacy"


@pytest.mark.asyncio
async def test_unset_option_reads_as_the_bring_up_default() -> None:
    hass = FakeHass(config_entries=[_server()])

    line = await _report(hass)._detect_server_entry_status()

    assert line == "added (loaded), webhook auth: none"


@pytest.mark.asyncio
async def test_disabled_webhook_is_not_reported_as_open() -> None:
    """Local-only mode keeps the configured auth option, but no webhook exists;
    ``none`` would read as an unauthenticated public webhook."""
    options = {"webhook_auth": "none", "enable_webhook": False}
    hass = FakeHass(config_entries=[_server(options)])

    line = await _report(hass)._detect_server_entry_status()

    assert line == "added (loaded), webhook auth: disabled"


@pytest.mark.asyncio
async def test_component_without_the_field_leaves_the_line_unchanged() -> None:
    """A component older than the field omits it; the report keeps the plain
    entry status rather than inventing a mode."""
    hass = FakeHass(config_entries=[_server({"webhook_auth": "ha_auth"})])

    line = await _report(hass, info_drop="webhook_auth")._detect_server_entry_status()

    assert line == "added (loaded)"


@pytest.mark.asyncio
async def test_failed_info_call_leaves_the_line_unchanged() -> None:
    """The entry status came from ``config_entries/get``; a failing ``info``
    call must not take it out of the report."""
    report = _report(FakeHass(config_entries=[_server()]))
    rows = {"success": True, "result": [SERVER_ROW]}
    report._client.send_websocket_message.side_effect = [rows, RuntimeError("boom")]

    assert await report._detect_server_entry_status() == "added (loaded)"


def test_no_server_entry_reports_no_mode() -> None:
    """Only the tools entry is set up (an external server): there is no
    in-process webhook, so the field is None."""
    tools = FakeConfigEntry(DOMAIN, data={"entry_type": "tools"})

    assert wsapi._do_info(FakeHass(config_entries=[tools]))["webhook_auth"] is None
