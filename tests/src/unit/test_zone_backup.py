"""Zone backups snapshot and restore the zone the zone tools change (#2632),
on a real ``hass`` through the server's own client.

The zone tools resolve a zone by its storage id, else by its registry
entity_id, so a backup must resolve it the same way; a create has no zone to
back up. Core allows identical zones, so a restore cannot tell a zone an
earlier restore created from one the user made.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_zones import ZoneTools

from ._backup_core import backup_settings as backup_settings
from ._backup_core import handler_for
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_target_identity_core import (
    create_named,
    create_tag,
    entity_of,
    held_by,
    move_entity,
    rename_item,
)
from ._receipt_faults import on_receipt, revoke
from ._zone_icon_core import icon_of


@pytest.fixture
async def zones(
    hass: HomeAssistant, snapshot_manager: bm.BackupManager
) -> bm.BackupManager:
    """``snapshot_manager`` on a ``hass`` with Core's zones."""
    assert await async_setup_component(hass, "zone", {})
    return snapshot_manager


async def create_zone(
    client: HomeAssistantClient, name: str = "Office", **fields: Any
) -> str:
    """A stored zone called ``name``; its storage id."""
    at = {"latitude": 52.5, "longitude": 13.4, **fields}
    return await create_named(client, "zone", name, **at)


async def stored_zones(client: HomeAssistantClient) -> dict[str, Any]:
    """Core's stored zones by id."""
    listed = await client.send_websocket_message({"type": "zone/list"})
    assert listed["success"], listed
    return {zone["id"]: zone for zone in listed["result"]}


async def remove_zone(client: HomeAssistantClient, zone_id: str) -> None:
    removed = await client.send_websocket_message(
        {"type": "zone/delete", "zone_id": zone_id}
    )
    assert removed["success"], removed


async def restore(
    manager: bm.BackupManager,
    client: HomeAssistantClient,
    snapshot: dict[str, Any],
    domain: str = "zone",
) -> Any:
    """What the ``domain`` handler's restore of ``snapshot`` returns."""
    return await handler_for(manager, domain).restore(client, snapshot["id"], snapshot)


def drop_on_receipt(hass: HomeAssistant, command: str, token: str) -> Any:
    """Core closes the client's socket as it receives ``command``."""
    return on_receipt(hass, command, lambda msg: revoke(hass, token))


@pytest.mark.parametrize("target", ["office", "zone.office"])
@pytest.mark.parametrize("domain", ["zone", "helper_zone"])
async def test_snapshot_records_the_registry_icon(
    core_client: HomeAssistantClient,
    zones: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    domain: str,
    target: str,
) -> None:
    """The zone tools take a zone_id or the zone's entity_id."""
    await create_zone(core_client)
    entity_registry.async_update_entity("zone.office", icon="mdi:briefcase")

    snapshot = await held_by(zones, core_client, domain, target)

    office = (await stored_zones(core_client))["office"]
    assert snapshot == {**office, "registry_icon": "mdi:briefcase"}


@pytest.mark.parametrize(("target", "held"), [("zone.work", "office"), ("work", None)])
async def test_snapshot_names_a_renamed_zone_only_by_its_entity_id(
    core_client: HomeAssistantClient,
    zones: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    target: str,
    held: str | None,
) -> None:
    """The zone tools resolve ``zone.work`` to office and a bare ``work``
    to no zone, so the backup must too."""
    await create_zone(core_client)
    move_entity(entity_registry, "zone", "office", "zone.work")

    snapshot = await held_by(zones, core_client, "zone", target)

    assert (snapshot["id"] if snapshot else None) == held


async def changed_zone(
    manager: bm.BackupManager, client: HomeAssistantClient, target: str
) -> tuple[str, str]:
    """The zone ``ha_set_zone(zone_id=target)`` changed, and the zone its own
    automatic backup holds."""
    await ZoneTools(client).ha_set_zone(zone_id=target, radius=250, wait=False)
    (changed,) = [
        z for z in (await stored_zones(client)).values() if z["radius"] == 250
    ]
    (row,) = manager.list_snapshots(domain="zone")
    return changed["id"], manager.read_snapshot(row["name"])["config"]["id"]


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        # The stored zone "Home" is zone.home_2: the home zone holds zone.home.
        ("zone.home_2", "home"),
        # A zone named "home_2" does not shadow the zone with that id.
        ("home_2", "home_2"),
    ],
)
async def test_snapshot_is_of_the_zone_the_tool_changes(
    core_client: HomeAssistantClient,
    zones: bm.BackupManager,
    target: str,
    expected: str,
) -> None:
    """A wrong snapshot would leave the changed zone without a rollback."""
    home = await create_zone(core_client, "Home")
    await create_zone(core_client, "Home 2")
    await rename_item(core_client, "zone", home, "home_2")

    assert await changed_zone(zones, core_client, target) == (expected, expected)


