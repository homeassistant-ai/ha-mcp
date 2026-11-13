"""Concurrent registry writes from this server must not lose one another's labels.

Home Assistant has no atomic label add: a tool reads the labels, adds one and
writes the whole list back. Two such writes on one entity or area, or a
replacement of the list racing an add, lose a change unless the server holds
the resource's lock from the read through the write.

Each race holds the first write inside Core: Core's own handler is
re-registered behind an event and still answers. A restore is held instead
just before it reads the recreated entity back. The hold ends as soon as the
second call either waits for the same resource's lock or has its own write
reach Core. With the lock, the second call then reads what the first one
wrote; without it, the first write lands last and the second call's change is
lost.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from homeassistant.util import slugify
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp import backup_entity_ids
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_manager import (
    BackupRestoreError,
    DomainHandler,
    get_backup_manager,
)
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.config import get_global_settings
from ha_mcp.tools.tools_areas import AreaTools
from ha_mcp.tools.tools_config_helpers import HelperConfigTools
from ha_mcp.tools.tools_entities import EntityTools
from ha_mcp.tools.tools_labels import LabelTools
from ha_mcp.utils.registry_update_lock import (
    registry_update_lock,
    registry_update_locks,
)

# Every module that resolves ``registry_update_lock`` by name when it locks:
# ``registry_update_locks`` (entity tools) resolves it in its own module, and
# ``backup_entity_ids`` through ``backup_manager``.
_LOCK_SITES = (
    "ha_mcp.tools.tools_labels",
    "ha_mcp.tools.tools_areas",
    "ha_mcp.utils.registry_update_lock",
    "ha_mcp.tools.config_helpers.registry",
    "ha_mcp.tools.config_helpers.create",
    "ha_mcp.tools.config_helpers.update",
    "ha_mcp.backup_manager",
)
# Bounds a wait that only runs out when the protection is gone.
_WAIT_S = 5


class _LockWaits:
    """Notes a call that asks for one resource's lock while it is held, so
    the call is about to wait; the call still gets the real lock."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, registry: str, resource_id: str
    ) -> None:
        self.waiting = asyncio.Event()
        contested = (registry, resource_id)

        def observed(registry: str, resource_id: str) -> asyncio.Lock:
            lock = registry_update_lock(registry, resource_id)
            if (registry, resource_id) == contested and lock.locked():
                self.waiting.set()
            return lock

        for site in _LOCK_SITES:
            monkeypatch.setattr(f"{site}.registry_update_lock", observed)


class _CoreWrites:
    """Core's own handler for ``command``, re-registered so the test sees the
    messages ``match`` picks. Core still answers every one.

    With ``hold``, the first picked message waits inside Core for ``release``
    and sets ``entered``; every other picked message sets ``seen``.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        command: str,
        match: Callable[[dict[str, Any]], bool],
        *,
        hold: bool,
    ) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.seen = asyncio.Event()
        core_handler, schema = hass.data[websocket_api.DOMAIN][command]

        async def observe(
            core: HomeAssistant,
            connection: websocket_api.ActiveConnection,
            msg: dict[str, Any],
        ) -> None:
            if match(msg):
                if hold and not self.entered.is_set():
                    self.entered.set()
                    await self.release.wait()
                else:
                    self.seen.set()
            core_handler(core, connection, msg)

        websocket_api.async_register_command(
            hass, command, websocket_api.async_response(observe), schema
        )


def _held_write(hass: HomeAssistant, command: str, key: str, value: str) -> _CoreWrites:
    """Holds the first ``command`` whose ``key`` is ``value``."""
    return _CoreWrites(hass, command, lambda msg: msg.get(key) == value, hold=True)


async def _cancel_and_wait(*tasks: asyncio.Future[Any]) -> None:
    """Cancel what is still running and wait for it, so no task outlives the
    test (a finished task keeps its result)."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _first_wins_race(
    entered: asyncio.Event,
    release: asyncio.Event,
    progressed: list[asyncio.Event],
    first: Awaitable[Any],
    second: Callable[[], Awaitable[Any]],
) -> list[Any]:
    """Run ``first`` until it is held (``entered``), then ``second`` until one
    of ``progressed`` is set; release ``first`` and return both results."""
    tasks = [asyncio.ensure_future(first)]
    try:
        async with asyncio.timeout(_WAIT_S):
            await entered.wait()
            tasks.append(asyncio.ensure_future(second()))
            waits = [asyncio.create_task(event.wait()) for event in progressed]
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for wait in waits:
                    wait.cancel()
                await asyncio.gather(*waits, return_exceptions=True)
    finally:
        release.set()
        try:
            async with asyncio.timeout(_WAIT_S):
                await asyncio.wait(tasks)
        finally:
            await _cancel_and_wait(*tasks)
    return [task.result() for task in tasks]


