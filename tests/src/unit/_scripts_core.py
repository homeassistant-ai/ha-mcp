"""The script tools on a real ``hass`` whose scripts.yaml holds ``storage_key``,
shared by the script tool tests."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util.yaml import dump, load_yaml_dict

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import entity_registration
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

from ._config_dir_yaml import setup_from_configuration, write_config_files

KEY = "storage_key"
BODY = {"alias": "Morning", "sequence": [{"delay": {"seconds": 1}}]}
NEW_SEQUENCE = [{"delay": {"seconds": 5}}]
TRANSFORM = "config['mode'] = 'single'"
WRITE_MODES = ["full_config", "transform"]


@pytest.fixture
async def tools(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> ConfigScriptTools:
    """The script tools on a ``hass`` whose scripts.yaml holds ``storage_key``."""
    # A zero resolve budget is one registry read; storage_key is registered up
    # front, so that read finds it.
    monkeypatch.setattr(entity_registration, "RESOLVE_TIMEOUT", 0)
    await write_config_files(
        hass,
        {
            "configuration.yaml": "script: !include scripts.yaml\n",
            "scripts.yaml": dump({KEY: BODY}),
        },
    )
    await setup_from_configuration(hass, "script")
    return ConfigScriptTools(core_client)


async def stored_scripts(hass: HomeAssistant) -> dict[str, Any]:
    """The scripts Core's config view keeps in scripts.yaml."""
    return await hass.async_add_executor_job(
        load_yaml_dict, hass.config.path("scripts.yaml")
    )


async def write_arguments(
    tools: ConfigScriptTools, mode: str, script_id: str
) -> dict[str, Any]:
    """A full config, or a transform with the hash of a fresh read."""
    if mode == "transform":
        current = await tools.ha_config_get_script(script_id=script_id)
        return {"python_transform": TRANSFORM, "config_hash": current["config_hash"]}
    return {"config": {"alias": "Morning", "sequence": NEW_SEQUENCE}}
