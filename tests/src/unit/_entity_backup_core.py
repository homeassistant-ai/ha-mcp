"""A real ``light.desk`` and the entity backup handler on both restore paths.

``entity_client`` is the server's client on a ``hass`` that answers Core's
``config`` commands, once without the component (``websocket``: Core's
commands write the entity) and once with its commands (``component``: one
in-process registry write). ``desk`` is a registry entry with an area, a
label, a category and options of its own; a test changes it through Core's
registry and the manager reads it back over the client.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools import websocket_api as tools_api
from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp.client.rest_client import HomeAssistantClient

from ._command_faults import received, unanswered

NOT_RESTORED = "The device name is not part of an entity backup and was left as it is."
LOOKUP_COMMANDS = {
    "websocket": "config/entity_registry/list",
    "component": "ha_mcp_tools/entity_entry",
}
WRITE_COMMANDS = (
    "config/entity_registry/update",
    "homeassistant/expose_entity",
    "ha_mcp_tools/entity_restore",
)
COMPONENT_COMMANDS = ("ha_mcp_tools/entity_entry", "ha_mcp_tools/entity_restore")
DESK_OPTIONS: dict[str, dict[str, Any]] = {
    "light": {"favorite_colors": []},
    "conversation": {"should_expose": False},
    "cloud.alexa": {"should_expose": True},
}


@dataclass(frozen=True)
class Desk:
    """``light.desk`` as the fixture built it, and the registry ids it refers to."""

    entry_id: str
    office: str
    kitchen: str
    work: str
    cat_a: str
    cat_b: str
    cat_s: str
    entity_id: str = "light.desk"


@pytest.fixture(params=["websocket", "component"])
def entity_path(request: pytest.FixtureRequest) -> str:
    """Which restore path a test runs on: ``websocket`` or ``component``."""
    return str(request.param)


@pytest.fixture
def lookup_command(entity_path: str) -> str:
    """The command the restore finds an entry by its id with, on this path."""
    return LOOKUP_COMMANDS[entity_path]


@pytest.fixture
async def entity_client(
    entity_path: str, hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """``core_client`` on a ``hass`` with Core's registry commands, and the
    component's when the path is ``component``."""
    assert await async_setup_component(hass, "config", {})
    if entity_path == "component":
        tools_api.async_register_commands(hass)
    return core_client


@pytest.fixture
def entity_manager(
    entity_client: HomeAssistantClient, backup_settings: config.Settings
) -> bm.BackupManager:
    return bm.get_backup_manager(entity_client, backup_settings)


@pytest.fixture
def entity_handler(entity_manager: bm.BackupManager) -> bm.DomainHandler:
    """The manager's ``entity`` handler: ``fetch`` and ``restore``."""
    handler = entity_manager.handler_for("entity")
    assert handler is not None
    return handler


@pytest.fixture
async def desk(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    area_registry: ar.AreaRegistry,
    label_registry: lr.LabelRegistry,
    category_registry: cr.CategoryRegistry,
) -> Desk:
    """``light.desk``: in an area, with an alias, a label, a category and the
    options a voice assistant and a card write."""
    assert await async_setup_component(hass, "homeassistant", {})
    office = area_registry.async_create("Office")
    kitchen = area_registry.async_create("Kitchen")
    work = label_registry.async_create("Work")
    cat_a = category_registry.async_create(name="A", scope="automation")
    cat_b = category_registry.async_create(name="B", scope="automation")
    cat_s = category_registry.async_create(name="S", scope="script")
    entry = entity_registry.async_get_or_create(
        "light", "hue", "desk", suggested_object_id="desk"
    )
    entity_registry.async_update_entity(
        entry.entity_id,
        area_id=office.id,
        aliases=["desk lamp"],
        labels={work.label_id},
        categories={"automation": cat_a.category_id},
    )
    for domain, options in DESK_OPTIONS.items():
        entity_registry.async_update_entity_options(entry.entity_id, domain, options)
    return Desk(
        entry_id=entry.id,
        office=office.id,
        kitchen=kitchen.id,
        work=work.label_id,
        cat_a=cat_a.category_id,
        cat_b=cat_b.category_id,
        cat_s=cat_s.category_id,
    )


def with_config_entry(
    hass: HomeAssistant, registry: er.EntityRegistry, entity_id: str
) -> MockConfigEntry:
    """Give ``entity_id`` a config entry of its own."""
    config_entry = MockConfigEntry(domain="hue")
    config_entry.add_to_hass(hass)
    registry.async_update_entity(entity_id, config_entry_id=config_entry.entry_id)
    return config_entry


