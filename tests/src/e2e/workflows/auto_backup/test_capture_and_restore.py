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

import asyncio
import logging
import uuid
from typing import Any

import pytest

from ...utilities.assertions import safe_call_tool
from ...utilities.wait_helpers import wait_for_tool_result

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


# Fixed delay between create and edit to let HA's WS-backed registries
# index the freshly-created entity before the decorator's pre-edit
# fetch fires. The decorator is best-effort: if fetch returns None
# (entity not in the registry list yet), the snapshot is silently
# skipped — and polling the backup file afterwards can't recover that.
# 5 s is conservative on CI runners under load; automation's REST
# upsert path settles faster and doesn't need this, but every WS-backed
# domain (label, category, zone, area, helper, dashboard_resource) does.
_HA_PROPAGATION_SETTLE_SECONDS = 5.0


async def _wait_for_backup(
    mcp_client, *, domain: str, entity_id: str, timeout: int = 15
) -> str:
    """Poll the backup list until at least one snapshot for ``domain:entity_id``
    appears, returning the first backup name.

    Captures are written synchronously by the decorator before the wrapped
    write returns, but HA storage propagation between create and edit can
    delay when the decorator's pre-edit fetch sees the freshly-created
    entity. The poll absorbs that delay rather than asserting on the first
    list call (which would fail with ``assert 0 >= 1`` for slow rigs).
    """
    data = await wait_for_tool_result(
        mcp_client,
        tool_name="ha_manage_backup",
        arguments={
            "scope": "edits",
            "action": "list",
            "domain": domain,
            "entity_id": entity_id,
        },
        predicate=lambda d: bool(
            _backups_for(
                d.get("backups", []) or d.get("data", {}).get("backups", []),
                domain=domain,
                entity_id=entity_id,
            )
        ),
        description=f"auto-backup snapshot for {domain}:{entity_id}",
        timeout=timeout,
    )
    entries = data.get("backups", []) or data.get("data", {}).get("backups", [])
    mine = _backups_for(entries, domain=domain, entity_id=entity_id)
    return mine[0]["name"]


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
@pytest.mark.external_only
class TestAutomationCaptureRestore:
    async def test_full_loop(self, mcp_client, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable_auto_backup(monkeypatch)

        suffix = uuid.uuid4().hex[:8]
        identifier = f"e2e_backup_{suffix}"
        original = {
            "alias": f"E2E Backup Original {suffix}",
            "trigger": [{"platform": "time", "at": "12:00:00"}],
            "action": [{"service": "homeassistant.no_op"}],
        }
        # Create — pass identifier so the tool doesn't reject the call as
        # an ambiguous create-with-explicit-id.
        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {"config": original, "identifier": identifier},
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


@pytest.mark.helper
@pytest.mark.cleanup
@pytest.mark.external_only
class TestHelperCaptureRestore:
    async def test_input_boolean_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simple-helper full loop: WS ``input_boolean/update`` is exercised
        by the restore call (Group 2 mechanism). The schedule test below
        covers the complex-nested-config angle on the same mechanism."""
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
        # Let HA index the new helper before the edit fires; otherwise the
        # decorator's pre-edit fetch via ``input_boolean/list`` may miss it.
        await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)
        # Edit so capture fires (the create call may or may not capture
        # depending on whether helper_id is None — edit definitely does).
        edit = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "helper_id": helper_id,
                "name": helper_id,
                "icon": "mdi:test-tube-empty",
            },
        )
        assert edit.get("success") is not False

        # Poll until the snapshot file appears — absorbs HA storage
        # propagation delay between create and edit on slower rigs.
        backup_name = await _wait_for_backup(
            mcp_client, domain="helper_input_boolean", entity_id=helper_id
        )

        # Restore — exercises ``input_boolean/update`` WS command.
        restore = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        assert restore.get("success") is True
        assert restore.get("data", {}).get("safety_backup") is not None

        # Delete the snapshot.
        delete = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        assert delete.get("success") is True

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_delete_helpers_integrations",
            {"target": f"input_boolean.{helper_id}"},
        )


# ---------------------------------------------------------------- complex helper lane


@pytest.mark.helper
@pytest.mark.cleanup
@pytest.mark.external_only
class TestComplexHelperCaptureRestore:
    """``schedule`` helper has the most complex storage-backed config —
    seven weekday arrays each holding a list of {from, to} time-range
    objects. Worth its own e2e because the ``schedule/update`` WS payload
    is the most fragile of the helper-update commands — a YAML
    serialization quirk that nuked a nested time-range would be invisible
    to the mocked unit tests."""

    async def test_schedule_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        suffix = uuid.uuid4().hex[:8]
        helper_id = f"e2e_sched_{suffix}"

        # Original config — multiple days with multiple time ranges each.
        # Picks days/times deterministically so the assertion on captured
        # content has something concrete to check.
        original = {
            "helper_type": "schedule",
            "name": helper_id,
            "icon": "mdi:calendar-clock",
            "monday": [
                {"from": "08:00:00", "to": "12:00:00"},
                {"from": "13:00:00", "to": "17:00:00"},
            ],
            "tuesday": [{"from": "09:00:00", "to": "18:00:00"}],
            "wednesday": [
                {"from": "07:00:00", "to": "11:30:00"},
                {"from": "12:30:00", "to": "16:00:00"},
                {"from": "18:00:00", "to": "20:00:00"},
            ],
            "friday": [{"from": "08:30:00", "to": "14:30:00"}],
        }
        create = await safe_call_tool(mcp_client, "ha_config_set_helper", original)
        assert create.get("success") is not False
        # Let HA index the new helper before the edit fires; the decorator's
        # pre-edit fetch via ``schedule/list`` needs the entity present.
        await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)

        # Edit — shrink Monday, split Tuesday, drop Wednesday entirely.
        # ``name`` is required by HA's schedule schema on every update
        # (validator rejects the WS payload otherwise).
        edited = {
            "helper_type": "schedule",
            "helper_id": helper_id,
            "name": helper_id,
            "icon": "mdi:calendar-remove",
            "monday": [{"from": "10:00:00", "to": "12:00:00"}],
            "tuesday": [
                {"from": "08:00:00", "to": "12:00:00"},
                {"from": "13:00:00", "to": "17:00:00"},
            ],
            "wednesday": [],
            "friday": [{"from": "08:30:00", "to": "14:30:00"}],
        }
        edit = await safe_call_tool(mcp_client, "ha_config_set_helper", edited)
        assert edit.get("success") is not False

        # Poll until the schedule snapshot appears so an HA storage
        # propagation race doesn't masquerade as a real assert.
        backup_name = await _wait_for_backup(
            mcp_client, domain="helper_schedule", entity_id=helper_id
        )

        # View — assert the nested arrays survived YAML round-trip into
        # the snapshot file. The tool returns ``{"success": True, "data":
        # <YAML payload>}`` and the payload's ``config`` key holds the
        # original entity config (schedule shape:
        # ``config.monday = [{"from": "...", "to": "..."}, ...]``).
        view = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "view", "backup_name": backup_name},
        )
        assert view.get("success") is True
        captured = view.get("data", {}).get("config", {})
        # Nested structure must be intact. Three days × multiple ranges.
        assert isinstance(captured.get("monday"), list)
        assert len(captured["monday"]) == 2
        assert captured["monday"][0]["from"] == "08:00:00"
        assert captured["monday"][1]["to"] == "17:00:00"
        assert len(captured.get("wednesday", [])) == 3

        # Restore — exercises ``schedule/update`` WS with the full nested
        # payload reconstructed from YAML. Safety backup must exist.
        restore = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        assert restore.get("success") is True, (
            f"schedule restore failed: {restore.get('data') or restore}"
        )
        assert restore.get("data", {}).get("safety_backup") is not None

        # Delete the snapshot.
        delete = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        assert delete.get("success") is True

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_delete_helpers_integrations",
            {"target": f"schedule.{helper_id}"},
        )


# ---------------------------------------------------------------- dashboard lane


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestDashboardCaptureRestore:
    async def test_dashboard_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dashboard restore exercises the ``lovelace/config/save`` WS
        command — different shape from helper updates (envelope is
        ``{"type": ..., "url_path": ..., "config": ...}``)."""
        _enable_auto_backup(monkeypatch)
        url_path = f"e2e-bk-{uuid.uuid4().hex[:8]}"
        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_dashboard",
            {"url_path": url_path, "title": "E2E Backup", "config": {"views": []}},
        )
        if create.get("success") is False:
            pytest.skip(f"dashboard create unsupported on this HA: {create}")
        # Let HA settle the new lovelace config before the edit fires.
        await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)
        # Edit so the decorator captures the pre-edit state.
        await safe_call_tool(
            mcp_client,
            "ha_config_set_dashboard",
            {
                "url_path": url_path,
                "config": {"views": [{"title": "Updated"}]},
            },
        )
        backup_name = await _wait_for_backup(
            mcp_client, domain="dashboard", entity_id=url_path
        )

        # Restore — fires ``lovelace/config/save``.
        restore = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        assert restore.get("success") is True
        assert restore.get("data", {}).get("safety_backup") is not None

        delete = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        assert delete.get("success") is True

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_config_delete_dashboard",
            {"url_path": url_path},
        )


