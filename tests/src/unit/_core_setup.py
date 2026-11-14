"""Set Core up for a unit test: an integration from YAML, a config entry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import DOMAIN, TOOLS_ENTRY_TITLE
from ha_mcp.client.rest_client import HomeAssistantClient


async def set_up_domain(hass: HomeAssistant, domain: str, config: Any) -> None:
    """Set ``domain`` up from ``config`` and wait for what it starts."""
    assert await async_setup_component(hass, domain, {domain: config})
    await hass.async_block_till_done()


def add_test_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A config entry of the ``test`` domain that devices and entities belong to."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    return entry


async def set_up_tools_entry(hass: HomeAssistant) -> None:
    """Core sets the File & YAML Tools entry up: its ``read_file`` service and
    the component's ``ha_mcp_tools/*`` commands."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TOOLS_ENTRY_TITLE,
        version=HaMcpToolsConfigFlow.VERSION,
        minor_version=HaMcpToolsConfigFlow.MINOR_VERSION,
        data={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)


async def with_config_component(
    hass: HomeAssistant, client: HomeAssistantClient
) -> HomeAssistantClient:
    """``client`` on a ``hass`` whose ``config`` integration answers the
    registry commands."""
    assert await async_setup_component(hass, "config", {})
    return client


def install_custom_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, domain: str
) -> None:
    """A custom helper integration installed where Core's loader looks."""
    integration = tmp_path / "extra" / "custom_components" / domain
    integration.mkdir(parents=True)
    manifest = {
        "domain": domain,
        "name": "My Helper",
        "config_flow": True,
        "integration_type": "helper",
        "version": "1.0.0",
        "documentation": "https://example.invalid",
        "codeowners": [],
        "requirements": [],
    }
    (integration / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.syspath_prepend(str(tmp_path / "extra"))


def write_secrets(hass: HomeAssistant, text: str) -> None:
    """Core's ``secrets.yaml`` with throwaway test values."""
    Path(hass.config.path("secrets.yaml")).write_text(text, encoding="utf-8")
