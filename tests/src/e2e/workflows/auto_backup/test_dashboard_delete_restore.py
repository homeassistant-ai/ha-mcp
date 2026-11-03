"""
E2E: a dashboard's snapshot restores its settings, and a deleted dashboard (#2632).

Core's ``lovelace/config/save`` answers ``config_not_found`` for a deleted
dashboard, so the restore re-creates it from the settings row in the snapshot
before saving the config; a dashboard that still exists gets its settings back
through ``lovelace/dashboards/update``.
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
async def test_deleted_dashboard_is_recreated_by_restore(
    mcp_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_auto_backup(monkeypatch)
    url_path = f"e2e-bk-deleted-{uuid.uuid4().hex[:8]}"
    config = {"views": [{"title": "Restored", "cards": []}]}
    backup_name = None
    async with MCPAssertions(mcp_client) as mcp:
        try:
            await mcp.call_tool_success(
                "ha_config_set_dashboard",
                {
                    "url_path": url_path,
                    "title": "E2E Deleted Backup",
                    "icon": "mdi:backup-restore",
                    "config": config,
                    "MandatoryBPS": False,
                },
            )
            # Settle so the decorator's pre-delete fetch finds the dashboard.
            await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)

            await mcp.call_tool_success(
                "ha_config_delete_dashboard", {"url_path": url_path}
            )
            backup_name = await _wait_for_backup(
                mcp_client, domain="dashboard", entity_id=url_path
            )

            await mcp.call_tool_success(
                "ha_manage_backup",
                {"scope": "edits", "action": "restore", "backup_name": backup_name},
            )

            restored = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"url_path": url_path}
            )
            assert restored["config"] == config
            listed = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"list_only": True}
            )
            row = next(r for r in listed["dashboards"] if r["url_path"] == url_path)
            assert (row["title"], row["icon"]) == (
                "E2E Deleted Backup",
                "mdi:backup-restore",
            )
        finally:
            await safe_call_tool(
                mcp_client, "ha_config_delete_dashboard", {"url_path": url_path}
            )
            if backup_name:
                await safe_call_tool(
                    mcp_client,
                    "ha_manage_backup",
                    {"scope": "edits", "action": "delete", "backup_name": backup_name},
                )


@pytest.mark.cleanup
@pytest.mark.external_only
async def test_changed_dashboard_settings_are_put_back_by_restore(
    mcp_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settings snapshot restores every setting Core's update accepts."""
    _enable_auto_backup(monkeypatch)
    url_path = f"e2e-bk-settings-{uuid.uuid4().hex[:8]}"
    backup_name = None
    async with MCPAssertions(mcp_client) as mcp:
        try:
            await mcp.call_tool_success(
                "ha_config_set_dashboard",
                {
                    "url_path": url_path,
                    "title": "E2E Settings",
                    "icon": "mdi:cog",
                    "config": {"views": []},
                    "MandatoryBPS": False,
                },
            )
            await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)
            await mcp.call_tool_success(
                "ha_config_set_dashboard",
                {
                    "url_path": url_path,
                    "title": "E2E Settings Renamed",
                    "icon": "mdi:fridge",
                    "require_admin": True,
                    "MandatoryBPS": False,
                },
            )
            backup_name = await _wait_for_backup(
                mcp_client, domain="dashboard", entity_id=url_path
            )

            await mcp.call_tool_success(
                "ha_manage_backup",
                {"scope": "edits", "action": "restore", "backup_name": backup_name},
            )

            listed = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"list_only": True}
            )
            row = next(r for r in listed["dashboards"] if r["url_path"] == url_path)
            assert (row["title"], row["icon"], row["require_admin"]) == (
                "E2E Settings",
                "mdi:cog",
                False,
            )
        finally:
            await safe_call_tool(
                mcp_client, "ha_config_delete_dashboard", {"url_path": url_path}
            )
            if backup_name:
                await safe_call_tool(
                    mcp_client,
                    "ha_manage_backup",
                    {"scope": "edits", "action": "delete", "backup_name": backup_name},
                )
