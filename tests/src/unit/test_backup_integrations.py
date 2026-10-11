"""Restoring an integration auto-backup: a deleted entry is refused, an existing
one gets its enabled state back."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_integrations import restore_integration
from ha_mcp.tools import backup as backup_tool


def _ws(monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, Any]]) -> list:
    sent: list[dict[str, Any]] = []

    async def fake_ws_send(client: Any, message: dict[str, Any]) -> Any:
        sent.append(message)
        if message["type"] == "config_entries/get":
            return entries
        return {"require_restart": False}

    monkeypatch.setattr(bm, "_ws_send", fake_ws_send)
    return sent


@pytest.mark.asyncio
async def test_restoring_a_deleted_entry_is_refused_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _ws(monkeypatch, [{"entry_id": "other"}])
    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await restore_integration(None, "e1", {"domain": "otp", "disabled_by": None})
    assert exc_info.value.outcome["reason"] == "entry_deleted"
    assert exc_info.value.outcome["apply_status"] == "not_applied"
    assert "Config entry e1 (otp) no longer exists" in str(exc_info.value)
    assert [message["type"] for message in sent] == ["config_entries/get"]


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled_by,expected", [("user", "user"), (None, None)])
async def test_restoring_an_existing_entry_reapplies_its_enabled_state(
    monkeypatch: pytest.MonkeyPatch, disabled_by: str | None, expected: str | None
) -> None:
    sent = _ws(monkeypatch, [{"entry_id": "e1"}])
    await restore_integration(None, "e1", {"domain": "hue", "disabled_by": disabled_by})
    assert sent[-1] == {
        "type": "config_entries/disable",
        "entry_id": "e1",
        "disabled_by": expected,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "safety,expected",
    [
        (None, ["Add it again"]),
        ("safety.yaml", ["Add it again", "Use safety_backup to inspect or restore"]),
    ],
)
async def test_a_refused_restore_is_not_found_with_the_handlers_guidance(
    safety: str | None, expected: list[str]
) -> None:
    outcome: dict[str, Any] = {"suggestions": ["Add it again"]}
    if safety:
        outcome["safety_backup"] = safety
    mgr = MagicMock()
    mgr.restore_snapshot = AsyncMock(
        side_effect=bm.BackupRestoreError("gone", reason="entry_deleted", **outcome)
    )
    with pytest.raises(ToolError) as exc_info:
        await backup_tool._edits_restore(mgr, "edits", "restore", "b.yaml")
    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "RESOURCE_NOT_FOUND"
    suggestions = error.get("suggestions") or [error["suggestion"]]
    for suggestion, prefix in zip(suggestions, expected, strict=True):
        assert suggestion.startswith(prefix)


@pytest.mark.asyncio
async def test_a_refused_restore_without_handler_guidance_gets_the_generic_hint() -> (
    None
):
    mgr = MagicMock()
    mgr.restore_snapshot = AsyncMock(
        side_effect=bm.BackupRestoreError("refused", reason="restore_refused")
    )
    with pytest.raises(ToolError) as exc_info:
        await backup_tool._edits_restore(mgr, "edits", "restore", "b.yaml")
    error = json.loads(str(exc_info.value))["error"]
    suggestions = error.get("suggestions") or [error["suggestion"]]
    assert suggestions == [
        "Inspect the current configuration and restore outcome before retrying"
    ]
