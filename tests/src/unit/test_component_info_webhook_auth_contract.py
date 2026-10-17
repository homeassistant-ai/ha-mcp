"""Cross-seam contract test for the ``ha_mcp_tools/info`` ``webhook_auth`` field.

Pipes the REAL component ``_do_info(hass)`` through the REAL server-side
``get_component_caps`` negotiation into ``ha_report_issue``'s server-entry line
(#2584), so a name or shape drift between the component's additive field and
``ComponentCaps.webhook_auth`` fails here. Bridge pattern as in
``test_component_info_timezone_contract.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.tools import component_api
from ha_mcp.tools.tools_bug_report import BugReportTools

# Autouse: importing the fixture applies it to this module's tests too.
from .test_component_info_timezone_contract import _clear_caps_cache  # noqa: F401
from .test_component_ws_search import FakeConfigEntry, FakeHass, wsapi

DOMAIN = "ha_mcp_tools"
SERVER_ROW = {"entry_id": "a1", "title": "HA-MCP Server", "state": "loaded"}


async def _server_entry_line(hass: FakeHass, info_drop: str | None = None) -> Any:
    """``_detect_server_entry_status`` against a component serving ``hass``.

    ``info_drop`` removes that key from the real ``info`` reply, standing in
    for a component older than the field.
    """
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        assert command_type == "ha_mcp_tools/info"
        result = wsapi._do_info(hass)
        if info_drop is not None:
            del result[info_drop]
        return {"success": True, "result": result}

    ws.send_command = AsyncMock(side_effect=_send)
    client = MagicMock(base_url="http://ha.local:8123", token="tok")
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": [SERVER_ROW]}
    )
    with patch.object(
        component_api, "get_websocket_client", AsyncMock(return_value=ws)
    ):
        return await BugReportTools(client)._detect_server_entry_status()


def _server(options: dict[str, Any] | None = None) -> FakeConfigEntry:
    return FakeConfigEntry(DOMAIN, data={"entry_type": "server"}, options=options)


@pytest.mark.asyncio
async def test_configured_mode_reaches_the_report() -> None:
    tools = FakeConfigEntry(DOMAIN, data={"entry_type": "tools"})
    hass = FakeHass(config_entries=[tools, _server({"webhook_auth": "ha_auth"})])

    assert await _server_entry_line(hass) == "added (loaded), webhook auth: ha_auth"


@pytest.mark.asyncio
async def test_unset_option_reads_as_the_bring_up_default() -> None:
    hass = FakeHass(config_entries=[_server()])

    assert await _server_entry_line(hass) == "added (loaded), webhook auth: none"


@pytest.mark.asyncio
async def test_disabled_webhook_is_not_reported_as_open() -> None:
    """Local-only mode keeps the configured auth option, but no webhook exists;
    ``none`` would read as an unauthenticated public webhook."""
    options = {"webhook_auth": "none", "enable_webhook": False}
    hass = FakeHass(config_entries=[_server(options)])

    assert await _server_entry_line(hass) == "added (loaded), webhook auth: disabled"


def test_non_string_value_parses_to_none() -> None:
    caps = component_api._parse_caps(
        {"success": True, "result": {"schema_version": 1, "webhook_auth": 123}}
    )

    assert caps is not None
    assert caps.webhook_auth is None


@pytest.mark.asyncio
async def test_component_without_the_field_leaves_the_line_unchanged() -> None:
    """A component older than the field omits it; the report keeps the plain
    entry status rather than inventing a mode."""
    hass = FakeHass(config_entries=[_server({"webhook_auth": "ha_auth"})])

    line = await _server_entry_line(hass, info_drop="webhook_auth")

    assert line == "added (loaded)"


def test_no_server_entry_reports_no_mode() -> None:
    """Only the tools entry is set up (an external server): there is no
    in-process webhook, so the field is None."""
    tools = FakeConfigEntry(DOMAIN, data={"entry_type": "tools"})

    assert wsapi._do_info(FakeHass(config_entries=[tools]))["webhook_auth"] is None
