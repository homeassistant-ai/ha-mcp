"""An entity id stays in its state path.

The client puts the caller's entity id into ``states/<entity_id>``. An id
holding ``../`` segments, a ``?`` or ``#``, or one that is a bare ``.``, must not
leave that path: a read must not come back as another endpoint's answer, or
another entity's, taken for the entity's state.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp import backup_manager as bm
from ha_mcp.backup_todo import restore_todo_item
from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantClient

from ._backup_core import backup_settings as backup_settings
from ._backup_core import handler_for
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_todo_core import todo_core as todo_core

# Climbs to /api/config, which answers 200: an id landing on a 404 would
# raise with or without the fix.
CLIMBING = "x/../../config"


@pytest.mark.parametrize(
    "entity_id", [f"light.{CLIMBING}", ".", "light.a?x", "light.a#x"]
)
async def test_a_state_read_by_an_id_leaving_its_path_reads_no_other_endpoint_or_entity(
    hass: HomeAssistant, core_client: HomeAssistantClient, entity_id: str
) -> None:
    # The part before ``?`` or ``#`` names this entity, which must not answer.
    hass.states.async_set("light.a", "on")
    with pytest.raises(HomeAssistantAPIError):
        await core_client.get_entity_state(entity_id)


async def test_a_group_backup_by_a_climbing_id_does_not_hold_another_endpoints_reply(
    core_client: HomeAssistantClient, snapshot_manager: bm.BackupManager
) -> None:
    fetch = handler_for(snapshot_manager, "group").fetch

    with pytest.raises(HomeAssistantAPIError):
        await fetch(core_client, CLIMBING)


@pytest.mark.usefixtures("todo_core")
async def test_a_todo_restore_to_a_climbing_list_id_stops_at_the_state_read(
    core_client: HomeAssistantClient,
) -> None:
    # The list read would refuse the id too, but as a command error: the API
    # error comes from the state read before it.
    with pytest.raises(HomeAssistantAPIError):
        await restore_todo_item(
            core_client, f"todo.{CLIMBING}::milk", {"summary": "milk"}
        )
