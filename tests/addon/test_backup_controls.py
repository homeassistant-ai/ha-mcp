"""App backup controls reach the server through the real startup path."""

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from .test_addon_startup import _load_addon_start


@pytest.mark.parametrize("flavor", ["homeassistant-addon", "homeassistant-addon-dev"])
def test_app_exposes_backup_controls_for_supervisor_saves(flavor: str) -> None:
    manifest = yaml.safe_load(
        (Path(__file__).parents[2] / flavor / "config.yaml").read_text()
    )
    for option in ("enable_snapshot_actions", "backup_read_only"):
        assert manifest["schema"][option] == "bool?"
        assert option in manifest["options"]


def _boot_until_server_import(
    options: dict[str, Any] | str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], list[str]]:
    import json

    addon = _load_addon_start()
    monkeypatch.setattr(addon.os, "environ", dict(os.environ))
    options_path = tmp_path / "options.json"
    options_path.write_text(
        json.dumps(options) if isinstance(options, dict) else options
    )
    errors: list[str] = []
    monkeypatch.setattr(addon, "log_error", errors.append)
    warnings: list[str] = []
    monkeypatch.setattr(addon, "log_warning", warnings.append)
    monkeypatch.setattr(
        addon,
        "Path",
        lambda path: options_path if path == "/data/options.json" else tmp_path,
    )
    monkeypatch.setattr(addon, "cleanup_stale_migration_marker", lambda path: None)
    monkeypatch.setattr(
        addon, "get_or_create_secret_path", lambda *args: "/private_test"
    )
    monkeypatch.setattr(addon, "maybe_persist_secret_path", lambda *args: None)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    for env in ("ENABLE_SNAPSHOT_ACTIONS", "BACKUP_READ_ONLY", "ENABLE_AUTO_BACKUP"):
        monkeypatch.setenv(env, "previous-value")

    class StartupReachedServerImport(Exception):
        pass

    def stop_before_server_import(message: str) -> None:
        if message == "Importing ha_mcp module...":
            raise StartupReachedServerImport

    monkeypatch.setattr(addon, "log_info", stop_before_server_import)
    with pytest.raises(StartupReachedServerImport):
        addon.main()
    return errors, warnings


@pytest.mark.parametrize(
    ("options", "snapshot_actions", "read_only"),
    [
        ({}, "true", "false"),
        ({"enable_snapshot_actions": False, "backup_read_only": True}, "false", "true"),
        ({"enable_snapshot_actions": True, "backup_read_only": False}, "true", "false"),
        (
            {"enable_snapshot_actions": "false", "backup_read_only": "true"},
            "true",
            "false",
        ),
        ("{invalid json", "true", "false"),
    ],
)
def test_app_startup_exports_backup_controls(
    options: dict[str, Any] | str,
    snapshot_actions: str,
    read_only: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors, warnings = _boot_until_server_import(options, tmp_path, monkeypatch)

    assert os.environ["ENABLE_SNAPSHOT_ACTIONS"] == snapshot_actions
    assert os.environ["BACKUP_READ_ONLY"] == read_only
    assert os.environ["ENABLE_AUTO_BACKUP"] == "true"
    malformed = isinstance(options, dict) and isinstance(
        options.get("enable_snapshot_actions"), str
    )
    assert len(warnings) == (2 if malformed else 0)
    if isinstance(options, str):
        assert "reverts to its addon-schema default" in " ".join(errors)
