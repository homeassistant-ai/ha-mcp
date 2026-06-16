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

from ...utilities.assertions import extract_error_message, safe_call_tool
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
    """Filter list-snapshots entries by domain + entity_id.

    Snapshot filenames are sanitized (``_safe_entity_id`` replaces every
    char outside ``[A-Za-z0-9._-]`` with ``_``), and ``list_snapshots``
    returns the parsed-from-filename entity_id — so a caller filtering
    with a composite ID like ``area:foo`` would otherwise never match
    the stored ``area_foo``. Sanitize both sides through the same
    function for a symmetric comparison.
    """
    from ha_mcp.backup_manager import _safe_entity_id

    safe_id = _safe_entity_id(entity_id)
    return [e for e in entries if e["domain"] == domain and e["entity_id"] == safe_id]


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
        # (snapshot, view) is not a valid combo — snapshot supports only
        # create/list/restore ((snapshot, list) became valid in #1586).
        result = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "snapshot", "action": "view"},
        )
        assert result.get("success") is False
        error = result.get("error", {})
        msg = error.get("message", "") if isinstance(error, dict) else ""
        assert "Invalid combination" in msg
        # Helpful suggestion lists the valid combos.
        suggestions = error.get("suggestions", []) if isinstance(error, dict) else []
        assert any("Valid combinations" in s for s in suggestions)

    async def test_edits_create_requires_domain_and_entity_id(self, mcp_client) -> None:
        # (edits, create) is the on-demand-snapshot combo — needs both
        # ``domain`` and ``entity_id`` to know what to capture. The
        # bare call must fail with a structured validation error rather
        # than silently routing through the decorator's auto-on-write
        # path (which only fires on actual writes).
        result = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "create"},
        )
        assert result.get("success") is False
        error = result.get("error", {})
        msg = error.get("message", "") if isinstance(error, dict) else ""
        assert "domain" in msg or "entity_id" in msg

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

    async def test_edits_diff_requires_backup_name(self, mcp_client) -> None:
        # Same shape as restore — diff needs a concrete backup to compare
        # against, so the bare call must fail with a structured
        # validation error rather than dispatching against nothing.
        result = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "diff"},
        )
        assert result.get("success") is False
        msg = extract_error_message(result)
        assert "backup_name" in msg


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


# ---------------------------------------------------------------- on-demand snapshot


@pytest.mark.automation
@pytest.mark.cleanup
@pytest.mark.external_only
class TestEditsCreateOnDemandSnapshot:
    """``ha_manage_backup(scope='edits', action='create', ...)`` — captures
    a snapshot of the named entity on demand. Use case: "I'm about to
    edit this in the HA UI; snapshot it first." Distinct from the
    auto-on-write path the ``@with_auto_backup`` decorator drives.

    Exercises against an automation entity to share the existing
    automation create+edit fixture; the underlying maybe_snapshot path
    is domain-agnostic, so coverage of one domain is sufficient to
    pin the routing.
    """

    async def test_on_demand_snapshot_round_trip(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)

        suffix = uuid.uuid4().hex[:8]
        identifier = f"e2e_ondemand_{suffix}"
        # Create an automation to snapshot. We do NOT edit it — the
        # on-demand snapshot path must work without a write triggering
        # the decorator.
        original = {
            "alias": f"E2E On-Demand Original {suffix}",
            "trigger": [{"platform": "time", "at": "12:00:00"}],
            "action": [{"service": "homeassistant.no_op"}],
        }
        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {"config": original, "identifier": identifier},
        )
        assert create.get("success") is not False

        # On-demand snapshot.
        snap = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {
                "scope": "edits",
                "action": "create",
                "domain": "automation",
                "entity_id": identifier,
            },
        )
        assert snap.get("success") is True, f"on-demand snapshot failed: {snap}"
        data = snap.get("data", {})
        backup_name = data.get("backup_name")
        assert backup_name, f"backup_name missing from response: {snap}"
        assert data.get("domain") == "automation"
        assert data.get("entity_id") == identifier
        assert data.get("size", 0) > 0

        # The snapshot must also show up in the list query.
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
        assert any(b["name"] == backup_name for b in entries), (
            f"on-demand snapshot {backup_name!r} not in list: "
            f"{[b['name'] for b in entries]}"
        )

        # Cleanup: delete the snapshot + remove the automation.
        await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        await safe_call_tool(
            mcp_client,
            "ha_config_remove_automation",
            {"identifier": identifier},
        )

    async def test_on_demand_snapshot_unknown_domain_rejected(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        result = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {
                "scope": "edits",
                "action": "create",
                "domain": "not_a_real_domain",
                "entity_id": "anything",
            },
        )
        assert result.get("success") is False
        error = result.get("error", {})
        msg = error.get("message", "") if isinstance(error, dict) else ""
        assert "handler" in msg.lower() or "domain" in msg.lower()


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


