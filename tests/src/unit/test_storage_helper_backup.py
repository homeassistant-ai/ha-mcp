"""Pre-write backups for the zone, person and tag helpers (#2632).

ha_config_set_helper snapshots every helper it edits under
``helper_<helper_type>``. Zone, person and tag are storage collections like the
input_* helpers, so they share the same ``<type>/list`` capture and
``<type>/update`` restore, which writes the whole stored item back. A tag's
name lives in the entity registry, so its snapshot records the registry's own
name and a restore sends one only when the tag had it.

The handlers run on the server's client against a real ``hass``: what is
captured and what a restore leaves behind are Core's own stored items and
registry entries.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import Mock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.config import get_global_settings


@pytest.fixture
async def manager(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> AsyncIterator[bm.BackupManager]:
    # ``config`` serves the registry commands the handlers read and write.
    for domain in ("config", "zone", "person", "tag"):
        assert await async_setup_component(hass, domain, {})
    mgr = bm.BackupManager(get_global_settings(), core_client)
    bm.register_default_handlers(mgr, core_client)
    yield mgr
    await hass.async_block_till_done()


@contextmanager
def _sent_commands() -> Iterator[Mock]:
    """Spy on every command the backup's one-shot WebSocket clients send."""
    with patch.object(
        HomeAssistantWebSocketClient,
        "send_command",
        autospec=True,
        wraps=HomeAssistantWebSocketClient.send_command,
    ) as spy:
        yield spy


def _sent(spy: Mock, kind: str) -> list[dict[str, Any]]:
    return [dict(c.kwargs) for c in spy.call_args_list if c.args[1] == kind]


def _registry_names_sent(spy: Mock) -> list[dict[str, Any]]:
    """Registry updates that write a name (a restore also re-applies the
    recorded registry icon on its own)."""
    return [u for u in _sent(spy, "config/entity_registry/update") if "name" in u]


async def _send(client: HomeAssistantClient, message: dict[str, Any]) -> Any:
    reply = await client.send_websocket_message(message)
    assert reply["success"], reply
    return reply["result"]


async def _stored(client: HomeAssistantClient, helper_type: str, item_id: str) -> Any:
    listed = await _send(client, {"type": f"{helper_type}/list"})
    items = listed["storage"] if helper_type == "person" else listed
    return next(i for i in items if i["id"] == item_id)


async def _create(client: HomeAssistantClient, helper_type: str) -> str:
    """A stored zone or person; the name is fresh so no old registry entry
    is restored for it."""
    name = f"Office {uuid.uuid4().hex[:8]}"
    body: dict[str, Any] = (
        {"name": name, "latitude": 1.0, "longitude": 2.0}
        if helper_type == "zone"
        else {"name": name, "device_trackers": ["device_tracker.pat_phone"]}
    )
    created = await _send(client, {"type": f"{helper_type}/create", **body})
    return str(created["id"])


async def _create_tag(client: HomeAssistantClient) -> tuple[str, str]:
    """An unnamed tag, and its entity's id from the registry."""
    tag_id = f"abc-{uuid.uuid4().hex[:8]}"
    await _send(client, {"type": "tag/create", "tag_id": tag_id, "description": "door"})
    rows = await _send(client, {"type": "config/entity_registry/list"})
    (entity_id,) = [
        r["entity_id"]
        for r in rows
        if r["platform"] == "tag" and r["unique_id"] == tag_id
    ]
    return tag_id, entity_id


def _registry_name(entity_registry: er.EntityRegistry, entity_id: str) -> str | None:
    entry = entity_registry.async_get(entity_id)
    assert entry is not None
    return entry.name


@pytest.mark.parametrize("helper_type", ["zone", "person", "tag"])
def test_ha_config_set_helper_edits_have_a_backup_handler(
    core_client: HomeAssistantClient, helper_type: str
) -> None:
    mgr = bm.BackupManager(get_global_settings(), core_client)
    bm.register_default_handlers(mgr, core_client)
    assert f"helper_{helper_type}" in mgr.supported_domains()