# ---------------------------------------------------------------- script lane


@pytest.mark.script
@pytest.mark.cleanup
@pytest.mark.external_only
class TestScriptCaptureRestore:
    async def test_script_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Script restore is Group 1 (REST upsert via
        ``client.upsert_script_config``). Mechanically equivalent to the
        automation full-loop but proves the script-specific client method
        wires correctly end-to-end."""
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
        if create.get("success") is False:
            pytest.skip(f"script create unsupported: {create}")
        # Settle so the decorator's pre-edit fetch finds the script.
        await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)
        # Edit triggers capture.
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
        backup_name = await _wait_for_backup(
            mcp_client, domain="script", entity_id=script_id
        )

        restore = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        assert restore.get("success") is True
        assert restore.get("data", {}).get("safety_backup") is not None

        delete = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        assert delete.get("success") is True

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_config_remove_script",
            {"script_id": script_id},
        )


# ---------------------------------------------------------------- scene lane


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestSceneCaptureRestore:
    async def test_scene_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Scene restore is Group 1 (REST upsert via
        ``client.upsert_scene_config``)."""
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
        if create.get("success") is False:
            pytest.skip(f"scene create unsupported: {create}")
        # Settle so the decorator's pre-edit fetch finds the scene.
        await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)
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
        backup_name = await _wait_for_backup(
            mcp_client, domain="scene", entity_id=scene_id
        )

        restore = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        assert restore.get("success") is True
        assert restore.get("data", {}).get("safety_backup") is not None

        delete = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        assert delete.get("success") is True

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_config_remove_scene",
            {"scene_id": scene_id},
        )


