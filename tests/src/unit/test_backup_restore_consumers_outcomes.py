"""A restore that fails keeps what it did to Home Assistant in every consumer.

The manager raises one typed outcome; Settings answers it as JSON, the MCP tool
raises it as a structured error, and the page's script words it for the user.
The snapshots are of real labels, on a real ``hass``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import label_registry as lr
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError

from ._backup_core import add_label, snapshot_of
from ._backup_core import backup_settings as backup_settings
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_restore_consumers_core import (
    fill_the_disk,
    restore_fails_with,
    restore_through,
    run_restore,
)

Applied = Literal["not_applied", "unknown", "applied"]
Verified = Literal["not_run", "matched", "mismatched", "unavailable"]

FAILURES: list[tuple[Applied, Verified, str]] = [
    ("not_applied", "not_run", "Nothing was changed"),
    ("unknown", "unavailable", "could not be confirmed"),
    ("applied", "mismatched", "does not match"),
    ("applied", "unavailable", "verification is unavailable"),
]


@pytest.fixture
async def saved_label(
    snapshot_manager: bm.BackupManager, label_registry: lr.LabelRegistry
) -> Path:
    """A snapshot of a label that still exists, so its restore takes a safety
    snapshot first."""
    return await snapshot_of(snapshot_manager, add_label(label_registry, "Kitchen"))


@pytest.mark.parametrize("consumer", ["settings", "mcp", "script"])
@pytest.mark.parametrize(("applied", "verified", "words"), FAILURES)
async def test_a_failed_restore_tells_the_user_what_was_changed(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    settings_script: str,
    saved_label: Path,
    consumer: str,
    applied: Applied,
    verified: Verified,
    words: str,
) -> None:
    """Whether the restore changed anything, and the safety snapshot to revert
    with, are what the user needs after it fails."""
    error = bm.BackupRestoreError(
        "Restore could not be completed.",
        apply_status=applied,
        verification_status=verified,
    )
    restore_fails_with(snapshot_manager, "label", error)
    reply = await restore_through(
        "settings" if consumer == "script" else consumer,
        snapshot_manager,
        core_client,
        monkeypatch,
        saved_label.name,
    )

    payload = reply.payload
    assert payload["success"] is False
    assert payload["error"]["message"] == str(error)
    assert payload["data"]["apply_status"] == applied
    assert payload["data"]["verification_status"] == verified
    assert payload["data"]["restored_from"] == saved_label.name
    safety = payload["data"]["safety_backup"]
    assert safety != saved_label.name
    assert safety in {row["name"] for row in snapshot_manager.list_snapshots()}
    if consumer == "script":
        assert reply.status is not None and reply.status >= 400
        rendered = run_restore(
            settings_script,
            {"status": reply.status, "json": payload},
            saved_label.name,
        )
        assert len(rendered.alerts) == 1
        assert words in rendered.alerts[0]
        assert safety in rendered.alerts[0]
        assert len(rendered.fetches_to("/backups?")) == 1


@pytest.mark.parametrize("consumer", ["mcp", "settings"])
@pytest.mark.parametrize("cause", ["no-answer", "disk"])
async def test_a_restore_without_its_safety_snapshot_is_refused_with_the_backup_code(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    label_registry: lr.LabelRegistry,
    saved_label: Path,
    consumer: str,
    cause: str,
) -> None:
    """Restoring over the current label without its recovery point would lose
    it: both consumers refuse with the backup error code and change nothing."""
    label_id = snapshot_manager.read_snapshot(saved_label.name)["entity_id"]
    label_registry.async_update(label_id, name="Renamed since")
    if cause == "no-answer":
        monkeypatch.setattr(
            "ha_mcp.tools.helpers.get_connected_ws_client",
            AsyncMock(side_effect=HomeAssistantConnectionError("secret-marker down")),
        )
    else:
        fill_the_disk(snapshot_manager, monkeypatch)
    reply = await restore_through(
        consumer, snapshot_manager, core_client, monkeypatch, saved_label.name
    )

    if consumer == "settings":
        assert reply.status == 409
    payload = reply.payload
    assert payload["error"]["code"] == "BACKUP_CAPTURE_FAILED"
    assert payload["data"]["apply_status"] == "not_applied"
    assert "secret-marker" not in json.dumps(payload)
    if cause == "no-answer":
        assert "HomeAssistantConnectionError" in payload["error"]["message"]
    else:
        assert "No space left on device" in payload["error"]["message"]
    live = label_registry.async_get_label(label_id)
    assert live is not None and live.name == "Renamed since"


@pytest.mark.parametrize("consumer", ["settings", "mcp"])
async def test_a_deleted_integration_entry_is_reported_as_not_found(
    hass: HomeAssistant,
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
) -> None:
    """A snapshot holds an entry's options, not the entry: once Core has
    deleted it, the restore says it is gone and changed nothing."""
    entry = MockConfigEntry(domain="test", title="Gone")
    entry.add_to_hass(hass)
    snapshot = await snapshot_manager.maybe_snapshot(
        "integration", entry.entry_id, force=True
    )
    assert snapshot is not None
    assert await hass.config_entries.async_remove(entry.entry_id)

    reply = await restore_through(
        consumer, snapshot_manager, core_client, monkeypatch, snapshot.name
    )

    if consumer == "settings":
        assert reply.status == 404
    assert reply.payload["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert reply.payload["data"]["reason"] == "entry_deleted"
    assert reply.payload["data"]["apply_status"] == "not_applied"