async def test_a_home_zone_write_is_backed_up_and_restored(
    hass: HomeAssistant, core_client: HomeAssistantClient, zones: bm.BackupManager
) -> None:
    """The home zone is not stored: its snapshot is the general settings'
    location and radius, and the restore writes them back."""
    assert await async_setup_component(hass, "config", {})
    before = (hass.config.latitude, hass.config.longitude, hass.config.radius)

    await ZoneTools(core_client).ha_set_zone(
        zone_id="zone.home", latitude=48.1, radius=250, wait=False
    )
    (row,) = zones.list_snapshots(domain="zone")
    snapshot = zones.read_snapshot(row["name"])["config"]
    await handler_for(zones, "zone").restore(core_client, "zone.home", snapshot)

    after = (hass.config.latitude, hass.config.longitude, hass.config.radius)
    assert after == before != (48.1, before[1], 250)


async def test_a_home_zone_restore_is_not_backed_up_as_a_stored_zone_named_home(
    hass: HomeAssistant, core_client: HomeAssistantClient, zones: bm.BackupManager
) -> None:
    """A home zone snapshot filed under "home" keeps the general settings as
    its target once a stored zone takes that id: a safety backup of the stored
    zone could not undo the restore."""
    assert await async_setup_component(hass, "config", {})
    tools = ZoneTools(core_client)
    await tools.ha_set_zone(zone_id="home", radius=120, wait=False)
    (row,) = zones.list_snapshots(domain="zone")
    await create_zone(core_client, "Home")
    await tools.ha_set_zone(zone_id="zone.home", radius=333, wait=False)

    restored = await zones.restore_snapshot(row["name"])

    safety = zones.read_snapshot(restored["safety_backup"])["config"]
    assert safety["home_zone"]["radius"] == 333


async def test_a_zone_without_a_registry_entry_is_the_zone_backed_up(
    core_client: HomeAssistantClient,
    zones: bm.BackupManager,
    entity_registry: er.EntityRegistry,
) -> None:
    """Zone "work" lost its registry entry and zone "office" was renamed to
    zone.work: the tool changes "work", and its backup holds it."""
    await create_zone(core_client)
    work = await create_zone(core_client, "Work")
    entity_registry.async_remove(entity_of(entity_registry, "zone", work))
    move_entity(entity_registry, "zone", "office", "zone.work")

    assert await changed_zone(zones, core_client, "work") == ("work", "work")


@pytest.mark.parametrize(
    ("arguments", "captured"),
    [
        ({"name": "Office", "latitude": 52.5, "longitude": 13.4}, []),
        ({"zone_id": "office", "radius": 50}, [("zone", "office")]),
    ],
    ids=["create", "update"],
)
async def test_creating_a_zone_backs_up_no_other_zone(
    core_client: HomeAssistantClient,
    zones: bm.BackupManager,
    arguments: dict[str, Any],
    captured: list[tuple[str, str]],
) -> None:
    """A new zone has nothing to back up; a zone of the same name is not it."""
    await create_zone(core_client)

    await ZoneTools(core_client).ha_set_zone(**arguments, wait=False)

    rows = zones.list_snapshots()
    assert [(row["domain"], row["entity_id"]) for row in rows] == captured


# Core closes the socket itself (a revoked token); the client's close reply
# then re-arms aiohttp's heartbeat timer on that closed server socket.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_a_failed_registry_read_fails_the_snapshot(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    zones: bm.BackupManager,
    hass_access_token: str,
) -> None:
    """Saved without the icon key, it would read as an older snapshot."""
    await create_zone(core_client)

    with (
        drop_on_receipt(hass, "config/entity_registry/list", hass_access_token),
        pytest.raises(HomeAssistantConnectionError),
    ):
        await held_by(zones, core_client, "zone", "office")


@pytest.mark.parametrize("domain", ["zone", "helper_zone"])
async def test_restore_of_a_removed_zone_creates_it_again(
    core_client: HomeAssistantClient,
    zones: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    domain: str,
) -> None:
    """Core's zone/update cannot restore a zone that no longer exists. Core
    gives the zone back its old registry entry, icon included, so the
    recorded icon is not the one the entry held."""
    await create_zone(core_client)
    entity_registry.async_update_entity("zone.office", icon="mdi:briefcase")
    snapshot = await held_by(zones, core_client, domain, "office")
    entity_registry.async_update_entity("zone.office", icon="mdi:map")
    await remove_zone(core_client, "office")

    result = await restore(zones, core_client, snapshot, domain)

    assert result["restore_mode"] == "recreated"
    recreated = (await stored_zones(core_client))[result["id"]]
    assert {**recreated, "registry_icon": "mdi:briefcase"} == {
        **snapshot,
        "id": result["id"],
    }
    entity_id = entity_of(entity_registry, "zone", result["id"])
    assert icon_of(entity_registry, entity_id) == "mdi:briefcase"