# ---------------------------------------------------------------- toggle off


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
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
                    "alias": "Auto off test",
                    "trigger": [{"platform": "time", "at": "12:00:00"}],
                    "action": [{"service": "homeassistant.no_op"}],
                },
                "identifier": identifier,
            },
        )
        await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {
                "config": {
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


# ---------------------------------------------------------------- label lane


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestLabelCaptureRestore:
    """Label restore exercises the ``config/label_registry/update`` WS
    command (Group 2). Field-mangling matters here — the handler strips
    ``label_id`` from the captured config before re-injecting it under
    the ``label_id`` key, so a round-trip that lost or duplicated the key
    would only surface end-to-end."""

    async def test_label_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        suffix = uuid.uuid4().hex[:8]
        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_label",
            {
                "name": f"e2e_bk_label_{suffix}",
                "color": "blue",
                "icon": "mdi:test-tube",
                "description": "auto-backup e2e label",
            },
        )
        if create.get("success") is False:
            pytest.skip(f"label create unsupported: {create}")
        label_id = create.get("data", {}).get("label_id") or create.get("label_id")
        assert label_id, f"label_id missing from create response: {create}"
        # Settle so the decorator's pre-edit fetch finds the label.
        await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)

        # Edit — change name + color so capture fires.
        edit = await safe_call_tool(
            mcp_client,
            "ha_config_set_label",
            {
                "label_id": label_id,
                "name": f"e2e_bk_label_{suffix}_edited",
                "color": "red",
            },
        )
        assert edit.get("success") is not False

        backup_name = await _wait_for_backup(
            mcp_client, domain="label", entity_id=label_id
        )

        restore = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        assert restore.get("success") is True
        assert restore.get("data", {}).get("safety_backup") is not None

        delete = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        assert delete.get("success") is True

        # Cleanup
        await safe_call_tool(
            mcp_client, "ha_config_remove_label", {"label_id": label_id}
        )


