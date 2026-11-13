"""Deep search finds a renamed helper by its current name (issue #1794).

Core's storage-level helper record (``<type>/list``) carries the immutable
``id`` (the registry ``unique_id``) and the creation-time name. A UI rename
updates only the entity registry, so deep search reuses the ha_search
orchestrator's entity-registry snapshot (no request of its own) to score and
emit each helper's current ``entity_id`` and name, while still scoring the
storage name so config-body references and un-renamed helpers never regress.

``deep_search`` runs on the server's own client against a real ``hass``; the
snapshot is Core's own ``config/entity_registry/list`` answer.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.smart_search import SmartSearchTools

_REGISTRY_LIST = {"type": "config/entity_registry/list"}


@pytest.fixture
def suffix() -> str:
    """A storage id no deleted registry entry can be restored for."""
    return uuid.uuid4().hex[:8]


async def _create_boolean(
    hass: HomeAssistant, client: HomeAssistantClient, name: str
) -> str:
    """A stored input_boolean; returns its storage id."""
    assert await async_setup_component(hass, "input_boolean", {})
    created = await client.send_websocket_message(
        {"type": "input_boolean/create", "name": name}
    )
    assert created["success"], created
    await hass.async_block_till_done()
    helper_id: str = created["result"]["id"]
    return helper_id


async def _search_helpers(
    client: HomeAssistantClient, query: str, snapshot: Any
) -> list[dict[str, Any]]:
    result = await SmartSearchTools(client=client).deep_search(
        query=query, search_types=["helper"], limit=10, prefetched_registry=snapshot
    )
    helpers: list[dict[str, Any]] = result["helpers"]
    return helpers


async def _renamed(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    suffix: str,
) -> str:
    """``Old Name <suffix>`` renamed in the registry to ``New Name`` with a
    new entity_id; returns that entity_id."""
    helper_id = await _create_boolean(hass, client, f"Old Name {suffix}")
    new_entity_id = f"input_boolean.new_name_{suffix}"
    entity_registry.async_update_entity(
        f"input_boolean.{helper_id}", new_entity_id=new_entity_id, name="New Name"
    )
    await hass.async_block_till_done()
    return new_entity_id


@pytest.fixture
async def config(hass: HomeAssistant) -> None:
    """``config`` serves the registry list the snapshot is."""
    assert await async_setup_component(hass, "config", {})


@pytest.mark.usefixtures("config")
async def test_a_renamed_helper_is_found_by_its_current_name(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    suffix: str,
) -> None:
    new_entity_id = await _renamed(hass, core_client, entity_registry, suffix)
    snapshot = await core_client.send_websocket_message(_REGISTRY_LIST)

    (helper,) = await _search_helpers(core_client, "New Name", snapshot)

    assert helper["entity_id"] == new_entity_id
    assert helper["name"] == "New Name"
    assert helper["match_in_name"] is True


@pytest.mark.usefixtures("config")
async def test_the_pre_rename_name_still_finds_the_renamed_helper(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    suffix: str,
) -> None:
    """Config bodies keep the storage name: a search for it finds the helper
    through its storage record and emits the current values."""
    new_entity_id = await _renamed(hass, core_client, entity_registry, suffix)
    snapshot = await core_client.send_websocket_message(_REGISTRY_LIST)

    (helper,) = await _search_helpers(core_client, f"Old Name {suffix}", snapshot)

    assert helper["entity_id"] == new_entity_id
    assert helper["name"] == "New Name"
    assert helper["match_in_config"] is True


@pytest.mark.usefixtures("config")
async def test_a_helper_never_renamed_is_found_by_its_name(
    hass: HomeAssistant, core_client: HomeAssistantClient, suffix: str
) -> None:
    """Its registry entry has no name of its own, so its original name is
    the current one."""
    helper_id = await _create_boolean(hass, core_client, f"Kitchen Light {suffix}")
    snapshot = await core_client.send_websocket_message(_REGISTRY_LIST)

    (helper,) = await _search_helpers(core_client, f"Kitchen Light {suffix}", snapshot)

    assert helper["entity_id"] == f"input_boolean.{helper_id}"
    assert helper["name"] == f"Kitchen Light {suffix}"
    assert helper["match_in_name"] is True


@pytest.mark.usefixtures("config")
async def test_another_integrations_entry_with_the_helpers_id_is_not_used(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    suffix: str,
) -> None:
    """A unique_id is unique per platform only: a hue light registered after
    the helper with the helper's id must not lend the helper its entity."""
    helper_id = await _create_boolean(hass, core_client, f"Kitchen {suffix}")
    entity_registry.async_get_or_create(
        "light", "hue", helper_id, original_name=f"Kitchen {suffix}"
    )
    snapshot = await core_client.send_websocket_message(_REGISTRY_LIST)

    (helper,) = await _search_helpers(core_client, f"Kitchen {suffix}", snapshot)

    assert helper["entity_id"] == f"input_boolean.{helper_id}"


@pytest.mark.parametrize("snapshot_read", [False, True], ids=["none", "refused"])
async def test_without_a_usable_snapshot_the_storage_name_still_matches(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    suffix: str,
    snapshot_read: bool,
) -> None:
    """A direct deep_search caller hands down no snapshot, and without
    ``config`` set up Core refuses the registry read: the helper branch
    degrades to the storage name rather than trusting a failed read."""
    await _renamed(hass, core_client, entity_registry, suffix)
    snapshot = None
    if snapshot_read:
        snapshot = await core_client.send_websocket_message(_REGISTRY_LIST)
        assert snapshot["success"] is False

    (helper,) = await _search_helpers(core_client, f"Old Name {suffix}", snapshot)

    assert helper["name"] == f"Old Name {suffix}"
