"""Unit tests for `_get_local_backup_agent_id` in the backup tools module.

Regression coverage for the hardcoded `hassio.local` bug that broke `ha_backup_*`
tools on HA Core installs (which only register `backup.local`).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.backup import _get_local_backup_agent_id, restore_backup


def _ws_client(agents_payload: dict) -> AsyncMock:
    """Build a mock WS client whose `send_command("backup/agents/info")` returns the given payload."""
    ws = AsyncMock()
    ws.send_command.return_value = agents_payload
    return ws


class TestGetLocalBackupAgentId:
    @pytest.mark.asyncio
    async def test_core_only_returns_backup_local(self):
        """HA Core install (only `backup.local` registered) returns `backup.local`."""
        ws = _ws_client({
            "success": True,
            "result": {"agents": [{"agent_id": "backup.local", "name": "local"}]},
        })
        assert await _get_local_backup_agent_id(ws) == "backup.local"

    @pytest.mark.asyncio
    async def test_supervised_only_returns_hassio_local(self):
        """HA Supervised install (only `hassio.local` registered) returns `hassio.local`."""
        ws = _ws_client({
            "success": True,
            "result": {"agents": [{"agent_id": "hassio.local", "name": "local"}]},
        })
        assert await _get_local_backup_agent_id(ws) == "hassio.local"

    @pytest.mark.asyncio
    async def test_both_present_prefers_hassio_local(self):
        """When both agents are registered, prefer `hassio.local` (Supervisor)."""
        ws = _ws_client({
            "success": True,
            "result": {
                "agents": [
                    {"agent_id": "backup.local", "name": "local"},
                    {"agent_id": "hassio.local", "name": "local"},
                ],
            },
        })
        assert await _get_local_backup_agent_id(ws) == "hassio.local"

    @pytest.mark.asyncio
    async def test_only_remote_agents_raises(self):
        """No agent named `local` raises `ToolError` listing the available agents."""
        ws = _ws_client({
            "success": True,
            "result": {"agents": [{"agent_id": "google_drive.cloud", "name": "Google Drive"}]},
        })
        with pytest.raises(ToolError) as exc_info:
            await _get_local_backup_agent_id(ws)
        error = json.loads(str(exc_info.value))
        assert "No local backup agent found" in error["error"]["message"]
        assert error["available_agents"] == ["google_drive.cloud"]

    @pytest.mark.asyncio
    async def test_empty_agent_list_raises(self):
        """Empty agent list raises `ToolError` with an actionable suggestion."""
        ws = _ws_client({"success": True, "result": {"agents": []}})
        with pytest.raises(ToolError) as exc_info:
            await _get_local_backup_agent_id(ws)
        error = json.loads(str(exc_info.value))
        assert "No backup agents registered" in error["error"]["message"]

    @pytest.mark.asyncio
    async def test_send_command_failure_raises(self):
        """A `success=False` response from HA raises `ToolError`."""
        ws = _ws_client({"success": False, "error": "WS error"})
        with pytest.raises(ToolError) as exc_info:
            await _get_local_backup_agent_id(ws)
        error = json.loads(str(exc_info.value))
        assert "Failed to enumerate backup agents" in error["error"]["message"]

    @pytest.mark.asyncio
    async def test_malformed_local_entry_filtered_out(self):
        """An agent with `name=local` but missing `agent_id` is filtered, not returned as None."""
        ws = _ws_client({
            "success": True,
            "result": {
                "agents": [
                    {"name": "local"},  # malformed — no agent_id
                    {"agent_id": "backup.local", "name": "local"},
                ],
            },
        })
        assert await _get_local_backup_agent_id(ws) == "backup.local"

    @pytest.mark.asyncio
    async def test_only_malformed_local_entry_raises(self):
        """If the only `name=local` entry is malformed, raise rather than return None."""
        ws = _ws_client({
            "success": True,
            "result": {"agents": [{"name": "local"}]},
        })
        with pytest.raises(ToolError) as exc_info:
            await _get_local_backup_agent_id(ws)
        error = json.loads(str(exc_info.value))
        assert "No local backup agent found" in error["error"]["message"]


class TestRestoreBackupWarnings:
    """Pin the post-#1332 warnings-list contract on the restore_backup
    success path (``backup.py`` ~L447-457). Pre-#1332 emitted singular
    ``warning``; the migrated shape is ``warnings: list[str]`` containing
    the connection-lost-during-restart notice.
    """

    @pytest.mark.asyncio
    async def test_success_returns_top_level_warnings_list(self):
        ws = AsyncMock()
        ws.send_command.side_effect = [
            # backup/info — verify backup exists
            {"success": True, "result": {"backups": [{"backup_id": "abc123"}]}},
            # _get_local_backup_agent_id → backup/agents/info
            {"success": True, "result": {"agents": [{"agent_id": "backup.local", "name": "local"}]}},
            # _get_backup_password → backup/config/info
            {"success": True, "result": {"config": {"create_backup": {"password": "pw"}}}},
            # _create_safety_backup → backup/generate
            {"success": True, "result": {"backup_job_id": "safety_job_1"}},
            # _create_safety_backup polling → backup/info loop
            {"success": True, "result": {"backups": [{"name": "Pre_Restore_Safety", "backup_id": "safety_xyz"}]}},
            # backup/restore — the actual restore call
            {"success": True},
        ]

        client = MagicMock()
        client.base_url = "http://test"
        client.token = "token"
        client.verify_ssl = False

        with patch(
            "ha_mcp.tools.backup.get_connected_ws_client",
            new=AsyncMock(return_value=(ws, None)),
        ):
            result = await restore_backup(client, "abc123")

        assert result["success"] is True
        assert result["backup_id"] == "abc123"
        warnings = result.get("warnings")
        assert isinstance(warnings, list) and warnings, (
            f"Expected non-empty warnings list, got: {result!r}"
        )
        assert any("Connection will be temporarily lost" in w for w in warnings), (
            f"Expected connection-lost warning content; got: {warnings!r}"
        )
