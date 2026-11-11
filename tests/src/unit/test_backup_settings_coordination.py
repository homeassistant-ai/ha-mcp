"""Settings reloads must preserve in-flight recovery guards and snapshot pins."""

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm
from ha_mcp.backup_manager import DomainHandler, get_backup_manager
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import Settings


def _settings(directory: Path | str = "", **values: Any) -> Settings:
    """The server's settings with these values; no dotenv file is read."""
    fields: dict[str, Any] = {
        "_env_file": None,
        "ENABLE_AUTO_BACKUP": True,
        "HAMCP_BACKUP_DIR": str(directory),
        "AUTO_BACKUP_THROTTLE_MINUTES": 0,
        **values,
    }
    return Settings(**fields)


async def test_uninitialized_client_cache_is_not_reused(
    core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    manager = get_backup_manager(core_client, _settings(tmp_path))
    async with manager.config_entry_write_guard("entry"):
        assert isinstance(manager, bm.BackupManager)


@pytest.mark.parametrize("change_directory", [False, True])
async def test_settings_reload_preserves_restore_admission(
    core_client: HomeAssistantClient, tmp_path: Path, change_directory: bool
) -> None:
    settings = _settings(tmp_path / "original")
    manager = get_backup_manager(core_client, settings)
    attempted = asyncio.Event()
    entered = asyncio.Event()

    async def write(refreshed: bm.BackupManager) -> None:
        attempted.set()
        async with refreshed.config_entry_write_guard("replacement-entry"):
            entered.set()

    async with asyncio.timeout(2):
        async with manager.config_entry_write_guard("deleted-entry", exclusive=True):
            changes: dict[str, Any] = {"enable_auto_backup": False}
            if change_directory:
                changes["auto_backup_dir"] = str(tmp_path / "changed")
            refreshed = get_backup_manager(
                core_client, settings.model_copy(update=changes)
            )
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


async def test_settings_reload_keeps_source_pinned_during_rotation(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    label_registry: lr.LabelRegistry,
    tmp_path: Path,
) -> None:
    # The config integration answers the label registry commands.
    assert await async_setup_component(hass, "config", {})
    label = label_registry.async_create("Original")
    settings = _settings(tmp_path, AUTO_BACKUP_RETAIN_PER_ENTITY=1)
    manager = get_backup_manager(core_client, settings)
    source = await manager.maybe_snapshot("label", label.label_id)
    assert source is not None
    label_handler = manager.handler_for("label")
    assert label_handler is not None
    during_restore: dict[str, Any] = {}

    async def restore_after_reload(client: Any, entity_id: str, config: Any) -> Any:
        # The restore holds its pin on ``source`` while this runs.
        refreshed = get_backup_manager(client, settings.model_copy())
        label_registry.async_update(label.label_id, name="Current")
        during_restore["later"] = await refreshed.maybe_snapshot(
            "label", label.label_id, force=True
        )
        during_restore["source_kept"] = await asyncio.to_thread(source.exists)
        return await label_handler.restore(client, entity_id, config)

    manager.register(DomainHandler("label", label_handler.fetch, restore_after_reload))
    await manager.restore_snapshot(source.name, take_safety_backup=False)

    later = during_restore["later"]
    assert later is not None and later != source
    assert during_restore["source_kept"], "Settings reload dropped the restore pin"


async def test_disabled_write_guard_does_not_create_backup_directory(
    core_client: HomeAssistantClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "never-created"
    mkdir_calls = []
    original_mkdir = Path.mkdir

    def mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        mkdir_calls.append(path)
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    manager = get_backup_manager(
        core_client, _settings(directory, ENABLE_AUTO_BACKUP=False)
    )
    async with manager.config_entry_write_guard("entry"):
        pass
    assert directory not in mkdir_calls


async def test_forced_capture_creates_directory_off_event_loop(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    label_registry: lr.LabelRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await async_setup_component(hass, "config", {})
    label = label_registry.async_create("Saved")
    directory = tmp_path / "first-capture"
    loop_thread = threading.get_ident()
    mkdir_threads = []
    original_mkdir = Path.mkdir

    def mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == directory:
            mkdir_threads.append(threading.get_ident())
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    manager = get_backup_manager(
        core_client, _settings(directory, ENABLE_AUTO_BACKUP=False)
    )
    snapshot = await manager.maybe_snapshot("label", label.label_id, force=True)
    assert snapshot is not None
    assert mkdir_threads and all(thread != loop_thread for thread in mkdir_threads)


async def test_disabled_guard_does_not_resolve_default_storage(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_resolution() -> Path:
        pytest.fail("Disabled coordination must not probe default storage")

    monkeypatch.setattr(bm, "_resolve_default_dir", unexpected_resolution)
    settings = _settings(ENABLE_AUTO_BACKUP=False)
    manager = get_backup_manager(core_client, settings)
    async with manager.config_entry_write_guard("entry"):
        refreshed = get_backup_manager(core_client, settings.model_copy())
        async with refreshed.config_entry_write_guard("entry"):
            pass
