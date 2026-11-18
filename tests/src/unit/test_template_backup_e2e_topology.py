"""The no-component E2E branch must refuse capture before any HA mutation."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp import Client
from ha_mcp.client.rest_client import HomeAssistantClient

from ..e2e.utilities.assertions import MCPAssertions
from ..e2e.workflows.auto_backup.test_template_capture_restore import (
    TestTemplateHelperCaptureRestore as TemplateHelperScenario,
)
from ._real_server import build_server
from ._template_helper_core import create_template_sensor


async def test_missing_component_never_creates_or_edits_helper(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the real E2E scenario through the real server on Core without the
    component. Its absent-component branch must get the server's refusals and
    leave Home Assistant without a template helper it would have to clean up.
    """
    monkeypatch.setenv("E2E_NO_TOOLS_ENTRY", "1")
    monkeypatch.delenv("E2E_BACKEND", raising=False)
    monkeypatch.delenv("HAOS_TEST_MODE", raising=False)
    assert await async_setup_component(hass, "config", {})
    server = build_server(monkeypatch, core_client)

    async with Client(server.mcp) as mcp_client:
        await TemplateHelperScenario().test_template_options_full_loop(
            mcp_client=mcp_client,
            ha_client=core_client,
            ha_container_with_fresh_config={},
            template_type="sensor",
        )

    assert hass.config_entries.async_entries("template") == []


async def test_an_existing_template_helper_is_not_captured_without_the_component(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scenario above asks for a made-up id, which is refused either way.
    A real helper must be refused too: without the component the server cannot
    read its stored options, so nothing is saved and the helper is unchanged."""
    entry = await create_template_sensor(hass, "saved")
    entity_id = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)[
        0
    ].entity_id
    options = dict(entry.options)
    server = build_server(monkeypatch, core_client)

    async with Client(server.mcp) as mcp_client:
        mcp = MCPAssertions(mcp_client)
        refused = await mcp.call_tool_failure(
            "ha_manage_backup",
            {
                "scope": "edits",
                "action": "create",
                "domain": "helper_template",
                "entity_id": entity_id,
            },
        )
        listing = await mcp.call_tool_success(
            "ha_manage_backup",
            {
                "scope": "edits",
                "action": "list",
                "domain": "helper_template",
                "entity_id": entity_id,
            },
        )

    assert refused["error"]["code"] == "BACKUP_CAPTURE_FAILED"
    assert listing["data"]["backups"] == []
    assert dict(entry.options) == options
