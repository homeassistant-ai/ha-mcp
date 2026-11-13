"""Directory health and Template recovery keep safe local diagnostics.

A real Template helper is captured, listed and restored through the backup
manager's public calls. Disk failures come from the file system calls the
manager makes; an upstream failure is Core's own error reply, or the client
getting no answer at all.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.errors import ErrorCode
from ha_mcp.tools import helpers

from ._template_backup_core import legacy_snapshot, real_snapshot
from ._template_backup_core import manager as manager
from ._template_backup_core import settings as settings
from ._template_backup_core import template_entry as template_entry
from ._template_helper_core import set_template_state, state_template

_PRIVATE = "private-template-value"


def _in(directory: Path, path: Any) -> bool:
    return Path(os.fsdecode(path)).parent == directory


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
async def test_shared_directory_allows_capture_without_changing_existing_modes(
    manager: bm.BackupManager, template_entry: ConfigEntry
) -> None:
    directory = manager.backup_dir
    directory.mkdir(parents=True)
    directory.chmod(0o777)
    retained = directory / "automation.retained.20260910_000000.yaml"
    retained.write_text("# ha_mcp_backup\nschema_version: 1\n", encoding="utf-8")
    retained.chmod(0o666)

    snapshot = await real_snapshot(manager, template_entry)

    stored = manager.read_snapshot(snapshot.name)["config"]
    assert stored["entry_id"] == template_entry.entry_id
    assert stat.S_IMODE(directory.stat().st_mode) == 0o777
    assert stat.S_IMODE(retained.stat().st_mode) == 0o666
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600


async def test_snapshot_temp_is_exclusively_created_private_before_writing(
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_open = os.open
    creations: list[tuple[Path, int, int]] = []

    def open_file(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        if flags & os.O_CREAT and _in(manager.backup_dir, path):
            creations.append((Path(os.fsdecode(path)), flags, mode))
            assert mode == 0o600
            assert flags & os.O_EXCL
            assert not Path(os.fsdecode(path)).exists()
        return native_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    snapshot = await real_snapshot(manager, template_entry)

    assert len(creations) == 1
    assert creations[0][0] != snapshot.with_suffix(".yaml.tmp")
    stored = manager.read_snapshot(snapshot.name)["config"]
    assert stored["options"]["state"] == state_template("saved")


async def test_snapshot_does_not_reuse_an_existing_predictable_temp(
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file at the name a capture would derive for its temp is left alone."""

    class _FixedClock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> _FixedClock:
            return cls(2026, 9, 10, tzinfo=UTC)

    monkeypatch.setattr(bm, "datetime", _FixedClock)
    first = await real_snapshot(manager, template_entry)
    first.unlink()
    leftover = first.with_suffix(".yaml.tmp")
    leftover.write_text("unrelated unfinished write", encoding="utf-8")

    snapshot = await real_snapshot(manager, template_entry)

    assert snapshot.exists()
    assert leftover.read_text(encoding="utf-8") == "unrelated unfinished write"


