"""The one-time migration that clears a saved server package override."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools import entry_migration
from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import DOMAIN, OPT_PIP_SPEC


async def _migrate(
    hass: HomeAssistant, minor_version: int, options: dict[str, Any]
) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, minor_version=minor_version, options=options)
    entry.add_to_hass(hass)
    assert await entry_migration.async_migrate_entry(hass, entry) is True
    return entry


async def test_an_entry_from_before_the_update_loses_its_override(
    hass: HomeAssistant,
) -> None:
    entry = await _migrate(
        hass, 1, {OPT_PIP_SPEC: "ha-mcp-dev==9.0.0.dev1", "server_port": 9583}
    )
    assert dict(entry.options) == {"server_port": 9583}
    assert entry.minor_version == 2


async def test_an_override_saved_after_the_update_stays(hass: HomeAssistant) -> None:
    entry = await _migrate(hass, 2, {OPT_PIP_SPEC: "ha-mcp-dev==9.0.0.dev1"})
    assert dict(entry.options) == {OPT_PIP_SPEC: "ha-mcp-dev==9.0.0.dev1"}
    assert entry.minor_version == 2


def test_new_entries_start_migrated() -> None:
    assert HaMcpToolsConfigFlow.MINOR_VERSION == 2
