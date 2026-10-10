"""Pre-write backups for the zone, person and tag helpers (#2632).

ha_config_set_helper snapshots every helper it edits under
``helper_<helper_type>``. Zone, person and tag are storage collections like the
input_* helpers, so they share the same ``<type>/list`` capture and
``<type>/update`` restore, which writes the whole stored item back. A tag's
name lives in the entity registry, so its snapshot records the registry's own
name and a restore sends one only when the tag had it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp import backup_manager as bm

_ITEMS: dict[str, Any] = {
    "zone": [{"id": "office", "name": "Office", "latitude": 1.0, "longitude": 2.0}],
    "person": {
        "storage": [{"id": "pat", "name": "Pat", "device_trackers": ["a.b"]}],
        "config": [],
    },
    # tag/list fills the name from the registry: name or "Tag <id>".
    "tag": [{"id": "abc-1", "name": "Tag abc-1", "description": "door"}],
}


@pytest.fixture
def registry() -> list[dict[str, Any]]:
    """The tag's registry row; ``name`` None means it was never named."""
    return [
        {
            "entity_id": "tag.tag_abc_1",
            "platform": "tag",
            "unique_id": "abc-1",
            "name": None,
            "original_name": "Tag abc-1",
        }
    ]


@pytest.fixture
def ws(monkeypatch: pytest.MonkeyPatch, registry: list[dict[str, Any]]) -> AsyncMock:
    async def send(client: Any, message: dict[str, Any]) -> Any:
        if message["type"] == "config/entity_registry/list":
            return registry
        if message["type"] == "config/entity_registry/update":
            registry[0]["name"] = message["name"]
            return {}
        helper_type, _, command = message["type"].partition("/")
        return _ITEMS[helper_type] if command == "list" else {}

    mock = AsyncMock(side_effect=send)
    monkeypatch.setattr(bm, "_ws_send", mock)
    return mock


def _stored(helper_type: str) -> dict[str, Any]:
    items = _ITEMS[helper_type]
    return (items["storage"] if isinstance(items, dict) else items)[0]


def _sent(ws: AsyncMock, kind: str) -> list[dict[str, Any]]:
    return [c.args[1] for c in ws.await_args_list if c.args[1]["type"] == kind]


@pytest.mark.parametrize("helper_type", ["zone", "person", "tag"])
def test_ha_config_set_helper_edits_have_a_backup_handler(helper_type: str) -> None:
    mgr = bm.BackupManager.__new__(bm.BackupManager)
    mgr._handlers = {}
    bm.register_default_handlers(mgr, None)
    assert f"helper_{helper_type}" in mgr._handlers


@pytest.mark.asyncio
@pytest.mark.parametrize("helper_type", ["zone", "person"])
async def test_capture_reads_the_stored_item(ws: AsyncMock, helper_type: str) -> None:
    item = _stored(helper_type)
    snapshot = await bm._fetch_helper(None, item["id"], helper_type)
    # A zone's snapshot also records its registry icon (backup_zones.py).
    snapshot.pop("registry_icon", None)
    assert snapshot == item


@pytest.mark.asyncio
@pytest.mark.parametrize("helper_type", ["zone", "person"])
async def test_restore_writes_the_whole_item_back(
    ws: AsyncMock, helper_type: str
) -> None:
    item = _stored(helper_type)
    await bm._restore_helper(None, item["id"], item, helper_type)
    sent = ws.await_args.args[1]
    assert sent["type"] == f"{helper_type}/update"
    assert sent[f"{helper_type}_id"] == item["id"]
    assert {
        k: v for k, v in sent.items() if k not in {"type", f"{helper_type}_id"}
    } == {k: v for k, v in item.items() if k != "id"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("registry_name", "captured"), [(None, None), ("Front", "Front")]
)
async def test_tag_capture_records_the_registry_name_not_the_listed_default(
    ws: AsyncMock,
    registry: list[dict[str, Any]],
    registry_name: str | None,
    captured: str | None,
) -> None:
    registry[0]["name"] = registry_name
    snapshot = await bm._fetch_helper(None, "abc-1", "tag")
    assert snapshot == {"id": "abc-1", "name": captured, "description": "door"}


@pytest.mark.asyncio
async def test_restoring_an_unnamed_tag_pins_no_name(ws: AsyncMock) -> None:
    """tag/update writes any name into the registry; the default must not be."""
    await bm._restore_helper(
        None, "abc-1", {"id": "abc-1", "name": None, "description": "door"}, "tag"
    )
    (update,) = _sent(ws, "tag/update")
    assert update == {"type": "tag/update", "tag_id": "abc-1", "description": "door"}
    assert _sent(ws, "config/entity_registry/update") == []


@pytest.mark.asyncio
async def test_restore_clears_a_name_given_after_an_unnamed_snapshot(
    ws: AsyncMock, registry: list[dict[str, Any]]
) -> None:
    """tag/update cannot clear a name, so the registry entry is cleared."""
    registry[0]["name"] = "Later"
    await bm._restore_helper(
        None, "abc-1", {"id": "abc-1", "name": None, "description": "door"}, "tag"
    )
    (clear,) = _sent(ws, "config/entity_registry/update")
    assert clear == {
        "type": "config/entity_registry/update",
        "entity_id": "tag.tag_abc_1",
        "name": None,
    }
    assert registry[0]["name"] is None


@pytest.mark.asyncio
async def test_restoring_a_named_tag_sends_its_name(ws: AsyncMock) -> None:
    await bm._restore_helper(
        None, "abc-1", {"id": "abc-1", "name": "Front", "description": "door"}, "tag"
    )
    (update,) = _sent(ws, "tag/update")
    assert update["name"] == "Front"
    assert _sent(ws, "config/entity_registry/update") == []