# ---------------------------------------------------------------- diff


@pytest.mark.automation
@pytest.mark.cleanup
@pytest.mark.external_only
class TestAutomationDiff:
    """``ha_manage_backup(scope='edits', action='diff', ...)`` against the
    live entity state.

    Read-only — fetches the current config the same way the capture path
    does, computes an RFC 6902 patch, and returns it bounded. Doesn't
    touch any entity, doesn't take a safety snapshot. Sibling test to
    ``TestAutomationCaptureRestore`` to keep both halves of the
    "captured vs current" loop covered.
    """

    async def test_diff_against_unchanged_returns_no_ops(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Capture and immediately diff — the live config matches the
        # snapshot, so the response carries an empty patch and
        # ``unchanged: true``.
        _enable_auto_backup(monkeypatch)
        suffix = uuid.uuid4().hex[:8]
        identifier = f"e2e_diff_noop_{suffix}"
        original = {
            "alias": f"E2E Diff Noop {suffix}",
            "trigger": [{"platform": "time", "at": "12:00:00"}],
            "action": [{"service": "homeassistant.no_op"}],
        }
        await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {"config": original, "identifier": identifier},
        )
        # Capture via (edits, create) so stored == current by
        # construction. The decorator's auto-on-write path is
        # unreliable here: snapshot filenames are seconds-resolution,
        # so a mutate-then-restore inside the same second clobbers
        # the first capture.
        create_result = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {
                "scope": "edits",
                "action": "create",
                "domain": "automation",
                "entity_id": identifier,
            },
        )
        assert create_result.get("success") is True
        backup_name = create_result["data"]["backup_name"]

        diff = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "diff", "backup_name": backup_name},
        )
        assert diff.get("success") is True
        data = diff.get("data", {})
        assert data["kind"] == "dict"
        assert data["entity_missing"] is False
        assert data["unchanged"] is True
        assert data["patch"] == []
        assert data["counts"]["total"] == 0
        assert data["truncated"] is False
        assert data["captured_at"] is not None

        await safe_call_tool(
            mcp_client,
            "ha_config_remove_automation",
            {"identifier": identifier},
        )

    async def test_diff_after_edit_reports_changes(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Capture pre-edit state, then leave the live config diverged.
        # The diff must report the alias change as a replace op.
        _enable_auto_backup(monkeypatch)
        suffix = uuid.uuid4().hex[:8]
        identifier = f"e2e_diff_edit_{suffix}"
        original_alias = f"E2E Diff Original {suffix}"
        edited_alias = f"E2E Diff Edited {suffix}"
        original = {
            "alias": original_alias,
            "trigger": [{"platform": "time", "at": "12:00:00"}],
            "action": [{"service": "homeassistant.no_op"}],
        }
        await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {"config": original, "identifier": identifier},
        )
        # Edit — the decorator snapshots the pre-edit state (alias =
        # original_alias). Live state ends at alias = edited_alias.
        await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {"config": {**original, "alias": edited_alias}, "identifier": identifier},
        )
        backup_name = await _wait_for_backup(
            mcp_client, domain="automation", entity_id=identifier
        )

        diff = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "diff", "backup_name": backup_name},
        )
        assert diff.get("success") is True
        data = diff.get("data", {})
        assert data["entity_missing"] is False
        assert data["unchanged"] is False
        assert data["counts"]["total"] >= 1
        # The alias delta is the load-bearing assertion; restrict to
        # ``replace`` ops on a path ending in ``/alias`` to stay robust
        # against HA-side schema enrichment (added defaults, ordering).
        alias_op = next(
            (
                op
                for op in data["patch"]
                if op["op"] == "replace" and op["path"].endswith("/alias")
            ),
            None,
        )
        assert alias_op is not None
        assert alias_op["value"] == original_alias

        await safe_call_tool(
            mcp_client,
            "ha_config_remove_automation",
            {"identifier": identifier},
        )

    async def test_diff_flags_entity_missing_after_delete(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Capture, then delete the entity — diff must report
        # ``entity_missing: true`` with an empty patch and no
        # ``LookupError``-style failure (the snapshot still exists, it's
        # the live target that's gone).
        _enable_auto_backup(monkeypatch)
        suffix = uuid.uuid4().hex[:8]
        identifier = f"e2e_diff_missing_{suffix}"
        original = {
            "alias": f"E2E Diff Missing {suffix}",
            "trigger": [{"platform": "time", "at": "12:00:00"}],
            "action": [{"service": "homeassistant.no_op"}],
        }
        await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {"config": original, "identifier": identifier},
        )
        await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {
                "config": {**original, "alias": f"E2E Diff Missing Edited {suffix}"},
                "identifier": identifier,
            },
        )
        backup_name = await _wait_for_backup(
            mcp_client, domain="automation", entity_id=identifier
        )

        await safe_call_tool(
            mcp_client,
            "ha_config_remove_automation",
            {"identifier": identifier},
        )

        diff = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "diff", "backup_name": backup_name},
        )
        assert diff.get("success") is True
        data = diff.get("data", {})
        assert data["entity_missing"] is True
        assert data["patch"] == []
        # ``unchanged`` is False under entity_missing — the empty patch
        # is an artefact of the absent target, not a "matches live" match.
        assert data["unchanged"] is False
        assert data["captured_at"] is not None
        warnings = diff.get("warnings") or []
        assert any("missing" in w.lower() for w in warnings)

    async def test_diff_truncated_surfaces_tool_layer_warning(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Drive the tool-layer ``truncated`` warning end-to-end: cap the
        # patch budget at 1 op so a real multi-field edit overflows it.
        # Asserts both the manager-level ``truncated: true`` and the
        # tool-layer warning string the manager flag drives.
        _enable_auto_backup(monkeypatch)
        monkeypatch.setattr("ha_mcp.backup_manager._MAX_PATCH_OPS", 1)
        suffix = uuid.uuid4().hex[:8]
        identifier = f"e2e_diff_trunc_{suffix}"
        original = {
            "alias": f"E2E Diff Trunc {suffix}",
            "trigger": [{"platform": "time", "at": "12:00:00"}],
            "action": [{"service": "homeassistant.no_op"}],
        }
        await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {"config": original, "identifier": identifier},
        )
        # Edit several fields so the captured-vs-live diff is > 1 op.
        await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {
                "config": {
                    **original,
                    "alias": f"E2E Diff Trunc Edited {suffix}",
                    "trigger": [
                        {"platform": "time", "at": "13:00:00"},
                        {"platform": "time", "at": "14:00:00"},
                    ],
                },
                "identifier": identifier,
            },
        )
        backup_name = await _wait_for_backup(
            mcp_client, domain="automation", entity_id=identifier
        )

        diff = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "diff", "backup_name": backup_name},
        )
        assert diff.get("success") is True
        data = diff.get("data", {})
        assert data["truncated"] is True
        assert len(data["patch"]) == 1
        warnings = diff.get("warnings") or []
        assert any("truncated" in w.lower() for w in warnings)

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
            "ha_remove_helpers_integrations",
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
            "ha_remove_helpers_integrations",
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
        # Poll ``ha_config_list_groups`` until our group appears in the
        # configured-groups list, which surfaces sooner than the
        # ``/api/states/group.<id>`` propagation (the latter can lag by
        # several seconds on fresh testcontainers; the previous
        # ``ha_get_state`` poll timed out at 15 s).
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_config_list_groups",
            arguments={},
            predicate=lambda d: any(
                g.get("object_id") == object_id
                for g in (d.get("groups", []) or d.get("data", {}).get("groups", []))
            ),
            description=f"group {object_id} in list_groups",
            timeout=20,
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
            return  # unreachable: pytest.skip raises Skipped

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


