"""Entity backups restored through the component's in-process write.

Core answers the component's ``ha_mcp_tools/entity_entry`` and
``entity_restore`` commands; these tests cover what only that path does and how
the server reads the component's answers. The tests both paths share are in
``test_entity_backup.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ha_mcp.tools.component_api import get_component_caps

from ._backup_core import backup_settings as backup_settings
from ._command_faults import received, unanswered
from ._entity_backup_core import (
    NOT_RESTORED,
    Desk,
    before_command,
    component_commands_removed,
    crashing_on,
    entity_snapshot,
    with_config_entry,
    with_device,
    write_count,
    written,
)
from ._entity_backup_core import desk as desk
from ._entity_backup_core import entity_client as entity_client
from ._entity_backup_core import entity_handler as entity_handler
from ._entity_backup_core import entity_manager as entity_manager
from ._entity_backup_core import entity_path as entity_path

RESTORE = "ha_mcp_tools/entity_restore"
CORE_UPDATE = "config/entity_registry/update"
INFO = "ha_mcp_tools/info"

pytestmark = pytest.mark.parametrize("entity_path", ["component"])


Setter = Callable[[er.EntityRegistry, str, bool], None]


def _hidden(registry: er.EntityRegistry, entity_id: str, on: bool) -> None:
    registry.async_update_entity(
        entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION if on else None
    )


def _disabled(registry: er.EntityRegistry, entity_id: str, on: bool) -> None:
    registry.async_update_entity(
        entity_id, disabled_by=er.RegistryEntryDisabler.HASS if on else None
    )


def _disabler_changed(registry: er.EntityRegistry, entity_id: str, on: bool) -> None:
    registry.async_update_entity(
        entity_id,
        disabled_by=er.RegistryEntryDisabler.HASS
        if on
        else er.RegistryEntryDisabler.USER,
    )


@pytest.mark.parametrize(
    ("field", "value", "setter"),
    [
        pytest.param("hidden_by", "integration", _hidden, id="hidden"),
        pytest.param("disabled_by", "hass", _disabled, id="disabled"),
        pytest.param("disabled_by", "hass", _disabler_changed, id="disabler_changed"),
    ],
)
async def test_the_component_writes_a_value_only_core_sets(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
    field: str,
    value: str,
    setter: Setter,
) -> None:
    """Core's own update command refuses these; the component's write does
    not, and a change of disabler needs no Core update."""
    setter(entity_registry, desk.entity_id, True)
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    setter(entity_registry, desk.entity_id, False)

    with written(hass) as seen:
        result = await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    entry = entity_registry.async_get(desk.entity_id)
    assert entry is not None
    assert getattr(entry, field) == value
    assert result["warnings"] == [NOT_RESTORED]
    assert [m["settings"][field] for m in seen[RESTORE]] == [value]
    assert seen[CORE_UPDATE] == []


async def test_the_component_enables_through_cores_update(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    """The component write leaves out ``disabled_by`` None; Core's update, with
    its device check and restart answer, enables the entity."""
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    entity_registry.async_update_entity(
        desk.entity_id, disabled_by=er.RegistryEntryDisabler.USER, name="Desk"
    )

    with written(hass) as seen:
        await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    assert await entity_handler.fetch(entity_client, desk.entity_id) == snapshot
    [write] = seen[RESTORE]
    assert "disabled_by" not in write["settings"]
    [enable] = seen[CORE_UPDATE]
    assert enable["disabled_by"] is None


async def test_the_components_warnings_reach_the_result(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    desk: Desk,
) -> None:
    """A device disabler written while its device is enabled would never go."""
    device = with_device(hass, entity_registry, device_registry, desk.entity_id)
    device_registry.async_update_device(
        device.id, disabled_by=dr.DeviceEntryDisabler.USER
    )
    await hass.async_block_till_done()
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    assert snapshot is not None
    assert snapshot["disabled_by"] == "device"
    device_registry.async_update_device(device.id, disabled_by=None)
    await hass.async_block_till_done()
    entity_registry.async_update_entity(
        desk.entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )

    result = await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    entry = entity_registry.async_get(desk.entity_id)
    assert entry is not None
    assert entry.disabled_by == er.RegistryEntryDisabler.USER
    assert result["warnings"] == [
        NOT_RESTORED,
        "disabled_by stays 'user': the backup holds 'device', "
        "and the device is not disabled now",
    ]


async def test_what_core_keeps_of_a_removed_entity_with_a_config_entry(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    with_config_entry(hass, entity_registry, desk.entity_id)
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    entity_registry.async_remove(desk.entity_id)

    with written(hass) as seen, pytest.raises(bm.BackupRestoreError) as err:
        await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    assert err.value.outcome["reason"] == "entry_deleted"
    assert err.value.outcome["suggestions"][0].startswith(
        "Home Assistant keeps the registry settings of light.desk "
        "while its config entry exists"
    )
    assert write_count(seen) == 0


async def test_what_core_keeps_of_a_removed_entity_without_one(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    """Core purges the settings 30 days after the entity was removed."""
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    entity_registry.async_remove(desk.entity_id)
    [deleted] = entity_registry.deleted_entities.values()
    assert deleted.orphaned_timestamp is not None
    until = datetime.fromtimestamp(deleted.orphaned_timestamp, UTC) + timedelta(days=30)

    with pytest.raises(bm.BackupRestoreError) as err:
        await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    assert err.value.outcome["reason"] == "entry_deleted"
    assert err.value.outcome["suggestions"][0].startswith(
        f"Home Assistant keeps the registry settings of light.desk until "
        f"{until.isoformat()}"
    )


async def test_a_purged_entity_is_said_to_have_no_settings_kept(
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    entity_registry.async_remove(desk.entity_id)
    with freeze_time(datetime.now(UTC) + timedelta(days=31)):
        entity_registry.async_purge_expired_orphaned_entities()
    assert not entity_registry.deleted_entities

    with pytest.raises(bm.BackupRestoreError) as err:
        await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    assert err.value.outcome["reason"] == "entry_deleted"
    assert err.value.outcome["suggestions"][0].startswith(
        "Home Assistant no longer keeps this entity's registry settings"
    )


async def test_an_entry_removed_before_the_component_write_is_reported(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    entity_registry.async_update_entity(desk.entity_id, name="Desk")

    with (
        before_command(
            hass, RESTORE, lambda: entity_registry.async_remove(desk.entity_id)
        ),
        pytest.raises(bm.BackupRestoreError) as err,
    ):
        await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    assert err.value.outcome["reason"] == "entry_deleted"
    assert err.value.outcome["suggestions"][0].startswith(
        "Home Assistant keeps the registry settings of light.desk until "
    )


async def test_an_entry_renamed_before_the_component_write_is_not_written(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    """The component checks the entity_id it was told against the entry's."""
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    entity_registry.async_update_entity(desk.entity_id, name="Desk")

    def rename() -> None:
        entity_registry.async_update_entity(desk.entity_id, new_entity_id="light.lamp")

    with (
        before_command(hass, RESTORE, rename),
        pytest.raises(bm.BackupRestoreError) as err,
    ):
        await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    assert err.value.outcome["reason"] == "entity_identity_mismatch"
    assert err.value.outcome["apply_status"] == "not_applied"
    entry = entity_registry.async_get(desk.entry_id)
    assert entry is not None
    assert (entry.entity_id, entry.name) == ("light.lamp", "Desk")


