"""Directory health and Template recovery retain safe local diagnostics."""

import logging
import os
import stat
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ha_mcp import backup_manager as bm

from .test_backup_manager import _mk_handler, _mk_manager
from .test_template_backup_review import _config
from .test_template_backup_review import manager as manager


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_shared_directory_allows_capture_without_changing_existing_modes(tmp_path):
    tmp_path.chmod(0o777)
    retained = tmp_path / "automation.retained.20260910_000000.yaml"
    retained.write_text("# ha_mcp_backup\nschema_version: 1\n", encoding="utf-8")
    retained.chmod(0o666)
    manager = _mk_manager(tmp_path)

    snapshot = manager._write_snapshot("automation", "example", {"alias": "old"}, None)

    assert manager.read_snapshot(snapshot.name)["config"] == {"alias": "old"}
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o777
    assert stat.S_IMODE(retained.stat().st_mode) == 0o666
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600


def test_snapshot_temp_is_exclusively_created_private_before_writing(
    tmp_path, monkeypatch
):
    manager = _mk_manager(tmp_path)
    native_open = os.open
    creations = []

    def open_file(path, flags, mode=0o777, **kwargs):
        if flags & os.O_CREAT:
            creations.append((Path(path), flags, mode))
            assert mode == 0o600
            assert flags & os.O_EXCL
            assert not Path(path).exists()
        return native_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    snapshot = manager._write_snapshot(
        "helper_template", "entry", {"options": {"state": "private value"}}, None
    )

    assert len(creations) == 1
    assert creations[0][0].parent == tmp_path
    assert creations[0][0] != snapshot.with_suffix(".yaml.tmp")
    assert (
        manager.read_snapshot(snapshot.name)["config"]["options"]["state"]
        == "private value"
    )


def test_snapshot_does_not_reuse_an_existing_predictable_temp(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "_now_ts", lambda: "20260910_000000")
    leftover = tmp_path / "automation.example.20260910_000000.yaml.tmp"
    leftover.write_text("unrelated unfinished write", encoding="utf-8")

    snapshot = _mk_manager(tmp_path)._write_snapshot("automation", "example", {}, None)

    assert snapshot.exists()
    assert leftover.read_text(encoding="utf-8") == "unrelated unfinished write"


@pytest.mark.parametrize("failure", ["fdopen", "write", "replace"])
def test_failed_snapshot_removes_only_its_temporary_file(
    tmp_path, monkeypatch, failure
):
    retained = tmp_path / "retained.yaml"
    retained.write_text("retained", encoding="utf-8")
    manager = _mk_manager(tmp_path)
    native_fdopen = os.fdopen

    class FailedWrite:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def write(self, content):
            self.stream.write(content[:20])
            raise OSError("disk full")

        def __exit__(self, *args):
            self.stream.close()

    if failure == "fdopen":

        def fail_fdopen(fd, *args):
            raise OSError("fdopen failed")

        monkeypatch.setattr(os, "fdopen", fail_fdopen)
    elif failure == "write":
        monkeypatch.setattr(
            os,
            "fdopen",
            lambda *args, **kwargs: FailedWrite(native_fdopen(*args, **kwargs)),
        )
    else:

        def fail_replace(*args):
            raise OSError("replace failed")

        monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match=r"disk full|replace failed|fdopen failed"):
        manager._write_snapshot("automation", "example", {}, None)

    assert list(tmp_path.iterdir()) == [retained]
    assert retained.read_text(encoding="utf-8") == "retained"


async def test_listing_checks_directory_off_loop_before_first_capture(
    tmp_path, monkeypatch
):
    directory = tmp_path / "backups"
    loop_thread = threading.get_ident()
    probe_threads = []
    native_mkdir = Path.mkdir

    def mkdir(path, *args, **kwargs):
        if path == directory:
            probe_threads.append(threading.get_ident())
        return native_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    manager = _mk_manager(directory)
    assert probe_threads == []
    await manager.list_edits_and_legacy(domain="automation")
    assert probe_threads and all(t != loop_thread for t in probe_threads)
    assert manager.enabled and manager.init_dir_error is None
    await manager.list_edits_and_legacy(domain="automation")
    assert len(probe_threads) == 1


async def test_disabled_health_check_and_capture_do_not_resolve_storage(monkeypatch):
    def unexpected_storage():
        pytest.fail("Disabled status/capture must not resolve or probe storage")

    monkeypatch.setattr(bm, "_resolve_default_dir", unexpected_storage)
    manager = _mk_manager(Path(""), enable_auto_backup=False)
    manager._settings.auto_backup_dir = ""
    manager.register(_mk_handler(fetched={"state": "saved"}))
    await manager.ensure_directory_ready()
    assert await manager.maybe_snapshot("automation", "example") is None
    assert manager.enabled is False
    assert manager.init_dir_error is None


