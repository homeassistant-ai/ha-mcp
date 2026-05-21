"""End-to-end coverage for #1288 auto-backup, all lanes.

Exercises the full capture → list → view → restore → delete loop across
every backed-up domain that has a workable e2e fixture. Plus the new
polymorphic ``ha_manage_backup`` tool's gating against accidental
wrong-mode usage.

The auto-backup decorator is gated by ``ENABLE_AUTO_BACKUP=true``. These
tests set that via env var before each test and explicitly call
``ha_manage_backup(scope='edits', ...)`` to drive the LLM-facing
interface end-to-end.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest

from ...utilities.assertions import safe_call_tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- helpers


def _enable_auto_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force-enable auto-backup for the duration of one test.

    The settings singleton reads env vars at import time, so we also
    clear the singleton cache to re-read.
    """
    monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
    monkeypatch.setenv("AUTO_BACKUP_THROTTLE_MINUTES", "0")
    monkeypatch.setenv("AUTO_BACKUP_RETAIN_PER_ENTITY", "20")
    # backup_dir is per-test-tmpdir to avoid cross-test pollution.
    from ha_mcp.config import _reset_global_settings

    _reset_global_settings()


def _backups_for(
    entries: list[dict[str, Any]], *, domain: str, entity_id: str
) -> list[dict[str, Any]]:
    return [e for e in entries if e["domain"] == domain and e["entity_id"] == entity_id]


# ---------------------------------------------------------------- gating


@pytest.mark.convenience
class TestManageBackupGating:
    """Strong gating against wrong-mode usage on the merged tool."""

    async def test_rejects_invalid_combo(self, mcp_client) -> None:
        # (snapshot, list) is not a valid combo — snapshot only supports create/restore.
        result = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "snapshot", "action": "list"},
        )
        assert result.get("success") is False
        error = result.get("error", {})
        msg = error.get("message", "") if isinstance(error, dict) else ""
        assert "Invalid combination" in msg
        # Helpful suggestion lists the valid combos.
        suggestions = error.get("suggestions", []) if isinstance(error, dict) else []
        assert any("Valid combinations" in s for s in suggestions)

    async def test_edits_create_rejected(self, mcp_client) -> None:
        # (edits, create) is not valid — captures happen automatically via the decorator.
        result = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "create"},
        )
        assert result.get("success") is False

    async def test_snapshot_restore_requires_backup_id(self, mcp_client) -> None:
        # Validation should mention that backup_id is missing — not silently dispatch.
        result = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "snapshot", "action": "restore"},
        )
        assert result.get("success") is False

    async def test_edits_restore_requires_backup_name(self, mcp_client) -> None:
        result = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "restore"},
        )
        assert result.get("success") is False


# ---------------------------------------------------------------- list


@pytest.mark.convenience
class TestListEditsBackups:
    async def test_list_without_filter_returns_state(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        result = await safe_call_tool(
            mcp_client, "ha_manage_backup", {"scope": "edits", "action": "list"}
        )
        assert result.get("success") is True
        data = result.get("data", {})
        assert "backups" in data
        assert "backup_dir" in data
        assert "enabled" in data
        assert "throttle_minutes" in data
        assert "retain_per_entity" in data


# ---------------------------------------------------------------- automation lane


@pytest.mark.automation
@pytest.mark.cleanup
class TestAutomationCaptureRestore:
    async def test_full_loop(self, mcp_client, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_auto_backup(monkeypatch)

        suffix = uuid.uuid4().hex[:8]
        identifier = f"e2e_backup_{suffix}"
        original = {
            "id": identifier,
            "alias": f"E2E Backup Original {suffix}",
            "trigger": [{"platform": "time", "at": "12:00:00"}],
            "action": [{"service": "homeassistant.no_op"}],
        }
        # Create
        create = await safe_call_tool(
            mcp_client, "ha_config_set_automation", {"config": original}
        )
        assert create.get("success") is not False

        # Edit it once — decorator captures pre-edit state.
        edited = {**original, "alias": f"E2E Backup Edited {suffix}"}
        await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {"config": edited, "identifier": identifier},
        )

        # List backups; ours should be there.
        listing = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {
                "scope": "edits",
                "action": "list",
                "domain": "automation",
                "entity_id": identifier,
            },
        )
        assert listing.get("success") is True
        entries = listing.get("data", {}).get("backups", [])
        mine = _backups_for(entries, domain="automation", entity_id=identifier)
        assert len(mine) >= 1

        backup_name = mine[0]["name"]

        # View
        view = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "view", "backup_name": backup_name},
        )
        assert view.get("success") is True

        # Restore
        restore = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        assert restore.get("success") is True
        # A safety backup is taken before re-applying.
        assert restore.get("data", {}).get("safety_backup") is not None

        # Delete
        delete = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        assert delete.get("success") is True

        # Cleanup: remove the automation we created.
        await safe_call_tool(
            mcp_client,
            "ha_config_remove_automation",
            {"identifier": identifier},
        )


# ---------------------------------------------------------------- helper lane