async def test_a_core_refusal_in_the_component_writes_nothing(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    """An entity_id taken after the check makes Core refuse the one update
    that carries the rename and every other setting."""
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    entity_registry.async_update_entity(
        desk.entity_id, new_entity_id="light.lamp", name="Desk"
    )

    def take_the_old_id() -> None:
        entity_registry.async_get_or_create(
            "light", "hue", "other", suggested_object_id="desk"
        )

    with (
        before_command(hass, RESTORE, take_the_old_id),
        pytest.raises(bm.BackupRestoreError) as err,
    ):
        await entity_handler.restore(entity_client, desk.entity_id, snapshot)

    assert err.value.outcome["reason"] == "rejected_by_home_assistant"
    assert err.value.outcome["apply_status"] == "not_applied"
    assert "light.desk" in str(err.value)
    entry = entity_registry.async_get(desk.entry_id)
    assert entry is not None
    assert (entry.entity_id, entry.name) == ("light.lamp", "Desk")


async def test_an_error_reply_after_the_component_write_was_sent_is_unknown(
    hass: HomeAssistant,
    entity_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    """The write may have run, so Core's commands do not write it again."""
    path = await entity_snapshot(entity_manager, desk.entity_id)
    entity_registry.async_update_entity(desk.entity_id, name="Desk")

    with (
        crashing_on(hass, RESTORE, lambda msg: True),
        written(hass) as seen,
        pytest.raises(HomeAssistantCommandError) as err,
    ):
        await entity_manager.restore_snapshot(path.name)

    assert err.value.code == "unknown_error"
    assert vars(err.value)["restore_outcome"]["apply_status"] == "unknown"
    assert [len(seen[RESTORE]), len(seen[CORE_UPDATE])] == [1, 0]


async def test_no_answer_after_the_component_write_was_sent_is_unknown(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    entity_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    """The component ran; only its reply is missing, so Core's commands do not
    write it again."""
    path = await entity_snapshot(entity_manager, desk.entity_id)
    entity_registry.async_update_entity(desk.entity_id, name="Desk")

    async with unanswered(hass, RESTORE, monkeypatch):
        with written(hass) as seen, pytest.raises(HomeAssistantCommandTimeout) as err:
            await entity_manager.restore_snapshot(path.name)

    assert vars(err.value)["restore_outcome"]["apply_status"] == "unknown"
    assert [len(seen[RESTORE]), len(seen[CORE_UPDATE])] == [1, 0]


async def test_a_component_without_the_commands_falls_back(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    """A component older than the commands answers unknown_command; Core's
    commands restore, and the capabilities are probed again next time."""
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    entity_registry.async_update_entity(
        desk.entity_id, name="Desk", categories={"script": desk.cat_s}
    )
    assert await get_component_caps(entity_client) is not None

    with (
        component_commands_removed(hass),
        written(hass) as seen,
        received(hass, INFO) as probes,
    ):
        await entity_handler.restore(entity_client, desk.entity_id, snapshot)
        probed_while_restoring = len(probes[INFO])
        await get_component_caps(entity_client)

    assert await entity_handler.fetch(entity_client, desk.entity_id) == snapshot
    assert seen[CORE_UPDATE]
    assert (probed_while_restoring, len(probes[INFO])) == (1, 2)
