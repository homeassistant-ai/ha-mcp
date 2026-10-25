"""E2E: an entity edit backup restores the entity's registry settings and ID."""

from __future__ import annotations

import uuid

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.wait_helpers import wait_for_tool_result
from .test_capture_and_restore import _enable_auto_backup, _wait_for_backup


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestEntityCaptureRestore:
    """``ha_set_entity`` writes the entity registry, so its backup must bring
    registry settings back. The lane edits a disposable input_boolean rather
    than a demo entity other lanes use, and renames it, which the restore must
    undo through the registry entry's stable id."""

    async def test_restore_renames_back_and_unhides(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        helper_id = f"e2e_bk_ent_{uuid.uuid4().hex[:8]}"
        entity_id = f"input_boolean.{helper_id}"
        renamed = f"{entity_id}_renamed"
        backup_name = None
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_success(
                "ha_config_set_helper",
                {"helper_type": "input_boolean", "name": helper_id},
            )
            try:
                await wait_for_tool_result(
                    mcp_client,
                    tool_name="ha_get_entity",
                    arguments={"entity_id": entity_id},
                    predicate=lambda d: d["entity_entry"]["hidden"] is False,
                    description=f"{entity_id} in the entity registry",
                )
                await mcp.call_tool_success(
                    "ha_set_entity",
                    {"entity_id": entity_id, "hidden": True, "new_entity_id": renamed},
                )
                backup_name = await _wait_for_backup(
                    mcp_client, domain="entity", entity_id=entity_id
                )

                await mcp.call_tool_success(
                    "ha_manage_backup",
                    {"scope": "edits", "action": "restore", "backup_name": backup_name},
                )
                # A restore that only reset the state, as it once did, leaves
                # the entity renamed and hidden: the old ID is then not found.
                restored = await mcp.call_tool_success(
                    "ha_get_entity", {"entity_id": entity_id}
                )
                assert restored["entity_entry"]["hidden"] is False
            finally:
                for target in (entity_id, renamed):
                    await safe_call_tool(
                        mcp_client, "ha_remove_helpers_integrations", {"target": target}
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
