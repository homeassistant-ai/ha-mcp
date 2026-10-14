"""
E2E: a deleted dashboard resource is restored from its edit backup (#2632).

Core's ``lovelace/resources/update`` answers ``not_found`` for a deleted id,
so the restore re-creates the resource with ``lovelace/resources/create``.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from ha_mcp._vendor.fastmcp import Client

from ...utilities.assertions import MCPAssertions, safe_call_tool
from .test_capture_and_restore import (
    _HA_PROPAGATION_SETTLE_SECONDS,
    _enable_auto_backup,
    _wait_for_backup,
)


@pytest.mark.cleanup
@pytest.mark.external_only
async def test_deleted_dashboard_resource_is_recreated_by_restore(
    mcp_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_auto_backup(monkeypatch)
    # css, not the default module, so a restore that drops res_type shows up.
    url = f"/local/e2e-bk-deleted-{uuid.uuid4().hex[:8]}.css"
    backup_name = None
    restored_ids: list[str] = []
    async with MCPAssertions(mcp_client) as mcp:
        try:
            created = await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {"url": url, "resource_type": "css"},
            )
            resource_id = str(created["resource_id"])
            # Settle so the decorator's pre-delete fetch finds the resource.
            await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)

            await mcp.call_tool_success(
                "ha_config_delete_dashboard_resource", {"resource_id": resource_id}
            )
            backup_name = await _wait_for_backup(
                mcp_client, domain="dashboard_resource", entity_id=resource_id
            )

            await mcp.call_tool_success(
                "ha_manage_backup",
                {"scope": "edits", "action": "restore", "backup_name": backup_name},
            )

            listed = await mcp.call_tool_success(
                "ha_config_list_dashboard_resources", {"limit": 500}
            )
            matches = [r for r in listed["resources"] if r.get("url") == url]
            restored_ids = [str(r["id"]) for r in matches]
            assert len(matches) == 1, f"expected one restored resource: {matches}"
            assert matches[0]["type"] == "css"
        finally:
            for restored_id in restored_ids:
                await safe_call_tool(
                    mcp_client,
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": restored_id},
                )
            if backup_name:
                await safe_call_tool(
                    mcp_client,
                    "ha_manage_backup",
                    {"scope": "edits", "action": "delete", "backup_name": backup_name},
                )
