"""Unit tests for the post-timeout backup-list check in `_poll_backup_completion`.

Regression coverage for #1433: the polling loop used to raise `TIMEOUT_OPERATION`
unconditionally when it didn't observe `state=idle` + `event_state=completed`
within `max_wait_seconds`, even when the backup actually completed (just slower
than the poll window). The fix performs one final `backup/info` lookup before
raising and returns the canonical success-shape (with a `warnings` entry) if a
backup matching the requested name is present.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tools.backup import (
    _build_success_response_if_found,
    _poll_backup_completion,
)


def _ws_client(*responses: dict) -> AsyncMock:
    """Build a mock WS client whose `send_command` returns each response in turn."""
    ws = AsyncMock()
    ws.send_command.side_effect = list(responses)
    return ws


def _backup_info(state: str, event_state: str | None, backups: list[dict]) -> dict:
    """Compose a `backup/info` response body."""
    last_event = {"state": event_state} if event_state else {}
    return {
        "success": True,
        "result": {
            "state": state,
            "last_action_event": last_event,
            "backups": backups,
        },
    }


def _backup_entry(
    name: str, *, backup_id: str = "abc123", agent_id: str = "backup.local"
) -> dict:
    """Compose one entry of `result.backups` matching the shape HA returns."""
    return {
        "backup_id": backup_id,
        "name": name,
        "date": "2026-05-24T20:00:00Z",
        "agents": {agent_id: {"size": 12345}},
    }


class TestBuildSuccessResponseIfFound:
    def test_returns_none_when_name_not_in_backups(self):
        info = _backup_info("idle", "completed", [_backup_entry("Other_Backup")])
        assert (
            _build_success_response_if_found(
                info,
                name="Looking_For_This",
                backup_job_id="job-1",
                agent_id="backup.local",
                duration_seconds=10,
            )
            is None
        )

    def test_returns_success_shape_when_name_matches(self):
        info = _backup_info(
            "idle", "completed", [_backup_entry("My_Backup", backup_id="xyz")]
        )
        result = _build_success_response_if_found(
            info,
            name="My_Backup",
            backup_job_id="job-1",
            agent_id="backup.local",
            duration_seconds=42,
        )
        assert result == {
            "success": True,
            "backup_id": "xyz",
            "backup_job_id": "job-1",
            "name": "My_Backup",
            "date": "2026-05-24T20:00:00Z",
            "size_bytes": 12345,
            "status": "Backup completed successfully",
            "duration_seconds": 42,
            "note": "Backup uses your Home Assistant's default backup password",
        }

    def test_returns_none_on_empty_backups_list(self):
        info = _backup_info("idle", "completed", [])
        assert (
            _build_success_response_if_found(
                info,
                name="My_Backup",
                backup_job_id="job-1",
                agent_id="backup.local",
                duration_seconds=10,
            )
            is None
        )


class TestPollBackupCompletionPostTimeout:
    @pytest.mark.asyncio
    async def test_post_timeout_finds_backup_returns_success_with_warning(self):
        """Polling loop exits without observing idle+completed; final lookup
        finds the backup → return success with a `warnings` entry."""
        # max_wait_seconds=0 skips the loop entirely; the only send_command
        # call is the post-timeout final lookup.
        ws = _ws_client(
            _backup_info("idle", "completed", [_backup_entry("Slow_Backup")])
        )
        result = await _poll_backup_completion(
            ws,
            name="Slow_Backup",
            backup_job_id="job-1",
            max_wait_seconds=0,
            poll_interval=1,
            agent_id="backup.local",
        )
        assert result["success"] is True
        assert result["backup_id"] == "abc123"
        assert result["name"] == "Slow_Backup"
        assert result["duration_seconds"] == 0
        assert "warnings" in result
        assert isinstance(result["warnings"], list)
        assert any("poll window" in w for w in result["warnings"])
        ws.send_command.assert_awaited_once_with("backup/info")

    @pytest.mark.asyncio
    async def test_post_timeout_no_backup_raises_timeout_operation(self):
        """Polling loop exits, final lookup returns no matching backup →
        TIMEOUT_OPERATION as before."""
        ws = _ws_client(_backup_info("idle", "completed", []))
        with pytest.raises(ToolError) as exc_info:
            await _poll_backup_completion(
                ws,
                name="Missing_Backup",
                backup_job_id="job-1",
                max_wait_seconds=0,
                poll_interval=1,
                agent_id="backup.local",
            )
        # ToolError wraps the structured error JSON in `str(exc)`.
        assert "TIMEOUT_OPERATION" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_post_timeout_final_info_call_raises_falls_through_to_timeout(self):
        """If the final `backup/info` call raises (HA WS error, dropped
        connection, etc.), fall through to TIMEOUT_OPERATION rather than
        propagating an unrelated error that would mask the original timeout."""
        ws = AsyncMock()
        ws.send_command.side_effect = HomeAssistantCommandError("ws closed")
        with pytest.raises(ToolError) as exc_info:
            await _poll_backup_completion(
                ws,
                name="Any_Backup",
                backup_job_id="job-1",
                max_wait_seconds=0,
                poll_interval=1,
                agent_id="backup.local",
            )
        assert "TIMEOUT_OPERATION" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_in_loop_success_path_still_works_after_refactor(self):
        """Regression: the in-loop branch routes through the same helper as
        the post-timeout path; first idle+completed observation returns
        success WITHOUT a `warnings` entry."""
        ws = _ws_client(
            _backup_info("idle", "completed", [_backup_entry("Fast_Backup")])
        )
        with patch("ha_mcp.tools.backup.asyncio.sleep", new=AsyncMock()):
            result = await _poll_backup_completion(
                ws,
                name="Fast_Backup",
                backup_job_id="job-1",
                max_wait_seconds=10,
                poll_interval=2,
                agent_id="backup.local",
            )
        assert result["success"] is True
        assert result["backup_id"] == "abc123"
        assert result["duration_seconds"] == 2
        assert "warnings" not in result
        # In-loop success returns on the first poll; no post-timeout call.
        ws.send_command.assert_awaited_once_with("backup/info")