def with_device(
    hass: HomeAssistant,
    registry: er.EntityRegistry,
    devices: dr.DeviceRegistry,
    entity_id: str,
) -> dr.DeviceEntry:
    """Put ``entity_id`` on a device of its own, which has a config entry."""
    config_entry = with_config_entry(hass, registry, entity_id)
    device = devices.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("hue", "desk")}
    )
    registry.async_update_entity(entity_id, device_id=device.id)
    return device


@contextmanager
def component_commands_removed(hass: HomeAssistant) -> Iterator[None]:
    """Core does not know the component's entity commands, as after the
    component was replaced by a release that predates them."""
    registered = hass.data[websocket_api.DOMAIN]
    removed = {command: registered.pop(command) for command in COMPONENT_COMMANDS}
    try:
        yield
    finally:
        registered.update(removed)


async def entity_snapshot(manager: bm.BackupManager, entity_id: str) -> Path:
    """A snapshot of the registry entry ``entity_id``."""
    path = await manager.maybe_snapshot("entity", entity_id, tool_name="t")
    assert path is not None, f"nothing was captured for {entity_id}"
    return path


def backup_dir(settings: config.Settings) -> Path:
    """The directory snapshots are written to."""
    return Path(settings.auto_backup_dir)


def backup_files(directory: Path) -> list[str]:
    """The names in the backup directory (empty before the first snapshot)."""
    return sorted(p.name for p in directory.glob("*")) if directory.exists() else []


@contextmanager
def written(hass: HomeAssistant) -> Iterator[dict[str, list[Any]]]:
    """The registry writes Core received meanwhile, by command."""
    registered = hass.data[websocket_api.DOMAIN]
    commands = [command for command in WRITE_COMMANDS if command in registered]
    with received(hass, *commands) as seen:
        yield seen


def write_count(seen: dict[str, list[Any]]) -> int:
    return sum(len(messages) for messages in seen.values())


@contextmanager
def before_command(
    hass: HomeAssistant, command: str, action: Callable[[], None], *, nth: int = 1
) -> Iterator[None]:
    """Run ``action``, a change made in Core, just before Core's own handler
    takes the ``nth`` message of ``command``. Core still answers it."""
    core_handler, schema = hass.data[websocket_api.DOMAIN][command]
    seen = 0

    def act_then_answer(
        core: HomeAssistant, connection: Any, msg: dict[str, Any]
    ) -> None:
        nonlocal seen
        seen += 1
        if seen == nth:
            action()
        core_handler(core, connection, msg)

    websocket_api.async_register_command(hass, command, act_then_answer, schema)
    try:
        yield
    finally:
        websocket_api.async_register_command(hass, command, core_handler, schema)


@contextmanager
def crashing_on(
    hass: HomeAssistant, command: str, when: Callable[[dict[str, Any]], bool]
) -> Iterator[None]:
    """Core's handler of ``command`` raises for the messages ``when`` picks, so
    Core answers them with its own ``unknown_error``."""
    core_handler, schema = hass.data[websocket_api.DOMAIN][command]

    def crash_or_answer(
        core: HomeAssistant, connection: Any, msg: dict[str, Any]
    ) -> None:
        if when(msg):
            raise RuntimeError("handler crashed")
        core_handler(core, connection, msg)

    websocket_api.async_register_command(hass, command, crash_or_answer, schema)
    try:
        yield
    finally:
        websocket_api.async_register_command(hass, command, core_handler, schema)


@asynccontextmanager
async def first_unanswered(
    hass: HomeAssistant, command: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """Only the first message of ``command`` goes unanswered (see
    ``unanswered``); Core answers the later ones at once."""
    core_handler, schema = hass.data[websocket_api.DOMAIN][command]
    async with unanswered(hass, command, monkeypatch):
        held_handler = hass.data[websocket_api.DOMAIN][command][0]
        seen = 0

        def hold_first(
            core: HomeAssistant, connection: Any, msg: dict[str, Any]
        ) -> None:
            nonlocal seen
            seen += 1
            (held_handler if seen == 1 else core_handler)(core, connection, msg)

        websocket_api.async_register_command(hass, command, hold_first, schema)
        yield


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait for ``condition``; fail the test after ``timeout`` seconds."""
    step = 0.01
    for _ in range(round(timeout / step)):
        if condition():
            return
        await asyncio.sleep(step)
    raise TimeoutError("the condition never held")
