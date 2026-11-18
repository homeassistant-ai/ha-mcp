"""The no-component E2E branch must refuse capture before any HA mutation."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp import Client
from ha_mcp.client.rest_client import HomeAssistantClient

from ..e2e.workflows.auto_backup.test_template_capture_restore import (
    TestTemplateHelperCaptureRestore as TemplateHelperScenario,
)
from ._real_server import build_server


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
