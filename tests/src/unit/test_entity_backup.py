"""Entity backups restore the registry settings ``ha_set_entity`` changes.

Each case changes ``light.desk`` in Core's entity registry, as ``ha_set_entity``
would, then restores the snapshot taken before; Core's own registry has to give
the entry back exactly. Every test runs on both restore paths: Core's
``config/entity_registry`` commands alone, and the component's in-process
write.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.components.homeassistant.exposed_entities import (
    async_listen_entity_updates,
)
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.json import json_dumps
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_entities import current_entity_id
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantCommandError
from ha_mcp.tools.tools_entities import EntityTools

from ._backup_core import backup_settings as backup_settings
from ._backup_core import place_snapshot
from ._command_faults import unanswered
from ._entity_backup_core import (
    AREA_READS,
    DESK_OPTIONS,
    NOT_RESTORED,
    Desk,
    backup_dir,
    backup_files,
    crashing_on,
    write_count,
    written,
)
from ._entity_backup_core import desk as desk
from ._entity_backup_core import entity_client as entity_client
from ._entity_backup_core import entity_handler as entity_handler
from ._entity_backup_core import entity_manager as entity_manager
from ._entity_backup_core import entity_path as entity_path
from ._entity_backup_core import lookup_command as lookup_command

Edit = Callable[[er.EntityRegistry, Desk], Any]


def _edit(**fields: Any) -> Edit:
    return lambda registry, desk: registry.async_update_entity(desk.entity_id, **fields)


def _edit_with(fields: Callable[[Desk], dict[str, Any]]) -> Edit:
    """An edit whose values are the desk's own area or categories."""

    def edit(registry: er.EntityRegistry, desk: Desk) -> Any:
        return registry.async_update_entity(desk.entity_id, **fields(desk))

    return edit


def _options(domain: str, options: dict[str, Any] | None) -> Edit:
    return lambda registry, desk: registry.async_update_entity_options(
        desk.entity_id, domain, options
    )


def _renamed_then(then: Edit) -> Edit:
    def edit(registry: er.EntityRegistry, desk: Desk) -> None:
        registry.async_update_entity(desk.entity_id, new_entity_id="light.lamp")
        then(registry, Desk(**{**vars(desk), "entity_id": "light.lamp"}))

    return edit


EDITS: dict[str, Edit] = {
    "name": _edit(name="Desk"),
    "use_device_name": _edit(name=""),
    "icon": _edit(icon="mdi:lamp"),
    "area": _edit_with(lambda desk: {"area_id": desk.kitchen}),
    "no_area": _edit(area_id=None),
    "device_class": _edit(device_class="outlet"),
    "aliases": _edit(aliases=[er.COMPUTED_NAME, "lamp"]),
    "no_labels": _edit(labels=set()),
    "category_moved": _edit_with(
        lambda desk: {"categories": {"automation": desk.cat_b}}
    ),
    "category_added": _edit_with(
        lambda desk: {"categories": {"automation": desk.cat_a, "script": desk.cat_s}}
    ),
    "category_cleared": _edit(categories={}),
    "hidden": _edit(hidden_by=er.RegistryEntryHider.USER),
    "disabled": _edit(disabled_by=er.RegistryEntryDisabler.USER),
    "renamed": _edit(new_entity_id="light.lamp"),
    "renamed_and_changed": _edit(
        new_entity_id="light.lamp", name="Lamp", hidden_by=er.RegistryEntryHider.USER
    ),
    "light_options": _options("light", {"favorite_colors": [[1, 2]]}),
    "light_options_removed": _options("light", None),
    "sensor_options": _options("sensor", {"display_precision": 2}),
    # What expose_to={"conversation": True} leaves in the registry.
    "exposed_to_conversation": _options("conversation", {"should_expose": True}),
    "alexa_unexposed": _options("cloud.alexa", {"should_expose": False}),
    "renamed_then_exposed": _renamed_then(
        _options("conversation", {"should_expose": True})
    ),
}


