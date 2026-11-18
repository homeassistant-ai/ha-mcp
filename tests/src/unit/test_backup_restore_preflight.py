"""Known input failures remain distinct from failures after restore dispatch.

Snapshots are restored by the server's own manager into a real ``hass``,
through the Settings page route and the ``ha_manage_backup`` tool.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import Settings

from ._backup_core import backup_settings as backup_settings
from ._backup_core import manage_backup
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_restore_helpers import NAME, response_for, run_restore

_SAVED = {
    "id": "example",
    "alias": "Saved",
    "triggers": [{"trigger": "event", "event_type": "preflight_test"}],
    "actions": [],
}


@pytest.fixture
async def manager(
    hass: HomeAssistant,
    snapshot_manager: bm.BackupManager,
    backup_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> bm.BackupManager:
    """The server's manager, with auto-backup off and Core's automations up,
    loaded from ``automations.yaml`` as a default install does."""
    monkeypatch.setattr(backup_settings, "enable_auto_backup", False)
    await hass.async_add_executor_job(
        Path(hass.config.path("configuration.yaml")).write_text,
        "automation: !include automations.yaml\n",
    )
    await hass.async_add_executor_job(
        Path(hass.config.path("automations.yaml")).write_text, "[]\n"
    )
    assert await async_setup_component(hass, "automation", {})
    snapshot_manager.backup_dir.mkdir(parents=True, exist_ok=True)
    return snapshot_manager


def _write_snapshot(manager: bm.BackupManager) -> str:
    """A valid automation snapshot, in the shape the manager writes."""
    snapshot = {
        "schema_version": bm.SCHEMA_VERSION,
        "domain": "automation",
        "entity_id": "example",
        "captured": "2026-09-09T00:00:00+00:00",
        "tool": None,
        "config": _SAVED,
    }
    (manager.backup_dir / NAME).write_text(yaml.safe_dump(snapshot, sort_keys=False))
    return NAME


def _automations(hass: HomeAssistant) -> list[str]:
    return hass.states.async_entity_ids("automation")


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
    hass: HomeAssistant,
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
    failure: str,
    reason: str,
    status: int,
    code: str,
) -> None:
    snapshot: dict[str, Any] = {
        "schema_version": bm.SCHEMA_VERSION,
        "domain": "automation",
        "entity_id": "example",
        "config": dict(_SAVED, alias="secret-marker"),
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
        response = await response_for(monkeypatch, core_client, NAME)
        assert response.status_code == status
        payload = response.json()
    else:
        with pytest.raises(ToolError) as caught:
            await manage_backup(
                core_client, scope="edits", action="restore", backup_name=NAME
            )
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
    await hass.async_block_till_done()
    assert _automations(hass) == []


async def test_missing_snapshot_ui_reports_known_no_change(
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    settings_script: Any,
) -> None:
    response = await response_for(monkeypatch, core_client, NAME)
    rendered = run_restore(
        settings_script, {"status": response.status_code, "json": response.json()}, NAME
    )
    assert "Nothing was changed" in rendered.alerts[0]
    assert "could not be confirmed" not in rendered.alerts[0]
    assert not rendered.fetches_to("/backups?")


@pytest.mark.parametrize(
    "error", [ValueError("late rejection"), FileNotFoundError("late read")]
)
async def test_post_dispatch_failures_do_not_claim_no_change(
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    settings_script: Any,
    error: BaseException,
) -> None:
    changes: list[str] = []

    async def apply(*args: Any) -> None:
        changes.append("applied")
        raise error

    # The automation restore fails after it was sent.
    manager.register(
        bm.DomainHandler(
            domain="automation",
            fetch=AsyncMock(side_effect=AssertionError("auto-backup is off")),
            restore=apply,
        )
    )
    name = _write_snapshot(manager)
    response = await response_for(monkeypatch, core_client, name)
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
    hass: HomeAssistant,
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    settings_script: Any,
) -> None:
    name = _write_snapshot(manager)
    response = await response_for(monkeypatch, core_client, name)
    assert response.json()["data"]["safety_backup"] is None
    await hass.async_block_till_done()
    assert _automations(hass) == ["automation.saved"]
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