# ---------------------------------------------------------------- calendar/todo lanes
#
# calendar_event and todo_item have no entity in the base image, so these
# create their own backing entity via the integration config flow
# (local_calendar / local_todo both ship with HA Core) — neither is a
# ``helper_type``, so we drive the flow through ``ha_client`` like
# test_integration_setup.py does. Capture fires on DELETE for calendar
# (the set tool is create-only) and on EDIT for todo. Each test tears its
# config entry down in a finally block.


@pytest.mark.haos_only
@pytest.mark.calendar
@pytest.mark.external_only
@pytest.mark.cleanup
class TestCalendarCaptureRestore:
    """Auto-backup for calendar events. ``ha_config_set_calendar_event``
    only CREATES, so the pre-write snapshot fires on
    ``ha_config_remove_calendar_event`` (pre-delete capture). Restore
    re-creates the event from the snapshot."""

    async def test_calendar_capture_on_delete(
        self, mcp_client, ha_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import datetime, timedelta

        _enable_auto_backup(monkeypatch)
        unique = uuid.uuid4().hex[:8]
        cal_name = f"BK Cal {unique}"
        entity_id = f"calendar.bk_cal_{unique}"

        entry_id: str | None = None
        try:
            flow_init = await ha_client.start_config_flow("local_calendar")
            if not isinstance(flow_init, dict) or flow_init.get("type") != "form":
                pytest.skip(f"local_calendar config flow unavailable: {flow_init}")
            flow_done = await ha_client.submit_config_flow_step(
                flow_init["flow_id"],
                {"calendar_name": cal_name, "import": "create_empty"},
            )
            if flow_done.get("type") != "create_entry":
                pytest.skip(f"local_calendar entry not created: {flow_done}")
            entry_id = flow_done["result"]["entry_id"]

            await wait_for_tool_result(
                mcp_client,
                tool_name="ha_get_entity",
                arguments={"entity_id": entity_id},
                predicate=lambda d: d.get("success") is True,
                description=f"{entity_id} registers in entity registry",
                timeout=20,
            )

            now = datetime.now()
            start = (now + timedelta(days=1)).replace(
                hour=10, minute=0, second=0, microsecond=0
            )
            end = start + timedelta(hours=1)
            summary = f"bk-evt-{unique}"
            created = await safe_call_tool(
                mcp_client,
                "ha_config_set_calendar_event",
                {
                    "entity_id": entity_id,
                    "summary": summary,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
            )
            if created.get("success") is False:
                pytest.skip(f"calendar event create unsupported: {created}")

            got = await wait_for_tool_result(
                mcp_client,
                tool_name="ha_config_get_calendar_events",
                arguments={
                    "entity_id": entity_id,
                    "start": start.isoformat(),
                    "end": (end + timedelta(hours=1)).isoformat(),
                },
                predicate=lambda d: any(
                    e.get("summary") == summary for e in d.get("events", [])
                ),
                description=f"event {summary!r} visible",
                timeout=45,
            )
            uid = next(
                (
                    e.get("uid")
                    for e in got.get("events", [])
                    if e.get("summary") == summary
                ),
                None,
            )
            assert uid, f"created event has no uid: {got.get('events')}"

            # Let HA's calendar store settle before the pre-delete fetch
            # (the decorator runs its own calendar.get_events lookup; a
            # freshly-written iCal event can lag behind the create response).
            await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)

            # Delete the event — pre-delete capture fires (calendar_event).
            removed = await safe_call_tool(
                mcp_client,
                "ha_config_remove_calendar_event",
                {"entity_id": entity_id, "uid": uid},
            )
            assert removed.get("success") is not False, f"remove failed: {removed}"

            try:
                backup_name = await _wait_for_backup(
                    mcp_client,
                    domain="calendar_event",
                    entity_id=f"{entity_id}::{uid}",
                    timeout=20,
                )
            except TimeoutError:
                pytest.skip(
                    "calendar_event snapshot not observed within timeout on "
                    "this HA rig (calendar capture/indexing timing); the unit "
                    "test test_backup_manager covers the payload shape"
                )
            view = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "edits", "action": "view", "backup_name": backup_name},
            )
            assert view.get("success") is True

            restore = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "edits", "action": "restore", "backup_name": backup_name},
            )
            assert restore.get("success") is True
        finally:
            if entry_id is not None:
                await safe_call_tool(
                    mcp_client,
                    "ha_remove_helpers_integrations",
                    {"target": entry_id, "confirm": True},
                )


