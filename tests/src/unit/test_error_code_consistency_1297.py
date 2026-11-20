"""A missing label, category, zone, device, automation or script is reported as
``RESOURCE_NOT_FOUND`` with the ids that do exist (issue #1297).

Labels, categories, zones and devices are looked up by a registry id, not an
entity_id. An agent that branches on ``ENTITY_NOT_FOUND`` retries through
``ha_search()``, which lists none of them; the error must name the right tool
and the ids it can retry with. Every tool here runs on the server's own client
against a real ``hass``, so each registry row, list and 404 is Core's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_categories import CategoryTools
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools
from ha_mcp.tools.tools_labels import LabelTools
from ha_mcp.tools.tools_registry import RegistryTools
from ha_mcp.tools.tools_zones import ZoneTools

from ._automation_core import load_automations_file
from ._command_faults import dropped_on, pooled_socket
from ._config_writes import automation_tools, script_tools
from ._tool_errors import rejected_call_body

_SEQUENCE = [{"delay": {"seconds": 1}}]
_SCRIPTS = {
    "morning_routine": {"alias": "Morning routine", "sequence": []},
    "evening_routine": {"alias": "Evening routine", "sequence": []},
}
_AUTOMATIONS = [
    {"id": "1", "alias": "Morning routine", "triggers": [], "actions": []},
    {"id": "2", "alias": "Evening routine", "triggers": [], "actions": []},
]
_OTHER_AUTOMATION = [{"id": "3", "alias": "Something", "triggers": [], "actions": []}]


def _suggestions(body: dict[str, Any]) -> list[str]:
    """Every suggestion: a single one is under ``suggestion``, more under
    ``suggestions``."""
    error = body["error"]
    single = error.get("suggestion")
    return ([single] if single else []) + list(error.get("suggestions") or [])


async def _load_scripts(hass: HomeAssistant, scripts: dict[str, Any]) -> None:
    """Write ``scripts`` to scripts.yaml (JSON is YAML) and have Core reload them."""
    await hass.async_add_executor_job(
        Path(hass.config.path("scripts.yaml")).write_text, json.dumps(scripts)
    )
    await hass.services.async_call("script", "reload", blocking=True)
    await hass.async_block_till_done()


async def _stored_script_keys(hass: HomeAssistant) -> set[str]:
    stored = await hass.async_add_executor_job(
        load_yaml_dict, hass.config.path("scripts.yaml")
    )
    return set(stored)


@pytest.fixture
async def config_commands(hass: HomeAssistant) -> None:
    """Core's ``config`` commands, which the registries' WebSocket API is part of."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
async def scripts(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> ConfigScriptTools:
    """The script tools on a Core holding two scripts and an automation."""
    tools = await script_tools(hass, core_client, monkeypatch)
    assert await async_setup_component(hass, "automation", {})
    await _load_scripts(hass, _SCRIPTS)
    await load_automations_file(hass, "automations.yaml", _OTHER_AUTOMATION)
    return tools


@pytest.fixture
async def automations(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AutomationConfigTools:
    """The automation tools on a Core holding two automations and a script."""
    tools = await automation_tools(hass, core_client, monkeypatch)
    assert await async_setup_component(hass, "script", {})
    await load_automations_file(hass, "automations.yaml", _AUTOMATIONS)
    await _load_scripts(hass, {"something": {"sequence": []}})
    return tools


@pytest.mark.usefixtures("config_commands")
async def test_unknown_label_is_resource_not_found_with_the_label_ids(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Labels are registry metadata: the error points at ``ha_config_get_label``
    and lists the labels that exist, not the entity search."""
    label = lr.async_get(hass).async_create("Existing")

    body = await rejected_call_body(
        LabelTools(core_client).ha_config_get_label(label_id="missing")
    )

    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert any("ha_config_get_label" in s for s in _suggestions(body))
    assert body["available_label_ids"] == [label.label_id]


@pytest.mark.usefixtures("config_commands")
async def test_unknown_category_is_resource_not_found_with_the_category_ids(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    category = cr.async_get(hass).async_create(scope="automation", name="Existing")

    body = await rejected_call_body(
        CategoryTools(core_client).ha_config_get_category(
            scope="automation", category_id="missing"
        )
    )

    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert any("ha_config_get_category" in s for s in _suggestions(body))
    assert body["available_category_ids"] == [category.category_id]
    assert body["scope"] == "automation"


@pytest.mark.usefixtures("config_commands")
async def test_unknown_zone_is_resource_not_found_with_the_zone_ids(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A zone is asked for by its zone_id: the error points at ``ha_get_zone``."""
    assert await async_setup_component(hass, "zone", {})
    created = await core_client.send_websocket_message(
        {"type": "zone/create", "name": "Work", "latitude": 1.0, "longitude": 2.0}
    )
    assert created["success"], created

    body = await rejected_call_body(
        ZoneTools(core_client).ha_get_zone(zone_id="missing_zone")
    )

    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert any("ha_get_zone" in s for s in _suggestions(body))
    assert created["result"]["id"] in body["available_zone_ids"]


@pytest.mark.usefixtures("config_commands")
@pytest.mark.parametrize("tool", ["ha_get_device", "ha_remove_device"])
async def test_unknown_device_is_resource_not_found_with_the_device_ids(
    hass: HomeAssistant, core_client: HomeAssistantClient, tool: str
) -> None:
    """Devices are not entities: both device tools point at ``ha_get_device``
    and list the devices that exist."""
    entry = MockConfigEntry(domain="hue")
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    known = {
        registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={("hue", serial)}
        ).id
        for serial in ("a", "b")
    }

    body = await rejected_call_body(
        getattr(RegistryTools(core_client), tool)(device_id="missing-device-uuid")
    )

    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert any("ha_get_device" in s for s in _suggestions(body))
    assert set(body["available_device_ids"]) == known


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        pytest.param("ha_config_get_automation", {}, id="get"),
        pytest.param(
            "ha_config_set_automation",
            {"config": {"alias": "X", "triggers": [], "actions": []}},
            id="set",
        ),
        pytest.param("ha_config_remove_automation", {}, id="remove"),
    ],
)
async def test_missing_automation_lists_only_the_automations_that_exist(
    automations: AutomationConfigTools, tool: str, kwargs: dict[str, Any]
) -> None:
    """A read, update or removal of an automation nothing knows names the
    automations the caller can retry with; the script is not one of them."""
    body = await rejected_call_body(
        getattr(automations, tool)(entity_id="automation.missing", **kwargs)
    )

    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert body["automation_id"] == "automation.missing"
    assert sorted(body["available_automation_ids"]) == [
        "automation.evening_routine",
        "automation.morning_routine",
    ]
    assert any("ha_search(domain_filter='automation')" in s for s in _suggestions(body))


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        pytest.param("ha_config_get_script", {}, id="get"),
        pytest.param(
            "ha_config_set_script",
            {"config": {"sequence": _SEQUENCE}, "config_hash": "0" * 16},
            id="set",
        ),
        pytest.param("ha_config_remove_script", {}, id="remove"),
    ],
)
async def test_missing_script_lists_the_ids_of_the_scripts_that_exist(
    scripts: ConfigScriptTools, tool: str, kwargs: dict[str, Any]
) -> None:
    """The ids are entity_ids, which ``script_id`` takes as given (a bare slug
    can be another script's storage key); the automation is not one of them."""
    body = await rejected_call_body(
        getattr(scripts, tool)(script_id="missing_script", **kwargs)
    )

    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert body["script_id"] == "missing_script"
    assert sorted(body["available_script_ids"]) == [
        "script.evening_routine",
        "script.morning_routine",
    ]
    assert any("ha_search(domain_filter='script')" in s for s in _suggestions(body))


