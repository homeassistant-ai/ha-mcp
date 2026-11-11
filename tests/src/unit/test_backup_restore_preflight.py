"""Known input failures remain distinct from failures after restore dispatch."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ._backup_restore_helpers import NAME as NAME
from ._backup_restore_helpers import _backup_settings as _backup_settings
from ._backup_restore_helpers import _dispatcher as _dispatcher
from ._backup_restore_helpers import response_for as response_for
from ._backup_restore_helpers import run_restore as run_restore


@pytest.fixture
def fetch() -> AsyncMock:
    """The automation handler's fetch: this repository's own collaborator."""
    return AsyncMock()


@pytest.fixture
def restore() -> AsyncMock:
    """The automation handler's restore: this repository's own collaborator."""
    return AsyncMock()


@pytest.fixture
def manager(tmp_path: Path, fetch: AsyncMock, restore: AsyncMock) -> bm.BackupManager:
    settings = SimpleNamespace(
        enable_auto_backup=False,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=5,
        auto_backup_dir=str(tmp_path),
    )
    mgr = bm.get_backup_manager(SimpleNamespace(), settings)
    mgr.register(bm.DomainHandler(domain="automation", fetch=fetch, restore=restore))
    return mgr


def _write_snapshot(manager: bm.BackupManager) -> str:
    """A valid automation snapshot, in the shape the manager writes."""
    snapshot = {
        "schema_version": bm.SCHEMA_VERSION,
        "domain": "automation",
        "entity_id": "example",
        "captured": "2026-09-09T00:00:00+00:00",
        "tool": None,
        "config": {"alias": "Saved"},
    }
    (manager.backup_dir / NAME).write_text(yaml.safe_dump(snapshot, sort_keys=False))
    return NAME


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
    manager, fetch, restore, monkeypatch, consumer, failure, reason, status, code
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
        monkeypatch.setattr("ha_mcp.tools.backup.get_global_settings", _backup_settings)
        monkeypatch.setattr("ha_mcp.read_only.get_global_settings", _backup_settings)
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
    fetch.assert_not_awaited()
    restore.assert_not_awaited()


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
    manager, restore, monkeypatch, settings_script, error
):
    changes: list[str] = []

    async def apply(*args):
        changes.append("applied")
        raise error

    restore.side_effect = apply
    name = _write_snapshot(manager)
    response = response_for(monkeypatch, manager, name)
    assert changes == ["applied"]
    assert response.json().get("data", {}).get("apply_status") != "not_applied"
    rendered = run_restore(
        settings_script,
        {"status": response.status_code, "json": response.json()},
        name,
    )
    assert "could not be confirmed" in rendered.alerts[0]
    assert "Nothing was changed" not in rendered.alerts[0]


async def test_generic_disabled_restore_confirmation_does_not_promise_safety(
    manager, fetch, restore, monkeypatch, settings_script
):
    restore.return_value = {"success": True}
    name = _write_snapshot(manager)
    response = response_for(monkeypatch, manager, name)
    assert response.json()["data"]["safety_backup"] is None
    restore.assert_awaited_once()
    fetch.assert_not_awaited()
    rendered = run_restore(
        settings_script,
        {"status": response.status_code, "json": response.json()},
        name,
    )
    assert (
        "Existing Template helpers require a fresh safety backup"
        in rendered.confirms[0]
    )
    assert "may proceed without a new safety backup" in rendered.confirms[0]
