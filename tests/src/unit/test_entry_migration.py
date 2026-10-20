"""The one-time migration that clears a saved server package override."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import entry_migration  # noqa: E402
from custom_components.ha_mcp_tools.const import OPT_PIP_SPEC  # noqa: E402


def _migrate(minor_version: int, options: dict) -> MagicMock:
    hass = MagicMock(name="hass")
    entry = SimpleNamespace(minor_version=minor_version, options=options)
    assert asyncio.run(entry_migration.async_migrate_entry(hass, entry)) is True
    return hass.config_entries.async_update_entry


def test_an_entry_from_before_the_update_loses_its_override() -> None:
    update = _migrate(1, {OPT_PIP_SPEC: "ha-mcp-dev==9.0.0.dev1", "server_port": 9583})
    update.assert_called_once()
    assert update.call_args.kwargs == {
        "options": {"server_port": 9583},
        "minor_version": 2,
    }


def test_an_override_saved_after_the_update_stays() -> None:
    _migrate(2, {OPT_PIP_SPEC: "ha-mcp-dev==9.0.0.dev1"}).assert_not_called()


def test_new_entries_start_migrated() -> None:
    from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow

    assert HaMcpToolsConfigFlow.MINOR_VERSION == 2