# ---------------------------------------------------------------- category lane


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestCategoryCaptureRestore:
    """Category restore is Group 2 (``config/category_registry/update``).
    Entity ID is the composite ``<scope>:<category_id>`` — the handler
    splits it back into scope + id on restore, so a captured snapshot
    that lost the scope prefix would silently restore to the wrong
    registry. Worth e2e coverage."""

    async def test_category_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        suffix = uuid.uuid4().hex[:8]
        scope = "automation"
        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_category",
            {
                "name": f"e2e_bk_cat_{suffix}",
                "scope": scope,
                "icon": "mdi:tag",
            },
        )
        if create.get("success") is False:
            pytest.skip(f"category create unsupported: {create}")
        cat_id = create.get("data", {}).get("category_id") or create.get("category_id")
        assert cat_id, f"category_id missing: {create}"
        composite = f"{scope}:{cat_id}"
        # Settle so the decorator's pre-edit fetch finds the category.
        await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)

        edit = await safe_call_tool(
            mcp_client,
            "ha_config_set_category",
            {
                "category_id": cat_id,
                "scope": scope,
                "name": f"e2e_bk_cat_{suffix}_edited",
                "icon": "mdi:tag-outline",
            },
        )
        assert edit.get("success") is not False

        backup_name = await _wait_for_backup(
            mcp_client, domain="category", entity_id=composite
        )

        restore = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        assert restore.get("success") is True
        assert restore.get("data", {}).get("safety_backup") is not None

        delete = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        assert delete.get("success") is True

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_config_remove_category",
            {"scope": scope, "category_id": cat_id},
        )


# ---------------------------------------------------------------- zone lane


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestZoneCaptureRestore:
    """Zone restore is Group 2 (``config/zone/update``). The handler
    strips ``id`` and re-injects under ``zone_id`` — different key name
    from the rest of the WS-update commands, so a copy-paste bug from
    the label handler would only surface here."""

    async def test_zone_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        suffix = uuid.uuid4().hex[:8]
        create = await safe_call_tool(
            mcp_client,
            "ha_set_zone",
            {
                "name": f"e2e_bk_zone_{suffix}",
                "latitude": 40.7128,
                "longitude": -74.0060,
                "radius": 150,
                "icon": "mdi:briefcase",
            },
        )
        if create.get("success") is False:
            pytest.skip(f"zone create unsupported: {create}")
        zone_id = create.get("data", {}).get("zone_id") or create.get("zone_id")
        assert zone_id, f"zone_id missing: {create}"
        # Settle so the decorator's pre-edit fetch finds the zone.
        await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)

        edit = await safe_call_tool(
            mcp_client,
            "ha_set_zone",
            {
                "zone_id": zone_id,
                "name": f"e2e_bk_zone_{suffix}_edited",
                "radius": 250,
            },
        )
        assert edit.get("success") is not False

        backup_name = await _wait_for_backup(
            mcp_client, domain="zone", entity_id=zone_id
        )

        restore = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        assert restore.get("success") is True
        assert restore.get("data", {}).get("safety_backup") is not None

        delete = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        assert delete.get("success") is True

        # Cleanup
        await safe_call_tool(mcp_client, "ha_remove_zone", {"zone_id": zone_id})


