"""Prove alias deletion captures a fresh, restorable Template snapshot."""

from copy import deepcopy
from unittest.mock import AsyncMock

from ha_mcp.tools import auto_backup, tools_config_helpers
from ha_mcp.tools.tools_integrations import IntegrationTools

from .test_template_deleted_recovery import ENTITY, OPTIONS
from .test_template_deleted_recovery import recovery as recovery


async def test_alias_deletion_captures_before_remove_and_restores_exact_snapshot(
    recovery, monkeypatch
):
    manager = recovery.manager
    client = manager._client
    manager._settings.auto_backup_retain_per_entity = 5
    recovery.state.entries.append({"entry_id": "old-entry", "domain": "template"})
    recovery.state.records.append(
        {
            "entry_id": "old-entry",
            "entity_id": ENTITY["entity_id"],
            "kind": "flow",
            "helper_type": "template",
            "options": deepcopy(OPTIONS),
        }
    )
    recovery.state.registry.append(
        {**ENTITY, "config_entry_id": "old-entry", "platform": "template"}
    )
    before = {row["name"] for row in manager.list_snapshots()}

    def fresh_snapshot():
        names = {row["name"] for row in manager.list_snapshots()} - before
        assert len(names) == 1, "Alias deletion must capture its own fresh snapshot"
        (name,) = names
        snapshot = manager.read_snapshot(name)
        assert snapshot["entity_id"] == "old-entry"
        assert snapshot["config"] == recovery.config
        return name, snapshot

    async def send(message):
        if message["type"] == "config/entity_registry/get":
            assert message["entity_id"] == ENTITY["entity_id"]
            return {"success": True, "result": deepcopy(recovery.state.registry[0])}
        assert message["type"] == "config/entity_registry/list"
        return {"success": True, "result": deepcopy(recovery.state.registry)}

    async def delete(entry_id):
        assert entry_id == "old-entry"
        # This check runs before destruction, so a post-delete capture cannot pass.
        fresh_snapshot()
        recovery.state.entries.clear()
        recovery.state.records.clear()
        recovery.state.registry.clear()
        return {}

    client.send_websocket_message = AsyncMock(side_effect=send)
    client.delete_config_entry = AsyncMock(side_effect=delete)
    monkeypatch.setattr(auto_backup, "get_global_settings", lambda: manager._settings)
    monkeypatch.setattr(auto_backup, "get_backup_manager", lambda *_: manager)
    monkeypatch.setattr(
        tools_config_helpers,
        "fetch_entities_for_config_entry_via_component",
        AsyncMock(return_value=None),
    )

    removed = await IntegrationTools(client).ha_remove_helpers_integrations(
        target=ENTITY["entity_id"], helper_type="template", confirm=True, wait=False
    )
    assert removed["success"] is True
    assert removed["entry_id"] == "old-entry"
    assert removed["entity_ids"] == [ENTITY["entity_id"]]
    client.delete_config_entry.assert_awaited_once_with("old-entry")
    name, snapshot = fresh_snapshot()
    assert name != recovery.name

    restored = await manager.restore_snapshot(name)
    replacement = restored["result"]["entry_id"]
    assert replacement == "new-entry"
    assert restored["original_entry_id"] == "old-entry"
    assert restored["restore_mode"] == "recreated"
    assert restored["apply_status"] == "applied"
    assert restored["verification_status"] == "matched"
    assert recovery.state.records[0]["options"] == OPTIONS
    assert recovery.state.registry[0]["entity_id"] == ENTITY["entity_id"]
    assert recovery.state.registry[0]["name"] == ENTITY["name"]
    assert recovery.state.registry[0]["config_entry_id"] == replacement
    assert manager.read_snapshot(name) == snapshot
    recovery.create.assert_awaited_once()
