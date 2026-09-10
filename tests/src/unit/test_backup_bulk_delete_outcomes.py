"""Bulk snapshot deletion preserves actionable in-use refusals across consumers."""

import json

import pytest
from starlette.requests import Request

from ha_mcp.settings_ui import _handlers_backups as ui
from ha_mcp.tools.backup import _edits_delete

from .test_backup_manager import _mk_manager


@pytest.fixture
def deletion(tmp_path, monkeypatch):
    manager = _mk_manager(tmp_path)
    pinned = manager._write_snapshot("automation", "pinned", {}, "test")
    unavailable = manager._write_snapshot("automation", "unavailable", {}, "test")
    removable = manager._write_snapshot("automation", "removable", {}, "test")
    native_delete = manager.delete_snapshot

    def delete(name):
        if name == unavailable.name:
            raise OSError("private exception payload must not reach a response")
        return native_delete(name)

    monkeypatch.setattr(manager, "delete_snapshot", delete)
    manager._protect_snapshot(pinned.name)
    try:
        yield manager, pinned, unavailable, removable
    finally:
        manager._unprotect_snapshot(pinned.name)


def test_bulk_delete_retains_only_safe_known_failure_reasons(deletion):
    manager, pinned, unavailable, removable = deletion
    result = manager.delete_bulk(domain="automation")
    assert result["deleted"] == [removable.name]
    assert set(result["failed"]) == {pinned.name, unavailable.name}
    assert result["failure_reasons"] == {pinned.name: "snapshot_in_use"}
    assert "private exception payload" not in json.dumps(result)
    assert pinned.exists() and unavailable.exists()


async def test_mcp_bulk_delete_explains_retry_after_capture_or_restore(deletion):
    manager, pinned, unavailable, removable = deletion
    response = await _edits_delete(manager, "automation", None, None, None)
    assert response["data"]["deleted"] == [removable.name]
    assert set(response["data"]["failed"]) == {pinned.name, unavailable.name}
    assert response["data"]["failure_reasons"] == {pinned.name: "snapshot_in_use"}
    warning = " ".join(response["warnings"]).lower()
    assert "in use" in warning and "retry" in warning
    assert "capture" in warning and "restore" in warning
    assert "private exception payload" not in json.dumps(response)


async def test_settings_bulk_delete_preserves_retryable_reason(deletion, monkeypatch):
    manager, pinned, unavailable, removable = deletion
    monkeypatch.setattr(ui, "_backup_mgr", lambda server: manager)
    response = await ui._delete_backups_bulk(
        None, Request({"type": "http", "query_string": b"domain=automation"})
    )
    data = json.loads(response.body)
    assert data["deleted"] == [removable.name]
    assert set(data["failed"]) == {pinned.name, unavailable.name}
    assert data["failure_reasons"] == {pinned.name: "snapshot_in_use"}
    assert "private exception payload" not in response.body.decode()


def test_bulk_delete_without_known_refusals_preserves_existing_shape(tmp_path):
    manager = _mk_manager(tmp_path)
    removable = manager._write_snapshot("automation", "removable", {}, "test")
    assert manager.delete_bulk(domain="automation") == {
        "deleted": [removable.name],
        "failed": [],
    }


@pytest.mark.parametrize("consumer", ["manager", "mcp", "settings"])
async def test_bulk_delete_reports_when_every_snapshot_is_in_use(
    tmp_path, monkeypatch, consumer
):
    manager = _mk_manager(tmp_path)
    pinned = manager._write_snapshot("automation", "pinned", {}, "test")
    manager._protect_snapshot(pinned.name)
    try:
        if consumer == "manager":
            result = manager.delete_bulk(domain="automation")
        elif consumer == "mcp":
            response = await _edits_delete(manager, "automation", None, None, None)
            result = response["data"]
            assert "retry" in " ".join(response["warnings"]).lower()
        else:
            monkeypatch.setattr(ui, "_backup_mgr", lambda server: manager)
            response = await ui._delete_backups_bulk(
                None, Request({"type": "http", "query_string": b"domain=automation"})
            )
            result = json.loads(response.body)
        assert result["deleted"] == []
        assert result["failed"] == [pinned.name]
        assert result["failure_reasons"] == {pinned.name: "snapshot_in_use"}
        assert pinned.exists()
    finally:
        manager._unprotect_snapshot(pinned.name)
