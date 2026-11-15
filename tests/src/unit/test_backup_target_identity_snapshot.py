"""A backup snapshots the helper the write changes, and says when it cannot.

``ha_config_set_helper`` and ``ha_remove_helpers_integrations`` resolve a
storage helper as ``stored_target`` does: a storage id first, else the entity
registry entry whose entity_id the target is (``<type>.`` optional), whose
``unique_id`` is the helper's storage id. A renamed entity can carry another
helper's id as its object id, so the storage id wins; the snapshot resolves
the same way, so it holds the helper the write changes (#2632).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_integrations
from ha_mcp.tools.tools_config_helpers import HelperConfigTools

from ._backup_core import backup_settings as backup_settings
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_target_identity_core import (
    create_named,
    create_tag,
    entity_of,
    held_by,
    move_entity,
    remove_tag,
    tag_ids,
)
from ._helper_tool_core import set_up, stored_item
from ._tool_errors import rejected_call_body


async def _hall_and_porch(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    registry: er.EntityRegistry,
) -> None:
    """Helper "hall" renamed to input_boolean.porch; helper "porch" now has the
    entity input_boolean.porch_2."""
    await set_up(hass, "input_boolean")
    hall = await create_named(client, "input_boolean", "Hall")
    porch = await create_named(client, "input_boolean", "Porch")
    move_entity(registry, "input_boolean", porch, "input_boolean.porch_2")
    move_entity(registry, "input_boolean", hall, "input_boolean.porch")


@pytest.mark.parametrize(
    ("target", "helper"),
    [("porch", "porch"), ("input_boolean.porch", "hall")],
    ids=["storage-id-wins", "entity-id-is-looked-up"],
)
async def test_a_storage_id_wins_over_a_renamed_entity_with_its_text(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    target: str,
    helper: str,
) -> None:
    """The write changes "porch", whose entity is input_boolean.porch_2; only
    the entity_id form follows input_boolean.porch to the helper "hall"."""
    await _hall_and_porch(hass, core_client, entity_registry)

    held = await held_by(snapshot_manager, core_client, "helper_input_boolean", target)

    assert held is not None
    assert held["id"] == helper


async def test_an_entity_of_no_listed_helper_snapshots_nothing(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
) -> None:
    """The write fails on the registry's unique_id; the object id names
    another helper, which a snapshot of it would mislabel."""
    await set_up(hass, "input_boolean")
    porch = await create_named(core_client, "input_boolean", "Porch")
    move_entity(entity_registry, "input_boolean", porch, "input_boolean.porch_2")
    entity_registry.async_get_or_create(
        "input_boolean", "input_boolean", "yaml_only", suggested_object_id="porch"
    )

    held = await held_by(
        snapshot_manager, core_client, "helper_input_boolean", "input_boolean.porch"
    )

    assert held is None


async def test_an_entity_id_of_another_type_snapshots_nothing(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    """The write takes counter.porch for input_boolean.counter.porch and
    fails; a snapshot of helper "porch" would back up a helper not written."""
    await set_up(hass, "input_boolean")
    await create_named(core_client, "input_boolean", "Porch")

    other_type = await held_by(
        snapshot_manager, core_client, "helper_input_boolean", "counter.porch"
    )
    own_type = await held_by(
        snapshot_manager, core_client, "helper_input_boolean", "input_boolean.porch"
    )

    assert other_type is None
    assert own_type is not None
    assert own_type["id"] == "porch"


async def test_a_helper_without_a_registry_entry_is_found_by_its_id(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
) -> None:
    """The writes find a stored id with or without a registry entry."""
    await set_up(hass, "input_boolean")
    porch = await create_named(core_client, "input_boolean", "Porch")
    entity_registry.async_remove(entity_of(entity_registry, "input_boolean", porch))

    held = await held_by(snapshot_manager, core_client, "helper_input_boolean", "porch")

    assert held is not None
    assert held["id"] == "porch"


async def test_a_tag_entity_id_with_no_entry_names_no_tag(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    """tag.abc-1 is no entity (Core's is tag.abc_1); the id abc-1 is not guessed
    from it."""
    await set_up(hass, "tag")
    await create_tag(core_client, "abc-1")

    unguessed = await held_by(snapshot_manager, core_client, "helper_tag", "tag.abc-1")
    entity = await held_by(snapshot_manager, core_client, "helper_tag", "tag.abc_1")

    assert unguessed is None
    assert entity is not None
    assert entity["id"] == "abc-1"


async def _tag_front_is_xyzs(hass: HomeAssistant, client: HomeAssistantClient) -> None:
    """Tag xyz, named "Front", has the entity tag.front; tag front, with
    tag.front_2, is removed again."""
    await set_up(hass, "tag")
    await create_tag(client, "xyz", "Front")
    await create_tag(client, "front")
    deleted = await client.send_websocket_message(
        {"type": "tag/delete", "tag_id": "front"}
    )
    assert deleted["success"], deleted


async def test_a_repeated_bare_id_removal_deletes_no_other_tag(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _tag_front_is_xyzs(hass, core_client)

    body = await rejected_call_body(remove_tag(core_client, "front"))

    assert body["error"]["code"] == "ENTITY_NOT_FOUND"
    assert await tag_ids(core_client) == ["xyz"]


async def test_an_entity_id_removes_the_tag_its_entity_belongs_to(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _tag_front_is_xyzs(hass, core_client)

    result = await remove_tag(core_client, "tag.front")

    assert await tag_ids(core_client) == []
    assert result["entity_ids"] == ["tag.front"]


async def test_an_unreadable_registry_does_not_stop_the_removal(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entity only names the reply; the delete goes by the stored id. Without
    Core's ``config`` commands the registry read is refused."""
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "tag", {})
    await create_tag(core_client, "front")
    waited = AsyncMock(wraps=tools_integrations.verify_entity_removed)
    monkeypatch.setattr(tools_integrations, "verify_entity_removed", waited)

    result = await remove_tag(core_client, "front", wait=True)

    assert await tag_ids(core_client) == []
    assert result["success"] is True
    assert result["entity_ids"] == []
    waited.assert_not_awaited()
    assert "registry could not be read" in result["warnings"][0]


