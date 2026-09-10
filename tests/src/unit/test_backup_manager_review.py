"""Directory health and Template recovery retain safe local diagnostics."""

import logging
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ha_mcp import backup_manager as bm

from .test_backup_manager import _mk_handler, _mk_manager
from .test_template_backup_review import _config
from .test_template_backup_review import manager as manager


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
