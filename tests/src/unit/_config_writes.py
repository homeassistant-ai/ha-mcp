"""Automation and script writes through the tools, on a real ``hass``.

The tools write to Core's own config views, which keep automations.yaml and
scripts.yaml in Core's config directory, which the conftest's
``hass_config_dir`` makes a temporary directory of each test's own.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import entity_registration
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

# configuration.yaml includes the two files Core's config views write and reload.
_CONFIGURATION = {
    "configuration.yaml": (
        "automation: !include automations.yaml\nscript: !include scripts.yaml\n"
    ),
    "automations.yaml": "[]\n",
    "scripts.yaml": "{}\n",
}


async def _config_component(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The new entity is in Core when the write returns; no need to poll for it.
    monkeypatch.setattr(entity_registration, "RESOLVE_TIMEOUT", 0)
    for name, text in _CONFIGURATION.items():
        await hass.async_add_executor_job(Path(hass.config.path(name)).write_text, text)
    assert await async_setup_component(hass, "config", {})


async def automation_tools(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AutomationConfigTools:
    """The automation tools on a ``hass`` that keeps its automations in automations.yaml."""
    await _config_component(hass, monkeypatch)
    assert await async_setup_component(hass, "automation", {})
    return AutomationConfigTools(client)


async def script_tools(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> ConfigScriptTools:
    """The script tools on a ``hass`` whose scripts live in scripts.yaml."""
    await _config_component(hass, monkeypatch)
    assert await async_setup_component(hass, "script", {})
    return ConfigScriptTools(client)
