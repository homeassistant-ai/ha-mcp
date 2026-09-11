"""Known input failures remain distinct from failures after restore dispatch."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from fastmcp.exceptions import ToolError

from ha_mcp import backup_manager as bm

from .test_backup_diff_error_mapping import _dispatcher
from .test_backup_restore_consumers import response_for, run_restore
from .test_backup_restore_consumers import settings_script as settings_script

NAME = "automation.example.20260909_000000.yaml"


@pytest.fixture
def manager(tmp_path):
    settings = SimpleNamespace(
        enable_auto_backup=False,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=5,
        auto_backup_dir=str(tmp_path),
    )
    mgr = bm.get_backup_manager(SimpleNamespace(), settings)
    mgr._handlers["automation"] = replace(
        mgr.handler_for("automation"), fetch=AsyncMock(), restore=AsyncMock()
    )
    return mgr


@pytest.mark.parametrize("consumer", ["settings", "mcp"])
@pytest.mark.parametrize(
    ("failure", "reason", "status", "code"),
    [
        ("missing", "snapshot_not_found", 404, "RESOURCE_NOT_FOUND"),
        ("yaml", "invalid_snapshot", 400, "VALIDATION_INVALID_PARAMETER"),
        ("domain", "invalid_snapshot", 400, "VALIDATION_INVALID_PARAMETER"),
        ("entity_id", "invalid_snapshot", 400, "VALIDATION_INVALID_PARAMETER"),
        ("config", "invalid_snapshot", 400, "VALIDATION_INVALID_PARAMETER"),
        ("domain_type", "invalid_snapshot", 400, "VALIDATION_INVALID_PARAMETER"),
        ("handler", "unsupported_domain", 400, "VALIDATION_INVALID_PARAMETER"),
    ],
)
async def test_restore_input_failure_reports_no_write(
    manager, monkeypatch, consumer, failure, reason, status, code
):
    snapshot = {
        "schema_version": bm.SCHEMA_VERSION,
        "domain": "automation",
        "entity_id": "example",
        "config": {"alias": "Saved"},
    }
    if failure in {"domain", "entity_id", "config"}:
        snapshot.pop(failure)
    elif failure == "domain_type":
        snapshot["domain"] = []
    elif failure == "handler":
        snapshot["domain"] = "unregistered"
    if failure != "missing":
        content = (
            "config: [secret-marker" if failure == "yaml" else yaml.safe_dump(snapshot)
        )
        (manager.backup_dir / NAME).write_text(content)

    if consumer == "settings":
        response = response_for(monkeypatch, manager, NAME)
        assert response.status_code == status
        payload = response.json()
    else:
        monkeypatch.setattr(
            "ha_mcp.tools.backup.get_backup_manager", lambda *args: manager
        )
        monkeypatch.setattr("ha_mcp.tools.backup.get_global_settings", SimpleNamespace)
        with pytest.raises(ToolError) as caught:
            await _dispatcher()(scope="edits", action="restore", backup_name=NAME)
        payload = json.loads(str(caught.value))

    assert payload["error"]["code"] == code
    assert payload["data"]["apply_status"] == "not_applied"
    assert payload["data"]["verification_status"] == "not_run"
    assert payload["data"]["reason"] == reason
    assert payload["data"]["restored_from"] == NAME
    assert not any(
        "Use safety_backup" in item for item in payload["error"].get("suggestions", [])
    )
    assert "secret-marker" not in json.dumps(payload)
    manager.handler_for("automation").fetch.assert_not_awaited()
    manager.handler_for("automation").restore.assert_not_awaited()


def test_missing_snapshot_ui_reports_known_no_change(
    manager, monkeypatch, settings_script
):
    response = response_for(monkeypatch, manager, NAME)
    rendered = run_restore(
        settings_script, {"status": response.status_code, "json": response.json()}, NAME
    )
    assert "Nothing was changed" in rendered.alerts[0]
    assert "could not be confirmed" not in rendered.alerts[0]
    assert not rendered.fetches_to("/backups?")


@pytest.mark.parametrize(
    "error", [ValueError("late rejection"), FileNotFoundError("late read")]
)
def test_post_dispatch_failures_do_not_claim_no_change(
    manager, monkeypatch, settings_script, error
):
    changes = []

    async def restore(*args):
        changes.append("applied")
        raise error

    manager._handlers["automation"] = replace(
        manager.handler_for("automation"), restore=AsyncMock(side_effect=restore)
    )
    snapshot = manager._write_snapshot(
        "automation", "example", {"alias": "Saved"}, None
    )
    response = response_for(monkeypatch, manager, snapshot.name)
    assert changes == ["applied"]
    assert response.json().get("data", {}).get("apply_status") != "not_applied"
    rendered = run_restore(
        settings_script,
        {"status": response.status_code, "json": response.json()},
        snapshot.name,
    )
    assert "could not be confirmed" in rendered.alerts[0]
    assert "Nothing was changed" not in rendered.alerts[0]


async def test_generic_disabled_restore_confirmation_does_not_promise_safety(
    manager, monkeypatch, settings_script
):
    restore = manager.handler_for("automation").restore
    restore.return_value = {"success": True}
    snapshot = manager._write_snapshot(
        "automation", "example", {"alias": "Saved"}, None
    )
    response = response_for(monkeypatch, manager, snapshot.name)
    assert response.json()["data"]["safety_backup"] is None
    restore.assert_awaited_once()
    manager.handler_for("automation").fetch.assert_not_awaited()
    rendered = run_restore(
        settings_script,
        {"status": response.status_code, "json": response.json()},
        snapshot.name,
    )
    assert (
        "Existing Template helpers require a fresh safety backup"
        in rendered.confirms[0]
    )
    assert "may proceed without a new safety backup" in rendered.confirms[0]
