"""Core's Local To-do and a plain list on a real ``hass``, for the todo item backup tests.

``tasks`` is Core's Local To-do list ``todo.tasks``, set up from its config
entry; its rows get uids from Core. ``plain_list`` is a list of a test platform
under the same entity id, one that has no unique id and whose backend a test
can make fail or add rows of its own, which Local To-do never does.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from datetime import date
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from homeassistant.components.todo import DOMAIN as TODO_DOMAIN
from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.components.todo.const import DATA_COMPONENT
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_capture_events,
    setup_test_component_platform,
)
from starlette.requests import Request

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_todo import fetch_todo_item
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.settings_ui import build_settings_handlers

from ._backup_core import manage_backup

LIST = "todo.tasks"

PLAIN_FEATURES = (
    TodoListEntityFeature.CREATE_TODO_ITEM
    | TodoListEntityFeature.DELETE_TODO_ITEM
    | TodoListEntityFeature.UPDATE_TODO_ITEM
)
FULL_FEATURES = (
    PLAIN_FEATURES
    | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
    | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
    | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
)


@pytest.fixture
async def tasks(hass: HomeAssistant, hass_config_dir: str) -> MockConfigEntry:
    """Core's Local To-do list ``Tasks`` (``todo.tasks``), loaded and empty;
    its file goes under the test's own config directory."""
    storage = Path(hass_config_dir, ".storage")
    await hass.async_add_executor_job(partial(storage.mkdir, exist_ok=True))
    entry = MockConfigEntry(
        domain="local_todo",
        data={"storage_key": "tasks", "todo_list_name": "Tasks"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def todo_core(hass: HomeAssistant) -> None:
    """Core's todo integration with no list, and its registry commands: a list
    it does not hold is ``not_found``."""
    assert await async_setup_component(hass, TODO_DOMAIN, {})
    assert await async_setup_component(hass, "config", {})


async def write_item(
    hass: HomeAssistant,
    service: str,
    item: str,
    *,
    entity_id: str = LIST,
    **fields: Any,
) -> None:
    """Call Core's ``todo.<service>`` on the list, as a user's automation does."""
    await hass.services.async_call(
        TODO_DOMAIN,
        service,
        {"item": item, **fields},
        target={"entity_id": entity_id},
        blocking=True,
    )


async def add_item(
    hass: HomeAssistant,
    summary: str,
    *,
    completed: bool = False,
    entity_id: str = LIST,
    **fields: Any,
) -> str:
    """Add an item through Core's ``todo.add_item``; the uid Core gave it."""
    await write_item(hass, "add_item", summary, entity_id=entity_id, **fields)
    uid = uid_of(hass, summary, entity_id)
    if completed:
        await write_item(hass, "update_item", uid, status="completed")
    return uid


def uid_of(hass: HomeAssistant, summary: str, entity_id: str = LIST) -> str:
    """The uid of the last item named ``summary``."""
    entity = hass.data[DATA_COMPONENT].get_entity(entity_id)
    assert entity is not None
    found = [row.uid for row in entity.todo_items or [] if row.summary == summary]
    assert found and found[-1], f"no item named {summary!r} on {entity_id}"
    return found[-1]


async def move_to_top(hass: HomeAssistant, uid: str, entity_id: str = LIST) -> None:
    """Move an item to the top of the list, as the list's drag handle does."""
    entity = hass.data[DATA_COMPONENT].get_entity(entity_id)
    assert entity is not None
    await entity.async_move_todo_item(uid, None)


async def listed(
    client: HomeAssistantClient, entity_id: str = LIST
) -> list[dict[str, Any]]:
    """The rows Core lists for ``entity_id`` over ``todo/item/list``."""
    reply = await client.send_websocket_message(
        {"type": "todo/item/list", "entity_id": entity_id}
    )
    assert reply["success"], reply
    rows: list[dict[str, Any]] = reply["result"]["items"]
    return rows


def row_named(rows: list[dict[str, Any]], summary: str) -> dict[str, Any]:
    """The first listed row named ``summary``."""
    return next(row for row in rows if row["summary"] == summary)


async def item_snapshot(client: HomeAssistantClient, ref: str) -> dict[str, Any]:
    """The backup snapshot of item ``ref`` (summary or uid) of ``LIST``."""
    snapshot = await fetch_todo_item(client, f"{LIST}::{ref}")
    assert snapshot is not None, f"{ref!r} is not on {LIST}"
    return snapshot


def watch_services(
    hass: HomeAssistant,
) -> Callable[[], list[tuple[str, dict[str, Any]]]]:
    """From now on, the todo services Core is called with: each service and the
    data it got, read from Core's ``call_service`` events."""
    events = async_capture_events(hass, EVENT_CALL_SERVICE)

    def sent() -> list[tuple[str, dict[str, Any]]]:
        return [
            (event.data["service"], event.data["service_data"])
            for event in events
            if event.data["domain"] == TODO_DOMAIN
        ]

    return sent


async def take_offline(
    hass: HomeAssistant,
    registry: er.EntityRegistry,
    entry: MockConfigEntry,
    how: str,
) -> None:
    """Make Core's list not loaded: ``unavailable`` (its entry unloaded, so
    the state reads unavailable), ``disabled`` (no state, registry entry kept)
    or ``deleted`` (its entry removed, no state and no registry entry)."""
    if how == "unavailable":
        assert await hass.config_entries.async_unload(entry.entry_id)
    elif how == "disabled":
        registry.async_update_entity(LIST, disabled_by=er.RegistryEntryDisabler.USER)
    else:
        assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert (hass.states.get(LIST) is None) == (how != "unavailable")
    assert (registry.async_get(LIST) is None) == (how == "deleted")


async def settings_call(
    client: HomeAssistantClient, route: str, name: str
) -> tuple[int, dict[str, Any]]:
    """The Settings backup route ``route`` for snapshot ``name``, on a server
    whose client is ``client``: its status and JSON body."""
    server = cast(HomeAssistantSmartMCPServer, SimpleNamespace(client=client))
    request = Request(
        {"type": "http", "query_string": b"", "path_params": {"name": name}}
    )
    response = await build_settings_handlers(server)[route](request)
    return response.status_code, json.loads(bytes(response.body))


async def mcp_call(
    client: HomeAssistantClient, action: str, name: str
) -> dict[str, Any]:
    """The ``ha_manage_backup`` ``action`` on snapshot ``name`` that the tool
    refuses: its error body."""
    with pytest.raises(ToolError) as caught:
        await manage_backup(client, scope="edits", action=action, backup_name=name)
    body: dict[str, Any] = json.loads(str(caught.value))
    return body


class PlainList(TodoListEntity):
    """A todo integration's list: rows in memory, no unique id, no registry entry.

    ``created_as`` decides which rows a created item leaves on the list,
    ``update_failure`` is what its backend raises on an update, and
    ``is_available`` and ``features`` are what it reports once the test has
    written its state again, so a test can reach the backends Local To-do is not.
    """

    _attr_should_poll = False

    def __init__(self, rows: list[TodoItem]) -> None:
        self.entity_id = LIST
        self.rows = rows
        self._attr_todo_items = rows
        self.features = FULL_FEATURES
        self.is_available = True
        self.next_uid = 100
        self.update_failure: Exception | None = None
        self.created_as: Callable[[TodoItem], list[TodoItem]] = lambda item: [item]

    @property
    def available(self) -> bool:
        return self.is_available

    @property
    def supported_features(self) -> TodoListEntityFeature:
        return self.features

    async def async_create_todo_item(self, item: TodoItem) -> None:
        self.next_uid += 1
        self.rows.extend(
            self.created_as(dataclasses.replace(item, uid=str(self.next_uid)))
        )
        self.async_write_ha_state()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        if self.update_failure is not None:
            raise self.update_failure
        at = next(i for i, row in enumerate(self.rows) if row.uid == item.uid)
        self.rows[at] = item
        self.async_write_ha_state()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        self.rows[:] = [row for row in self.rows if row.uid not in uids]
        self.async_write_ha_state()


@pytest.fixture
async def plain_list(hass: HomeAssistant) -> PlainList:
    """``todo.tasks`` of a test platform: ``Buy milk`` (uid 1) and a completed,
    dated ``Pay`` (uid 2)."""
    entity = PlainList(
        [
            TodoItem(uid="1", summary="Buy milk", status=TodoItemStatus.NEEDS_ACTION),
            TodoItem(
                uid="2",
                summary="Pay",
                status=TodoItemStatus.COMPLETED,
                due=date(2026, 11, 1),
            ),
        ]
    )
    setup_test_component_platform(hass, TODO_DOMAIN, [entity])
    assert await async_setup_component(
        hass, TODO_DOMAIN, {TODO_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()
    return entity