async def test_a_readable_registry_names_the_entity_and_waits_for_its_removal(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The twin of the unreadable registry: the entity is named and waited on."""
    await set_up(hass, "tag")
    await create_tag(core_client, "front")
    waited = AsyncMock(wraps=tools_integrations.verify_entity_removed)
    monkeypatch.setattr(tools_integrations, "verify_entity_removed", waited)

    result = await remove_tag(core_client, "front", wait=True)

    assert await tag_ids(core_client) == []
    assert result["entity_ids"] == ["tag.front"]
    waited.assert_awaited_once()


async def test_an_unknown_tag_id_is_not_called_yaml_configured(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Tags have no YAML form; the id may be a name or a typo."""
    await set_up(hass, "tag")
    await create_tag(core_client, "front")

    error = (await rejected_call_body(remove_tag(core_client, "front_door")))["error"]

    assert error["code"] == "ENTITY_NOT_FOUND"
    assert "front_door" in error["message"]
    assert "YAML" not in error["message"] + error["suggestion"]
    assert await tag_ids(core_client) == ["front"]


async def test_an_update_by_a_storage_id_that_starts_with_the_type_finds_it(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A helper named "Input Boolean Hall" has the storage id input_boolean_hall;
    without the dot, the prefix check took that id for an entity_id."""
    await set_up(hass, "input_boolean")
    item_id = await create_named(core_client, "input_boolean", "Input Boolean Hall")
    assert item_id == "input_boolean_hall"

    await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="input_boolean", helper_id=item_id, name="Hall"
    )

    assert (await stored_item(core_client, "input_boolean", item_id))["name"] == "Hall"