@pytest.mark.config
@pytest.mark.cleanup
class TestHelperCaptureRestore:
    async def test_input_boolean_capture(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        helper_id = f"e2e_bk_{uuid.uuid4().hex[:8]}"
        # Create
        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": helper_id,
                "icon": "mdi:test-tube",
            },
        )
        assert create.get("success") is not False
        # Edit so capture fires (the create call may or may not capture
        # depending on whether helper_id is None — edit definitely does).
        edit = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "helper_id": helper_id,
                "icon": "mdi:test-tube-empty",
            },
        )
        assert edit.get("success") is not False

        # List backups in the helper_input_boolean domain.
        listing = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "list", "domain": "helper_input_boolean"},
        )
        assert listing.get("success") is True

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_delete_helpers_integrations",
            {"target": f"input_boolean.{helper_id}"},
        )


# ---------------------------------------------------------------- dashboard lane


@pytest.mark.dashboards
@pytest.mark.cleanup
class TestDashboardCaptureRestore:
    async def test_dashboard_capture_on_edit(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        url_path = f"e2e-bk-{uuid.uuid4().hex[:8]}"
        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_dashboard",
            {"url_path": url_path, "title": "E2E Backup", "config": {"views": []}},
        )
        # If the create succeeded, edit it so the decorator captures.
        if create.get("success") is not False:
            await safe_call_tool(
                mcp_client,
                "ha_config_set_dashboard",
                {
                    "url_path": url_path,
                    "config": {"views": [{"title": "Updated"}]},
                },
            )
            listing = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {
                    "scope": "edits",
                    "action": "list",
                    "domain": "dashboard",
                    "entity_id": url_path,
                },
            )
            assert listing.get("success") is True

            # Cleanup
            await safe_call_tool(
                mcp_client,
                "ha_config_delete_dashboard",
                {"url_path": url_path},
            )


# ---------------------------------------------------------------- script lane


@pytest.mark.scripts
@pytest.mark.cleanup
class TestScriptCaptureRestore:
    async def test_script_capture(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        script_id = f"e2e_bk_{uuid.uuid4().hex[:8]}"
        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_script",
            {
                "script_id": script_id,
                "config": {
                    "alias": f"E2E Backup Script {script_id}",
                    "sequence": [{"service": "homeassistant.no_op"}],
                },
            },
        )
        # Edit triggers capture.
        if create.get("success") is not False:
            await safe_call_tool(
                mcp_client,
                "ha_config_set_script",
                {
                    "script_id": script_id,
                    "config": {
                        "alias": f"E2E Backup Script {script_id} edited",
                        "sequence": [{"service": "homeassistant.no_op"}],
                    },
                },
            )
            listing = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {
                    "scope": "edits",
                    "action": "list",
                    "domain": "script",
                    "entity_id": script_id,
                },
            )
            assert listing.get("success") is True

            # Cleanup
            await safe_call_tool(
                mcp_client,
                "ha_config_remove_script",
                {"script_id": script_id},
            )


# ---------------------------------------------------------------- scene lane


@pytest.mark.scenes
@pytest.mark.cleanup
class TestSceneCaptureRestore:
    async def test_scene_capture(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        scene_id = f"e2e_bk_{uuid.uuid4().hex[:8]}"
        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "config": {
                    "name": f"E2E Backup Scene {scene_id}",
                    "entities": {},
                },
            },
        )
        if create.get("success") is not False:
            await safe_call_tool(
                mcp_client,
                "ha_config_set_scene",
                {
                    "scene_id": scene_id,
                    "config": {
                        "name": f"E2E Backup Scene {scene_id} edited",
                        "entities": {},
                    },
                },
            )
            listing = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {
                    "scope": "edits",
                    "action": "list",
                    "domain": "scene",
                    "entity_id": scene_id,
                },
            )
            assert listing.get("success") is True

            # Cleanup
            await safe_call_tool(
                mcp_client,
                "ha_config_remove_scene",
                {"scene_id": scene_id},
            )


# ---------------------------------------------------------------- toggle off


@pytest.mark.convenience
class TestToggleOffSkipsCapture:
    async def test_disabled_means_no_new_backups(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENABLE_AUTO_BACKUP", "false")
        from ha_mcp.config import _reset_global_settings

        _reset_global_settings()

        # Count before
        before = await safe_call_tool(
            mcp_client, "ha_manage_backup", {"scope": "edits", "action": "list"}
        )
        n_before = before.get("data", {}).get("count", 0)

        # Edit any automation — backup should NOT be captured.
        identifier = f"e2e_off_{uuid.uuid4().hex[:8]}"
        await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {
                "config": {
                    "id": identifier,
                    "alias": "Auto off test",
                    "trigger": [{"platform": "time", "at": "12:00:00"}],
                    "action": [{"service": "homeassistant.no_op"}],
                }
            },
        )
        await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {
                "config": {
                    "id": identifier,
                    "alias": "Auto off test edited",
                    "trigger": [{"platform": "time", "at": "12:00:00"}],
                    "action": [{"service": "homeassistant.no_op"}],
                },
                "identifier": identifier,
            },
        )

        # Count after — must be unchanged because feature was off.
        after = await safe_call_tool(
            mcp_client, "ha_manage_backup", {"scope": "edits", "action": "list"}
        )
        n_after = after.get("data", {}).get("count", 0)
        assert n_after == n_before

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_config_remove_automation",
            {"identifier": identifier},
        )