class _FailedWrite:
    """A snapshot stream whose write runs out of disk part way."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream

    def __enter__(self) -> _FailedWrite:
        return self

    def write(self, content: str) -> None:
        self.stream.write(content[:20])
        raise OSError("disk full")

    def __exit__(self, *args: Any) -> None:
        self.stream.close()


def _fail_replace(
    monkeypatch: pytest.MonkeyPatch, directory: Path, message: str
) -> None:
    """Make moving a finished temp file into ``directory`` fail."""
    native_replace = os.replace

    def replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if _in(directory, src):
            raise OSError(message)
        native_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


def _fail_stream(
    monkeypatch: pytest.MonkeyPatch, directory: Path, failure: str
) -> None:
    """Make opening (``fdopen``) or writing (``write``) a snapshot file fail."""
    native_open, native_fdopen = os.open, os.fdopen
    snapshot_fds: set[int] = set()

    def open_file(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        fd = native_open(path, flags, mode, **kwargs)
        if _in(directory, path):
            snapshot_fds.add(fd)
        return fd

    def fdopen(fd: int, *args: Any, **kwargs: Any) -> Any:
        if fd not in snapshot_fds:
            return native_fdopen(fd, *args, **kwargs)
        if failure == "fdopen":
            raise OSError("fdopen failed")
        return _FailedWrite(native_fdopen(fd, *args, **kwargs))

    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(os, "fdopen", fdopen)


@pytest.mark.parametrize("failure", ["fdopen", "write", "replace"])
async def test_failed_snapshot_removes_only_its_temporary_file(
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    directory = manager.backup_dir
    directory.mkdir(parents=True)
    retained = directory / "retained.yaml"
    retained.write_text("retained", encoding="utf-8")
    if failure == "replace":
        _fail_replace(monkeypatch, directory, "replace failed")
    else:
        _fail_stream(monkeypatch, directory, failure)

    with pytest.raises(bm.MandatoryBackupError):
        await manager.maybe_snapshot(
            "helper_template", template_entry.entry_id, force=True, mandatory=True
        )

    assert list(directory.iterdir()) == [retained]
    assert retained.read_text(encoding="utf-8") == "retained"


async def test_listing_checks_directory_off_loop_before_first_capture(
    manager: bm.BackupManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = manager.backup_dir
    loop_thread = threading.get_ident()
    probe_threads: list[int] = []
    native_mkdir = Path.mkdir

    def mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == directory:
            probe_threads.append(threading.get_ident())
        native_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    assert probe_threads == []
    await manager.list_edits_and_legacy(domain="helper_template")
    assert probe_threads and all(t != loop_thread for t in probe_threads)
    assert manager.enabled and manager.init_dir_error is None
    await manager.list_edits_and_legacy(domain="helper_template")
    assert len(probe_threads) == 1


async def test_disabled_health_check_and_capture_do_not_resolve_storage(
    component_core_client: HomeAssistantClient,
    settings: config.Settings,
    template_entry: ConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With auto-backup off and no directory configured, neither the status
    check nor a capture looks for a default directory."""
    monkeypatch.setattr(settings, "enable_auto_backup", False)
    monkeypatch.setattr(settings, "auto_backup_dir", "")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)

    def unexpected_home() -> Path:
        pytest.fail("Disabled status/capture must not resolve or probe storage")

    monkeypatch.setattr(Path, "home", unexpected_home)
    disabled = bm.get_backup_manager(component_core_client, settings)

    await disabled.ensure_directory_ready()
    assert (
        await disabled.maybe_snapshot("helper_template", template_entry.entry_id)
        is None
    )
    assert disabled.enabled is False
    assert disabled.init_dir_error is None


