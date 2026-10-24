"""E2E: a group edit backup restores the group's name and members (#2632)."""

from __future__ import annotations

import uuid

import pytest

from ...utilities.assertions import safe_call_tool
from ...utilities.wait_helpers import wait_for_tool_result
from .test_capture_and_restore import _enable_auto_backup, _wait_for_backup


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestGroupCaptureRestore:
    """Group restore is **Group 3** of ``test_capture_and_restore.py``
    (service-call restore: a WebSocket ``call_service`` of group.set).
    Different mechanism from the WS-update family — proves the service-call
    payload shape works end-to-end."""

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

        # The snapshot holds the pre-edit config: a restore that dropped
        # ``entities`` or reapplied the edit would still report success.
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_config_list_groups",
            arguments={},
            predicate=lambda d: any(
                g.get("object_id") == object_id
                and g.get("friendly_name") == f"E2E Backup Group {suffix}"
                and g.get("entity_ids") == ["light.bed_light"]
                for g in (d.get("groups", []) or d.get("data", {}).get("groups", []))
            ),
            description=f"group {object_id} back to its pre-edit name and members",
            timeout=20,
        )

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
