"""Unsafe local storage is an explicit refusal across public backup consumers."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp.exceptions import ToolError
from starlette.requests import Request

from ha_mcp import backup_manager as bm
from ha_mcp.settings_ui import _handlers_backups as ui

from .test_backup_diff_error_mapping import _dispatcher

NAME = "automation.example.20260910_000000.yaml"


def unavailable_manager():
    error = bm.UnsafeBackupStorageError("Backup storage permits untrusted writes")
    return SimpleNamespace(
        ensure_directory_ready=AsyncMock(),
        list_snapshots=Mock(side_effect=error),
        list_edits_and_legacy=AsyncMock(side_effect=error),
        read_snapshot=Mock(side_effect=error),
        snapshot_comparison=AsyncMock(side_effect=error),
        diff_snapshot=AsyncMock(side_effect=error),
        delete_snapshot=Mock(side_effect=error),
        delete_bulk=Mock(side_effect=error),
        maybe_snapshot=AsyncMock(side_effect=error),
        handler_for=Mock(return_value=object()),
        restore_snapshot=AsyncMock(
            side_effect=bm.BackupRestoreError(
                str(error),
                reason="unsafe_backup_storage",
                restored_from=NAME,
                safety_backup=None,
            )
        ),
    )


@pytest.mark.parametrize(
    "action", ["list", "view", "diff", "restore", "delete", "bulk"]
)
async def test_settings_storage_refusal_is_json_conflict(monkeypatch, action):
    manager = unavailable_manager()
    monkeypatch.setattr(ui, "_backup_mgr", lambda _: manager)
    handler_name = {
        "list": "list_backups",
        "bulk": "delete_backups_bulk",
    }.get(action, f"{action}_backup")
    response = await ui.build_backups_handlers(None)[handler_name](
        Request(
            {
                "type": "http",
                "path_params": {"name": NAME},
                "query_string": b"domain=automation",
            }
        )
    )

    assert response.status_code == 409
    payload = json.loads(response.body)
    assert payload["success"] is False
    assert payload["error"]["code"] == "CONFIG_VALIDATION_FAILED"
    assert payload["data"]["reason"] == "unsafe_backup_storage"
    if action == "restore":
        assert payload["data"]["apply_status"] == "not_applied"
        assert payload["data"]["safety_backup"] is None


@pytest.mark.parametrize(
    "action", ["create", "list", "view", "diff", "restore", "delete", "bulk"]
)
async def test_mcp_storage_refusal_preserves_reason(monkeypatch, action):
    manager = unavailable_manager()
    monkeypatch.setattr("ha_mcp.tools.backup.get_backup_manager", lambda *args: manager)
    monkeypatch.setattr("ha_mcp.tools.backup.get_global_settings", SimpleNamespace)

    with pytest.raises(ToolError) as caught:
        await _dispatcher()(
            scope="edits",
            action="delete" if action == "bulk" else action,
            domain="automation",
            entity_id="example",
            backup_name=None if action == "bulk" else NAME,
        )

    payload = json.loads(str(caught.value))
    assert payload["success"] is False
    assert payload["error"]["code"] == "CONFIG_VALIDATION_FAILED"
    assert payload["data"]["reason"] == "unsafe_backup_storage"
    if action == "restore":
        assert payload["data"]["apply_status"] == "not_applied"
        assert payload["data"]["safety_backup"] is None


@pytest.mark.parametrize("consumer", ["mcp", "settings"])
async def test_restore_safety_revalidation_refusal_preserves_not_applied(
    tmp_path, monkeypatch, consumer
):
    manager = bm.BackupManager(
        SimpleNamespace(
            auto_backup_dir=str(tmp_path),
            enable_auto_backup=True,
            auto_backup_throttle_minutes=0,
            auto_backup_retain_per_entity=5,
        ),
        SimpleNamespace(),
    )
    fetch = AsyncMock(return_value={"alias": "Current"})
    restore = AsyncMock(return_value={})
    manager.register(bm.DomainHandler("automation", fetch, restore))
    source = manager._write_snapshot(
        "automation", "example", {"alias": "Previous"}, None
    )
    monkeypatch.setattr(
        bm,
        "_require_restore_safety",
        Mock(side_effect=bm.UnsafeBackupStorageError("Backup storage became unsafe")),
    )

    if consumer == "mcp":
        monkeypatch.setattr(
            "ha_mcp.tools.backup.get_backup_manager", lambda *args: manager
        )
        monkeypatch.setattr("ha_mcp.tools.backup.get_global_settings", SimpleNamespace)
        with pytest.raises(ToolError) as caught:
            await _dispatcher()(
                scope="edits", action="restore", backup_name=source.name
            )
        payload = json.loads(str(caught.value))
    else:
        monkeypatch.setattr(ui, "_backup_mgr", lambda _: manager)
        response = await ui.build_backups_handlers(None)["restore_backup"](
            Request({"type": "http", "path_params": {"name": source.name}})
        )
        assert response.status_code == 409
        payload = json.loads(response.body)

    restore.assert_not_awaited()
    fetch.assert_awaited_once()
    assert len(list(tmp_path.glob("*.yaml"))) == 2
    assert not manager._protected_snapshot_names
    assert payload["error"]["code"] == "CONFIG_VALIDATION_FAILED"
    assert payload["data"]["reason"] == "unsafe_backup_storage"
    assert payload["data"]["apply_status"] == "not_applied"
    assert payload["data"]["verification_status"] == "not_run"
    assert payload["data"]["safety_backup"] is None