async def _held_write_race(
    held: _CoreWrites,
    locks: _LockWaits,
    first: Awaitable[Any],
    second: Callable[[], Awaitable[Any]],
) -> list[Any]:
    """``first``'s write is held in Core until ``second`` waits for the lock
    or its own write reaches Core."""
    return await _first_wins_race(
        held.entered, held.release, [locks.waiting, held.seen], first, second
    )


async def _after_lock_wait(
    locks: _LockWaits,
    registry: str,
    resource_id: str,
    call: Awaitable[Any],
    meanwhile: Callable[[], object],
) -> asyncio.Future[Any]:
    """Start ``call`` while the test holds the resource's lock; once the call
    waits for it, run ``meanwhile``, then let the call proceed."""
    async with registry_update_lock(registry, resource_id):
        pending = asyncio.ensure_future(call)
        try:
            async with asyncio.timeout(_WAIT_S):
                await locks.waiting.wait()
            meanwhile()
        except BaseException:
            await _cancel_and_wait(pending)
            raise
    return pending


@pytest.fixture
async def client(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    label_registry: lr.LabelRegistry,
) -> HomeAssistantClient:
    """Core with its registry commands and the labels the races use."""
    assert await async_setup_component(hass, "config", {})
    for name in ("Existing", "Red", "Blue"):
        label_registry.async_create(name)
    return core_client


@pytest.fixture
def target_area(area_registry: ar.AreaRegistry) -> str:
    area = area_registry.async_create("Target")
    area_registry.async_update(area.id, labels={"existing"})
    return area.id


@pytest.fixture
def target_entity(entity_registry: er.EntityRegistry) -> str:
    entry = entity_registry.async_get_or_create(
        "light", "test", "target", suggested_object_id="target"
    )
    entity_registry.async_update_entity(entry.entity_id, labels={"existing"})
    return entry.entity_id


def _area_labels(area_registry: ar.AreaRegistry, area_id: str) -> set[str]:
    area = area_registry.async_get_area(area_id)
    assert area is not None
    return set(area.labels)


def _entity_labels(entity_registry: er.EntityRegistry, entity_id: str) -> set[str]:
    entry = entity_registry.async_get(entity_id)
    assert entry is not None
    return set(entry.labels)


def _add_to_area(
    client: HomeAssistantClient, label_id: str, area_id: str
) -> Coroutine[Any, Any, Any]:
    """The label tool's own area assignment: an add, never a replacement."""
    return LabelTools(client).ha_config_set_label(
        name=label_id.title(), label_id=label_id, areas=[area_id]
    )


def _area_race(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, area_id: str
) -> tuple[_CoreWrites, _LockWaits]:
    return (
        _held_write(hass, "config/area_registry/update", "area_id", area_id),
        _LockWaits(monkeypatch, "area", area_id),
    )


def _entity_race(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, entity_id: str
) -> tuple[_CoreWrites, _LockWaits]:
    return (
        _held_write(hass, "config/entity_registry/update", "entity_id", entity_id),
        _LockWaits(monkeypatch, "entity", entity_id),
    )