async def renamed_and_removed(
    client: HomeAssistantClient, manager: bm.BackupManager
) -> Any:
    """The snapshot of zone "workplace", renamed "Office" and then removed:
    Core creates it again as zone "office"."""
    workplace = await create_zone(client, "Workplace")
    await rename_item(client, "zone", workplace, "Office")
    snapshot = await held_by(manager, client, "zone", workplace)
    await remove_zone(client, workplace)
    return snapshot


async def test_a_repeated_restore_does_not_add_another_zone(
    core_client: HomeAssistantClient, zones: bm.BackupManager
) -> None:
    """A zone renamed after the snapshot comes back under a new id."""
    snapshot = await renamed_and_removed(core_client, zones)

    await restore(zones, core_client, dict(snapshot))
    again = await restore(zones, core_client, dict(snapshot))

    assert list(await stored_zones(core_client)) == ["office"]
    assert again["restore_mode"] == "already_recreated"


async def test_a_different_zone_with_the_same_name_is_not_overwritten(
    core_client: HomeAssistantClient, zones: bm.BackupManager
) -> None:
    """Core allows two zones with one name: the user's own zone stays as it is."""
    snapshot = await renamed_and_removed(core_client, zones)
    await create_zone(core_client, latitude=1.0, longitude=2.0)
    users = (await stored_zones(core_client))["office"]

    await restore(zones, core_client, snapshot)

    after = await stored_zones(core_client)
    assert after["office"] == users
    assert list(after) == ["office", "office_2"]


async def identical_twin(
    client: HomeAssistantClient,
    manager: bm.BackupManager,
    registry: er.EntityRegistry,
    icon: str | None,
) -> Any:
    """The snapshot of zone office, removed after the user made an identical
    zone office_2, whose entity has no icon override."""
    await create_zone(client)
    registry.async_update_entity("zone.office", icon=icon)
    snapshot = await held_by(manager, client, "zone", "office")
    assert await create_zone(client) == "office_2"
    await remove_zone(client, "office")
    return snapshot


async def test_an_identical_zone_is_named_and_its_icon_left_alone(
    core_client: HomeAssistantClient,
    zones: bm.BackupManager,
    entity_registry: er.EntityRegistry,
) -> None:
    """Core allows identical zones, so the match may be the user's own zone.

    The warning names the recorded icon, so it can still be set by hand
    when an earlier restore failed to write it.
    """
    snapshot = await identical_twin(
        core_client, zones, entity_registry, "mdi:briefcase"
    )

    result = await restore(zones, core_client, snapshot)

    assert result["restore_mode"] == "already_recreated"
    assert "office_2" in result["warnings"][0]
    assert "mdi:briefcase" in result["warnings"][0]
    assert (
        icon_of(entity_registry, entity_of(entity_registry, "zone", "office_2")) is None
    )


async def test_a_snapshot_without_a_registry_icon_offers_none_to_set(
    core_client: HomeAssistantClient,
    zones: bm.BackupManager,
    entity_registry: er.EntityRegistry,
) -> None:
    """Telling the user to set the icon None would be a wrong next step."""
    snapshot = await identical_twin(core_client, zones, entity_registry, None)

    result = await restore(zones, core_client, snapshot)

    assert "ha_set_entity" not in result["warnings"][0]


async def test_a_zone_with_a_field_the_snapshot_lacks_is_not_the_restored_one(
    core_client: HomeAssistantClient, zones: bm.BackupManager
) -> None:
    """A zone recreated from an icon-less snapshot has no stored icon."""
    await create_zone(core_client)
    snapshot = await held_by(zones, core_client, "zone", "office")
    assert await create_zone(core_client, icon="mdi:home") == "office_2"
    await remove_zone(core_client, "office")

    result = await restore(zones, core_client, snapshot)

    assert result["restore_mode"] == "recreated"
    assert (await stored_zones(core_client))["office_2"]["icon"] == "mdi:home"


# The closed server socket of test_a_failed_registry_read_fails_the_snapshot.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_a_dropped_connection_during_the_icon_write_is_a_warning(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    zones: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    hass_access_token: str,
) -> None:
    """The zone is already restored, so the restore must not fail over it."""
    await create_zone(core_client)
    entity_registry.async_update_entity("zone.office", icon="mdi:star")
    snapshot = await held_by(zones, core_client, "zone", "office")

    with drop_on_receipt(hass, "config/entity_registry/update", hass_access_token):
        result = await restore(zones, core_client, snapshot)

    assert "registry icon was not" in result["warnings"][0]