def _entry(registry: er.EntityRegistry, desk: Desk) -> er.RegistryEntry:
    entry = registry.async_get(desk.entry_id)
    assert entry is not None
    return entry


@pytest.mark.parametrize("edit", EDITS.values(), ids=EDITS.keys())
async def test_restore_gives_back_the_snapshot(
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
    edit: Edit,
) -> None:
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    edit(entity_registry, desk)

    result = await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    assert await entity_handler.fetch(entity_client, desk.entity_id) == snapshot
    assert result["warnings"] == [NOT_RESTORED]
    entry = _entry(entity_registry, desk)
    assert result["entity_entry"] == json.loads(json_dumps(entry.extended_dict))


async def test_snapshot_holds_the_registry_fields_and_options(
    entity_client: HomeAssistantClient, entity_handler: bm.DomainHandler, desk: Desk
) -> None:
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)

    assert snapshot == {
        "id": desk.entry_id,
        "entity_id": "light.desk",
        "name": None,
        "icon": None,
        "area_id": desk.office,
        "device_class": None,
        "aliases": ["desk lamp"],
        "labels": [desk.work],
        "categories": {"automation": desk.cat_a},
        "hidden_by": None,
        "disabled_by": None,
        "options": DESK_OPTIONS,
    }


async def test_an_entity_without_a_registry_entry_has_no_snapshot(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
) -> None:
    hass.states.async_set("light.yaml_only", "on")

    assert await entity_handler.fetch(entity_client, "light.yaml_only") is None


async def test_a_registry_read_error_fails_the_snapshot(
    entity_client: HomeAssistantClient, entity_handler: bm.DomainHandler
) -> None:
    """Only a missing entry means "nothing to back up"; any other refusal must
    reach the capture, which blocks a restore without its safety backup."""
    with pytest.raises(HomeAssistantCommandError) as err:
        await entity_handler.fetch(entity_client, "not an entity id")

    assert err.value.code == "invalid_format"


@pytest.mark.parametrize(
    ("edit", "restart"),
    [("name", False), ("disabled", True)],
)
async def test_only_re_enabling_asks_for_a_restart(
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
    edit: str,
    restart: bool,
) -> None:
    """Core asks for one when an update re-enables an entity that has no
    config entry."""
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    EDITS[edit](entity_registry, desk)

    result = await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    assert ("require_restart" in result) is restart


@pytest.mark.parametrize("entity_path", ["websocket"])
async def test_a_hider_only_core_sets_is_left_with_cores_refusal(
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    """Core's command refuses it; the other settings are still restored."""
    entity_registry.async_update_entity(
        desk.entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION
    )
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    entity_registry.async_update_entity(desk.entity_id, hidden_by=None, name="Desk")

    result = await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    entry = _entry(entity_registry, desk)
    assert (entry.hidden_by, entry.name) == (None, None)
    notice, refusal = result["warnings"]
    assert notice == NOT_RESTORED
    assert refusal.startswith(
        "hidden_by stays None: Home Assistant refused 'integration': "
    )


@pytest.mark.parametrize("disabled", ["device", "config_entry"])
async def test_an_entity_core_keeps_disabled_stays_disabled_and_the_restore_says_so(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    desk: Desk,
    disabled: str,
) -> None:
    """Core derives an entity's disabler from its device or integration entry,
    so restoring an enabled snapshot cannot enable it. A restore that reports
    plain success leaves the user thinking the entity is back; the other
    settings stay."""
    config_entry = MockConfigEntry(domain="hue")
    config_entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("hue", "desk")}
    )
    entity_registry.async_update_entity(
        desk.entity_id,
        name="Desk",
        config_entry_id=config_entry.entry_id,
        device_id=device.id,
    )
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    if disabled == "device":
        device_registry.async_update_device(
            device.id, disabled_by=dr.DeviceEntryDisabler.USER
        )
    else:
        await hass.config_entries.async_set_disabled_by(
            config_entry.entry_id, ConfigEntryDisabler.USER
        )
    entity_registry.async_update_entity(desk.entity_id, name="Other")

    result = await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    entry = _entry(entity_registry, desk)
    assert (entry.disabled_by, entry.name) == (
        er.RegistryEntryDisabler(disabled),
        "Desk",
    )
    assert f"disabled_by stays {disabled!r} instead of None" in result["warnings"][-1]


