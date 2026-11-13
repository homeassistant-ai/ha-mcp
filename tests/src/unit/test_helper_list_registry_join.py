"""``ha_config_list_helpers`` shows a renamed helper's current entity (#1794).

Without the component the tool builds its records from Core's
``{helper_type}/list``, which carries the immutable storage ``id`` and the
creation-time ``name``. After a UI rename the current ``entity_id`` and display
name live in the entity registry, so the tool joins it: a renamed helper shows
its current ``entity_id`` and ``name`` and keeps the storage name as
``original_name``. The join is cosmetic, so a failed registry read degrades to
the plain list with a warning.

The tool runs on the server's own client against a real ``hass``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_helpers import HelperConfigTools


@pytest.fixture
def suffix() -> str:
    """Fresh storage ids no deleted registry entry can be restored for."""
    return uuid.uuid4().hex[:8]


async def _create_booleans(
    hass: HomeAssistant, client: HomeAssistantClient, *names: str
) -> list[str]:
    """Stored input_booleans; returns their storage ids."""
    assert await async_setup_component(hass, "input_boolean", {})
    ids = []
    for name in names:
        created = await client.send_websocket_message(
            {"type": "input_boolean/create", "name": name}
        )
        assert created["success"], created
        ids.append(created["result"]["id"])
    await hass.async_block_till_done()
    return ids


async def _list(client: HomeAssistantClient, helper_type: str) -> dict[str, Any]:
    result: dict[str, Any] = await HelperConfigTools(client).ha_config_list_helpers(
        helper_type=helper_type
    )
    assert result["success"] is True
    return result


async def test_a_renamed_helper_shows_its_current_entity_id_and_name(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    suffix: str,
) -> None:
    """Renamed in the UI (entity_id and name changed, storage id kept); a
    helper never renamed keeps its name and gains its entity_id."""
    assert await async_setup_component(hass, "config", {})
    dark, guest = await _create_booleans(
        hass, core_client, f"Dark {suffix}", f"Guest {suffix}"
    )
    entity_registry.async_update_entity(
        f"input_boolean.{dark}",
        new_entity_id=f"input_boolean.dark_mode_{suffix}",
        name="Dark Mode",
    )
    await hass.async_block_till_done()

    result = await _list(core_client, "input_boolean")

    by_id = {helper["id"]: helper for helper in result["helpers"]}
    assert by_id[dark]["entity_id"] == f"input_boolean.dark_mode_{suffix}"
    assert by_id[dark]["name"] == "Dark Mode"
    assert by_id[dark]["original_name"] == f"Dark {suffix}"
    assert by_id[guest]["entity_id"] == f"input_boolean.{guest}"
    assert by_id[guest]["name"] == f"Guest {suffix}"
    assert "warnings" not in result


async def test_an_entry_of_another_platform_with_the_same_id_is_not_joined(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    suffix: str,
) -> None:
    """The helper's own registry entry is gone and a sensor's entry carries
    its storage id as unique_id: the helper keeps its storage values."""
    assert await async_setup_component(hass, "config", {})
    (helper_id,) = await _create_booleans(hass, core_client, f"Shared {suffix}")
    entity_registry.async_remove(f"input_boolean.{helper_id}")
    entity_registry.async_get_or_create(
        "sensor", "template", helper_id, original_name="Some Sensor"
    )
    await hass.async_block_till_done()

    result = await _list(core_client, "input_boolean")

    (helper,) = result["helpers"]
    assert helper["id"] == helper_id
    assert helper["name"] == f"Shared {suffix}"
    assert "entity_id" not in helper


async def test_a_refused_registry_read_still_lists_the_helpers_with_a_warning(
    hass: HomeAssistant, core_client: HomeAssistantClient, suffix: str
) -> None:
    """Without ``config`` set up Core refuses the registry list command while
    ``input_boolean/list`` still answers."""
    (helper_id,) = await _create_booleans(hass, core_client, f"Dark {suffix}")

    result = await _list(core_client, "input_boolean")

    assert result["count"] == 1
    (helper,) = result["helpers"]
    assert helper["id"] == helper_id
    assert "entity_id" not in helper
    assert any("registry" in warning.lower() for warning in result["warnings"])


async def test_stored_and_yaml_persons_are_listed_and_joined(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    suffix: str,
) -> None:
    """Core's ``person/list`` splits stored and YAML persons; both are listed,
    counted and joined to the registry."""
    assert await async_setup_component(hass, "config", {})
    yaml_id = f"guest_{suffix}"
    assert await async_setup_component(
        hass, "person", {"person": [{"id": yaml_id, "name": "Guest"}]}
    )
    created = await core_client.send_websocket_message(
        {"type": "person/create", "name": f"Mirko {suffix}"}
    )
    assert created["success"], created
    stored_id = created["result"]["id"]
    await hass.async_block_till_done()
    stored_entity = entity_registry.async_get_entity_id("person", "person", stored_id)
    assert stored_entity is not None
    entity_registry.async_update_entity(stored_entity, name="Mirko Renamed")
    await hass.async_block_till_done()

    result = await _list(core_client, "person")

    assert result["count"] == 2
    by_id = {helper["id"]: helper for helper in result["helpers"]}
    assert by_id.keys() == {stored_id, yaml_id}
    assert by_id[stored_id]["entity_id"] == stored_entity
    assert by_id[stored_id]["name"] == "Mirko Renamed"
    assert by_id[stored_id]["original_name"] == f"Mirko {suffix}"
    assert by_id[yaml_id]["entity_id"] == "person.guest"
    assert by_id[yaml_id]["name"] == "Guest"
