"""The edit-backup E2E fixture must never select a developer's legacy history."""

import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp.utils import data_paths

from ..e2e import conftest as e2e_fixtures
from ..e2e.workflows.auto_backup import conftest as backup_fixtures
from ..e2e.workflows.auto_backup.test_capture_and_restore import _enable_auto_backup


@pytest.mark.parametrize("backend", ["container", "haos"])
def test_backup_fixture_overrides_existing_legacy_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str
) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    sentinel = legacy / "existing.yaml"
    sentinel.write_text("existing backup", encoding="utf-8")
    monkeypatch.setattr(bm, "_legacy_default_dir", lambda: legacy)
    monkeypatch.setattr(bm, "get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(data_paths, "get_data_dir", lambda: tmp_path / "data")
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("HAMCP_BACKUP_DIR", "")
    config._reset_global_settings()
    before = config.get_global_settings()
    client = SimpleNamespace()
    assert bm.get_backup_manager(client, before).backup_dir == legacy

    fixture = contextmanager(backup_fixtures.isolate_edit_backups.__wrapped__)
    with fixture(monkeypatch, tmp_path, {"backend": backend}):
        settings = config.get_global_settings()
        assert settings is not before
        manager = bm.get_backup_manager(client, settings)
        assert manager.backup_dir == tmp_path / "backups"
        # Existing tests reset settings again when enabling capture.
        _enable_auto_backup(monkeypatch)
        manager = bm.get_backup_manager(client, config.get_global_settings())
        assert manager.backup_dir == tmp_path / "backups"
        path = manager._write_snapshot(
            "automation", "fixture", {"id": "fixture"}, "test"
        )
        assert path.parent == tmp_path / "backups"
        assert list(legacy.iterdir()) == [sentinel]
        assert sentinel.read_text(encoding="utf-8") == "existing backup"

    assert os.environ["HAMCP_BACKUP_DIR"] == ""
    assert config.get_global_settings().auto_backup_dir == ""
    config._reset_global_settings()


@pytest.mark.parametrize(
    "backend", ["embedded", "haos_inaddon", "haos_embedded", "haos_stdio"]
)
def test_backup_fixture_preserves_out_of_process_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str
) -> None:
    monkeypatch.setenv("HAMCP_BACKUP_DIR", "/server/backups")
    reset = Mock()
    monkeypatch.setattr(config, "_reset_global_settings", reset)
    fixture = contextmanager(backup_fixtures.isolate_edit_backups.__wrapped__)
    with fixture(monkeypatch, tmp_path, {"backend": backend}):
        assert os.environ["HAMCP_BACKUP_DIR"] == "/server/backups"
        reset.assert_not_called()
    assert os.environ["HAMCP_BACKUP_DIR"] == "/server/backups"
    reset.assert_not_called()
    assert not (tmp_path / "backups").exists()


def test_stdio_environment_pins_backups_inside_its_disposable_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "stdio"
    monkeypatch.setenv("HAMCP_BACKUP_DIR", str(tmp_path / "developer-backups"))
    env = e2e_fixtures._stdio_env(
        {"base_url": "http://homeassistant.invalid:8123", "token": "test-token"},
        config_dir,
    )
    assert env["HAMCP_BACKUP_DIR"] == str(config_dir / "backups")
    default = Mock(side_effect=AssertionError("Must not inspect legacy backups"))
    monkeypatch.setattr(bm, "_resolve_default_dir", default)
    manager = bm.BackupManager(
        SimpleNamespace(auto_backup_dir=env["HAMCP_BACKUP_DIR"]), SimpleNamespace()
    )
    assert manager.backup_dir == config_dir / "backups"
    default.assert_not_called()
