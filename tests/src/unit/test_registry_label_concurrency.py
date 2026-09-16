"""Concurrent registry label writes must not overwrite another additive call."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ha_mcp.tools.tools_entities import EntityTools
from ha_mcp.tools.tools_labels import LabelTools


class Registry:
    """Model HA's whole-list label writes with a yield after each fresh read."""

    def __init__(self):
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
async def test_concurrent_area_adds_preserve_both_labels():
    registry = Registry()
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