# The same closed server socket as the test above.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_a_failed_icon_write_after_a_recreate_is_a_warning(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    zones: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    hass_access_token: str,
) -> None:
    """The removed zone is back, so the restore reports it as recreated."""
    await create_zone(core_client)
    entity_registry.async_update_entity("zone.office", icon="mdi:star")
    snapshot = await held_by(zones, core_client, "zone", "office")
    await remove_zone(core_client, "office")

    with drop_on_receipt(hass, "config/entity_registry/update", hass_access_token):
        result = await restore(zones, core_client, snapshot)

    assert result["restore_mode"] == "recreated"
    assert "registry icon was not" in result["warnings"][0]


@pytest.mark.parametrize("helper_type", ["person", "tag", "input_boolean"])
async def test_a_snapshot_records_and_restores_the_registry_icon(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    zones: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    helper_type: str,
) -> None:
    """The entity shows its registry icon, which an update writes for every
    type and the stored item can miss (person and tag have no icon field)."""
    assert await async_setup_component(hass, helper_type, {})
    if helper_type == "tag":
        await create_tag(core_client, "anna", "Anna")
        item_id = "anna"
    else:
        item_id = await create_named(core_client, helper_type, "Anna")
    entity_id = entity_of(entity_registry, helper_type, item_id)
    entity_registry.async_update_entity(entity_id, icon="mdi:account")
    handler = handler_for(zones, f"helper_{helper_type}")

    # Snapshots are keyed by entity_id; the restore writes the stored item's id.
    snapshot = await handler.fetch(core_client, entity_id)
    entity_registry.async_update_entity(entity_id, icon="mdi:star")
    await handler.restore(core_client, entity_id, snapshot)

    assert snapshot["registry_icon"] == "mdi:account"
    assert icon_of(entity_registry, entity_id) == "mdi:account"


@pytest.mark.parametrize(("icon", "warned"), [("mdi:briefcase", True), (None, False)])
async def test_a_recorded_icon_for_a_zone_without_an_entity_is_a_warning(
    core_client: HomeAssistantClient,
    zones: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    icon: str | None,
    warned: bool,
) -> None:
    """The user removed the zone's registry entry: there is no entity to
    give the icon, and with no icon recorded nothing was lost."""
    await create_zone(core_client)
    entity_registry.async_update_entity("zone.office", icon=icon)
    snapshot = await held_by(zones, core_client, "zone", "office")
    entity_registry.async_remove("zone.office")

    result = await restore(zones, core_client, snapshot)

    assert ("warnings" in result) is warned
    assert not warned or "has no entity" in result["warnings"][0]


async def test_restore_resets_a_registry_icon_set_after_the_snapshot(
    core_client: HomeAssistantClient,
    zones: bm.BackupManager,
    entity_registry: er.EntityRegistry,
) -> None:
    await create_zone(core_client)
    snapshot = await held_by(zones, core_client, "zone", "office")
    entity_registry.async_update_entity("zone.office", icon="mdi:briefcase")

    await restore(zones, core_client, snapshot)

    assert icon_of(entity_registry, "zone.office") is None


async def test_an_icon_stored_after_the_snapshot_is_a_warning(
    core_client: HomeAssistantClient, zones: bm.BackupManager
) -> None:
    """Core's zone/update merges, so it cannot remove the newer stored icon."""
    await create_zone(core_client)
    snapshot = await held_by(zones, core_client, "zone", "office")
    updated = await core_client.send_websocket_message(
        {"type": "zone/update", "zone_id": "office", "icon": "mdi:school"}
    )
    assert updated["success"], updated

    result = await restore(zones, core_client, snapshot)

    assert "mdi:school" in result["warnings"][0]


async def test_restore_of_an_older_snapshot_leaves_the_registry_alone(
    core_client: HomeAssistantClient,
    zones: bm.BackupManager,
    entity_registry: er.EntityRegistry,
) -> None:
    """An older release recorded no registry icon: the override set since stays."""
    await create_zone(core_client)
    entity_registry.async_update_entity("zone.office", icon="mdi:map")
    held = await held_by(zones, core_client, "zone", "office")
    older = {key: value for key, value in held.items() if key != "registry_icon"}

    await restore(zones, core_client, older)

    assert icon_of(entity_registry, "zone.office") == "mdi:map"


async def test_other_update_failures_are_not_turned_into_a_create(
    core_client: HomeAssistantClient, zones: bm.BackupManager
) -> None:
    await create_zone(core_client)
    snapshot = await held_by(zones, core_client, "zone", "office")

    with pytest.raises(HomeAssistantCommandError):
        await restore(zones, core_client, {**snapshot, "latitude": "north"})

    assert list(await stored_zones(core_client)) == ["office"]
