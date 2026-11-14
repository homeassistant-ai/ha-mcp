"""Automation, script and scene bodies come from Core's config files.

The component once served these bodies from an entity's ``raw_config``, which
lags the config file between a write and the next completed reload, so a get
racing a reload returned the pre-edit body. Every get now reads Core's config
API, which reads the file, even with the component installed; the category
comes from the entity's registry row. The bodies sit in ``automations.yaml``,
``scripts.yaml`` and ``scenes.yaml`` of the test's config directory.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.yaml import dump

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

from ._client_spies import sent_messages
from ._config_dir_yaml import setup_from_configuration, write_config_files
from ._tool_errors import error_of

# Written with the legacy ``trigger``/``platform`` and ``action``/``service``
# keys, which the get returns as Core stores them.
_AUTOMATION = {
    "id": "171",
    "alias": "Morning Routine",
    "trigger": [{"platform": "event", "event_type": "wake_up"}],
    "action": [{"service": "light.turn_on", "target": {"area_id": "bedroom"}}],
}
_SCRIPT = {"alias": "Morning Script", "sequence": [{"delay": {"seconds": 5}}]}
_SCENE = {
    "id": "movie_night",
    "name": "Movie Night",
    "entities": {"light.living_room": {"state": "on", "brightness": 50}},
}


def _categorize(hass: HomeAssistant, entity_id: str, scope: str) -> str:
    category = cr.async_get(hass).async_create(scope=scope, name=f"{scope} picks")
    er.async_get(hass).async_update_entity(
        entity_id, categories={scope: category.category_id}
    )
    return category.category_id


@pytest.fixture
async def stored(hass: HomeAssistant) -> None:
    await write_config_files(
        hass,
        {
            "automations.yaml": dump([_AUTOMATION]),
            "scripts.yaml": dump({"morning": _SCRIPT}),
            "scenes.yaml": dump([_SCENE]),
            "configuration.yaml": (
                "automation: !include automations.yaml\n"
                "script: !include scripts.yaml\n"
                "scene: !include scenes.yaml\n"
            ),
        },
    )
    await setup_from_configuration(hass, "automation", "script", "scene")


@pytest.mark.usefixtures("stored")
async def test_the_automation_body_is_the_stored_one_with_its_category(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    category = _categorize(hass, "automation.morning_routine", "automation")

    with sent_messages(component_core_client) as sent:
        resp = await AutomationConfigTools(
            component_core_client
        ).ha_config_get_automation(entity_id="automation.morning_routine")

    assert resp["automation_id"] == "automation.morning_routine"
    assert resp["config"] == {**_AUTOMATION, "category": category}
    assert "automation/config" not in [c.args[0]["type"] for c in sent.call_args_list]


@pytest.mark.usefixtures("stored")
async def test_an_automation_nothing_knows_is_not_found(
    component_core_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ToolError) as exc:
        await AutomationConfigTools(component_core_client).ha_config_get_automation(
            entity_id="ghost"
        )

    err = error_of(exc)
    assert (err["code"], err["message"]) == (
        "RESOURCE_NOT_FOUND",
        "Automation not found: ghost",
    )


@pytest.mark.usefixtures("stored")
async def test_the_script_body_is_the_stored_one_with_its_category(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """``config`` is the body alone: the storage key stays at the top level."""
    category = _categorize(hass, "script.morning", "script")

    resp = await ConfigScriptTools(component_core_client).ha_config_get_script(
        script_id="morning"
    )

    assert (resp["success"], resp["script_id"]) == (True, "morning")
    assert resp["config"] == {**_SCRIPT, "category": category}


@pytest.mark.usefixtures("stored")
async def test_a_script_entity_id_reads_the_same_body(
    component_core_client: HomeAssistantClient,
) -> None:
    resp = await ConfigScriptTools(component_core_client).ha_config_get_script(
        script_id="script.morning"
    )

    assert resp["script_id"] == "morning"
    assert resp["config"]["alias"] == "Morning Script"


@pytest.mark.usefixtures("stored")
async def test_a_script_nothing_knows_is_not_found(
    component_core_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ToolError) as exc:
        await ConfigScriptTools(component_core_client).ha_config_get_script(
            script_id="ghost"
        )

    err = error_of(exc)
    assert (err["code"], err["message"]) == (
        "RESOURCE_NOT_FOUND",
        "Script not found: ghost",
    )


@pytest.mark.usefixtures("stored")
async def test_the_scene_body_is_the_stored_one_with_its_category(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    category = _categorize(hass, "scene.movie_night", "scene")

    resp = await ConfigSceneTools(component_core_client).ha_config_get_scene(
        scene_id="movie_night"
    )

    assert (resp["success"], resp["scene_id"]) == (True, "movie_night")
    assert resp["config"] == _SCENE
    assert resp["category"] == category


@pytest.mark.usefixtures("stored")
async def test_a_scene_nothing_knows_is_entity_not_found(
    component_core_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ToolError) as exc:
        await ConfigSceneTools(component_core_client).ha_config_get_scene(
            scene_id="ghost"
        )

    err = error_of(exc)
    assert (err["code"], err["message"]) == (
        "ENTITY_NOT_FOUND",
        "Entity 'scene.ghost' not found",
    )
