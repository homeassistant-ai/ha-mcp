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
