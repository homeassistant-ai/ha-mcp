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
   that password into the `backup/restore` call — but only for a *protected*
   target, since HA rejects a password on an unprotected backup.

3. **Param reconciliation** — `password` and `restore_database` are forwarded
   based on the target backup's own metadata (`protected`, `database_included`)
   rather than ambient config / caller default, so an unprotected or
   DB-inclusive target restores cleanly instead of hitting an opaque HA error.

4. **Safety-backup poll budget + late-completion warnings** — the full safety
   backup polls with its own (larger) timeout and its late-completion warnings
   surface in the restore response.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.backup import (
    _SAFETY_BACKUP_MAX_WAIT_S,
    _create_safety_backup,
    restore_backup,
)


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


def _restore_responses(
    *,
    with_password: bool = True,
    protected: bool = True,
    database_included: bool | None = None,
) -> dict[str, Any]:
    """Canned WS responses for a full restore_backup run.

    ``protected`` / ``database_included`` populate the target backup/info entry
    so the param-reconciliation logic can be exercised.
    """
    config_block = {"create_backup": {"password": "pw"}} if with_password else {}
    target_entry: dict[str, Any] = {"backup_id": "target-slug", "protected": protected}
    if database_included is not None:
        target_entry["database_included"] = database_included
    return {
        # backup/info — backup-exists verification + target metadata
        "backup/info": {
            "success": True,
            "result": {"backups": [target_entry]},
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


def _restore_call(ws: AsyncMock) -> Any:
    """Return the recorded ``backup/restore`` call."""
    return next(
        c for c in ws.send_command.call_args_list if c.args[0] == "backup/restore"
    )


class TestRestoreAwaitsSafetyBackup:
    @pytest.mark.asyncio
    async def test_restore_issued_only_after_safety_backup_completes(self) -> None:
        """The core #1681 regression: `backup/restore` must be sent strictly
        after the safety-backup completion poll returns, never before."""
        call_log: list[str] = []
        ws = _scripted_ws(_restore_responses(), call_log)
        client = MagicMock(base_url="http://ha", token="tok", verify_ssl=True)

        def _marker(*_a: Any, **_k: Any) -> dict[str, Any]:
            call_log.append("poll-completed")
            return {}

        poll_order_marker = AsyncMock(side_effect=_marker)

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
    async def test_password_forwarded_for_protected_target(self) -> None:
        """A `protected: true` backup needs the default password on restore."""
        call_log: list[str] = []
        ws = _scripted_ws(
            _restore_responses(with_password=True, protected=True), call_log
        )
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

        assert _restore_call(ws).kwargs["password"] == "pw"

    @pytest.mark.asyncio
    async def test_password_omitted_for_unprotected_target(self) -> None:
        """An unprotected target must NOT receive the password even when a
        default password is configured — HA validates it against the target and
        rejects a password on an unprotected backup (#1681 reconciliation)."""
        call_log: list[str] = []
        ws = _scripted_ws(
            _restore_responses(with_password=True, protected=False), call_log
        )
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
        # A safety backup is still created (the default password drives that),
        # but it must not leak into the restore of an unprotected target.
        assert "password" not in _restore_call(ws).kwargs

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
        assert "password" not in _restore_call(ws).kwargs
        # No safety backup means no generate call at all.
        assert "backup/generate" not in call_log


class TestRestoreReconcilesDatabase:
    @pytest.mark.asyncio
    async def test_restore_database_derived_from_target(self) -> None:
        """A `database_included: true` target forces `restore_database=True`
        even when the caller defaulted it to False — HA requires the flag to
        match the backup when restoring Home Assistant, otherwise Supervisor
        raises "Restore database must match backup"."""
        call_log: list[str] = []
        ws = _scripted_ws(_restore_responses(database_included=True), call_log)
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

        assert _restore_call(ws).kwargs["restore_database"] is True
        assert result["restore_database"] is True
        # The silent override is surfaced as a warning.
        assert any("restore_database was adjusted" in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_restore_database_overridden_to_false(self) -> None:
        """A `database_included: false` target forces `restore_database=False`
        even when the caller requested True — the symmetric override of the
        True case, since HA requires the flag to match either way."""
        call_log: list[str] = []
        ws = _scripted_ws(_restore_responses(database_included=False), call_log)
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
            result = await restore_backup(client, "target-slug", restore_database=True)

        assert _restore_call(ws).kwargs["restore_database"] is False
        assert result["restore_database"] is False
        assert any("restore_database was adjusted" in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_restore_database_falls_back_when_field_absent(self) -> None:
        """When the target omits `database_included`, the caller's value is
        honoured unchanged and no override warning is emitted."""
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
                new=AsyncMock(return_value={"success": True}),
            ),
        ):
            result = await restore_backup(client, "target-slug", restore_database=True)

        assert _restore_call(ws).kwargs["restore_database"] is True
        assert not any("restore_database was adjusted" in w for w in result["warnings"])


class TestRestoreSurfacesSafetyBackupWarnings:
    @pytest.mark.asyncio
    async def test_late_completion_warning_folded_into_response(self) -> None:
        """A late-completion warning from the safety-backup poll must reach the
        restore response — it is the signal that the backup subsystem is slow,
        right before a destructive restore."""
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
                new=AsyncMock(
                    return_value={
                        "success": True,
                        "warnings": ["Backup completion observed only after ..."],
                    }
                ),
            ),
        ):
            result = await restore_backup(client, "target-slug", restore_database=True)

        assert any(
            "Backup completion observed only after" in w for w in result["warnings"]
        )


class TestCreateSafetyBackup:
    @pytest.mark.asyncio
    async def test_polls_to_completion_before_returning(self) -> None:
        """`_create_safety_backup` must await the backup it starts — passing
        the generated job id, the safety-backup name, and the local agent into
        the completion poll — and use the dedicated safety-backup timeout."""
        ws = AsyncMock()
        ws.send_command = AsyncMock(
            return_value={"success": True, "result": {"backup_job_id": "safety-job-1"}}
        )

        with patch(
            "ha_mcp.tools.backup._poll_backup_completion",
            new=AsyncMock(return_value={"warnings": ["late"]}),
        ) as poll:
            job_id, warnings = await _create_safety_backup(ws, "pw", "backup.local")

        assert job_id == "safety-job-1"
        assert warnings == ["late"]
        poll.assert_awaited_once()
        # job id is forwarded positionally; agent id is forwarded by keyword.
        assert poll.await_args.args[2] == "safety-job-1"
        assert poll.await_args.kwargs["agent_id"] == "backup.local"
        # the polled name is the PreRestore_Safety_* backup just created.
        assert poll.await_args.args[1].startswith("PreRestore_Safety_")
        # the full safety backup gets the larger timeout, not the fast-backup one.
        assert poll.await_args.kwargs["max_wait_seconds"] == _SAFETY_BACKUP_MAX_WAIT_S

    @pytest.mark.asyncio
    async def test_skips_creation_and_poll_when_password_none(self) -> None:
        """No default password → no safety backup, and crucially no poll."""
        ws = AsyncMock()

        with patch(
            "ha_mcp.tools.backup._poll_backup_completion", new=AsyncMock()
        ) as poll:
            job_id, warnings = await _create_safety_backup(ws, None, "backup.local")

        assert job_id is None
        assert warnings == []
        ws.send_command.assert_not_awaited()
        poll.assert_not_awaited()
