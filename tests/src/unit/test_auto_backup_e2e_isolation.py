"""The edit-backup E2E fixture must never select a developer's legacy history."""

import inspect
import os
import subprocess
import sys
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


def _legacy_history(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A developer's pre-#2372 backup store, holding a snapshot."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    legacy = tmp_path / "xdg" / "ha_mcp" / "backups"
    legacy.mkdir(parents=True)
    (legacy / "existing.yaml").write_text("existing backup", encoding="utf-8")
    return legacy


@pytest.mark.parametrize("backend", ["container", "haos"])
def test_backup_fixture_overrides_existing_legacy_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str
) -> None:
    legacy = _legacy_history(monkeypatch, tmp_path)
    monkeypatch.setattr(bm, "get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(data_paths, "get_data_dir", lambda: tmp_path / "data")
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("HAMCP_BACKUP_DIR", "")
    config.reset_global_settings()
    before = config.get_global_settings()
    client = SimpleNamespace()
    assert bm.get_backup_manager(client, before).backup_dir == legacy

    fixture = contextmanager(inspect.unwrap(backup_fixtures.isolate_edit_backups))
    with fixture(monkeypatch, tmp_path, {"backend": backend}):
        settings = config.get_global_settings()
        assert settings is not before
        manager = bm.get_backup_manager(client, settings)
        assert manager.backup_dir == tmp_path / "backups"
        # Existing tests reset settings again when enabling capture.
        _enable_auto_backup(monkeypatch)
        manager = bm.get_backup_manager(client, config.get_global_settings())
        assert manager.backup_dir == tmp_path / "backups"

    assert os.environ["HAMCP_BACKUP_DIR"] == ""
    assert config.get_global_settings().auto_backup_dir == ""
    config.reset_global_settings()


@pytest.mark.parametrize(
    "backend", ["embedded", "haos_inaddon", "haos_embedded", "haos_stdio"]
)
def test_backup_fixture_preserves_out_of_process_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str
) -> None:
    monkeypatch.setenv("HAMCP_BACKUP_DIR", "/server/backups")
    reset = Mock()
    monkeypatch.setattr(config, "reset_global_settings", reset)
    fixture = contextmanager(inspect.unwrap(backup_fixtures.isolate_edit_backups))
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
    env = e2e_fixtures.stdio_env(
        {"base_url": "http://homeassistant.invalid:8123", "token": "test-token"},
        config_dir,
    )
    assert env["HAMCP_BACKUP_DIR"] == str(config_dir / "backups")
    # The developer's legacy history is not where the stdio server writes.
    _legacy_history(monkeypatch, tmp_path)
    manager = bm.BackupManager(
        SimpleNamespace(auto_backup_dir=env["HAMCP_BACKUP_DIR"]), SimpleNamespace()
    )
    assert manager.backup_dir == config_dir / "backups"


def test_in_process_server_keeps_every_write_out_of_the_developer_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A local E2E run must not add snapshots or settings files to the
    developer's ``~/.ha-mcp`` or to an existing legacy backup store."""
    home = tmp_path / "home"
    legacy = home / ".local" / "share" / "ha_mcp" / "backups"
    legacy.mkdir(parents=True)
    sentinel = legacy / "existing.yaml"
    sentinel.write_text("existing backup", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    for name in (
        "HA_MCP_CONFIG_DIR",
        "HAMCP_BACKUP_DIR",
        "XDG_DATA_HOME",
        "SUPERVISOR_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    def mktemp(name: str) -> Path:
        path = tmp_path / "pytest" / name
        path.mkdir(parents=True)
        return path

    fixture = contextmanager(inspect.unwrap(e2e_fixtures.in_process_data_dir))
    with fixture(SimpleNamespace(mktemp=mktemp)) as config_dir:
        assert not data_paths.get_data_dir().is_relative_to(home)
        manager = bm.get_backup_manager(SimpleNamespace(), config.get_global_settings())

    assert manager.backup_dir.is_relative_to(config_dir)
    assert not (home / ".ha-mcp").exists()
    assert list(legacy.iterdir()) == [sentinel]
    assert "HAMCP_BACKUP_DIR" not in os.environ


def test_e2e_collection_leaves_the_developer_home_alone(tmp_path: Path) -> None:
    """Collecting E2E tests imports ``ha_mcp``, which reads settings; that must
    not create or read the developer's ``~/.ha-mcp``."""
    tests_dir = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"HA_MCP_CONFIG_DIR", "HAMCP_BACKUP_DIR", "PYTEST_ADDOPTS"}
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "src/e2e/workflows/config/test_helper_crud.py",
        ],
        cwd=tests_dir,
        env={**env, "HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (home / ".ha-mcp").exists()
