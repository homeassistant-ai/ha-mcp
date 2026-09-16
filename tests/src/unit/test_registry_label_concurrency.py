"""Concurrent registry label writes must not overwrite another additive call."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_manager import (
    BackupRestoreError,
    _restore_area_or_floor,
    _restore_template_entity_id,
)
from ha_mcp.tools.tools_areas import AreaTools
from ha_mcp.tools.tools_config_helpers import (
    _apply_create_entity_registry,
    _apply_update_icon_area_labels,
    _entity_registry_update_coro,
    _execute_fallback_registry_update,
)
from ha_mcp.tools.tools_entities import EntityTools
from ha_mcp.tools.tools_labels import LabelTools
from ha_mcp.utils.registry_update_lock import registry_update_lock


class Registry:
    """Model HA's whole-list label writes with a yield after each fresh read."""

    def __init__(self, *, echo_labels: bool = True) -> None:
        self.echo_labels = echo_labels
        self.labels: dict[str, list[str]] = {"target": ["existing"]}

    async def send_websocket_message(self, message: dict[str, Any]) -> dict[str, Any]:
        command = message["type"]
        if command == "config/label_registry/list":
            return {
                "success": True,
                "result": [
                    {"label_id": label} for label in ("existing", "red", "blue")
                ],
            }
        if command == "config/entity_registry/get":
            result: Any = {
                "entity_id": message["entity_id"],
                "labels": list(self.labels["target"]),
            }
            await asyncio.sleep(0)
            return {"success": True, "result": result}
        if command == "config/area_registry/list":
            result = [
                {"area_id": key, "name": key, "labels": list(labels)}
                for key, labels in self.labels.items()
            ]
            await asyncio.sleep(0)
            return {"success": True, "result": result}
        if command in ("config/area_registry/update", "config/entity_registry/update"):
            key = message.get("area_id", "target")
            self.labels[key] = list(message["labels"])
            result = {**message, "labels": list(self.labels[key])}
            if command == "config/area_registry/update" and not self.echo_labels:
                result = {"area_id": key}
            if command == "config/entity_registry/update":
                result = {"entity_entry": result}
            return {"success": True, "result": result}
        raise AssertionError(f"Unexpected WS message: {message}")

    def client(self) -> SimpleNamespace:
        # Different tool/client objects still address the same registry.
        return SimpleNamespace(send_websocket_message=self.send_websocket_message)


async def add_area(
    client: SimpleNamespace, label: str, resource: str = "target"
) -> None:
    await LabelTools(client)._add_label_to_one_area(label, resource, [])