async def test_directory_recovery_retries_failed_initialization(tmp_path, monkeypatch):
    directory = tmp_path / "backups"
    manager = _mk_manager(directory)
    fetch = AsyncMock(return_value={"alias": "Current"})
    manager.register(bm.DomainHandler("automation", fetch, AsyncMock()))
    native_mkdir = Path.mkdir
    attempts = []
    available = False

    def mkdir(path, *args, **kwargs):
        if path == directory:
            attempts.append(path)
            if not available:
                raise PermissionError("Backup directory temporarily unavailable")
        return native_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    for _ in range(2):
        await manager.ensure_directory_ready()
        assert manager.enabled is False
        assert "temporarily unavailable" in manager.init_dir_error
    assert len(attempts) == 2
    fetch.assert_not_awaited()

    available = True
    await manager.ensure_directory_ready()
    assert manager.enabled is True
    assert manager.init_dir_error is None
    assert len(attempts) == 3
    await manager.ensure_directory_ready()
    assert len(attempts) == 3
    captured = await manager.maybe_snapshot("automation", "example")
    assert captured is not None and captured.exists()
    fetch.assert_awaited_once()


@pytest.mark.parametrize("failure", ["yaml", "identity", "unreadable"])
def test_template_identity_failure_logs_filename_and_safe_type(
    manager, monkeypatch, caplog, failure
):
    path = manager._write_snapshot(
        "helper_template", "template-entry", _config(), "test"
    )
    if failure == "yaml":
        path.write_text("config: [private-template-value\n", encoding="utf-8")
        expected_type = "ValueError"
    elif failure == "identity":
        path = manager._write_snapshot(
            "helper_template", "template-entry", _config(entry_id="other-entry"), "test"
        )
        expected_type = "HomeAssistantError"
    else:
        native_stat = Path.stat

        def stat(target, *args, **kwargs):
            if target == path:
                raise PermissionError("private-template-value")
            return native_stat(target, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat)
        expected_type = "PermissionError"
    with caplog.at_level(logging.WARNING):
        assert manager._template_snapshot_identity(path) is None
    assert path.name in caplog.text
    assert expected_type in caplog.text
    assert "private-template-value" not in caplog.text


@pytest.mark.parametrize("failure", ["directory", "disk_full", "upstream"])
async def test_template_safety_failure_keeps_reason_and_safe_diagnostic(
    manager, monkeypatch, caplog, failure
):
    source = manager._write_snapshot(
        "helper_template", "template-entry", _config(), "test"
    )
    fetch = AsyncMock(return_value=_config(state="current"))
    restore = AsyncMock()
    manager.register(bm.DomainHandler("helper_template", fetch, restore))
    if failure == "upstream":
        fetch.side_effect = [
            _config(state="current"),
            bm.HomeAssistantError("private-template-value"),
        ]
        expected_detail = None
    else:
        expected_detail = (
            "read-only filesystem"
            if failure == "directory"
            else "No space left on device"
        )

        def storage_error(*args, **kwargs):
            raise OSError(expected_detail)

        if failure == "directory":
            monkeypatch.setattr(Path, "mkdir", storage_error)
        else:
            monkeypatch.setattr(manager, "_write_snapshot", storage_error)
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(bm.BackupRestoreError) as caught,
    ):
        await manager.restore_snapshot(source.name)
    assert caught.value.outcome["reason"] == "backup_capture_failed"
    assert caught.value.outcome["apply_status"] == "not_applied"
    assert caught.value.outcome["safety_backup"] is None
    assert "MandatoryBackupError" in caplog.text
    if expected_detail:
        assert expected_detail in caplog.text
        assert expected_detail in str(caught.value)
    assert "private-template-value" not in caplog.text
    assert "private-template-value" not in str(caught.value)
    restore.assert_not_awaited()


async def test_template_safety_read_oserror_keeps_diagnostic(
    manager, monkeypatch, caplog
):
    source = manager._write_snapshot(
        "helper_template", "template-entry", _config(), "test"
    )
    restore = AsyncMock()
    manager.register(
        bm.DomainHandler("helper_template", AsyncMock(return_value=_config()), restore)
    )

    def unreadable(_path):
        raise PermissionError("Cannot read retained safety snapshot")

    monkeypatch.setattr(bm, "_require_restore_safety", unreadable)
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(bm.BackupRestoreError) as caught,
    ):
        await manager.restore_snapshot(source.name)
    assert caught.value.outcome["reason"] == "backup_storage_failed"
    assert caught.value.outcome["apply_status"] == "not_applied"
    assert "Cannot read retained safety snapshot" in caplog.text
    assert "Cannot read retained safety snapshot" in str(caught.value)
    restore.assert_not_awaited()


@pytest.mark.parametrize("error_type", [OSError, TimeoutError, ConnectionError])
async def test_upstream_oserror_does_not_expose_private_restore_payload(
    manager, caplog, error_type
):
    source = manager._write_snapshot(
        "helper_template", "template-entry", _config(), "test"
    )
    manager.register(
        bm.DomainHandler(
            "helper_template",
            AsyncMock(return_value=_config()),
            AsyncMock(side_effect=error_type("private-template-value")),
        )
    )
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(bm.BackupRestoreError) as caught,
    ):
        await manager.restore_snapshot(source.name)
    assert caught.value.outcome["reason"] == "upstream_error"
    assert caught.value.outcome["apply_status"] == "unknown"
    assert "private-template-value" not in str(caught.value)
    assert "private-template-value" not in caplog.text
