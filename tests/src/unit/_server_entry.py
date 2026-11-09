"""A server config entry in a real ``hass``, with the server itself replaced.

Home Assistant sets the entry up through its own config-entry machinery: the
component's ``async_setup_entry``, the device, the access switch platform. The
in-process server (its bring-up and teardown) and the sidebar panel are this
component's own collaborators and are replaced, so no server starts.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools import embedded_setup, ui_panel
from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import (
    CONF_ENTRY_TYPE,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    SERVER_ENTRY_TITLE,
)


def replace_server(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace the server bring-up / teardown and the sidebar panel."""
    server = SimpleNamespace(
        bring_up=AsyncMock(name="async_bring_up_server"),
        teardown=AsyncMock(name="async_teardown_server"),
        register_panel=AsyncMock(name="async_register_ui_panel"),
        unregister_panel=MagicMock(name="async_unregister_ui_panel"),
    )
    monkeypatch.setattr(embedded_setup, "async_bring_up_server", server.bring_up)
    monkeypatch.setattr(embedded_setup, "async_teardown_server", server.teardown)
    monkeypatch.setattr(ui_panel, "async_register_ui_panel", server.register_panel)
    monkeypatch.setattr(ui_panel, "async_unregister_ui_panel", server.unregister_panel)
    return server


def add_server_entry(
    hass: HomeAssistant,
    *,
    data: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """Add a server entry that already has its webhook id and secret path."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=SERVER_ENTRY_TITLE,
        version=HaMcpToolsConfigFlow.VERSION,
        minor_version=HaMcpToolsConfigFlow.MINOR_VERSION,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_SERVER,
            DATA_SECRET_PATH: "/private_x",
            DATA_WEBHOOK_ID: "mcp_x",
            **(data or {}),
        },
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


async def set_up(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    """Set the entry up the way Home Assistant does; True when it loaded."""
    loaded = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return loaded
