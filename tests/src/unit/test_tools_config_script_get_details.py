"""``ha_config_get_script``: the script's registry row, load failure and references.

Core loads the test's own ``scripts.yaml``, so the registry rows, the loaded
entities and the repair issue of a script whose blueprint lacks a required
input are the ones Core makes. The component's ``automation_config`` lookup
answers them in process; without it the tool reads Core's registry and repairs
commands.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import dump

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import entity_registration
from ha_mcp.tools.automation_registry_details import (
    REFERENCES_OMITTED_WITHOUT_COMPONENT,
    references_not_loaded,
)
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

from ._component_frames import older_component
from ._config_dir_yaml import setup_from_configuration, write_config_files

_BLUEPRINT_PATH = "ha_mcp_test/flash.yaml"
_BLUEPRINT = """\
blueprint:
  name: Flash
  domain: script
  input:
    target:
      selector: {target: {}}
sequence:
  - action: light.turn_on
    target: !input target
"""
_WORKING = {
    "use_blueprint": {
        "path": _BLUEPRINT_PATH,
        "input": {"target": {"entity_id": ["light.porch"]}},
    }
}
# The blueprint's required input is missing, so Core fails to load it.
_FAILING = {"use_blueprint": {"path": _BLUEPRINT_PATH}}


@pytest.fixture(autouse=True)
async def scripts(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Core with ``working`` and ``failing`` loaded, and the registry and
    repairs commands the tool falls back to."""
    monkeypatch.setattr(entity_registration, "RESOLVE_TIMEOUT", 0)
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "repairs", {})
    await write_config_files(
        hass,
        {
            f"blueprints/script/{_BLUEPRINT_PATH}": _BLUEPRINT,
            "scripts.yaml": dump({"working": _WORKING, "failing": _FAILING}),
            "configuration.yaml": "script: !include scripts.yaml\n",
        },
    )
    await setup_from_configuration(hass, "script")


async def _get(client: HomeAssistantClient, script_id: str) -> dict[str, Any]:
    return await ConfigScriptTools(client).ha_config_get_script(script_id=script_id)


def _assert_failed_blueprint(got: dict[str, Any]) -> None:
    assert got["entity"]["entity_id"] == "script.failing"
    assert got["validation"]["status"] == "failed_blueprint"
    assert got["validation"]["error"]
    assert got["references"] is None


async def test_a_script_core_failed_to_load_reports_its_validation(
    component_core_client: HomeAssistantClient,
) -> None:
    got = await _get(component_core_client, "failing")

    _assert_failed_blueprint(got)
    assert got["warnings"] == [references_not_loaded("script")]


async def test_a_loaded_script_carries_the_references_core_reads(
    component_core_client: HomeAssistantClient,
) -> None:
    got = await _get(component_core_client, "working")

    assert got["entity"]["entity_id"] == "script.working"
    assert got["validation"] is None
    assert got["references"]["entities"] == ["light.porch"]
    assert got["references"]["blueprint"] == _BLUEPRINT_PATH
    assert "warnings" not in got


async def test_without_the_component_the_repair_issue_answers(
    core_client: HomeAssistantClient,
) -> None:
    got = await _get(core_client, "failing")

    _assert_failed_blueprint(got)
    assert got["warnings"] == [REFERENCES_OMITTED_WITHOUT_COMPONENT]


async def test_a_component_without_the_script_lookup_is_not_asked_for_it(
    monkeypatch: pytest.MonkeyPatch, component_core_client: HomeAssistantClient
) -> None:
    """A released component's lookup refuses ``domain``; the server takes the
    registry and repairs commands instead."""
    older_component(monkeypatch, "script_lookup")

    got = await _get(component_core_client, "failing")

    _assert_failed_blueprint(got)
    assert got["warnings"] == [REFERENCES_OMITTED_WITHOUT_COMPONENT]
