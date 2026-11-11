"""``ha_config_set_helper`` rules that are Core's to decide, on a real ``hass``.

A new tag's id, what an ``input_datetime`` needs, and which names collide with
an existing helper are Core's own answers here, reached through the server's
client and the component's commands.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_helpers import HelperConfigTools


@pytest.fixture
async def tools(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> AsyncIterator[HelperConfigTools]:
    # ``config`` serves the registry commands.
    for domain in ("config", "input_boolean", "input_datetime", "tag"):
        assert await async_setup_component(hass, domain, {})
    yield HelperConfigTools(component_core_client)
    await hass.async_block_till_done()


async def test_a_tag_created_without_an_id_gets_the_id_core_generates(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """The tool sends no id of its own making: Core's is a UUID in its
    canonical form, as a tag made in the HA UI gets."""
    await tools.ha_config_set_helper(helper_type="tag", name="Front Door")

    (tag,) = [
        entry for entry in er.async_get(hass).entities.values() if entry.domain == "tag"
    ]
    assert str(uuid.UUID(tag.unique_id)) == tag.unique_id


async def test_core_decides_which_helper_needs_a_name(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """A tag needs no name in Core and an input_boolean does: the tool sends
    both as written and Core refuses the one it cannot create."""
    await tools.ha_config_set_helper(helper_type="tag", config={"tag_id": "front-door"})
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_helper(helper_type="input_boolean")

    assert "name" in json.loads(str(exc_info.value))["error"]["message"]
    assert hass.states.async_entity_ids("input_boolean") == []
    tags = [e for e in er.async_get(hass).entities.values() if e.domain == "tag"]
    assert [tag.unique_id for tag in tags] == ["front-door"]


async def test_an_input_datetime_without_date_or_time_is_cores_to_refuse(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """Core requires one of the two and says so; the tool does not pick."""
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_helper(helper_type="input_datetime", name="Wake")

    assert "at least a date or a time" in str(exc_info.value)
    assert hass.states.async_entity_ids("input_datetime") == []

    await tools.ha_config_set_helper(
        helper_type="input_datetime", name="Wake", config={"has_time": True}
    )

    state = hass.states.get("input_datetime.wake")
    assert state is not None
    assert state.attributes["has_time"] is True
    assert state.attributes["has_date"] is False


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            {"helper_type": "input_boolean", "name": "Night Mode"},
            {"helper_type": "input_boolean", "name": "night-mode!"},
        ),
        (
            {
                "helper_type": "template",
                "menu_choice": "sensor",
                "config": {"name": "Flow Probe", "state": "{{ 1 }}"},
            },
            {
                "helper_type": "template",
                "menu_choice": "sensor",
                "config": {"name": "flow_probe", "state": "{{ 2 }}"},
            },
        ),
    ],
    ids=["storage", "flow"],
)
async def test_a_name_core_would_number_after_an_existing_helper_is_refused(
    hass: HomeAssistant,
    tools: HelperConfigTools,
    first: dict[str, Any],
    second: dict[str, Any],
) -> None:
    """A name that differs only in case or punctuation gets the same id from
    Core, which would create ``..._2`` beside the helper the caller meant."""
    await tools.ha_config_set_helper(**first)

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_helper(**second)

    error = json.loads(str(exc_info.value))
    assert "already exists" in error["error"]["message"]
    domain = "sensor" if first["helper_type"] == "template" else "input_boolean"
    assert len(hass.states.async_entity_ids(domain)) == 1


@pytest.mark.parametrize(
    ("domain", "yaml", "name"),
    [
        ("person", [{"id": "pat1", "name": "Yaml Pat"}], "yaml pat"),
        ("input_boolean", {"yaml_flag": None}, "Yaml Flag"),
        ("zone", [], "Home"),
    ],
    ids=["a YAML person", "a YAML input_boolean", "Core's own home zone"],
)
async def test_a_helper_that_is_not_stored_is_found_as_a_duplicate(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    domain: str,
    yaml: Any,
    name: str,
) -> None:
    """Core numbers a new helper's entity after any entity of that id. One
    defined in YAML, or the home zone Core makes itself, is not among the
    stored helpers."""
    assert await async_setup_component(hass, domain, {domain: yaml})
    before = hass.states.async_entity_ids(domain)

    with pytest.raises(ToolError) as exc_info:
        await HelperConfigTools(component_core_client).ha_config_set_helper(
            helper_type=domain, name=name
        )

    message = json.loads(str(exc_info.value))["error"]["message"]
    assert "already exists" in message and "not a stored helper" in message
    assert hass.states.async_entity_ids(domain) == before


async def test_an_id_core_has_reserved_is_found_after_its_entity_was_renamed(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """A YAML helper keeps its id in the id manager Core generates a new
    helper's id with, whatever its entity is called now: Core would store
    the new one as ``yaml_flag_2``."""
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"yaml_flag": None}}
    )
    er.async_get(hass).async_update_entity(
        "input_boolean.yaml_flag", new_entity_id="input_boolean.renamed"
    )
    await hass.async_block_till_done()

    with pytest.raises(ToolError) as exc_info:
        await HelperConfigTools(component_core_client).ha_config_set_helper(
            helper_type="input_boolean", name="Yaml Flag"
        )

    assert "not a stored helper" in json.loads(str(exc_info.value))["error"]["message"]
    assert hass.states.async_entity_ids("input_boolean") == ["input_boolean.renamed"]


async def test_without_the_component_a_create_says_what_went_unchecked(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Which names Core numbers is Core's slugify, read through the component.
    Without it only an exact name is refused, and the response says so."""
    for domain in ("config", "input_boolean"):
        assert await async_setup_component(hass, domain, {})

    created = await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="input_boolean", name="Night Mode"
    )
    await hass.async_block_till_done()

    (warning,) = created["warnings"]
    assert "differs only in case or punctuation" in warning
    assert "ha_mcp_tools component" in warning


async def test_with_the_component_a_create_carries_no_duplicate_warning(
    tools: HelperConfigTools,
) -> None:
    created = await tools.ha_config_set_helper(
        helper_type="input_boolean", name="Night Mode"
    )

    assert not any("ha_mcp_tools" in w for w in created.get("warnings", []))
