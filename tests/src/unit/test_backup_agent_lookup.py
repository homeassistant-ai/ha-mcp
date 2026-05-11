"""Unit tests for `_get_local_backup_agent_id` in the backup tools module.

Regression coverage for the hardcoded `hassio.local` bug that broke `ha_backup_*`
tools on HA Core installs (which only register `backup.local`).
"""

import json
from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.backup import _get_local_backup_agent_id


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