@pytest.mark.parametrize("helper_type", ["zone", "person"])
async def test_capture_reads_the_stored_item(
    manager: bm.BackupManager, core_client: HomeAssistantClient, helper_type: str
) -> None:
    item_id = await _create(core_client, helper_type)
    handler = manager.handler_for(f"helper_{helper_type}")
    assert handler is not None

    snapshot = await handler.fetch(core_client, item_id)

    # A zone's snapshot also records its registry icon (backup_zones.py).
    snapshot.pop("registry_icon", None)
    assert snapshot == await _stored(core_client, helper_type, item_id)


@pytest.mark.parametrize("helper_type", ["zone", "person"])
async def test_restore_writes_the_whole_item_back(
    manager: bm.BackupManager, core_client: HomeAssistantClient, helper_type: str
) -> None:
    """Every field changed after the snapshot is put back, not only the name."""
    item_id = await _create(core_client, helper_type)
    handler = manager.handler_for(f"helper_{helper_type}")
    assert handler is not None
    before = await _stored(core_client, helper_type, item_id)
    snapshot = await handler.fetch(core_client, item_id)
    changed = (
        {"name": "Elsewhere", "latitude": 5.0, "longitude": 6.0, "radius": 500}
        if helper_type == "zone"
        else {"name": "Someone", "device_trackers": []}
    )
    await _send(
        core_client,
        {"type": f"{helper_type}/update", f"{helper_type}_id": item_id, **changed},
    )

    await handler.restore(core_client, item_id, snapshot)

    assert await _stored(core_client, helper_type, item_id) == before


@pytest.mark.parametrize("registry_name", [None, "Front"])
async def test_tag_capture_records_the_registry_name_not_the_listed_default(
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    registry_name: str | None,
) -> None:
    """tag/list fills a never-named tag's name with "Tag <id>"."""
    tag_id, entity_id = await _create_tag(core_client)
    entity_registry.async_update_entity(entity_id, name=registry_name)
    handler = manager.handler_for("helper_tag")
    assert handler is not None

    snapshot = await handler.fetch(core_client, tag_id)

    assert (snapshot["id"], snapshot["description"]) == (tag_id, "door")
    assert snapshot["name"] == registry_name
    assert snapshot["registry_icon"] is None


async def test_restoring_an_unnamed_tag_pins_no_name(
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """tag/update writes any name into the registry; the default must not be."""
    tag_id, entity_id = await _create_tag(core_client)
    handler = manager.handler_for("helper_tag")
    assert handler is not None
    snapshot = await handler.fetch(core_client, tag_id)
    await _send(
        core_client, {"type": "tag/update", "tag_id": tag_id, "description": "gate"}
    )

    with _sent_commands() as spy:
        await handler.restore(core_client, tag_id, snapshot)

    (update,) = _sent(spy, "tag/update")
    assert "name" not in update
    assert _registry_names_sent(spy) == []
    assert _registry_name(entity_registry, entity_id) is None
    assert (await _stored(core_client, "tag", tag_id))["description"] == "door"


async def test_restore_clears_a_name_given_after_an_unnamed_snapshot(
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """tag/update cannot clear a name, so the registry entry is cleared."""
    tag_id, entity_id = await _create_tag(core_client)
    handler = manager.handler_for("helper_tag")
    assert handler is not None
    snapshot = await handler.fetch(core_client, tag_id)
    entity_registry.async_update_entity(entity_id, name="Later")

    with _sent_commands() as spy:
        await handler.restore(core_client, tag_id, snapshot)

    (clear,) = _registry_names_sent(spy)
    assert clear == {"entity_id": entity_id, "name": None}
    assert _registry_name(entity_registry, entity_id) is None


async def test_restoring_a_named_tag_sends_its_name(
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    tag_id, entity_id = await _create_tag(core_client)
    entity_registry.async_update_entity(entity_id, name="Front")
    handler = manager.handler_for("helper_tag")
    assert handler is not None
    snapshot = await handler.fetch(core_client, tag_id)
    entity_registry.async_update_entity(entity_id, name="Other")

    with _sent_commands() as spy:
        await handler.restore(core_client, tag_id, snapshot)

    (update,) = _sent(spy, "tag/update")
    assert update["name"] == "Front"
    assert _registry_names_sent(spy) == []
    assert _registry_name(entity_registry, entity_id) == "Front"
