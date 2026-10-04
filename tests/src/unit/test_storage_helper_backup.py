"""Pre-write backups for the zone, person and tag helpers (#2632).

ha_config_set_helper snapshots every helper it edits under
``helper_<helper_type>``. Zone, person and tag are storage collections like the
input_* helpers, so they share the same ``<type>/list`` capture and
``<type>/update`` restore, which writes the whole stored item back.
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
    "tag": [{"id": "abc-1", "name": "Front", "description": "door"}],
}


@pytest.fixture
def ws(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    async def send(client: Any, message: dict[str, Any]) -> Any:
        helper_type, _, command = message["type"].partition("/")
        return _ITEMS[helper_type] if command == "list" else {}

    mock = AsyncMock(side_effect=send)
    monkeypatch.setattr(bm, "_ws_send", mock)
    return mock


def _stored(helper_type: str) -> dict[str, Any]:
    items = _ITEMS[helper_type]
    return (items["storage"] if isinstance(items, dict) else items)[0]


@pytest.mark.parametrize("helper_type", ["zone", "person", "tag"])
def test_ha_config_set_helper_edits_have_a_backup_handler(helper_type: str) -> None:
    mgr = bm.BackupManager.__new__(bm.BackupManager)
    mgr._handlers = {}
    bm.register_default_handlers(mgr, None)
    assert f"helper_{helper_type}" in mgr._handlers


@pytest.mark.asyncio
@pytest.mark.parametrize("helper_type", ["zone", "person", "tag"])
async def test_capture_reads_the_stored_item(ws: AsyncMock, helper_type: str) -> None:
    item = _stored(helper_type)
    assert await bm._fetch_helper(None, item["id"], helper_type) == item


@pytest.mark.asyncio
@pytest.mark.parametrize("helper_type", ["zone", "person", "tag"])
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