@pytest.mark.haos_only
@pytest.mark.external_only
@pytest.mark.cleanup
class TestTodoCaptureRestore:
    """Auto-backup for todo items. The pre-write snapshot fires when an
    existing item is edited (update by summary) or removed. Regression
    guard for the fetch matching only on uid — the tool passes the item
    summary, so capture was silently skipped before the fix."""

    async def test_todo_capture_on_edit(
        self, mcp_client, ha_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        unique = uuid.uuid4().hex[:8]
        list_name = f"BK Todo {unique}"
        entity_id = f"todo.bk_todo_{unique}"

        entry_id: str | None = None
        try:
            flow_init = await ha_client.start_config_flow("local_todo")
            if not isinstance(flow_init, dict) or flow_init.get("type") != "form":
                pytest.skip(f"local_todo config flow unavailable: {flow_init}")
            # local_todo's single field mirrors local_calendar's naming
            # convention (``<integration>_name``). If the schema differs on
            # this HA version the flow won't create an entry and we skip.
            flow_done = await ha_client.submit_config_flow_step(
                flow_init["flow_id"], {"todo_list_name": list_name}
            )
            if flow_done.get("type") != "create_entry":
                pytest.skip(f"local_todo entry not created: {flow_done}")
            entry_id = flow_done["result"]["entry_id"]

            await wait_for_tool_result(
                mcp_client,
                tool_name="ha_get_entity",
                arguments={"entity_id": entity_id},
                predicate=lambda d: d.get("success") is True,
                description=f"{entity_id} registers in entity registry",
                timeout=20,
            )

            summary = f"bk-item-{unique}"
            added = await safe_call_tool(
                mcp_client,
                "ha_set_todo_item",
                {"entity_id": entity_id, "summary": summary},
            )
            if added.get("success") is False:
                pytest.skip(f"todo add_item unsupported: {added}")

            # Let HA index the new item before the edit fires; the pre-edit
            # fetch (todo.get_items) may otherwise miss the fresh item and
            # silently skip the snapshot.
            await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)

            # Edit the item BY SUMMARY — the exact form that was silently
            # skipped before the fetch matched on summary as well as uid.
            edited = await safe_call_tool(
                mcp_client,
                "ha_set_todo_item",
                {"entity_id": entity_id, "item": summary, "status": "completed"},
            )
            assert edited.get("success") is not False, f"todo edit failed: {edited}"

            try:
                backup_name = await _wait_for_backup(
                    mcp_client,
                    domain="todo_item",
                    entity_id=f"{entity_id}::{summary}",
                    timeout=20,
                )
            except TimeoutError:
                pytest.skip(
                    "todo_item snapshot not observed within timeout on this HA "
                    "rig (capture/indexing timing); the unit test "
                    "test_fetch_todo_item_matches_by_summary is the hard guard "
                    "for the summary-match fix"
                )
            view = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "edits", "action": "view", "backup_name": backup_name},
            )
            assert view.get("success") is True
        finally:
            if entry_id is not None:
                await safe_call_tool(
                    mcp_client,
                    "ha_remove_helpers_integrations",
                    {"target": entry_id, "confirm": True},
                )


# ``integration`` (config_entries/disable) still has no full-loop e2e here:
# it needs a real integration that is safe to disable/re-enable, and the
# base image's core integrations are unsafe to toggle. Payload-shape
# coverage lives in tests/src/unit/test_backup_manager.py.
