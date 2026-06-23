"""Unit tests for the snapshot-restore path in `backup.py`.

Regression coverage for #1681:

1. **Self-induced deadlock** — `restore_backup` created a pre-restore safety
   backup and issued `backup/restore` back-to-back without waiting. HA's
   `backup/generate` returns once the job is *initiated*, not finished, and the
   backup manager rejects any new operation while a backup runs
   ("Backup manager busy: create_backup"). The restore therefore collided with
   the safety backup the same call had just started. The fix awaits the safety
   backup (via `_poll_backup_completion`) before restoring.

2. **Missing decryption key** — `restore_params` omitted `password`, so a
   `protected: true` backup could not be decrypted on restore even though the
   default password was already fetched for the safety backup. The fix forwards
   that password into the `backup/restore` call when one is available.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.backup import _create_safety_backup, restore_backup


def _scripted_ws(responses: dict[str, Any], call_log: list[str]) -> AsyncMock:
    """Build a mock WS client that maps each WS command to a canned response
    and appends the command name to ``call_log`` in call order.

    A missing command raises, so an unexpected extra call fails loudly instead
    of silently returning a generic success.
    """
    ws = AsyncMock()

    async def _send(command: str, **_kwargs: Any) -> Any:
        call_log.append(command)
        if command not in responses:
            raise AssertionError(f"unexpected WS command: {command!r}")
        return responses[command]

    ws.send_command.side_effect = _send
    ws.disconnect = AsyncMock()
    return ws


def _restore_responses(*, with_password: bool = True) -> dict[str, Any]:
    """Canned WS responses for a full restore_backup run."""
    config_block = {"create_backup": {"password": "pw"}} if with_password else {}
    return {
        # backup/info — backup-exists verification
        "backup/info": {
            "success": True,
            "result": {"backups": [{"backup_id": "target-slug"}]},
        },
        # backup/agents/info — local agent discovery
        "backup/agents/info": {
            "success": True,
            "result": {"agents": [{"agent_id": "backup.local", "name": "local"}]},
        },
        # backup/config/info — default-password lookup
        "backup/config/info": {
            "success": True,
            "result": {"config": config_block},
        },
        # backup/generate — the safety backup
        "backup/generate": {
            "success": True,
            "result": {"backup_job_id": "safety-job-1"},
        },
        # backup/restore — the actual restore
        "backup/restore": {"success": True},
    }


class TestRestoreAwaitsSafetyBackup:
    @pytest.mark.asyncio
    async def test_restore_issued_only_after_safety_backup_completes(self) -> None:
        """The core #1681 regression: `backup/restore` must be sent strictly
        after the safety-backup completion poll returns, never before."""
        call_log: list[str] = []
        ws = _scripted_ws(_restore_responses(), call_log)
        client = MagicMock(base_url="http://ha", token="tok", verify_ssl=True)

        poll_order_marker = AsyncMock(
            side_effect=lambda *a, **k: call_log.append("poll-completed")
        )

        with (
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(ws, None)),
            ),
            patch(
                "ha_mcp.tools.backup._poll_backup_completion",
                new=poll_order_marker,
            ),
        ):
            result = await restore_backup(client, "target-slug", restore_database=True)

        assert result["success"] is True
        # The safety backup was awaited exactly once.
        poll_order_marker.assert_awaited_once()
        # Ordering is the actual fix: poll completion precedes the restore call.
        assert "poll-completed" in call_log
        assert "backup/restore" in call_log
        assert call_log.index("poll-completed") < call_log.index("backup/restore")

    @pytest.mark.asyncio
    async def test_restore_aborts_when_safety_backup_times_out(self) -> None:
        """If the safety backup never completes, `_poll_backup_completion`
        raises and the restore must NOT be issued — better to abort than
        restore without a confirmed safety net (or into a busy manager)."""
        call_log: list[str] = []
        ws = _scripted_ws(_restore_responses(), call_log)
        client = MagicMock(base_url="http://ha", token="tok", verify_ssl=True)

        with (
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(ws, None)),
            ),
            patch(
                "ha_mcp.tools.backup._poll_backup_completion",
                new=AsyncMock(side_effect=ToolError("timed out")),
            ),
            pytest.raises(ToolError),
        ):
            await restore_backup(client, "target-slug", restore_database=True)

        assert "backup/restore" not in call_log


class TestRestoreForwardsPassword:
    @pytest.mark.asyncio
    async def test_password_forwarded_into_restore_params(self) -> None:
        """A `protected: true` backup needs the default password on restore."""
        call_log: list[str] = []
        ws = _scripted_ws(_restore_responses(with_password=True), call_log)
        client = MagicMock(base_url="http://ha", token="tok", verify_ssl=True)

        with (
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(ws, None)),
            ),
            patch(
                "ha_mcp.tools.backup._poll_backup_completion",
                new=AsyncMock(return_value={"success": True}),
            ),
        ):
            await restore_backup(client, "target-slug", restore_database=False)

        restore_call = next(
            c for c in ws.send_command.call_args_list if c.args[0] == "backup/restore"
        )
        assert restore_call.kwargs["password"] == "pw"

    @pytest.mark.asyncio
    async def test_password_omitted_when_unavailable(self) -> None:
        """When no default password is configured, `password` must be absent
        from the restore params entirely — HA types it as `str`, so passing
        None would fail voluptuous validation. (Safety backup is also skipped
        in this case, mirroring the existing no-password behaviour.)"""
        call_log: list[str] = []
        # backup/config/info reports no create_backup password → helper raises
        # → restore_backup falls back to password=None.
        ws = _scripted_ws(_restore_responses(with_password=False), call_log)
        client = MagicMock(base_url="http://ha", token="tok", verify_ssl=True)

        with (
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(ws, None)),
            ),
            patch(
                "ha_mcp.tools.backup._poll_backup_completion",
                new=AsyncMock(return_value={"success": True}),
            ),
        ):
            result = await restore_backup(client, "target-slug", restore_database=False)

        assert result["success"] is True
        assert result["safety_backup_id"] is None
        restore_call = next(
            c for c in ws.send_command.call_args_list if c.args[0] == "backup/restore"
        )
        assert "password" not in restore_call.kwargs
        # No safety backup means no generate call at all.
        assert "backup/generate" not in call_log


class TestCreateSafetyBackup:
    @pytest.mark.asyncio
    async def test_polls_to_completion_before_returning(self) -> None:
        """`_create_safety_backup` must await the backup it starts — passing
        the generated job id, the safety-backup name, and the local agent into
        the completion poll."""
        ws = AsyncMock()
        ws.send_command = AsyncMock(
            return_value={"success": True, "result": {"backup_job_id": "safety-job-1"}}
        )

        with patch(
            "ha_mcp.tools.backup._poll_backup_completion", new=AsyncMock()
        ) as poll:
            result = await _create_safety_backup(ws, "pw", "backup.local")

        assert result == "safety-job-1"
        poll.assert_awaited_once()
        # job id is forwarded positionally; agent id is forwarded by keyword.
        assert poll.await_args.args[2] == "safety-job-1"
        assert poll.await_args.kwargs["agent_id"] == "backup.local"
        # the polled name is the PreRestore_Safety_* backup just created.
        assert poll.await_args.args[1].startswith("PreRestore_Safety_")

    @pytest.mark.asyncio
    async def test_skips_creation_and_poll_when_password_none(self) -> None:
        """No default password → no safety backup, and crucially no poll."""
        ws = AsyncMock()

        with patch(
            "ha_mcp.tools.backup._poll_backup_completion", new=AsyncMock()
        ) as poll:
            result = await _create_safety_backup(ws, None, "backup.local")

        assert result is None
        ws.send_command.assert_not_awaited()
        poll.assert_not_awaited()
