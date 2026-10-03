"""App backup controls reach the server through the real startup path."""

import os
from pathlib import Path

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


@pytest.mark.parametrize(
    ("options", "snapshot_actions", "read_only"),
    [
        ({}, "true", "false"),
        ({"enable_snapshot_actions": False, "backup_read_only": True}, "false", "true"),
        ({"enable_snapshot_actions": True, "backup_read_only": False}, "true", "false"),
        (
            {"enable_snapshot_actions": "false", "backup_read_only": "true"},
            "false",
            "true",
        ),
        ("{invalid json", "true", "false"),
    ],
)
def test_app_startup_exports_backup_controls(
    options: dict | str, snapshot_actions: str, read_only: str, tmp_path, monkeypatch
) -> None:
    import json

    addon = _load_addon_start()
    monkeypatch.setattr(addon.os, "environ", dict(os.environ))
    options_path = tmp_path / "options.json"
    options_path.write_text(
        json.dumps(options) if isinstance(options, dict) else options
    )
    errors = []
    monkeypatch.setattr(addon, "log_error", errors.append)
    warnings = []
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

    assert os.environ["ENABLE_SNAPSHOT_ACTIONS"] == snapshot_actions
    assert os.environ["BACKUP_READ_ONLY"] == read_only
    assert os.environ["ENABLE_AUTO_BACKUP"] == "true"
    malformed = isinstance(options, dict) and isinstance(
        options.get("enable_snapshot_actions"), str
    )
    assert len(warnings) == (2 if malformed else 0)
    if isinstance(options, str):
        message = " ".join(errors)
        assert "decoded backup options still apply" in message
        assert "enable_snapshot_actions=true" in message
        assert "backup_read_only=false" in message


@pytest.mark.parametrize("invalid", ["false", "true", 0, 1, None, [], {}])
def test_malformed_app_backup_controls_warn_and_restrict_ai_actions(
    invalid, monkeypatch
) -> None:
    addon = _load_addon_start()
    monkeypatch.setattr(addon.os, "environ", dict(os.environ))
    warnings = []
    monkeypatch.setattr(addon, "log_warning", warnings.append)
    addon._apply_backup_env(
        {"enable_snapshot_actions": invalid, "backup_read_only": invalid}
    )

    assert os.environ["ENABLE_SNAPSHOT_ACTIONS"] == "false"
    assert os.environ["BACKUP_READ_ONLY"] == "true"
    assert len(warnings) == 2
    assert "enable_snapshot_actions" in warnings[0]
    assert "False" in warnings[0]
    assert "backup_read_only" in warnings[1]
    assert "True" in warnings[1]


def test_existing_app_backup_options_survive_startup_extraction(monkeypatch) -> None:
    addon = _load_addon_start()
    monkeypatch.setattr(addon.os, "environ", dict(os.environ))
    expected = {
        "ENABLE_AUTO_BACKUP": "false",
        "AUTO_BACKUP_THROTTLE_MINUTES": "12",
        "AUTO_BACKUP_RETAIN_PER_ENTITY": "50",
        "ENABLE_SNAPSHOT_DELETE": "true",
        "SNAPSHOT_DELETE_MIN_AGE_DAYS": "20",
    }
    for env in expected:
        monkeypatch.setenv(env, "previous-value")
    addon._apply_backup_env(
        {
            "enable_auto_backup": False,
            "auto_backup_throttle_minutes": 12,
            "auto_backup_retain_per_entity": 50,
            "enable_snapshot_delete": True,
            "snapshot_delete_min_age_days": 20,
        }
    )
    assert {env: os.environ[env] for env in expected} == expected