async def test_an_unreadable_entity_registry_still_reports_the_missing_script(
    hass: HomeAssistant, scripts: ConfigScriptTools, core_client: HomeAssistantClient
) -> None:
    """The id list is best effort: a registry list that gets no answer leaves
    it empty instead of hiding the not-found behind the transport failure."""
    socket = await pooled_socket(core_client)

    with dropped_on(hass, "config/entity_registry/list", socket):
        body = await rejected_call_body(
            scripts.ha_config_get_script(script_id="missing_script")
        )

    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert body["available_script_ids"] == []


@pytest.mark.parametrize("script_id", ["morning_routine", "script.morning_routine"])
async def test_get_script_takes_the_entity_id_or_the_bare_key(
    scripts: ConfigScriptTools, script_id: str
) -> None:
    """``ha_search`` hands out entity_ids; feeding one back must read the script."""
    result = await scripts.ha_config_get_script(script_id=script_id)

    assert result["script_id"] == "morning_routine"
    assert result["config"]["alias"] == "Morning routine"


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        pytest.param("ha_config_get_script", {}, id="get"),
        pytest.param(
            "ha_config_set_script", {"config": {"sequence": _SEQUENCE}}, id="set"
        ),
        pytest.param("ha_config_remove_script", {}, id="remove"),
    ],
)
async def test_bare_script_prefix_is_an_invalid_parameter(
    hass: HomeAssistant, scripts: ConfigScriptTools, tool: str, kwargs: dict[str, Any]
) -> None:
    """``script.`` is empty once the prefix is stripped: an input error, not a
    missing script, and nothing is written or removed."""
    body = await rejected_call_body(
        getattr(scripts, tool)(script_id="script.", **kwargs)
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert await _stored_script_keys(hass) == set(_SCRIPTS)


async def test_set_script_with_the_entity_id_updates_the_bare_key(
    hass: HomeAssistant, scripts: ConfigScriptTools
) -> None:
    """Not a new ``script.morning_routine`` storage key beside the script."""
    await scripts.ha_config_set_script(
        script_id="script.morning_routine",
        config={"alias": "Morning routine", "sequence": _SEQUENCE},
        wait=False,
    )

    stored = await hass.async_add_executor_job(
        load_yaml_dict, hass.config.path("scripts.yaml")
    )
    assert set(stored) == set(_SCRIPTS)
    assert stored["morning_routine"]["sequence"] == _SEQUENCE


async def test_remove_script_with_the_entity_id_removes_the_script(
    hass: HomeAssistant, scripts: ConfigScriptTools
) -> None:
    """The removal is waited for on the script's own entity, so it is gone
    when the call returns."""
    result = await scripts.ha_config_remove_script(script_id="script.morning_routine")

    assert result["success"] is True
    assert "warnings" not in result
    assert hass.states.get("script.morning_routine") is None
    assert await _stored_script_keys(hass) == {"evening_routine"}