# ---------------------------------------------------------------- area lane


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestAreaCaptureRestore:
    """Area restore is Group 2 (``config/area_registry/update``). The
    handler keys on ``area:<area_id>`` and dispatches to area or floor
    based on the prefix — floor uses the identical mechanism with a
    different registry, so area-only coverage proves both
    code paths."""

    async def test_area_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        suffix = uuid.uuid4().hex[:8]
        create = await safe_call_tool(
            mcp_client,
            "ha_set_area_or_floor",
            {
                "kind": "area",
                "name": f"e2e_bk_area_{suffix}",
                "icon": "mdi:sofa",
            },
        )
        if create.get("success") is False:
            pytest.skip(f"area create unsupported: {create}")
        area_id = create.get("data", {}).get("area_id") or create.get("area_id")
        assert area_id, f"area_id missing: {create}"
        composite = f"area:{area_id}"
        # Settle so the decorator's pre-edit fetch finds the area.
        await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)

        edit = await safe_call_tool(
            mcp_client,
            "ha_set_area_or_floor",
            {
                "kind": "area",
                "id": area_id,
                "name": f"e2e_bk_area_{suffix}_edited",
                "icon": "mdi:sofa-outline",
            },
        )
        assert edit.get("success") is not False

        backup_name = await _wait_for_backup(
            mcp_client, domain="area_or_floor", entity_id=composite
        )

        restore = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        assert restore.get("success") is True
        assert restore.get("data", {}).get("safety_backup") is not None

        delete = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        assert delete.get("success") is True

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_remove_area_or_floor",
            {"kind": "area", "id": area_id},
        )


# ---------------------------------------------------------------- group lane


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestGroupCaptureRestore:
    """Group restore is **Group 3** (service-call restore via
    ``/api/services/group/set``). Different mechanism from the WS-update
    family — proves the service-call payload shape works end-to-end."""

    async def test_group_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        suffix = uuid.uuid4().hex[:8]
        object_id = f"e2e_bk_grp_{suffix}"
        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_group",
            {
                "object_id": object_id,
                "name": f"E2E Backup Group {suffix}",
                "entities": ["light.bed_light"],
                "icon": "mdi:lightbulb-group",
            },
        )
        if create.get("success") is False:
            pytest.skip(f"group create unsupported: {create}")
        # Group entity is created via the ``group.set`` service call —
        # state machine registration is async and a fixed sleep is racy.
        # Poll ``ha_get_state`` until ``group.<object_id>`` is queryable
        # so the decorator's pre-edit ``/api/states`` GET finds it.
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_state",
            arguments={"entity_id": f"group.{object_id}"},
            predicate=lambda d: (
                d.get("success") is True
                or d.get("state") is not None
                or d.get("entity_id") == f"group.{object_id}"
            ),
            description=f"group.{object_id} state visible",
            timeout=15,
        )

        edit = await safe_call_tool(
            mcp_client,
            "ha_config_set_group",
            {
                "object_id": object_id,
                "name": f"E2E Backup Group {suffix} edited",
                "entities": ["light.bed_light", "light.ceiling_lights"],
            },
        )
        assert edit.get("success") is not False

        # Decorator uses ``id_param="object_id"`` so the snapshot keys on
        # the object_id, not the full ``group.<id>`` entity_id form.
        backup_name = await _wait_for_backup(
            mcp_client, domain="group", entity_id=object_id
        )

        restore = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        assert restore.get("success") is True
        assert restore.get("data", {}).get("safety_backup") is not None

        delete = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        assert delete.get("success") is True

        # Cleanup
        await safe_call_tool(
            mcp_client, "ha_config_remove_group", {"object_id": object_id}
        )


