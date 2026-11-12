"""Read Only Mode reaches the real server whatever it was at startup.

The settings UI flips ``read_only_mode`` without a restart in standalone-HTTP
and embedded mode, so the catalog filter and the write blocker must be part of
the server built with the mode off as well as on, and read the live flag.
"""

from __future__ import annotations

import pytest

from ha_mcp._vendor.fastmcp import Client
from ha_mcp.config import get_global_settings
from ha_mcp.server import HomeAssistantSmartMCPServer

WRITE_TOOL = "ha_config_set_automation"


@pytest.mark.parametrize("on_at_startup", [False, True])
async def test_read_only_mode_toggled_after_startup_hides_and_blocks_writes(
    monkeypatch: pytest.MonkeyPatch, on_at_startup: bool
) -> None:
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "unused")
    monkeypatch.setenv("READ_ONLY_MODE", str(on_at_startup).lower())
    settings = get_global_settings()
    assert settings.read_only_mode is on_at_startup
    server = HomeAssistantSmartMCPServer()

    async with Client(server.mcp) as client:
        monkeypatch.setattr(settings, "read_only_mode", False)
        assert WRITE_TOOL in {t.name for t in await client.list_tools()}

        monkeypatch.setattr(settings, "read_only_mode", True)
        names = {t.name for t in await client.list_tools()}
        assert WRITE_TOOL not in names
        assert "ha_get_state" in names
        result = await client.call_tool(
            WRITE_TOOL,
            {"config": {"alias": "x", "triggers": [], "actions": []}},
            raise_on_error=False,
        )
    assert result.is_error
    assert "READ_ONLY_MODE" in result.content[0].text
