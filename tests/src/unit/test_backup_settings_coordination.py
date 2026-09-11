"""Settings reloads must preserve in-flight recovery guards and snapshot pins."""

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ha_mcp import backup_manager as bm
from ha_mcp.backup_manager import get_backup_manager

from .test_backup_manager import _mk_handler, _StubClient, _StubSettings


async def test_uninitialized_client_cache_is_not_reused(tmp_path):
    client = MagicMock()
    manager = get_backup_manager(client, _StubSettings(auto_backup_dir=str(tmp_path)))
    async with manager.config_entry_write_guard("entry"):
        assert isinstance(manager, bm.BackupManager)


@pytest.mark.parametrize("change_directory", [False, True])
async def test_settings_reload_preserves_restore_admission(tmp_path, change_directory):
    client = _StubClient()
    settings = _StubSettings(auto_backup_dir=str(tmp_path / "original"))
    manager = get_backup_manager(client, settings)
    attempted = asyncio.Event()
    entered = asyncio.Event()

    async def write(refreshed):
        attempted.set()
        async with refreshed.config_entry_write_guard("replacement-entry"):
            entered.set()

    async with asyncio.timeout(2):
        async with manager.config_entry_write_guard("deleted-entry", exclusive=True):
            updated = replace(settings, enable_auto_backup=False)
            if change_directory:
                updated.auto_backup_dir = str(tmp_path / "changed")
            refreshed = get_backup_manager(client, updated)
            assert not refreshed.enabled
            writer = asyncio.create_task(write(refreshed))
            try:
                await attempted.wait()
                assert not entered.is_set(), (
                    "Settings reload bypassed the active restore"
                )
            finally:
                writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)
        await write(refreshed)
        assert entered.is_set()


async def test_settings_reload_keeps_source_pinned_during_rotation(tmp_path):
    client = _StubClient()
    settings = _StubSettings(
        auto_backup_dir=str(tmp_path), auto_backup_retain_per_entity=1
    )
    manager = get_backup_manager(client, settings)
    manager.register(_mk_handler(fetched={"value": "original"}))
    source = await manager.maybe_snapshot("automation", "test")
    assert source is not None
    manager._protect_snapshot(source.name)
    try:
        refreshed = get_backup_manager(client, replace(settings))
        refreshed.register(_mk_handler(fetched={"value": "current"}))
        later = await refreshed.maybe_snapshot("automation", "test", force=True)
        assert later is not None and later != source
        assert await asyncio.to_thread(source.exists), (
            "Settings reload dropped the restore pin"
        )
    finally:
        manager._unprotect_snapshot(source.name)


async def test_disabled_write_guard_does_not_create_backup_directory(
    tmp_path, monkeypatch
):
    directory = tmp_path / "never-created"
    mkdir_calls = []
    original_mkdir = Path.mkdir

    def mkdir(path, *args, **kwargs):
        mkdir_calls.append(path)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    manager = get_backup_manager(
        _StubClient(),
        _StubSettings(enable_auto_backup=False, auto_backup_dir=str(directory)),
    )
    async with manager.config_entry_write_guard("entry"):
        pass
    assert directory not in mkdir_calls


async def test_forced_capture_creates_directory_off_event_loop(tmp_path, monkeypatch):
    import threading

    directory = tmp_path / "first-capture"
    loop_thread = threading.get_ident()
    mkdir_threads = []
    original_mkdir = Path.mkdir

    def mkdir(path, *args, **kwargs):
        if path == directory:
            mkdir_threads.append(threading.get_ident())
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    manager = get_backup_manager(
        _StubClient(),
        _StubSettings(enable_auto_backup=False, auto_backup_dir=str(directory)),
    )
    manager.register(_mk_handler(fetched={"value": "saved"}))
    snapshot = await manager.maybe_snapshot("automation", "test", force=True)
    assert snapshot is not None
    assert mkdir_threads and all(thread != loop_thread for thread in mkdir_threads)


async def test_disabled_guard_does_not_resolve_default_storage(monkeypatch):
    def unexpected_resolution():
        pytest.fail("Disabled coordination must not probe default storage")

    monkeypatch.setattr(bm, "_resolve_default_dir", unexpected_resolution)
    client = _StubClient()
    settings = _StubSettings(enable_auto_backup=False)
    manager = get_backup_manager(client, settings)
    async with manager.config_entry_write_guard("entry"):
        refreshed = get_backup_manager(client, replace(settings))
        async with refreshed.config_entry_write_guard("entry"):
            pass