async def update_entity(
    client: SimpleNamespace, labels: list[str], operation: str
) -> dict[str, Any]:
    return await EntityTools(client)._update_single_entity(
        entity_id="light.target",
        area_id=None,
        name=None,
        icon=None,
        enabled=None,
        hidden=None,
        parsed_aliases=None,
        parsed_categories=None,
        parsed_labels=labels,
        label_operation=operation,
        parsed_expose_to=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("echo_labels", [True, False])
async def test_concurrent_area_adds_preserve_both_labels(echo_labels: bool) -> None:
    registry = Registry(echo_labels=echo_labels)
    await asyncio.gather(
        add_area(registry.client(), "red"),
        add_area(registry.client(), "blue"),
    )
    assert set(registry.labels["target"]) == {"existing", "red", "blue"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("labels", "operation", "expected"),
    [
        (["blue"], "add", {"existing", "red", "blue"}),
        (["existing"], "remove", {"red"}),
        (["blue"], "set", {"blue"}),
    ],
)
async def test_concurrent_entity_label_operations(
    labels: list[str], operation: str, expected: set[str]
) -> None:
    registry = Registry()
    await asyncio.gather(
        update_entity(registry.client(), ["red"], "add"),
        update_entity(registry.client(), labels, operation),
    )
    assert set(registry.labels["target"]) == expected


@pytest.mark.asyncio
async def test_different_areas_can_progress_independently() -> None:
    registry = Registry()
    registry.labels["other"] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(message: dict[str, Any]) -> dict[str, Any]:
        if (
            message["type"] == "config/area_registry/update"
            and message["area_id"] == "target"
        ):
            entered.set()
            await release.wait()
        return await registry.send_websocket_message(message)

    first = asyncio.create_task(
        add_area(SimpleNamespace(send_websocket_message=send), "red")
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(add_area(registry.client(), "blue", "other"), 1)
        assert registry.labels["other"] == ["blue"]
    finally:
        release.set()
        await asyncio.wait_for(first, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError, asyncio.CancelledError])
async def test_failed_area_write_releases_resource_for_next_call(
    error: type[BaseException],
) -> None:
    registry = Registry()
    before = deepcopy(registry.labels)

    async def send(message: dict[str, Any]) -> dict[str, Any]:
        if message["type"] == "config/area_registry/update":
            raise error()
        return await registry.send_websocket_message(message)

    with pytest.raises(error):
        await add_area(SimpleNamespace(send_websocket_message=send), "red")
    assert registry.labels == before
    await asyncio.wait_for(add_area(registry.client(), "blue"), 1)
    assert set(registry.labels["target"]) == {"existing", "blue"}


@pytest.mark.asyncio
async def test_bulk_and_single_entity_adds_share_coordination() -> None:
    registry = Registry()
    results = await asyncio.gather(
        EntityTools(registry.client()).ha_set_entity(
            entity_id=["light.target"], labels=["red"], label_operation="add"
        ),
        EntityTools(registry.client()).ha_set_entity(
            entity_id="light.target", labels=["blue"], label_operation="add"
        ),
    )
    assert all(result["success"] for result in results)
    assert set(registry.labels["target"]) == {"existing", "red", "blue"}


@pytest.mark.asyncio
async def test_area_replacement_waits_for_inflight_add(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = Registry()
    entered = asyncio.Event()
    release = asyncio.Event()
    replacement_waiting = asyncio.Event()

    @asynccontextmanager
    async def observed_lock(registry: str, resource_id: str) -> AsyncIterator[None]:
        replacement_waiting.set()
        async with registry_update_lock(registry, resource_id):
            yield

    monkeypatch.setattr("ha_mcp.tools.tools_areas.registry_update_lock", observed_lock)

    async def send(message: dict[str, Any]) -> dict[str, Any]:
        if (
            message["type"] == "config/area_registry/update"
            and "red" in message["labels"]
        ):
            entered.set()
            await release.wait()
        return await registry.send_websocket_message(message)

    client = SimpleNamespace(send_websocket_message=send)
    first = asyncio.create_task(add_area(client, "red"))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(
            AreaTools(client).ha_set_area_or_floor(
                kind="area", id="target", labels=["blue"]
            )
        )
        await asyncio.wait_for(replacement_waiting.wait(), 1)
        assert registry.labels["target"] == ["existing"]
    finally:
        release.set()
        await asyncio.wait_for(first, 1)
        if second is not None:
            result = await asyncio.wait_for(second, 1)
            assert result["success"]
    assert registry.labels["target"] == ["blue"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "replace"),
    [
        (
            "entity",
            lambda c: _entity_registry_update_coro(c, "light.target", None, ["blue"]),
        ),
        (
            "entity",
            lambda c: _apply_create_entity_registry(
                c, "light.target", None, None, ["blue"], {}, []
            ),
        ),
        (
            "entity",
            lambda c: _apply_update_icon_area_labels(
                c, "light.target", None, None, ["blue"], {}, []
            ),
        ),
        (
            "entity",
            lambda c: _execute_fallback_registry_update(
                c, "test", "light.target", None, None, None, ["blue"], None, []
            ),
        ),
        (
            "area",
            lambda c: _restore_area_or_floor(c, "area:target", {"labels": ["blue"]}),
        ),
    ],
    ids=[
        "flow-helper",
        "created-helper",
        "updated-helper",
        "fallback-helper",
        "area-restore",
    ],
)
async def test_other_replacements_wait_for_inflight_add(
    kind: str,
    replace: Callable[[SimpleNamespace], Awaitable[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = Registry()
    entered = asyncio.Event()
    release = asyncio.Event()
    replacing = asyncio.Event()

    async def send(message: dict[str, Any]) -> dict[str, Any]:
        if message["type"].endswith("/update") and "red" in message.get("labels", []):
            entered.set()
            await release.wait()
        return await registry.send_websocket_message(message)

    client = SimpleNamespace(send_websocket_message=send)

    async def backup_send(_client: SimpleNamespace, message: dict[str, Any]) -> Any:
        return (await send(message))["result"]

    monkeypatch.setattr("ha_mcp.backup_manager._ws_send", backup_send)

    async def replacement() -> None:
        replacing.set()
        await replace(client)

    first = asyncio.create_task(
        add_area(client, "red")
        if kind == "area"
        else update_entity(client, ["red"], "add")
    )
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(replacement())
        await asyncio.wait_for(replacing.wait(), 1)
        assert registry.labels["target"] == ["existing"]
    finally:
        release.set()
        await asyncio.wait_for(first, 1)
        if second is not None:
            await asyncio.wait_for(second, 1)
    assert registry.labels["target"] == ["blue"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["floor", "label"])
async def test_area_revalidates_references_after_lock_wait(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting = asyncio.Event()
    entries = [{f"{kind}_id": "reference"}]
    updates: list[dict[str, Any]] = []

    @asynccontextmanager
    async def observed_lock(registry: str, resource_id: str) -> AsyncIterator[None]:
        waiting.set()
        async with registry_update_lock(registry, resource_id):
            yield

    async def send(message: dict[str, Any]) -> dict[str, Any]:
        if message["type"] == f"config/{kind}_registry/list":
            return {"success": True, "result": list(entries)}
        if message["type"] == "config/area_registry/update":
            updates.append(message)
            return {"success": True, "result": {"area_id": "target", **message}}
        raise AssertionError(f"Unexpected WS message: {message}")

    monkeypatch.setattr("ha_mcp.tools.tools_areas.registry_update_lock", observed_lock)
    client = SimpleNamespace(send_websocket_message=send)
    params = {"floor_id": "reference"} if kind == "floor" else {"labels": ["reference"]}
    async with registry_update_lock("area", "target"):
        pending = asyncio.create_task(
            AreaTools(client).ha_set_area_or_floor(kind="area", id="target", **params)
        )
        await asyncio.wait_for(waiting.wait(), 1)
        entries.clear()

    with pytest.raises(ToolError) as excinfo:
        await asyncio.wait_for(pending, 1)
    assert (
        json.loads(str(excinfo.value))["error"]["code"]
        == "VALIDATION_INVALID_PARAMETER"
    )
    assert updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["sensor.created", "sensor.restored"])
async def test_template_restore_verifies_before_concurrent_name_update(
    target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    row: dict[str, Any] = {"entity_id": "sensor.created", "name": None}
    verifying = asyncio.Event()
    release = asyncio.Event()
    updating = asyncio.Event()
    written = False

    async def created(client: Any, entry_id: str) -> dict[str, Any]:
        if written:
            verifying.set()
            await release.wait()
        return dict(row)

    async def collision(client: Any, target: str, **kwargs: Any) -> None:
        return None

    async def restore_send(client: Any, message: dict[str, Any]) -> dict[str, Any]:
        nonlocal written
        row["entity_id"] = message.get("new_entity_id", row["entity_id"])
        row["name"] = message.get("name", row["name"])
        written = True
        return dict(row)

    async def entity_send(message: dict[str, Any]) -> dict[str, Any]:
        assert message["type"] == "config/entity_registry/update"
        assert message["entity_id"] == row["entity_id"]
        row["name"] = message["name"]
        return {"success": True, "result": {"entity_entry": dict(row)}}

    monkeypatch.setattr("ha_mcp.backup_manager._created_template_entity", created)
    monkeypatch.setattr(
        "ha_mcp.backup_manager._check_template_entity_collision", collision
    )
    monkeypatch.setattr("ha_mcp.backup_manager._ws_send", restore_send)
    client = SimpleNamespace(send_websocket_message=entity_send)

    async def rename() -> dict[str, Any]:
        updating.set()
        return await EntityTools(client).ha_set_entity(
            entity_id=target, name="Later name"
        )

    first = asyncio.create_task(
        _restore_template_entity_id(
            client, "entry", {"entity_id": target, "name": "Saved name"}
        )
    )
    second = None
    try:
        await asyncio.wait_for(verifying.wait(), 1)
        second = asyncio.create_task(rename())
        await asyncio.wait_for(updating.wait(), 1)
        blocked = not second.done()
    finally:
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(
                first, *([second] if second is not None else []), return_exceptions=True
            ),
            1,
        )
    assert blocked, "Name update interleaved with restore verification"
    assert results[0] == {
        "created_entity_id": "sensor.created",
        "restored_entity_id": target,
    }
    assert isinstance(results[1], dict) and results[1]["success"]
    assert row["name"] == "Later name"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["name", "entity_id"])
async def test_template_restore_rereads_after_lock_wait(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = "sensor.created", "sensor.restored"
    row: dict[str, Any] = {"entity_id": source, "name": "Saved name"}
    waiting = asyncio.Event()
    writes: list[dict[str, Any]] = []

    @asynccontextmanager
    async def observed_lock(registry: str, resource_id: str) -> AsyncIterator[None]:
        waiting.set()
        async with registry_update_lock(registry, resource_id):
            yield

    async def created(client: Any, entry_id: str) -> dict[str, Any]:
        return dict(row)

    async def collision(client: Any, target: str, **kwargs: Any) -> None:
        return None

    async def send(client: Any, message: dict[str, Any]) -> dict[str, Any]:
        writes.append(message)
        row["entity_id"] = message.get("new_entity_id", row["entity_id"])
        row["name"] = message.get("name", row["name"])
        return dict(row)

    monkeypatch.setattr("ha_mcp.backup_manager.registry_update_lock", observed_lock)
    monkeypatch.setattr("ha_mcp.backup_manager._created_template_entity", created)
    monkeypatch.setattr(
        "ha_mcp.backup_manager._check_template_entity_collision", collision
    )
    monkeypatch.setattr("ha_mcp.backup_manager._ws_send", send)
    async with registry_update_lock("entity", source):
        pending = asyncio.create_task(
            _restore_template_entity_id(
                SimpleNamespace(), "entry", {"entity_id": target, "name": "Saved name"}
            )
        )
        try:
            await asyncio.wait_for(waiting.wait(), 1)
            row[field] = "sensor.changed" if field == "entity_id" else "Changed name"
        except BaseException:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            raise

    if field == "entity_id":
        with pytest.raises(BackupRestoreError) as excinfo:
            await asyncio.wait_for(pending, 1)
        assert excinfo.value.outcome["reason"] == "entity_identity_mismatch"
        assert writes == []
    else:
        await asyncio.wait_for(pending, 1)
        assert row == {"entity_id": target, "name": "Saved name"}
