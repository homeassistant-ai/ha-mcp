"""Template helpers on a real ``hass`` for the restore-safety, recreation and
review-outcome tests: a deleted helper with a renamed entity, a restore held
when it starts to apply, and Core's own answer to a command followed by a
change the test makes.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm

from ._template_backup_core import real_snapshot
from ._template_helper_core import create_template_sensor

DOMAIN = "helper_template"
RENAMED = "sensor.renamed_example"
CUSTOM_NAME = "Custom name"


@dataclasses.dataclass
class DeletedTemplate:
    """A snapshot of a Template sensor whose config entry Core then removed."""

    manager: bm.BackupManager
    name: str
    entry_id: str


@pytest.fixture
async def deleted_template(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, manager: bm.BackupManager
) -> DeletedTemplate:
    """Its entity was renamed to ``RENAMED`` / ``CUSTOM_NAME`` before capture."""
    entry = await create_template_sensor(hass, "saved")
    (row,) = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    entity_registry.async_update_entity(
        row.entity_id, new_entity_id=RENAMED, name=CUSTOM_NAME
    )
    path = await real_snapshot(manager, entry)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    return DeletedTemplate(manager, path.name, entry.entry_id)


def recreated_entry(hass: HomeAssistant) -> ConfigEntry:
    """The one Template entry Core holds after the deleted one came back."""
    (entry,) = hass.config_entries.async_entries("template")
    return entry


async def create_template_button(
    hass: HomeAssistant, press: list[dict[str, Any]]
) -> ConfigEntry:
    """A Template button made by Core's config flow, running ``press``."""
    assert await async_setup_component(hass, "config", {})
    result = await hass.config_entries.flow.async_init(
        "template", context={"source": "user"}
    )
    for answer in ({"next_step_id": "button"}, {"name": "Example", "press": press}):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], answer
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result["result"]


@dataclasses.dataclass
class HeldRestore:
    """The Template restore's apply, held until ``release`` is set."""

    entered: asyncio.Event
    release: asyncio.Event
    targets: list[str]


def hold_restore(manager: bm.BackupManager) -> HeldRestore:
    """Stop each Template restore as it starts to apply; once released, the
    manager's own restore runs. ``targets`` records the id each one applied to."""
    handler = manager.handler_for(DOMAIN)
    assert handler is not None
    held = HeldRestore(asyncio.Event(), asyncio.Event(), [])

    async def restore(client: Any, entity_id: str, config: Any) -> Any:
        held.targets.append(entity_id)
        held.entered.set()
        await held.release.wait()
        return await handler.restore(client, entity_id, config)

    manager.register(dataclasses.replace(handler, restore=restore))
    return held


def edit_snapshot(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    """Change a snapshot file in place, as a hand edit or an older release would."""
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data))


def stored_state(manager: bm.BackupManager, name: str) -> str:
    """The state template a Template snapshot holds."""
    return str(manager.read_snapshot(name)["config"]["options"]["state"])


@contextmanager
def then_after(
    hass: HomeAssistant, command: str, change: Callable[[], None]
) -> Iterator[None]:
    """Core answers ``command`` itself; ``change`` runs right after its handler,
    as another user's edit landing before the next read would."""
    core_handler, schema = hass.data[websocket_api.DOMAIN][command]

    def answer_then_change(core: HomeAssistant, connection: Any, msg: Any) -> None:
        core_handler(core, connection, msg)
        change()

    websocket_api.async_register_command(hass, command, answer_then_change, schema)
    try:
        yield
    finally:
        websocket_api.async_register_command(hass, command, core_handler, schema)
