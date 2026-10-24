"""E2E: a group edit backup restores the group's name, icon and members (#2632)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.wait_helpers import wait_for_tool_result
from .test_capture_and_restore import _enable_auto_backup, _wait_for_backup


async def _wait_for_group(
    mcp_client, object_id: str, description: str, **expected: Any
) -> None:
    """Poll ``ha_config_list_groups`` until the group shows every expected field.

    ``group.set`` registers the group state asynchronously, so one read is racy.
    """
    await wait_for_tool_result(
        mcp_client,
        tool_name="ha_config_list_groups",
        arguments={},
        predicate=lambda d: any(
            g.get("object_id") == object_id
            and all(g.get(key) == value for key, value in expected.items())
            for g in d.get("groups", [])
        ),
        description=description,
        timeout=20,
    )


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestGroupCaptureRestore:
    """Group restore is a WebSocket ``call_service`` of group.set, unlike the
    WS-update lanes in ``test_capture_and_restore.py``."""

    async def test_group_restore_brings_back_pre_edit_name_icon_and_members(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        suffix = uuid.uuid4().hex[:8]
        object_id = f"e2e_bk_grp_{suffix}"
        original = {
            "friendly_name": f"E2E Backup Group {suffix}",
            "icon": "mdi:lightbulb-group",
            "entity_ids": ["light.bed_light"],
        }
        edited = {
            "friendly_name": f"E2E Backup Group {suffix} edited",
            "icon": "mdi:lamp",
            "entity_ids": ["light.bed_light", "light.ceiling_lights"],
        }
        backup_name = None
        async with MCPAssertions(mcp_client) as mcp:
            try:
                await mcp.call_tool_success(
                    "ha_config_set_group",
                    {
                        "object_id": object_id,
                        "name": original["friendly_name"],
                        "icon": original["icon"],
                        "entities": original["entity_ids"],
                    },
                )
                # The edit's pre-write fetch takes no snapshot of a group it
                # cannot see yet.
                await _wait_for_group(
                    mcp_client, object_id, f"group {object_id} created", **original
                )

                await mcp.call_tool_success(
                    "ha_config_set_group",
                    {
                        "object_id": object_id,
                        "name": edited["friendly_name"],
                        "icon": edited["icon"],
                        "entities": edited["entity_ids"],
                    },
                )
                # An edit that never landed would let the read-back below pass
                # on unchanged state.
                await _wait_for_group(
                    mcp_client, object_id, f"group {object_id} edited", **edited
                )

                # Decorator uses ``id_param="object_id"`` so the snapshot keys on
                # the object_id, not the full ``group.<id>`` entity_id form.
                backup_name = await _wait_for_backup(
                    mcp_client, domain="group", entity_id=object_id
                )

                restored = await mcp.call_tool_success(
                    "ha_manage_backup",
                    {"scope": "edits", "action": "restore", "backup_name": backup_name},
                )
                assert restored.get("data", {}).get("safety_backup") is not None
                # A restore that dropped a field or reapplied the edit would
                # still report success.
                await _wait_for_group(
                    mcp_client,
                    object_id,
                    f"group {object_id} back to its pre-edit fields",
                    **original,
                )

                await mcp.call_tool_success(
                    "ha_manage_backup",
                    {"scope": "edits", "action": "delete", "backup_name": backup_name},
                )
                backup_name = None
            finally:
                await safe_call_tool(
                    mcp_client, "ha_config_remove_group", {"object_id": object_id}
                )
                if backup_name:
                    await safe_call_tool(
                        mcp_client,
                        "ha_manage_backup",
                        {
                            "scope": "edits",
                            "action": "delete",
                            "backup_name": backup_name,
                        },
                    )
