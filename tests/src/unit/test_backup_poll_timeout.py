"""Unit tests for the post-timeout backup-list check in `_poll_backup_completion`.

Regression coverage for #1433: the polling loop used to raise `TIMEOUT_OPERATION`
unconditionally when it didn't observe `state=idle` + `event_state=completed`
within `max_wait_seconds`, even when the backup actually completed (just slower
than the poll window).

The fix performs one final `backup/info` lookup before raising and returns the
canonical success-shape (with a `warnings` entry) ONLY if both hold:

1. The HA top-level `state == "idle"` AND `last_action_event.state == "completed"`
   (i.e. HA finished finalizing — `backups` membership alone is insufficient,
   HA registers the entry before compression/encryption finish).
2. A backup entry matches by `last_action_event.backup_id` (authoritative) or
   by `name` filtered to entries dated at-or-after the job start (rejects stale
   prior-run entries that share a name).
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)
from ha_mcp.errors import ErrorCode
from ha_mcp.tools.backup import (
    _build_success_response_if_found,
    _poll_backup_completion,
)

# Stable job-start anchor used across helper-level tests. Backup entries dated
# >= this value count as fresh; entries dated earlier are stale.
_JOB_START = datetime(2026, 5, 24, 19, 50, 0, tzinfo=UTC)


def _ws_client(*responses: dict) -> AsyncMock:
    """Build a mock WS client whose `send_command` returns each response in turn."""
    ws = AsyncMock()
    ws.send_command.side_effect = list(responses)
    return ws


def _backup_info(
    state: str,
    event_state: str | None,
    backups: list[dict],
    *,
    last_event_backup_id: str | None = None,
) -> dict:
    """Compose a `backup/info` response body.

    ``last_event_backup_id`` populates the ``backup_id`` field of
    ``last_action_event`` — used by the helper's identity-matching path.
    """
    last_event: dict = {}
    if event_state is not None:
        last_event["state"] = event_state
    if last_event_backup_id is not None:
        last_event["backup_id"] = last_event_backup_id
    return {
        "success": True,
        "result": {
            "state": state,
            "last_action_event": last_event,
            "backups": backups,
        },
    }


# Default fixture date is in the far future so entries are "fresh" relative
# to both the fixed `_JOB_START` used in helper tests AND the live
# `datetime.now()` used by `_poll_backup_completion` when called from
# polling-level tests. Tests that need explicitly stale entries pass `date=`.
_FRESH_DATE = "2099-01-01T00:00:00Z"


def _backup_entry(
    name: str,
    *,
    backup_id: str = "abc123",
    agent_id: str = "backup.local",
    date: str = _FRESH_DATE,
) -> dict:
    """Compose one entry of `result.backups` matching the shape HA returns."""
    return {
        "backup_id": backup_id,
        "name": name,
        "date": date,
        "agents": {agent_id: {"size": 12345}},
    }


class TestBuildSuccessResponseIfFound:
    """Helper-level coverage. The helper owns state-gate + identity-match."""

    def test_returns_none_when_name_not_in_backups(self):
        info = _backup_info("idle", "completed", [_backup_entry("Other_Backup")])
        assert (
            _build_success_response_if_found(
                info,
                name="Looking_For_This",
                backup_job_id="job-1",
                agent_id="backup.local",
                duration_seconds=10,
                job_start_ts=_JOB_START,
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
            job_start_ts=_JOB_START,
        )
        # Subset assertions on load-bearing keys — adding a field
        # (e.g., `compressed: bool`) does not break this test.
        assert result is not None
        assert result["success"] is True
        assert result["backup_id"] == "xyz"
        assert result["name"] == "My_Backup"
        assert result["backup_job_id"] == "job-1"
        assert result["size_bytes"] == 12345
        assert result["duration_seconds"] == 42

    def test_returns_none_on_empty_backups_list(self):
        info = _backup_info("idle", "completed", [])
        assert (
            _build_success_response_if_found(
                info,
                name="My_Backup",
                backup_job_id="job-1",
                agent_id="backup.local",
                duration_seconds=10,
                job_start_ts=_JOB_START,
            )
            is None
        )

    def test_returns_none_when_state_active_even_if_name_in_list(self):
        """List-membership alone is insufficient. HA registers `backups`
        entries while still finalizing (compression/encryption). Returning
        success here would let callers immediately attempt restore on a
        half-written file."""
        info = _backup_info(
            "create_backup", "in_progress", [_backup_entry("Slow_Backup")]
        )
        assert (
            _build_success_response_if_found(
                info,
                name="Slow_Backup",
                backup_job_id="job-1",
                agent_id="backup.local",
                duration_seconds=10,
                job_start_ts=_JOB_START,
            )
            is None
        )

    def test_returns_none_when_event_state_not_completed(self):
        info = _backup_info("idle", "in_progress", [_backup_entry("Backup_X")])
        assert (
            _build_success_response_if_found(
                info,
                name="Backup_X",
                backup_job_id="job-1",
                agent_id="backup.local",
                duration_seconds=10,
                job_start_ts=_JOB_START,
            )
            is None
        )

    def test_target_id_matching_stale_entry_falls_back_to_fresh(self):
        """Even if `last_action_event.backup_id` matches an entry, that entry
        must still pass the freshness gate. Multi-job scenario: a concurrent
        UI-triggered backup updates `last_action_event` to its own
        backup_id; that target_id may match a stale entry from a prior run
        with the same name. The freshness filter rejects the stale match
        and picks the fresh entry instead."""
        stale_date = (
            (_JOB_START - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        )
        info = _backup_info(
            "idle",
            "completed",
            [
                _backup_entry("Backup_A", backup_id="STALE_ID", date=stale_date),
                _backup_entry("Backup_A", backup_id="FRESH_ID"),  # default _FRESH_DATE
            ],
            last_event_backup_id="STALE_ID",
        )
        result = _build_success_response_if_found(
            info,
            name="Backup_A",
            backup_job_id="job-1",
            agent_id="backup.local",
            duration_seconds=0,
            job_start_ts=_JOB_START,
        )
        assert result is not None
        assert result["backup_id"] == "FRESH_ID"

    def test_prefers_last_action_event_backup_id_when_present(self):
        """When HA exposes `last_action_event.backup_id`, match by backup_id
        is authoritative — same-name collisions resolve correctly."""
        info = _backup_info(
            "idle",
            "completed",
            [
                _backup_entry("Pre_Test", backup_id="OLD_FROM_PRIOR_RUN"),
                _backup_entry("Pre_Test", backup_id="NEW_THIS_JOB"),
            ],
            last_event_backup_id="NEW_THIS_JOB",
        )
        result = _build_success_response_if_found(
            info,
            name="Pre_Test",
            backup_job_id="job-2",
            agent_id="backup.local",
            duration_seconds=10,
            job_start_ts=_JOB_START,
        )
        assert result is not None
        assert result["backup_id"] == "NEW_THIS_JOB"

    def test_falls_back_to_date_filter_when_no_backup_id_in_event(self):
        """No `last_action_event.backup_id` — name match must filter by
        `date >= job_start_ts` to skip stale prior-run entries with same name."""
        stale_date = (
            (_JOB_START - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
        )
        fresh_date = (
            (_JOB_START + timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        )
        info = _backup_info(
            "idle",
            "completed",
            [
                _backup_entry("Pre_Test", backup_id="STALE", date=stale_date),
                _backup_entry("Pre_Test", backup_id="FRESH", date=fresh_date),
            ],
        )
        result = _build_success_response_if_found(
            info,
            name="Pre_Test",
            backup_job_id="job-2",
            agent_id="backup.local",
            duration_seconds=10,
            job_start_ts=_JOB_START,
        )
        assert result is not None
        assert result["backup_id"] == "FRESH"

    def test_date_filter_rejects_all_stale_entries(self):
        """Pure name-match against stale-only entries returns None.
        Protection against the name-collision class on retry."""
        stale_date = (
            (_JOB_START - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        )
        info = _backup_info(
            "idle",
            "completed",
            [_backup_entry("Pre_Test", backup_id="STALE", date=stale_date)],
        )
        assert (
            _build_success_response_if_found(
                info,
                name="Pre_Test",
                backup_job_id="job-2",
                agent_id="backup.local",
                duration_seconds=10,
                job_start_ts=_JOB_START,
            )
            is None
        )

    def test_helper_tolerates_null_agents_field(self):
        """HA can return `agents: null` per entry — size_bytes degrades to
        None rather than crashing."""
        info = _backup_info(
            "idle",
            "completed",
            [
                {
                    "backup_id": "x",
                    "name": "Any",
                    "date": "2026-05-24T20:00:00Z",
                    "agents": None,
                }
            ],
        )
        result = _build_success_response_if_found(
            info,
            name="Any",
            backup_job_id="job-1",
            agent_id="backup.local",
            duration_seconds=0,
            job_start_ts=_JOB_START,
        )
        assert result is not None
        assert result["size_bytes"] is None

    def test_helper_tolerates_null_backups_field(self):
        """HA can return `backups: null` when the field is absent."""
        info = {
            "success": True,
            "result": {
                "state": "idle",
                "last_action_event": {"state": "completed"},
                "backups": None,
            },
        }
        assert (
            _build_success_response_if_found(
                info,
                name="Any",
                backup_job_id="job-1",
                agent_id="backup.local",
                duration_seconds=0,
                job_start_ts=_JOB_START,
            )
            is None
        )


class TestPollBackupCompletionPostTimeout:
    @pytest.mark.asyncio
    async def test_post_timeout_finds_backup_returns_success_with_warning(self):
        """Polling exits without observing idle+completed; final lookup finds
        the backup with proper state → success with top-level `warnings: list[str]`."""
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
        # Top-level `warnings: list[str]` per AGENTS.md tool-return contract.
        assert "warnings" in result
        assert isinstance(result["warnings"], list)
        assert all(isinstance(w, str) for w in result["warnings"])
        assert any("poll window" in w for w in result["warnings"])
        ws.send_command.assert_awaited_once_with("backup/info")

    @pytest.mark.asyncio
    async def test_post_timeout_no_backup_raises_timeout_operation(self):
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
        assert ErrorCode.TIMEOUT_OPERATION.value in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_post_timeout_state_still_active_keeps_timeout_with_in_progress_flag(
        self,
    ):
        """List contains entry but HA state still indicates creation in
        progress → keep TIMEOUT_OPERATION but surface `likely_in_progress`
        in context so callers can back off retries."""
        ws = _ws_client(
            _backup_info("create_backup", "in_progress", [_backup_entry("Slow_Backup")])
        )
        with pytest.raises(ToolError) as exc_info:
            await _poll_backup_completion(
                ws,
                name="Slow_Backup",
                backup_job_id="job-1",
                max_wait_seconds=0,
                poll_interval=1,
                agent_id="backup.local",
            )
        msg = str(exc_info.value)
        assert ErrorCode.TIMEOUT_OPERATION.value in msg
        assert "likely_in_progress" in msg

    @pytest.mark.asyncio
    async def test_post_timeout_command_error_surfaces_verification_context(self):
        """Verification failure (HA-side error during the final lookup) must
        not stay invisible behind a misleading TIMEOUT_OPERATION — surface as
        `verification_error` in the error context."""
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
        msg = str(exc_info.value)
        assert ErrorCode.TIMEOUT_OPERATION.value in msg
        assert "verification_error" in msg
        assert "ws closed" in msg

    @pytest.mark.asyncio
    async def test_post_timeout_connection_error_falls_through(self):
        """Pin: the broad `HomeAssistantError` catch covers
        `HomeAssistantConnectionError` too. Prevents a future narrowing
        regression that would propagate connection-errors and mask the
        original timeout signal."""
        ws = AsyncMock()
        ws.send_command.side_effect = HomeAssistantConnectionError("connection lost")
        with pytest.raises(ToolError) as exc_info:
            await _poll_backup_completion(
                ws,
                name="Any_Backup",
                backup_job_id="job-1",
                max_wait_seconds=0,
                poll_interval=1,
                agent_id="backup.local",
            )
        msg = str(exc_info.value)
        assert ErrorCode.TIMEOUT_OPERATION.value in msg
        assert "verification_error" in msg

    @pytest.mark.asyncio
    async def test_post_timeout_final_info_success_false_falls_through(self):
        """A degraded `success: False` response from the final lookup falls
        through to TIMEOUT_OPERATION — must not claim success on a partial
        response that future regressions could produce."""
        ws = _ws_client({"success": False, "error": "transient"})
        with pytest.raises(ToolError) as exc_info:
            await _poll_backup_completion(
                ws,
                name="Any_Backup",
                backup_job_id="job-1",
                max_wait_seconds=0,
                poll_interval=1,
                agent_id="backup.local",
            )
        assert ErrorCode.TIMEOUT_OPERATION.value in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_post_timeout_failed_state_raises_service_call_failed(self):
        """If the post-timeout lookup observes the backup failed in the gap
        between the last in-loop poll and the final check, raise
        SERVICE_CALL_FAILED — not TIMEOUT_OPERATION. The failure mode is
        known and unambiguous; `likely_in_progress` would be actively
        misleading."""
        ws = _ws_client(_backup_info("idle", "failed", []))
        with pytest.raises(ToolError) as exc_info:
            await _poll_backup_completion(
                ws,
                name="Failed_Backup",
                backup_job_id="job-1",
                max_wait_seconds=0,
                poll_interval=1,
                agent_id="backup.local",
            )
        msg = str(exc_info.value)
        assert ErrorCode.SERVICE_CALL_FAILED.value in msg
        assert "likely_in_progress" not in msg

    @pytest.mark.asyncio
    async def test_post_timeout_name_collision_picks_fresh_entry(self):
        """Post-timeout window is exactly when stale collisions are likely
        (prior run timed out, retried with same name). Date-filter must pick
        the fresh entry, not the stale one."""
        ws = _ws_client(
            _backup_info(
                "idle",
                "completed",
                [
                    _backup_entry(
                        "Pre_Test",
                        backup_id="STALE",
                        date="2025-01-01T00:00:00Z",
                    ),
                    _backup_entry(
                        "Pre_Test",
                        backup_id="FRESH",
                        date="2099-01-01T00:00:00Z",
                    ),
                ],
            )
        )
        result = await _poll_backup_completion(
            ws,
            name="Pre_Test",
            backup_job_id="job-1",
            max_wait_seconds=0,
            poll_interval=1,
            agent_id="backup.local",
        )
        assert result["backup_id"] == "FRESH"


class TestPollBackupCompletionInLoop:
    @pytest.mark.asyncio
    async def test_in_loop_success_path_still_works_after_refactor(self):
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
        ws.send_command.assert_awaited_once_with("backup/info")

    @pytest.mark.asyncio
    async def test_in_loop_state_idle_name_not_in_list_continues_then_finds(self):
        """Same bug class as #1433 one tick earlier: first poll observes
        idle+completed but the backup hasn't appeared in the list yet (race
        between state transition and backup-list write). Must `continue`, not
        exit with success=None. Second poll picks it up."""
        ws = _ws_client(
            _backup_info("idle", "completed", []),
            _backup_info("idle", "completed", [_backup_entry("Slow_Race")]),
        )
        with patch("ha_mcp.tools.backup.asyncio.sleep", new=AsyncMock()):
            result = await _poll_backup_completion(
                ws,
                name="Slow_Race",
                backup_job_id="job-1",
                max_wait_seconds=10,
                poll_interval=2,
                agent_id="backup.local",
            )
        assert result["success"] is True
        assert result["backup_id"] == "abc123"
        assert "warnings" not in result
        assert ws.send_command.await_count == 2

    @pytest.mark.asyncio
    async def test_in_loop_failed_state_raises(self):
        """Regression: explicit `event_state == failed` still surfaces
        SERVICE_CALL_FAILED before the post-timeout path runs."""
        ws = _ws_client(_backup_info("idle", "failed", []))
        with (
            patch("ha_mcp.tools.backup.asyncio.sleep", new=AsyncMock()),
            pytest.raises(ToolError) as exc_info,
        ):
            await _poll_backup_completion(
                ws,
                name="Bad",
                backup_job_id="job-1",
                max_wait_seconds=10,
                poll_interval=2,
                agent_id="backup.local",
            )
        assert ErrorCode.SERVICE_CALL_FAILED.value in str(exc_info.value)


class TestCreateBackupWrapperSeam:
    """The `create_backup` wrapper is the user-facing seam that produces
    duplicates when callers retry on `success: false`. Pin that the
    post-timeout `warnings`-bearing dict survives through the wrapper's
    outer try/except Exception — that's the surface the fix actually
    protects against the duplicate-retry pattern in #1433."""

    @pytest.mark.asyncio
    async def test_create_backup_returns_warnings_dict_unchanged(self):
        from ha_mcp.tools import backup as backup_module

        late_success = {
            "success": True,
            "backup_id": "abc",
            "name": "Wrapper_Test",
            "backup_job_id": "j-1",
            "duration_seconds": 120,
            "warnings": [
                "Backup completion observed only after the 120s poll window — "
                "the operation succeeded but took longer than expected."
            ],
        }

        ws_mock = AsyncMock()
        ws_mock.send_command.side_effect = [
            # _get_backup_password
            {
                "success": True,
                "result": {"config": {"create_backup": {"password": "pw"}}},
            },
            # _get_local_backup_agent_id
            {
                "success": True,
                "result": {"agents": [{"agent_id": "backup.local", "name": "local"}]},
            },
            # backup/generate
            {"success": True, "result": {"backup_job_id": "j-1"}},
        ]
        ws_mock.disconnect = AsyncMock()

        client_mock = MagicMock(base_url="http://ha", token="tok", verify_ssl=True)

        with (
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(ws_mock, None)),
            ),
            patch(
                "ha_mcp.tools.backup._poll_backup_completion",
                new=AsyncMock(return_value=late_success),
            ),
        ):
            result = await backup_module.create_backup(client_mock, name="Wrapper_Test")

        # The wrapper must pass through the warnings dict unchanged — the
        # outer `except Exception` would otherwise re-map it to INTERNAL_ERROR
        # via `exception_to_structured_error`.
        assert result == late_success
        assert "warnings" in result
        assert isinstance(result["warnings"], list)
        assert all(isinstance(w, str) for w in result["warnings"])