async def test_directory_recovery_retries_failed_initialization(
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = manager.backup_dir
    native_mkdir = Path.mkdir
    attempts: list[Path] = []
    available = False

    def mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == directory:
            attempts.append(path)
            if not available:
                raise PermissionError("Backup directory temporarily unavailable")
        native_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    for _ in range(2):
        await manager.ensure_directory_ready()
        assert manager.enabled is False
        assert "temporarily unavailable" in (manager.init_dir_error or "")
    assert len(attempts) == 2

    available = True
    await manager.ensure_directory_ready()
    assert manager.enabled is True
    assert manager.init_dir_error is None
    assert len(attempts) == 3
    await manager.ensure_directory_ready()
    assert len(attempts) == 3
    captured = await manager.maybe_snapshot("helper_template", template_entry.entry_id)
    assert captured is not None and captured.exists()


@pytest.mark.parametrize("failure", ["yaml", "identity", "unreadable"])
async def test_template_identity_failure_logs_filename_and_safe_type(
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    """A snapshot whose identity cannot be read is named in the log with only
    the error's type, never a value from the file."""
    path = await real_snapshot(manager, template_entry)
    if failure == "yaml":
        path.write_text(f"config: [{_PRIVATE}\n", encoding="utf-8")
        expected_type = "ValueError"
    elif failure == "identity":
        legacy_snapshot(manager, path, template_entry.entry_id, entry_id="other-entry")
        expected_type = "HomeAssistantError"
    else:
        native_stat = Path.stat

        def unreadable(target: Path, *args: Any, **kwargs: Any) -> os.stat_result:
            if target == path:
                raise PermissionError(_PRIVATE)
            return native_stat(target, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", unreadable)
        expected_type = "PermissionError"

    with caplog.at_level(logging.WARNING):
        rows = manager.list_snapshots(
            domain="helper_template", entity_id=template_entry.entry_id
        )

    assert path.name not in {row["name"] for row in rows}
    assert path.name in caplog.text
    assert expected_type in caplog.text
    assert _PRIVATE not in caplog.text


@pytest.mark.parametrize("failure", ["directory", "disk_full", "upstream"])
async def test_template_safety_failure_keeps_reason_and_safe_diagnostic(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    settings: config.Settings,
    template_entry: ConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    """A restore whose safety capture fails is refused unapplied, with the
    storage reason kept and nothing private in the log or the error."""
    source = await real_snapshot(
        bm.get_backup_manager(component_core_client, settings), template_entry
    )
    await set_template_state(hass, template_entry, "current")
    # A fresh manager checks its directory again on its first capture.
    restoring = bm.BackupManager(settings, component_core_client)
    bm.register_default_handlers(restoring, component_core_client)
    expected_detail: str | None
    if failure == "upstream":
        native_connect = helpers.get_connected_ws_client
        connects = 0

        async def unreachable_after_preflight(*args: Any, **kwargs: Any) -> Any:
            # The restore's two preflight reads are answered; the safety
            # capture's own read then finds Home Assistant unreachable.
            nonlocal connects
            connects += 1
            if connects > 2:
                raise HomeAssistantConnectionError(_PRIVATE)
            return await native_connect(*args, **kwargs)

        monkeypatch.setattr(
            helpers, "get_connected_ws_client", unreachable_after_preflight
        )
        expected_detail = None
    elif failure == "directory":
        expected_detail = "read-only filesystem"
        native_mkdir = Path.mkdir

        def mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
            if path == restoring.backup_dir:
                raise OSError(expected_detail)
            native_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", mkdir)
    else:
        expected_detail = "No space left on device"
        _fail_replace(monkeypatch, restoring.backup_dir, expected_detail)

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(bm.BackupRestoreError) as caught,
    ):
        await restoring.restore_snapshot(source.name)

    assert caught.value.outcome["reason"] == "backup_capture_failed"
    assert caught.value.outcome["apply_status"] == "not_applied"
    assert caught.value.outcome["safety_backup"] is None
    assert "MandatoryBackupError" in caplog.text
    if expected_detail:
        assert expected_detail in caplog.text
        assert expected_detail in str(caught.value)
    assert _PRIVATE not in caplog.text
    assert _PRIVATE not in str(caught.value)
    assert template_entry.options["state"] == state_template("current")


async def _restore_without_answer(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Restore a snapshot whose options flow gets no answer; nothing private
    reaches the error or the log."""
    source = await real_snapshot(manager, template_entry)
    await set_template_state(hass, template_entry, "current")
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(bm.BackupRestoreError) as caught,
    ):
        await manager.restore_snapshot(source.name)
    assert bm.restore_error_code(caught.value) == ErrorCode.SERVICE_CALL_FAILED
    assert caught.value.outcome["apply_status"] == "not_applied"
    assert _PRIVATE not in str(caught.value)
    assert _PRIVATE not in caplog.text


async def test_restore_os_error_does_not_expose_private_payload(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        component_core_client,
        "start_options_flow",
        AsyncMock(side_effect=OSError(_PRIVATE)),
    )
    await _restore_without_answer(hass, manager, template_entry, caplog)


async def test_restore_timeout_does_not_expose_private_payload(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        component_core_client,
        "start_options_flow",
        AsyncMock(side_effect=TimeoutError(_PRIVATE)),
    )
    await _restore_without_answer(hass, manager, template_entry, caplog)


async def test_restore_dropped_connection_does_not_expose_private_payload(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        component_core_client,
        "start_options_flow",
        AsyncMock(side_effect=ConnectionError(_PRIVATE)),
    )
    await _restore_without_answer(hass, manager, template_entry, caplog)
