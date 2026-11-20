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
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.utils import data_paths

from ..e2e import conftest as e2e_fixtures
from ..e2e.utilities.assertions import parse_mcp_result
from ..e2e.workflows.auto_backup import conftest as backup_fixtures
from ..e2e.workflows.auto_backup.test_capture_and_restore import _enable_auto_backup


def _legacy_history(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A developer's pre-#2372 backup store, holding a snapshot."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    legacy = tmp_path / "xdg" / "ha_mcp" / "backups"
    legacy.mkdir(parents=True)
    (legacy / "existing.yaml").write_text("existing backup", encoding="utf-8")
    return legacy


async def _capture(hass: HomeAssistant, manager: bm.BackupManager) -> Path:
    """A snapshot the server captures of a real config entry."""
    assert await async_setup_component(hass, "config", {})
    entry = MockConfigEntry(domain="test", title="Isolation")
    entry.add_to_hass(hass)
    path = await manager.maybe_snapshot("integration", entry.entry_id, force=True)
    assert path is not None
    return path


@pytest.mark.parametrize("backend", ["container", "haos"])
async def test_backup_fixture_overrides_existing_legacy_directory(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    backend: str,
) -> None:
    legacy = _legacy_history(monkeypatch, tmp_path)
    monkeypatch.setattr(bm, "get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(data_paths, "get_data_dir", lambda: tmp_path / "data")
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("HAMCP_BACKUP_DIR", "")
    config.reset_global_settings()
    before = config.get_global_settings()
    assert bm.get_backup_manager(core_client, before).backup_dir == legacy

    fixture = contextmanager(inspect.unwrap(backup_fixtures.isolate_edit_backups))
    with fixture(monkeypatch, tmp_path, {"backend": backend}):
        settings = config.get_global_settings()
        assert settings is not before
        manager = bm.get_backup_manager(core_client, settings)
        assert manager.backup_dir == tmp_path / "backups"
        # Existing tests reset settings again when enabling capture.
        _enable_auto_backup(monkeypatch)
        manager = bm.get_backup_manager(core_client, config.get_global_settings())
        assert (await _capture(hass, manager)).parent == tmp_path / "backups"
        assert [path.name for path in legacy.iterdir()] == ["existing.yaml"]

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


@pytest.fixture
def developer_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A developer's home holding a pre-#2372 backup store, and their own
    backup directory setting."""
    home = tmp_path / "home"
    legacy = home / ".local" / "share" / "ha_mcp" / "backups"
    legacy.mkdir(parents=True)
    (legacy / "existing.yaml").write_text("existing backup", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HAMCP_BACKUP_DIR", str(tmp_path / "developer-backups"))


@pytest.mark.usefixtures("developer_home")
async def test_the_stdio_server_keeps_its_backups_in_its_disposable_directory(
    core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    """The stdio lanes run the installed ``ha-mcp`` with an environment of its
    own: the developer's backup setting does not reach it, and its config
    directory alone would still let the legacy store in their home be chosen."""
    config_dir = tmp_path / "stdio"
    fixture = inspect.unwrap(e2e_fixtures.stdio_mcp_client)
    clients = fixture(
        {"base_url": core_client.base_url, "token": core_client.token}, config_dir
    )

    client = await anext(clients)
    try:
        result = await client.call_tool(
            "ha_manage_backup", {"scope": "edits", "action": "list"}
        )
    finally:
        await clients.aclose()

    assert parse_mcp_result(result)["data"]["backup_dir"] == str(config_dir / "backups")


async def test_in_process_server_keeps_every_write_out_of_the_developer_home(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
        manager = bm.get_backup_manager(core_client, config.get_global_settings())
        path = await _capture(hass, manager)

    assert path.is_relative_to(config_dir)
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
