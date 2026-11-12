"""Bulk snapshot deletion preserves actionable in-use refusals across consumers.

The snapshots are captures of real config entries, taken through the server's
own client on a real ``hass``. A snapshot is in use while a restore of it runs:
the restore waits on its entry's write guard, which the test holds.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from starlette.requests import Request

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_manager import BackupManager, get_backup_manager
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.settings_ui import build_settings_handlers

from ._backup_restore_helpers import backup_tool, use_backup_manager

Deletion = tuple[BackupManager, Path, Path, Path]


@pytest.fixture
async def manager(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> BackupManager:
    assert await async_setup_component(hass, "config", {})
    settings = SimpleNamespace(
        enable_auto_backup=True,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=5,
        auto_backup_dir=str(tmp_path),
    )
    return get_backup_manager(core_client, settings)


async def _capture(hass: HomeAssistant, manager: BackupManager, title: str) -> Path:
    """A snapshot of a new config entry, captured from Core."""
    entry = MockConfigEntry(domain="test", title=title)
    entry.add_to_hass(hass)
    snapshot = await manager.maybe_snapshot("integration", entry.entry_id, force=True)
    assert snapshot is not None
    return snapshot


@asynccontextmanager
async def _in_restore(manager: BackupManager, snapshot: Path) -> AsyncIterator[None]:
    """Keep ``snapshot`` in use: its restore waits on the entry's write guard."""
    entry_id = manager.read_snapshot(snapshot.name)["entity_id"]
    async with manager.config_entry_write_guard(entry_id):
        restore = asyncio.create_task(manager.restore_snapshot(snapshot.name))
        await asyncio.sleep(0)
        try:
            yield
        finally:
            restore.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(restore)


@pytest.fixture
async def deletion(
    hass: HomeAssistant, manager: BackupManager, monkeypatch: pytest.MonkeyPatch
) -> Deletion:
    pinned = await _capture(hass, manager, "pinned")
    unavailable = await _capture(hass, manager, "unavailable")
    removable = await _capture(hass, manager, "removable")
    native_delete = manager.delete_snapshot

    def delete(name: str) -> Path:
        if name == unavailable.name:
            raise OSError("private exception payload must not reach a response")
        return native_delete(name)

    monkeypatch.setattr(manager, "delete_snapshot", delete)
    return manager, pinned, unavailable, removable


async def test_bulk_delete_retains_only_safe_known_failure_reasons(
    deletion: Deletion,
) -> None:
    manager, pinned, unavailable, removable = deletion
    async with _in_restore(manager, pinned):
        result = manager.delete_bulk(domain="integration")
    assert result["deleted"] == [removable.name]
    assert set(result["failed"]) == {pinned.name, unavailable.name}
    assert result["failure_reasons"] == {pinned.name: "snapshot_in_use"}
    assert "private exception payload" not in json.dumps(result)
    assert pinned.exists() and unavailable.exists()


async def test_mcp_bulk_delete_explains_retry_after_capture_or_restore(
    deletion: Deletion, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, pinned, unavailable, removable = deletion
    tool = backup_tool(monkeypatch, manager)
    async with _in_restore(manager, pinned):
        response = await tool(scope="edits", action="delete", domain="integration")
    assert response["data"]["deleted"] == [removable.name]
    assert set(response["data"]["failed"]) == {pinned.name, unavailable.name}
    assert response["data"]["failure_reasons"] == {pinned.name: "snapshot_in_use"}
    warning = " ".join(response["warnings"]).lower()
    assert "in use" in warning and "retry" in warning
    assert "capture" in warning and "restore" in warning
    assert "private exception payload" not in json.dumps(response)


async def test_settings_bulk_delete_preserves_retryable_reason(
    deletion: Deletion, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, pinned, unavailable, removable = deletion
    use_backup_manager(monkeypatch, manager)
    handlers = build_settings_handlers(None)
    async with _in_restore(manager, pinned):
        response = await handlers["delete_backups_bulk"](
            Request({"type": "http", "query_string": b"domain=integration"})
        )
    data = json.loads(response.body)
    assert data["deleted"] == [removable.name]
    assert set(data["failed"]) == {pinned.name, unavailable.name}
    assert data["failure_reasons"] == {pinned.name: "snapshot_in_use"}
    assert "private exception payload" not in response.body.decode()


async def test_bulk_delete_without_known_refusals_preserves_existing_shape(
    hass: HomeAssistant, manager: BackupManager
) -> None:
    removable = await _capture(hass, manager, "removable")
    assert manager.delete_bulk(domain="integration") == {
        "deleted": [removable.name],
        "failed": [],
    }


@pytest.mark.parametrize("consumer", ["manager", "mcp", "settings"])
async def test_bulk_delete_reports_when_every_snapshot_is_in_use(
    hass: HomeAssistant,
    manager: BackupManager,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
) -> None:
    pinned = await _capture(hass, manager, "pinned")
    result: Any
    async with _in_restore(manager, pinned):
        if consumer == "manager":
            result = manager.delete_bulk(domain="integration")
        elif consumer == "mcp":
            tool = backup_tool(monkeypatch, manager)
            response = await tool(scope="edits", action="delete", domain="integration")
            result = response["data"]
            assert "retry" in " ".join(response["warnings"]).lower()
        else:
            use_backup_manager(monkeypatch, manager)
            reply = await build_settings_handlers(None)["delete_backups_bulk"](
                Request({"type": "http", "query_string": b"domain=integration"})
            )
            result = json.loads(reply.body)
        assert result["deleted"] == []
        assert result["failed"] == [pinned.name]
        assert result["failure_reasons"] == {pinned.name: "snapshot_in_use"}
        assert pinned.exists()


@pytest.mark.parametrize("consumer", ["mcp", "settings"])
async def test_single_delete_preserves_retryable_in_use_reason(
    deletion: Deletion, monkeypatch: pytest.MonkeyPatch, consumer: str
) -> None:
    manager, pinned, _, _ = deletion
    async with _in_restore(manager, pinned):
        if consumer == "mcp":
            tool = backup_tool(monkeypatch, manager)
            with pytest.raises(ToolError) as caught:
                await tool(scope="edits", action="delete", backup_name=pinned.name)
            payload = json.loads(str(caught.value))
        else:
            use_backup_manager(monkeypatch, manager)
            response = await build_settings_handlers(None)["delete_backup"](
                Request({"type": "http", "path_params": {"name": pinned.name}})
            )
            assert response.status_code == 409
            payload = json.loads(response.body)
    assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
    assert payload["data"]["reason"] == "snapshot_in_use"
    assert "retry" in payload["error"]["suggestion"].lower()
    assert pinned.exists()


async def test_mcp_bulk_delete_omits_empty_warnings(
    hass: HomeAssistant, manager: BackupManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    removable = await _capture(hass, manager, "removable")
    tool = backup_tool(monkeypatch, manager)
    response = await tool(scope="edits", action="delete", domain="integration")
    assert response["data"]["deleted"] == [removable.name]
    assert "warnings" not in response
