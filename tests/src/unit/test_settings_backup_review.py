"""Settings diff failures preserve missing/invalid/unavailable distinctions."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantConnectionError,
)
from ha_mcp.settings_ui import _handlers_backups as ui

NAME = "automation.example.20260909_000000.yaml"


async def test_settings_inventory_reports_unusable_directory_before_capture(
    tmp_path, monkeypatch
):
    parent = tmp_path / "not-a-directory"
    parent.write_text("occupied", encoding="utf-8")
    settings = SimpleNamespace(
        enable_auto_backup=True,
        auto_backup_dir=str(parent / "backups"),
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=5,
    )
    manager = bm.BackupManager(settings, SimpleNamespace())
    monkeypatch.setattr(ui, "_backup_mgr", lambda server: manager)
    monkeypatch.setattr(ui, "get_global_settings", lambda: settings)

    response = await ui._list_backups(
        None, Request({"type": "http", "query_string": b""})
    )

    assert response.status_code == 200
    assert json.loads(response.body)["enabled"] is False
    assert manager.init_dir_error is not None
    assert parent.read_text(encoding="utf-8") == "occupied"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (FileNotFoundError(NAME), 404, "RESOURCE_NOT_FOUND"),
        (LookupError("No diff handler registered"), 404, "RESOURCE_NOT_FOUND"),
        (ValueError("Invalid snapshot structure"), 400, "VALIDATION_INVALID_PARAMETER"),
        (HomeAssistantConnectionError("secret-marker"), 502, "CONNECTION_FAILED"),
    ],
)
async def test_settings_diff_error_classification(monkeypatch, error, status, code):
    manager = SimpleNamespace(snapshot_comparison=AsyncMock(side_effect=error))
    monkeypatch.setattr(ui, "_backup_mgr", lambda server: manager)

    response = await ui._diff_backup(
        None, Request({"type": "http", "path_params": {"name": NAME}})
    )

    assert response.status_code == status
    payload = json.loads(response.body)
    assert payload["error"]["code"] == code
    assert "secret-marker" not in response.body.decode()
    manager.snapshot_comparison.assert_awaited_once_with(NAME)


async def test_settings_diff_logs_safe_failure_diagnostics(monkeypatch, caplog):
    manager = SimpleNamespace(
        snapshot_comparison=AsyncMock(
            side_effect=HomeAssistantAPIError(
                "option-value-secret <html>upstream body</html>", status_code=503
            )
        )
    )
    monkeypatch.setattr(ui, "_backup_mgr", lambda server: manager)

    with caplog.at_level(logging.WARNING, logger=ui.__name__):
        response = await ui._diff_backup(
            None, Request({"type": "http", "path_params": {"name": NAME}})
        )

    assert response.status_code == 502
    assert "comparison" in caplog.text.lower()
    assert NAME in caplog.text
    assert "HomeAssistantAPIError" in caplog.text
    assert "503" in caplog.text
    assert "option-value-secret" not in caplog.text
    assert "<html>" not in caplog.text


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        (
            "template_read_unsupported",
            "The component cannot authoritatively read template helpers",
        ),
        (
            "secret_scrub_degraded",
            "Template helper secret scrub is degraded; capture is unsafe",
        ),
        (
            "ambiguous_entry_identity",
            "Template helper listing has ambiguous identities",
        ),
        ("ambiguous_target", "Template helper target is ambiguous"),
        ("ambiguous_registry", "Entity registry has ambiguous identities"),
        (
            "redacted_options",
            "Template helper options contain redacted values; capture is incomplete",
        ),
        ("invalid_options", "Template helper options must be an object"),
    ],
)
async def test_settings_diff_preserves_template_refusal_reason(
    monkeypatch, reason, message
):
    manager = SimpleNamespace(
        snapshot_comparison=AsyncMock(
            side_effect=bm._TemplateReadError(message, reason)
        )
    )
    monkeypatch.setattr(ui, "_backup_mgr", lambda server: manager)

    response = await ui._diff_backup(
        None, Request({"type": "http", "path_params": {"name": NAME}})
    )

    assert response.status_code == 409
    payload = json.loads(response.body)
    assert payload["error"]["code"] == "CONFIG_VALIDATION_FAILED"
    assert payload["error"]["message"] == message
    assert payload["data"]["reason"] == reason


@pytest.mark.parametrize("action", ["view", "diff", "restore"])
async def test_settings_snapshot_validation_never_echoes_yaml_values(
    tmp_path, monkeypatch, action
):
    manager = bm.BackupManager(
        SimpleNamespace(auto_backup_dir=str(tmp_path), enable_auto_backup=True),
        SimpleNamespace(),
    )
    (tmp_path / NAME).write_text(
        "config:\n  private-config-value: [broken\n", encoding="utf-8"
    )
    monkeypatch.setattr(ui, "_backup_mgr", lambda server: manager)

    response = await getattr(ui, f"_{action}_backup")(
        None, Request({"type": "http", "path_params": {"name": NAME}})
    )

    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert payload["error"]["message"] == (
        "Snapshot is invalid; inspect its YAML and schema version"
    )
    assert "private-config-value" not in response.body.decode()


@pytest.mark.parametrize(
    "config",
    [
        {"options": {"name": "Example", "template_type": "sensor", "state": "1"}},
        {"entry_id": "original-entry"},
        {"entry_id": "original-entry", "options": []},
    ],
    ids=["missing-identity", "missing-options", "invalid-options"],
)
async def test_settings_diff_rejects_invalid_template_snapshot_before_fetch(
    tmp_path, monkeypatch, config
):
    settings = SimpleNamespace(
        enable_auto_backup=True,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=5,
        auto_backup_dir=str(tmp_path),
    )
    manager = bm.get_backup_manager(SimpleNamespace(), settings)
    snapshot = manager._write_snapshot(
        "helper_template", "sensor.example", config, None
    )
    fetch = AsyncMock(return_value={"entry_id": "original-entry", "options": {}})
    manager._handlers["helper_template"] = replace(
        manager.handler_for("helper_template"), fetch=fetch
    )
    monkeypatch.setattr(ui, "_backup_mgr", lambda server: manager)

    response = await ui._diff_backup(
        None, Request({"type": "http", "path_params": {"name": snapshot.name}})
    )

    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "snapshot" in payload["error"]["message"].lower()
    assert payload["error"]["message"] != "'options'"
    fetch.assert_not_awaited()
