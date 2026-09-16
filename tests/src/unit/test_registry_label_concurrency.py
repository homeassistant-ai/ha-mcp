"""Concurrent registry label writes must not overwrite another additive call."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ha_mcp.backup_manager import _restore_area_or_floor
from ha_mcp.tools.tools_areas import AreaTools
from ha_mcp.tools.tools_config_helpers import (
    _apply_create_entity_registry,
    _apply_update_icon_area_labels,
    _entity_registry_update_coro,
    _execute_fallback_registry_update,
)
from ha_mcp.tools.tools_entities import EntityTools
from ha_mcp.tools.tools_labels import LabelTools


class Registry:
    """Model HA's whole-list label writes with a yield after each fresh read."""

    def __init__(self, *, echo_labels=True):
        self.echo_labels = echo_labels
        self.labels = {"target": ["existing"]}

    async def send_websocket_message(self, message):
        command = message["type"]
        if command == "config/label_registry/list":
            return {
                "success": True,
                "result": [
                    {"label_id": label} for label in ("existing", "red", "blue")
                ],
            }
        if command == "config/entity_registry/get":
            result = {
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

    def client(self):
        # Different tool/client objects still address the same registry.
        return SimpleNamespace(send_websocket_message=self.send_websocket_message)


async def add_area(client, label, resource="target"):
    await LabelTools(client)._add_label_to_one_area(label, resource, [])


async def update_entity(client, labels, operation):
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
async def test_concurrent_area_adds_preserve_both_labels(echo_labels):
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
async def test_concurrent_entity_label_operations(labels, operation, expected):
    registry = Registry()
    await asyncio.gather(
        update_entity(registry.client(), ["red"], "add"),
        update_entity(registry.client(), labels, operation),
    )
    assert set(registry.labels["target"]) == expected


@pytest.mark.asyncio
async def test_different_areas_can_progress_independently():
    registry = Registry()
    registry.labels["other"] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(message):
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
        await first


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError, asyncio.CancelledError])
async def test_failed_area_write_releases_resource_for_next_call(error):
    registry = Registry()
    before = deepcopy(registry.labels)

    async def send(message):
        if message["type"] == "config/area_registry/update":
            raise error()
        return await registry.send_websocket_message(message)

    with pytest.raises(error):
        await add_area(SimpleNamespace(send_websocket_message=send), "red")
    assert registry.labels == before
    await asyncio.wait_for(add_area(registry.client(), "blue"), 1)
    assert set(registry.labels["target"]) == {"existing", "blue"}


@pytest.mark.asyncio
async def test_bulk_and_single_entity_adds_share_coordination():
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
async def test_area_replacement_waits_for_inflight_add():
    registry = Registry()
    entered = asyncio.Event()
    release = asyncio.Event()
    replacement_validated = asyncio.Event()

    async def send(message):
        if (
            message["type"] == "config/area_registry/update"
            and "red" in message["labels"]
        ):
            entered.set()
            await release.wait()
        if message["type"] == "config/label_registry/list":
            replacement_validated.set()
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
        await asyncio.wait_for(replacement_validated.wait(), 1)
        # Let validation return and the replacement reach the pending write.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert registry.labels["target"] == ["existing"]
    finally:
        release.set()
        await first
        if second is not None:
            result = await second
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
async def test_other_replacements_wait_for_inflight_add(kind, replace):
    registry = Registry()
    entered = asyncio.Event()
    release = asyncio.Event()
    replacing = asyncio.Event()

    async def send(message):
        if message["type"].endswith("/update") and "red" in message.get("labels", []):
            entered.set()
            await release.wait()
        return await registry.send_websocket_message(message)

    client = SimpleNamespace(send_websocket_message=send)

    async def replacement():
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
        await first
        if second is not None:
            await second
    assert registry.labels["target"] == ["blue"]