async def test_concurrent_label_adds_to_one_area_keep_both_labels(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    target_area: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held, locks = _area_race(hass, monkeypatch, target_area)

    results = await _held_write_race(
        held,
        locks,
        _add_to_area(client, "red", target_area),
        lambda: _add_to_area(client, "blue", target_area),
    )

    assert all(result["success"] for result in results)
    assert _area_labels(area_registry, target_area) == {"existing", "red", "blue"}


@pytest.mark.parametrize(
    ("entity_ids", "labels", "operation", "expected"),
    [
        ("single", ["blue"], "add", {"existing", "red", "blue"}),
        ("single", ["existing"], "remove", {"red"}),
        ("single", ["blue"], "set", {"blue"}),
        ("bulk", ["blue"], "add", {"existing", "red", "blue"}),
    ],
    ids=["add", "remove", "set", "bulk-add"],
)
async def test_concurrent_entity_label_writes_apply_in_turn(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    target_entity: str,
    monkeypatch: pytest.MonkeyPatch,
    entity_ids: str,
    labels: list[str],
    operation: str,
    expected: set[str],
) -> None:
    """A second write on the entity sees the in-flight add, also from a bulk call."""
    held, locks = _entity_race(hass, monkeypatch, target_entity)
    tools = EntityTools(client)
    second_target: str | list[str] = (
        [target_entity] if entity_ids == "bulk" else target_entity
    )

    results = await _held_write_race(
        held,
        locks,
        tools.ha_set_entity(
            entity_id=target_entity, labels=["red"], label_operation="add"
        ),
        lambda: tools.ha_set_entity(
            entity_id=second_target, labels=labels, label_operation=operation
        ),
    )

    assert all(result["success"] for result in results)
    assert _entity_labels(entity_registry, target_entity) == expected


async def test_label_add_to_another_area_is_not_blocked_by_a_held_write(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    target_area: str,
) -> None:
    """The lock is per area: a slow write to one area does not stall another."""
    other = area_registry.async_create("Other").id
    held = _held_write(hass, "config/area_registry/update", "area_id", target_area)
    first = asyncio.create_task(_add_to_area(client, "red", target_area))
    try:
        async with asyncio.timeout(_WAIT_S):
            await held.entered.wait()
            result = await _add_to_area(client, "blue", other)
    finally:
        held.release.set()
        async with asyncio.timeout(_WAIT_S):
            held_result = await first

    assert result["success"]
    assert held_result["success"]
    assert _area_labels(area_registry, other) == {"blue"}


async def test_area_label_add_that_gets_no_answer_frees_the_area(
    client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    target_area: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write that fails inside the area's critical section does not wedge
    the next label add on that area."""
    locks = _LockWaits(monkeypatch, "area", target_area)
    no_answer = patch.object(
        client,
        "send_websocket_message",
        side_effect=HomeAssistantConnectionError("Home Assistant did not answer"),
    )
    pending = await _after_lock_wait(
        locks,
        "area",
        target_area,
        _add_to_area(client, "red", target_area),
        no_answer.start,
    )
    try:
        with pytest.raises(ToolError, match="did not answer"):
            await asyncio.wait_for(pending, _WAIT_S)
    finally:
        no_answer.stop()

    result = await asyncio.wait_for(_add_to_area(client, "blue", target_area), _WAIT_S)

    assert result["success"]
    assert _area_labels(area_registry, target_area) == {"existing", "blue"}


async def test_area_label_replacement_waits_for_an_inflight_add(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    target_area: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ha_set_area_or_floor(labels=...)`` replaces the set after the add,
    so the add cannot bring back labels the caller just replaced."""
    held, locks = _area_race(hass, monkeypatch, target_area)

    results = await _held_write_race(
        held,
        locks,
        _add_to_area(client, "red", target_area),
        lambda: AreaTools(client).ha_set_area_or_floor(
            kind="area", id=target_area, labels=["blue"]
        ),
    )

    assert all(result["success"] for result in results)
    assert _area_labels(area_registry, target_area) == {"blue"}


@pytest.mark.parametrize("kind", ["floor", "label"])
async def test_area_write_rechecks_a_reference_deleted_while_it_waited(
    client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
    label_registry: lr.LabelRegistry,
    target_area: str,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """A floor or label deleted while the area write waited for the area is
    refused, not stored as a dangling floor or silently dropped label."""
    locks = _LockWaits(monkeypatch, "area", target_area)
    if kind == "floor":
        floor = floor_registry.async_create("Reference").floor_id
        params: dict[str, Any] = {"floor_id": floor}

        def delete() -> None:
            floor_registry.async_delete(floor)
    else:
        label = label_registry.async_create("Reference").label_id
        params = {"labels": [label]}

        def delete() -> None:
            label_registry.async_delete(label)

    pending = await _after_lock_wait(
        locks,
        "area",
        target_area,
        AreaTools(client).ha_set_area_or_floor(kind="area", id=target_area, **params),
        delete,
    )

    with pytest.raises(ToolError) as excinfo:
        await asyncio.wait_for(pending, _WAIT_S)
    error = json.loads(str(excinfo.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    area = area_registry.async_get_area(target_area)
    assert area is not None
    assert area.floor_id is None
    assert area.labels == {"existing"}


@pytest.mark.parametrize("kind", ["label", "category"])
async def test_bulk_entity_write_rechecks_a_reference_deleted_while_it_waited(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    label_registry: lr.LabelRegistry,
    category_registry: cr.CategoryRegistry,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """In a bulk call, the entity that waited for its lock re-checks the
    reference; the entity written before the deletion stays a success."""
    ready, waiting = (
        entity_registry.async_get_or_create(
            "light", "test", name, suggested_object_id=name
        ).entity_id
        for name in ("ready", "waiting")
    )
    locks = _LockWaits(monkeypatch, "entity", waiting)
    if kind == "category":
        reference = category_registry.async_create(
            scope="helpers", name="Reference"
        ).category_id
        params: dict[str, Any] = {"categories": {"helpers": reference}}

        def delete() -> None:
            category_registry.async_delete(scope="helpers", category_id=reference)
    else:
        reference = label_registry.async_create("Reference").label_id
        params = {"labels": [reference], "label_operation": "add"}

        def delete() -> None:
            label_registry.async_delete(reference)

    ready_written = asyncio.Event()

    @callback
    def note_ready_write(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        if event.data["action"] == "update" and event.data["entity_id"] == ready:
            ready_written.set()

    unsubscribe = hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED, note_ready_write
    )
    try:
        async with registry_update_lock("entity", waiting):
            pending = asyncio.create_task(
                EntityTools(client).ha_set_entity(entity_id=[ready, waiting], **params)
            )
            try:
                async with asyncio.timeout(_WAIT_S):
                    await locks.waiting.wait()
                    await ready_written.wait()
                delete()
            except BaseException:
                await _cancel_and_wait(pending)
                raise
    finally:
        unsubscribe()

    result = await asyncio.wait_for(pending, _WAIT_S)

    assert result["success"] is False
    assert result["partial"] is True
    assert [entry["entity_id"] for entry in result["succeeded"]] == [ready]
    [failed] = result["failed"]
    assert failed["entity_id"] == waiting
    assert reference in failed["error"]
    entry = entity_registry.async_get(waiting)
    assert entry is not None
    assert entry.labels == set()
    assert entry.categories == {}


# A derivative helper: a flow helper of Core's own whose one sensor's unique_id
# is its config entry's id, so a recreated entry gets a new entity.
_DERIVATIVE_FORM = {
    "source": "sensor.power",
    "round": 2.0,
    "time_window": {"hours": 0, "minutes": 0, "seconds": 0},
    "unit_time": "h",
}
_DERIVATIVE_ENTITY = "sensor.power_change"


async def _derivative(hass: HomeAssistant) -> MockConfigEntry:
    hass.states.async_set("sensor.power", "1", {"unit_of_measurement": "W"})
    entry = MockConfigEntry(
        domain="derivative",
        title="Power change",
        options={"name": "Power change", **_DERIVATIVE_FORM},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _input_boolean_name() -> str:
    """A fresh name: Core gives a removed helper's registry entry back to one
    created again under the same id."""
    return f"Target {uuid.uuid4().hex[:8]}"


async def test_label_add_waits_for_a_new_helpers_label_write(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A label add on a just-created helper lands after the creation's own
    label write, so neither replaces the other."""
    assert await async_setup_component(hass, "input_boolean", {})
    name = _input_boolean_name()
    entity_id = f"input_boolean.{slugify(name)}"
    held, locks = _entity_race(hass, monkeypatch, entity_id)

    results = await _held_write_race(
        held,
        locks,
        HelperConfigTools(client).ha_config_set_helper(
            helper_type="input_boolean", name=name, labels=["blue"], wait=False
        ),
        lambda: EntityTools(client).ha_set_entity(
            entity_id=entity_id, labels=["red"], label_operation="add"
        ),
    )

    assert all(result["success"] for result in results)
    assert _entity_labels(entity_registry, entity_id) == {"blue", "red"}


async def test_helper_label_update_waits_for_an_inflight_add(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await async_setup_component(hass, "input_boolean", {})
    helpers = HelperConfigTools(client)
    name = _input_boolean_name()
    created = await helpers.ha_config_set_helper(
        helper_type="input_boolean", name=name, wait=False
    )
    entity_id = created["entity_id"]
    entity_registry.async_update_entity(entity_id, labels={"existing"})
    held, locks = _entity_race(hass, monkeypatch, entity_id)

    results = await _held_write_race(
        held,
        locks,
        EntityTools(client).ha_set_entity(
            entity_id=entity_id, labels=["red"], label_operation="add"
        ),
        lambda: helpers.ha_config_set_helper(
            helper_type="input_boolean",
            helper_id=slugify(name),
            name=name,
            labels=["blue"],
            wait=False,
            action="update",
        ),
    )

    assert all(result["success"] for result in results)
    assert _entity_labels(entity_registry, entity_id) == {"blue"}


async def test_flow_helper_label_update_waits_for_an_inflight_add(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await _derivative(hass)
    entity_registry.async_update_entity(_DERIVATIVE_ENTITY, labels={"existing"})
    held, locks = _entity_race(hass, monkeypatch, _DERIVATIVE_ENTITY)

    results = await _held_write_race(
        held,
        locks,
        EntityTools(client).ha_set_entity(
            entity_id=_DERIVATIVE_ENTITY, labels=["red"], label_operation="add"
        ),
        lambda: HelperConfigTools(client).ha_config_set_helper(
            helper_type="derivative",
            helper_id=entry.entry_id,
            config=dict(_DERIVATIVE_FORM),
            labels=["blue"],
            wait=False,
            action="update",
        ),
    )

    assert all(result["success"] for result in results)
    assert _entity_labels(entity_registry, _DERIVATIVE_ENTITY) == {"blue"}


def _handler(client: HomeAssistantClient, domain: str) -> DomainHandler:
    handler = get_backup_manager(client, get_global_settings()).handler_for(domain)
    assert handler is not None
    return handler


async def test_area_restore_waits_for_an_inflight_label_add(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    target_area: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring an area's snapshot replaces its labels after the add."""
    handler = _handler(client, "area_or_floor")
    area_registry.async_update(target_area, labels={"blue"})
    snapshot = await handler.fetch(client, f"area:{target_area}")
    area_registry.async_update(target_area, labels={"existing"})
    held, locks = _area_race(hass, monkeypatch, target_area)

    await _held_write_race(
        held,
        locks,
        _add_to_area(client, "red", target_area),
        lambda: handler.restore(client, f"area:{target_area}", snapshot),
    )

    assert _area_labels(area_registry, target_area) == {"blue"}


@pytest.fixture
def restore_client(
    client: HomeAssistantClient, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """``client`` on a Core whose component also lists the flow helpers a
    snapshot reads."""
    assert component_core_client is client
    return client


async def _deleted_derivative(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    **saved: Any,
) -> tuple[DomainHandler, str, dict[str, Any]]:
    """A derivative whose entity carried ``saved``, snapshotted and deleted."""
    entry = await _derivative(hass)
    entity_registry.async_update_entity(_DERIVATIVE_ENTITY, **saved)
    handler = _handler(client, "helper_derivative")
    snapshot = await handler.fetch(client, entry.entry_id)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    return handler, entry.entry_id, snapshot


class _HeldReadback:
    """Holds the restore's read of the recreated entity once ``ready`` is true;
    the read itself is the real one."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, ready: Callable[[], bool]
    ) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        read = backup_entity_ids.created_entity

        async def held(
            client: HomeAssistantClient, entry_id: str, unique_id: str
        ) -> dict[str, Any]:
            if not self.entered.is_set() and ready():
                self.entered.set()
                await self.release.wait()
            return await read(client, entry_id, unique_id)

        monkeypatch.setattr(backup_entity_ids, "created_entity", held)


@pytest.mark.parametrize(
    "restored", [_DERIVATIVE_ENTITY, "sensor.restored"], ids=["same-id", "renamed"]
)
async def test_restore_verifies_the_entity_before_a_concurrent_rename_lands(
    hass: HomeAssistant,
    restore_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
    restored: str,
) -> None:
    """A rename of the restored entity waits until the restore has verified
    the ID and name it wrote, so the restore does not report a mismatch."""
    saved: dict[str, Any] = {"name": "Saved name"}
    if restored != _DERIVATIVE_ENTITY:
        saved["new_entity_id"] = restored
    handler, entry_id, snapshot = await _deleted_derivative(
        hass, restore_client, entity_registry, **saved
    )

    def written() -> bool:
        entry = entity_registry.async_get(restored)
        return entry is not None and entry.name == "Saved name"

    readback = _HeldReadback(monkeypatch, written)
    locks = _LockWaits(monkeypatch, "entity", restored)
    renamed = _CoreWrites(
        hass,
        "config/entity_registry/update",
        lambda msg: msg.get("name") == "Later name",
        hold=False,
    )

    outcome, rename = await _first_wins_race(
        readback.entered,
        readback.release,
        [locks.waiting, renamed.seen],
        handler.restore(restore_client, entry_id, snapshot),
        lambda: EntityTools(restore_client).ha_set_entity(
            entity_id=restored, name="Later name"
        ),
    )

    assert outcome["entity_id_mapping"] == [
        {"created_entity_id": _DERIVATIVE_ENTITY, "restored_entity_id": restored}
    ]
    assert rename["success"]
    entry = entity_registry.async_get(restored)
    assert entry is not None
    assert entry.name == "Later name"


@pytest.mark.parametrize("changed", ["name", "entity_id"])
async def test_restore_rereads_the_entity_after_waiting_for_its_lock(
    hass: HomeAssistant,
    restore_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    """What changed while the restore waited decides its write: a new name is
    put back to the saved one, a new entity ID stops the restore unwritten."""
    handler, entry_id, snapshot = await _deleted_derivative(
        hass, restore_client, entity_registry, new_entity_id="sensor.restored"
    )
    locks = _LockWaits(monkeypatch, "entity", _DERIVATIVE_ENTITY)
    change: dict[str, Any] = (
        {"name": "Changed name"}
        if changed == "name"
        else {"new_entity_id": "sensor.changed"}
    )

    pending = await _after_lock_wait(
        locks,
        "entity",
        _DERIVATIVE_ENTITY,
        handler.restore(restore_client, entry_id, snapshot),
        lambda: entity_registry.async_update_entity(_DERIVATIVE_ENTITY, **change),
    )

    if changed == "entity_id":
        with pytest.raises(BackupRestoreError) as excinfo:
            await asyncio.wait_for(pending, _WAIT_S)
        assert excinfo.value.outcome["reason"] == "entity_identity_mismatch"
        entry = entity_registry.async_get("sensor.changed")
        assert entry is not None
        assert entry.name is None
        assert entity_registry.async_get("sensor.restored") is None
    else:
        await asyncio.wait_for(pending, _WAIT_S)
        entry = entity_registry.async_get("sensor.restored")
        assert entry is not None
        assert entry.name is None


@pytest.mark.parametrize("failure", ["no-answer", "cancelled"])
async def test_failed_restore_frees_both_entity_ids(
    hass: HomeAssistant,
    restore_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A restore that fails inside its critical section leaves neither the
    created nor the saved entity ID locked."""
    target = "sensor.restored"
    handler, entry_id, snapshot = await _deleted_derivative(
        hass, restore_client, entity_registry, new_entity_id=target
    )
    readback = _HeldReadback(
        monkeypatch,
        lambda: registry_update_lock("entity", _DERIVATIVE_ENTITY).locked(),
    )
    restore = asyncio.ensure_future(handler.restore(restore_client, entry_id, snapshot))
    no_answer = patch.object(
        HomeAssistantWebSocketClient,
        "send_command",
        side_effect=HomeAssistantConnectionError("Home Assistant did not answer"),
    )
    try:
        async with asyncio.timeout(_WAIT_S):
            await readback.entered.wait()
    except BaseException:
        readback.release.set()
        await _cancel_and_wait(restore)
        raise
    if failure == "cancelled":
        restore.cancel()
        expected: type[BaseException] = asyncio.CancelledError
    else:
        no_answer.start()
        expected = BackupRestoreError
    readback.release.set()
    try:
        with pytest.raises(expected):
            await asyncio.wait_for(restore, _WAIT_S)
    finally:
        if failure == "no-answer":
            no_answer.stop()

    async with asyncio.timeout(_WAIT_S):
        async with registry_update_locks("entity", _DERIVATIVE_ENTITY, target):
            pass