async def test_an_entity_its_integration_hid_since_is_shown_again(
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    entity_registry.async_update_entity(
        desk.entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION
    )

    await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    assert _entry(entity_registry, desk).hidden_by is None


async def test_an_exposure_the_backup_holds_is_told_to_the_assistant(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    """Core tells the voice assistant about an exposure change only when the
    exposure, not the options write, is what changes the value."""
    told: list[None] = []
    async_listen_entity_updates(hass, "conversation", lambda: told.append(None))
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    EDITS["exposed_to_conversation"](entity_registry, desk)

    await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    assert told == [None]
    options = _entry(entity_registry, desk).options
    assert options["conversation"] == {"should_expose": False}


async def test_an_assistant_the_backup_holds_nothing_for_is_reset(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    """Its options go; no exposure value is told, since the backup has none,
    and Core works out the default on the next read."""
    entity_registry.async_update_entity_options(desk.entity_id, "conversation", None)
    told: list[None] = []
    async_listen_entity_updates(hass, "conversation", lambda: told.append(None))
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    EDITS["exposed_to_conversation"](entity_registry, desk)

    await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    assert "conversation" not in _entry(entity_registry, desk).options
    assert told == []


async def test_an_unreadable_area_registry_refuses_the_restore(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    entity_path: str,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    area_registry: ar.AreaRegistry,
    desk: Desk,
) -> None:
    """A deleted area could otherwise pass unchecked. No read of the area
    registry is answered: on the component path that is the component's and
    the list command the restore falls back to."""
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    area_registry.async_delete(desk.office)

    with written(hass) as seen:
        first, *fallbacks = AREA_READS[entity_path]
        async with unanswered(hass, first, monkeypatch, *fallbacks):
            with pytest.raises(ToolError):
                await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    assert write_count(seen) == 0


@pytest.mark.parametrize("entity_path", ["websocket"])
async def test_an_unexpected_core_error_on_a_hider_fails_the_restore(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    """Only Core's refusals become warnings; anything else is reported."""
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    EDITS["hidden"](entity_registry, desk)

    with (
        crashing_on(
            hass, "config/entity_registry/update", lambda msg: "hidden_by" in msg
        ),
        pytest.raises(HomeAssistantCommandError) as err,
    ):
        await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    assert err.value.code == "unknown_error"


@pytest.mark.parametrize("kind", ["area", "label", "category"])
async def test_a_deleted_area_label_or_category_refuses_the_restore(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    area_registry: ar.AreaRegistry,
    label_registry: lr.LabelRegistry,
    category_registry: cr.CategoryRegistry,
    desk: Desk,
    kind: str,
) -> None:
    """Core would store the area or category ID as sent and drop the label
    without an error, and report success."""
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    entity_registry.async_update_entity(desk.entity_id, name="Desk")
    deleted = {"area": desk.office, "label": desk.work, "category": desk.cat_a}[kind]
    if kind == "area":
        area_registry.async_delete(deleted)
    elif kind == "label":
        label_registry.async_delete(deleted)
    else:
        category_registry.async_delete(scope="automation", category_id=deleted)

    with written(hass) as seen, pytest.raises(bm.BackupRestoreError) as err:
        await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    assert err.value.outcome["reason"] == "missing_reference"
    assert err.value.outcome["apply_status"] == "not_applied"
    assert repr(deleted) in str(err.value)
    assert write_count(seen) == 0
    assert _entry(entity_registry, desk).name == "Desk"


@pytest.mark.parametrize("entity_path", ["websocket"])
async def test_a_removed_entity_points_to_restarting_home_assistant(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    """A reload does not add a stored helper's entity again; a restart does."""
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    entity_registry.async_remove(desk.entity_id)

    with written(hass) as seen, pytest.raises(bm.BackupRestoreError) as err:
        await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    assert err.value.outcome["reason"] == "entry_deleted"
    assert err.value.outcome["suggestions"][0].startswith("Restart Home Assistant")
    assert write_count(seen) == 0


@pytest.mark.parametrize(
    "legacy",
    [{"entity_id": "light.desk", "state": "on", "attributes": {}}, "on"],
    ids=["state_dict", "state_string"],
)
async def test_a_state_snapshot_from_an_earlier_version_is_refused(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    desk: Desk,
    legacy: Any,
) -> None:
    """It holds no registry settings; re-posting its state would only
    overwrite what Home Assistant shows for the entity."""
    with written(hass) as seen, pytest.raises(bm.InvalidBackupSnapshotError):
        await entity_handler.restore(entity_client, desk.entity_id, legacy)

    assert write_count(seen) == 0


async def test_the_current_entity_id_follows_the_entry(
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    """A gone entry has none; a state snapshot has no entry to follow."""
    saved = await entity_handler.fetch(entity_client, desk.entity_id)
    assert saved is not None
    EDITS["renamed"](entity_registry, desk)

    assert await current_entity_id(entity_client, "light.desk", saved) == "light.lamp"
    assert (
        await current_entity_id(entity_client, "light.desk", {**saved, "id": "x"})
        is None
    )
    with pytest.raises(bm.InvalidBackupSnapshotError):
        await current_entity_id(entity_client, "light.desk", {"state": "on"})


class TestStateSnapshotFile:
    """A state snapshot from an earlier version, as a file in the backup
    directory: refused before the safety backup and in the diff, since there is
    nothing to restore and neither may report a change."""

    @pytest.fixture
    def legacy(self, backup_settings: config.Settings, desk: Desk) -> str:
        name = "entity.light.desk.20260101_000000.yaml"
        held = {"entity_id": "light.desk", "state": "on", "attributes": {}}
        place_snapshot(
            backup_dir(backup_settings),
            name,
            "light.desk",
            domain="entity",
            held=held,
            text=False,
        )
        return name

    async def test_restore_writes_no_safety_backup(
        self,
        entity_manager: bm.BackupManager,
        backup_settings: config.Settings,
        legacy: str,
    ) -> None:
        files = await asyncio.to_thread(backup_files, backup_dir(backup_settings))

        with pytest.raises(bm.InvalidBackupSnapshotError):
            await entity_manager.restore_snapshot(legacy)

        assert (
            await asyncio.to_thread(backup_files, backup_dir(backup_settings)) == files
        )

    async def test_diff_is_refused(
        self, entity_manager: bm.BackupManager, legacy: str
    ) -> None:
        with pytest.raises(bm.InvalidBackupSnapshotError):
            await entity_manager.snapshot_comparison(legacy)


async def test_a_bulk_call_backs_up_no_entity(
    entity_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    backup_settings: config.Settings,
    desk: Desk,
) -> None:
    """Snapshotting the first entity of a list would leave the rest without a
    backup while looking covered, so a list names no entity. A list of one is
    a single edit to ha_set_entity."""
    entity_registry.async_get_or_create(
        "light", "hue", "lamp", suggested_object_id="lamp"
    )
    tools = EntityTools(entity_client)
    directory = backup_dir(backup_settings)

    await tools.ha_set_entity(
        entity_id=["light.desk", "light.lamp"], labels=[desk.work]
    )
    after_bulk = await asyncio.to_thread(backup_files, directory)
    await tools.ha_set_entity(entity_id=["light.desk"], labels=[desk.work])
    after_single = await asyncio.to_thread(backup_files, directory)

    assert after_bulk == []
    assert [name.split(".20")[0] for name in after_single] == ["entity.light.desk"]
