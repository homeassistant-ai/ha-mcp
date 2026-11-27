"""An automation, script or scene id holding ``/`` stays in its config path.

The client puts the caller's id into ``config/<domain>/config/<id>``. An id
holding ``/`` and ``..`` segments must not climb out of that path to another
endpoint of Core's API: a read must not come back as another endpoint's
answer, a write must not land somewhere else, and a delete must not remove
something else.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import Event, HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantClient

from ._backup_core import backup_settings as backup_settings
from ._backup_core import snapshot_manager as snapshot_manager
from ._rest_client_config_core import config_client as config_client

DOMAINS = ["automation", "script", "scene"]
VALID = {
    "automation": {"alias": "x", "triggers": [], "actions": []},
    "script": {"alias": "x", "sequence": [{"delay": 0}]},
    "scene": {"name": "x", "entities": {}},
}


@pytest.mark.parametrize("domain", DOMAINS)
async def test_an_id_that_climbs_the_path_does_not_read_another_endpoint(
    config_client: HomeAssistantClient, domain: str
) -> None:
    read = getattr(config_client, f"get_{domain}_config")

    with pytest.raises(HomeAssistantAPIError):
        await read("../../../config")


# The script and scene backups read a bare id themselves; the automation
# backup reads through ``get_automation_config``, covered above.
@pytest.mark.parametrize("domain", ["script", "scene"])
async def test_a_backup_of_an_id_that_climbs_the_path_holds_no_other_endpoint(
    config_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    domain: str,
) -> None:
    handler = snapshot_manager.handler_for(domain)
    assert handler is not None

    with pytest.raises(HomeAssistantAPIError):
        await handler.fetch(config_client, "../../../config")


@pytest.mark.parametrize("domain", DOMAINS)
async def test_an_id_that_climbs_the_path_does_not_post_elsewhere(
    hass: HomeAssistant, config_client: HomeAssistantClient, domain: str
) -> None:
    fired: list[Event[Any]] = []
    hass.bus.async_listen("climbed", fired.append)
    upsert = getattr(config_client, f"upsert_{domain}_config")

    with pytest.raises(HomeAssistantAPIError):
        await upsert(VALID[domain], "../../../events/climbed")
    await hass.async_block_till_done()

    assert fired == []


@pytest.mark.parametrize("domain", DOMAINS)
async def test_an_id_that_climbs_the_path_does_not_delete_elsewhere(
    hass: HomeAssistant, config_client: HomeAssistantClient, domain: str
) -> None:
    entry = MockConfigEntry(domain="climbed")
    entry.add_to_hass(hass)
    delete = getattr(config_client, f"delete_{domain}_config")

    with pytest.raises(HomeAssistantAPIError):
        await delete(f"../../config_entries/entry/{entry.entry_id}")

    assert hass.config_entries.async_get_entry(entry.entry_id) is not None