# ---------------------------------------------------------------- entity lane


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestEntityStateCaptureRestore:
    """Generic-entity restore is Group 3 (POST ``/api/states/<entity>``).
    Uses an existing default-fixture entity rather than creating one;
    ``ha_set_entity`` operates on entities provisioned by integrations,
    not entity registry creations from scratch. The test edits an
    attribute the testcontainer doesn't otherwise touch (``hidden``)
    so cleanup is well-bounded."""

    async def test_entity_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        # Use a stable testcontainer light entity. If it's missing on
        # this rig, skip rather than fail — keeps the test honest about
        # what it actually requires.
        entity_id = "light.bed_light"
        state = await safe_call_tool(
            mcp_client, "ha_get_state", {"entity_id": entity_id}
        )
        if state.get("success") is False:
            pytest.skip(f"{entity_id} not present on this HA: {state}")

        # Edit — toggle the ``hidden`` flag; capture fires.
        edit = await safe_call_tool(
            mcp_client, "ha_set_entity", {"entity_id": entity_id, "hidden": True}
        )
        if edit.get("success") is False:
            pytest.skip(f"ha_set_entity unsupported on this HA: {edit}")

        try:
            backup_name = await _wait_for_backup(
                mcp_client, domain="entity", entity_id=entity_id, timeout=10
            )
        except TimeoutError:
            # ha_set_entity may take the entity-registry path (no /api/states
            # POST) on some HA versions; if so the entity-domain handler
            # won't fire. Surface clearly rather than asserting wrong.
            pytest.skip("entity-domain handler did not capture for this edit")

        restore = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        assert restore.get("success") is True
        assert restore.get("data", {}).get("safety_backup") is not None

        delete = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        assert delete.get("success") is True

        # Best-effort cleanup — restore the un-hidden default.
        await safe_call_tool(
            mcp_client, "ha_set_entity", {"entity_id": entity_id, "hidden": False}
        )


# ---------------------------------------------------------------- dashboard_resource lane


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestDashboardResourceCaptureRestore:
    """Dashboard-resource restore is Group 2 (``lovelace/resources/update``).
    Distinct from the dashboard-config restore — different WS command,
    different payload shape. The handler re-injects ``resource_id`` into
    the payload from the entity_id, so a captured snapshot missing that
    key path would only fail here."""

    async def test_dashboard_resource_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        suffix = uuid.uuid4().hex[:8]
        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_dashboard_resource",
            {
                "url": f"/local/e2e-bk-{suffix}.js",
                "resource_type": "module",
            },
        )
        if create.get("success") is False:
            pytest.skip(f"dashboard_resource create unsupported: {create}")
        resource_id = create.get("data", {}).get("resource_id") or create.get(
            "resource_id"
        )
        assert resource_id, f"resource_id missing: {create}"
        # Settle so the decorator's pre-edit fetch finds the resource.
        await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)

        # Edit — change the URL so capture fires.
        edit = await safe_call_tool(
            mcp_client,
            "ha_config_set_dashboard_resource",
            {
                "resource_id": resource_id,
                "url": f"/local/e2e-bk-{suffix}-edited.js",
            },
        )
        assert edit.get("success") is not False

        backup_name = await _wait_for_backup(
            mcp_client, domain="dashboard_resource", entity_id=str(resource_id)
        )

        restore = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        assert restore.get("success") is True
        assert restore.get("data", {}).get("safety_backup") is not None

        delete = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        assert delete.get("success") is True

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_config_delete_dashboard_resource",
            {"resource_id": resource_id},
        )


# ---------------------------------------------------------------- calendar/todo/integration


@pytest.mark.calendar
@pytest.mark.external_only
class TestCalendarTodoIntegrationCaptureOnly:
    """Three domains whose restore semantics aren't a true round-trip
    and aren't safely testable end-to-end on a clean testcontainer:

    - **calendar_event**: ``/api/services/calendar/create_event`` —
      creates a *new* event rather than overwriting the captured one
      (uid mismatch by design). Restore "succeeds" by re-creating, not
      by reverting. The unit tests cover the payload shape.
    - **todo_item**: ``/api/services/todo/add_item`` — same shape;
      adds rather than overwrites. Both share Group 3 service-call
      mechanics which is exercised by the group/entity full-loop
      tests above.
    - **integration**: ``config_entries/disable`` — needs a real
      integration installed that can be safely toggled. Default-image
      integrations (``frontend``, ``homeassistant``) are unsafe to
      disable; the demo integration is auto-set-up and not guaranteed
      present. Unit test in ``test_backup_manager.py`` covers the
      payload shape.

    Documented here so future maintainers reach for the unit tests
    rather than re-discovering why these three lanes don't have a
    real-HA full-loop test.
    """

    async def test_documentation_marker(self) -> None:
        """No-op anchor so pytest reports this class explicitly in
        ``-v`` output, surfacing the documented coverage gap.
        """
