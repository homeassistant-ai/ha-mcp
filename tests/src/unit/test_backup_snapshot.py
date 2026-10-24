"""Unit tests for ``ha_manage_backup(scope="snapshot")`` in ``tools/backup_snapshot.py``.

The snapshot actions follow Home Assistant's own contracts (#2632, #2786):

* new backups go where Home Assistant puts its own backup before a Core
  update: the configured automatic-backup agents, else the default agent;
* completion comes from ``backup/subscribe_events``; the result is the
  backup the job added to ``backup/info``;
* the list returns Core's ``backup/info`` reply.

Restore-specific behaviour is in ``test_backup_snapshot_restore.py``; the
connection and heartbeat tests here also cover restore.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.errors import ErrorCode
from ha_mcp.tools import backup_snapshot
from ha_mcp.tools.backup_snapshot import create_backup, list_backups
from ha_mcp.tools.backup_snapshot_restore import _backup_protected, restore_backup

from ._backup_snapshot_fakes import (
    COMPLETED,
    FAILED,
    UPLOAD_FAILED,
    FakeWS,
    _client,
    _entry,
    _run,
    _short_backup_wait,  # noqa: F401 - pytest fixture
)


class TestWhereBackupsGo:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("configured", "supervised", "mounts", "expected"),
        [
            pytest.param(["hassio.nas"], True, {}, ["hassio.nas"], id="configured"),
            pytest.param(
                ["hassio.nas", "cloud.cloud"], True, {}, ["hassio.nas"], id="no-cloud"
            ),
            pytest.param(
                ["cloud.cloud"],
                True,
                {"default_backup_mount": "nas"},
                ["hassio.nas"],
                id="cloud-only-uses-default",
            ),
            pytest.param(
                [], True, {"default_backup_mount": "nas"}, ["hassio.nas"], id="mount"
            ),
            pytest.param(
                [], True, {"default_backup_mount": None}, ["hassio.local"], id="local"
            ),
            pytest.param([], False, {}, ["backup.local"], id="core-install"),
        ],
    )
    async def test_backup_lands_where_home_assistant_puts_its_own(
        self, configured, supervised, mounts, expected
    ) -> None:
        """A backup lands where Home Assistant backs up, not on the local disk."""
        ws = FakeWS(agent_ids=configured)
        await _run(create_backup, ws, _client(supervised=supervised), mounts=mounts)
        assert ws.sent("backup/generate")[0]["agent_ids"] == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("supervised", "addons"), [(True, True), (False, False)])
    async def test_addons_are_requested_only_on_supervisor_installs(
        self, supervised, addons
    ) -> None:
        """Core rejects add-ons in a backup; Supervisor backups must include them."""
        ws = FakeWS()
        await _run(create_backup, ws, _client(supervised=supervised))
        sent = ws.sent("backup/generate")[0]
        assert sent["include_all_addons"] is addons
        # The documented fast backup: configuration without the database.
        assert sent["include_homeassistant"] is True
        assert sent["include_database"] is False

    @pytest.mark.asyncio
    async def test_failed_install_probe_fails_instead_of_dropping_addons(
        self,
    ) -> None:
        """A Supervisor install read as Core would back up without add-ons."""
        ws = FakeWS()
        client = _client()
        client.get_config = AsyncMock(side_effect=OSError("config unreachable"))
        with pytest.raises(ToolError):
            await _run(create_backup, ws, client)
        assert not ws.sent("backup/generate")

    @pytest.mark.asyncio
    async def test_create_without_default_password_is_refused(self) -> None:
        ws = FakeWS(password=None)
        with pytest.raises(ToolError, match="No default backup password"):
            await _run(create_backup, ws, _client())
        assert not ws.sent("backup/generate")

    @pytest.mark.asyncio
    async def test_name_is_matched_as_home_assistant_stores_it(self) -> None:
        """Core strips the name; an unstripped match would miss the new backup."""
        ws = FakeWS()
        result = await _run(create_backup, ws, _client(), name="  mine ")
        assert ws.sent("backup/generate")[0]["name"] == "mine"
        assert result["backup_id"] == "new-backup"


class TestConnection:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            pytest.param(create_backup, (), id="create"),
            pytest.param(restore_backup, ("b1",), id="restore"),
        ],
    )
    async def test_connect_failure_keeps_home_assistants_reason(
        self, tool, args
    ) -> None:
        reason = {"error": {"code": "CONNECTION_FAILED", "message": "TLS handshake"}}
        with (
            patch.object(
                backup_snapshot,
                "get_connected_ws_client",
                AsyncMock(return_value=(None, reason)),
            ),
            pytest.raises(ToolError, match="TLS handshake"),
        ):
            await tool(_client(), *args)

    @pytest.mark.asyncio
    async def test_cleanup_transport_error_keeps_a_finished_backup(self) -> None:
        """A retry after a false failure would start a second backup."""
        ws = FakeWS()
        ws.disconnect = AsyncMock(side_effect=OSError("socket gone"))
        result = await _run(create_backup, ws, _client())
        assert result["backup_id"] == "new-backup"

    @pytest.mark.asyncio
    async def test_unreadable_install_type_fails_instead_of_guessing(self) -> None:
        """An empty /api/config reply must not read as a Core install."""
        ws = FakeWS()
        client = _client()
        client.get_config = AsyncMock(return_value={})
        with pytest.raises(ToolError):
            await _run(create_backup, ws, client)
        assert not ws.sent("backup/generate")


class TestWaitingForTheBackup:
    @pytest.mark.asyncio
    async def test_result_is_the_backup_the_job_added(self) -> None:
        """On Supervisor the job id is not the backup id; the reply names the backup."""
        ws = FakeWS()
        result = await _run(create_backup, ws, _client(supervised=True))
        assert result["backup_id"] == "new-backup"
        assert result["agents"] == {"backup.local": {"protected": True, "size": 10}}
        assert "warnings" not in result
        ws.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_older_backup_with_the_same_name_is_not_taken(self) -> None:
        """Home Assistant does not keep names unique; a reused name must not match."""
        ws = FakeWS()
        ws.stored = [_entry("older", name="mine")]
        result = await _run(create_backup, ws, _client(), name="mine")
        assert result["backup_id"] == "new-backup"

    @pytest.mark.asyncio
    async def test_backup_hidden_from_the_first_listing_is_not_guessed(
        self,
    ) -> None:
        """An older same-named backup that only reappears later is not returned."""
        ws = FakeWS()
        ws.late = [_entry("hidden", name="mine")]
        with pytest.raises(ToolError, match="lists 2 new backups") as exc_info:
            await _run(create_backup, ws, _client(), name="mine")
        assert "Check the snapshot list" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_failed_upload_does_not_take_an_older_backup_for_its_own(
        self,
    ) -> None:
        """A same-named backup on an agent that failed the first listing is old."""
        ws = FakeWS(events=[UPLOAD_FAILED], new_backup={"name": "someone-else"})
        ws.unlisted = {"hassio.nas": "unreachable"}
        ws.late = [
            _entry(
                "old",
                name="mine",
                date="2020-01-01T00:00:00+00:00",
                agents={"hassio.nas": {"protected": True, "size": 10}},
            )
        ]
        with pytest.raises(ToolError, match="Backup creation failed"):
            await _run(create_backup, ws, _client(), name="mine")

    @pytest.mark.asyncio
    async def test_new_backup_is_found_next_to_one_an_unlisted_agent_held(
        self,
    ) -> None:
        """A failing agent's older backup does not hide the job's own.

        The job's backup is also on that agent: one listed agent holding it
        is enough.
        """
        both = {"backup.local": {"protected": True}, "hassio.nas": {"protected": True}}
        ws = FakeWS(new_backup={"agents": both})
        ws.unlisted = {"hassio.nas": "unreachable"}
        ws.late = [
            _entry("old", name="mine", agents={"hassio.nas": {"protected": True}})
        ]
        result = await _run(create_backup, ws, _client(), name="mine")
        assert result["backup_id"] == "new-backup"

    @pytest.mark.asyncio
    async def test_backup_only_unlisted_agents_hold_names_their_errors(
        self,
    ) -> None:
        """It may predate the job, so it is not named; the error says why."""
        nas = {"hassio.nas": {"protected": True, "size": 10}}
        ws = FakeWS(agent_ids=["hassio.nas"], new_backup={"agents": nas})
        ws.unlisted = {"hassio.nas": "timeout"}
        with pytest.raises(ToolError, match="lists 0 new backups") as exc_info:
            await _run(create_backup, ws, _client(), name="mine")
        assert "agent_errors_before_job" in str(exc_info.value)
        assert "timeout" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_finished_job_without_its_backup_is_an_error(self) -> None:
        """A backup listed under another name is not taken for this job's."""
        ws = FakeWS(new_backup={"name": "someone-else"})
        with pytest.raises(ToolError) as exc_info:
            await _run(create_backup, ws, _client(), name="mine")
        assert ErrorCode.SERVICE_CALL_FAILED.value in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_failed_backup_is_reported_as_failure(self) -> None:
        ws = FakeWS(events=[FAILED])
        with pytest.raises(ToolError) as exc_info:
            await _run(create_backup, ws, _client())
        assert ErrorCode.SERVICE_CALL_FAILED.value in str(exc_info.value)
        assert "unknown_error" in str(exc_info.value)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failed_agents", [["hassio.nas"], []])
    async def test_backup_kept_after_a_failed_upload_is_returned_with_a_warning(
        self, failed_agents
    ) -> None:
        """Core keeps the backup on the agents whose upload worked.

        Core does not always record the failed agents, so the warning must
        not depend on them.
        """
        ws = FakeWS(
            events=[UPLOAD_FAILED], new_backup={"failed_agent_ids": failed_agents}
        )
        result = await _run(create_backup, ws, _client())
        assert result["backup_id"] == "new-backup"
        assert result["warnings"][-1] == (
            "Home Assistant reported the upload as failed; the backup is "
            "stored on: backup.local"
        )

    @pytest.mark.asyncio
    async def test_failed_upload_without_a_stored_backup_is_a_failure(self) -> None:
        ws = FakeWS(events=[UPLOAD_FAILED], new_backup={"name": "someone-else"})
        with pytest.raises(ToolError, match="Backup creation failed"):
            await _run(create_backup, ws, _client(), name="mine")

    @pytest.mark.asyncio
    async def test_addons_and_folders_left_out_are_named_in_warnings(self) -> None:
        """Core reports a backup as done when add-ons or folders failed."""
        ws = FakeWS(
            new_backup={
                "failed_addons": [{"slug": "core_ssh", "name": "SSH"}],
                "failed_folders": ["media"],
            }
        )
        result = await _run(create_backup, ws, _client(supervised=True))
        assert result["warnings"] == [
            "Apps (add-ons) that could not be backed up: core_ssh",
            "Folders that could not be backed up: media",
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "last",
        [
            {"manager_state": "create_backup", "state": "in_progress"},
            {"manager_state": "restore_backup", "state": "in_progress"},
            {"manager_state": "receive_backup", "state": "in_progress"},
            {"manager_state": "blocked"},
        ],
        ids=["creating", "restoring", "receiving", "starting-up"],
    )
    async def test_busy_backup_manager_is_reported_before_starting(self, last) -> None:
        """Another operation's events must not be read as this job's."""
        ws = FakeWS(last=last)
        with pytest.raises(ToolError, match="busy"):
            await _run(create_backup, ws, _client())
        assert not ws.sent("backup/generate")
        ws.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_timeout_says_the_backup_may_still_be_running(self) -> None:
        """A retry after a timeout must not start a second backup blindly."""
        ws = FakeWS(events=[])
        with (
            patch.object(backup_snapshot, "_BACKUP_MAX_WAIT_S", 0.05),
            patch.object(backup_snapshot, "_BACKUP_PROGRESS_INTERVAL_S", 0.01),
            pytest.raises(ToolError) as exc_info,
        ):
            await _run(create_backup, ws, _client())
        assert ErrorCode.TIMEOUT_OPERATION.value in str(exc_info.value)
        assert "likely_in_progress" in str(exc_info.value)
        assert "Check the snapshot list" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_closed_connection_says_the_backup_may_still_be_running(
        self,
    ) -> None:
        ws = FakeWS(events=[])

        async def _close_later() -> None:
            await asyncio.sleep(0.02)
            ws.queue.shutdown(immediate=True)

        closer = asyncio.create_task(_close_later())
        with pytest.raises(ToolError) as exc_info:
            await _run(create_backup, ws, _client())
        await asyncio.gather(closer)
        assert ErrorCode.CONNECTION_FAILED.value in str(exc_info.value)
        assert "likely_in_progress" in str(exc_info.value)
        assert "Check the snapshot list" in str(exc_info.value)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            pytest.param(create_backup, (), id="create"),
            pytest.param(restore_backup, ("b1",), id="restore"),
        ],
    )
    async def test_long_backup_sends_progress_heartbeats(self, tool, args) -> None:
        """#1861: without heartbeats a client drops a long-running call."""
        ws = FakeWS(events=[], details=_entry("b1"))
        ctx = MagicMock()
        ctx.report_progress = AsyncMock()

        async def _finish_later() -> None:
            await asyncio.sleep(0.05)
            ws.queue.put_nowait({"type": "event", "event": COMPLETED})

        with patch.object(backup_snapshot, "_BACKUP_PROGRESS_INTERVAL_S", 0.01):
            finisher = asyncio.create_task(_finish_later())
            await _run(tool, ws, _client(), *args, ctx=ctx)
            await asyncio.gather(finisher)
        assert ctx.report_progress.await_count >= 3

    @pytest.mark.asyncio
    async def test_subscription_is_released_when_the_backup_cannot_start(
        self,
    ) -> None:
        """Core keeps an unreleased subscription for the socket's lifetime."""
        ws = FakeWS(errors={"backup/generate": RuntimeError("generate rejected")})
        with pytest.raises(ToolError):
            await _run(create_backup, ws, _client())
        ws.release_subscription.assert_awaited_once_with(7)


class TestList:
    @pytest.mark.asyncio
    async def test_list_keeps_cores_fields_newest_first(self) -> None:
        """Per-backup details and Core's top-level fields reach the caller."""
        # Same second: as strings "...00Z" would sort after "...00.500000+00:00".
        older = _entry("old", date="2026-10-01T00:00:00Z")
        newer = _entry(
            "new", date="2026-10-01T00:00:00.500000+00:00", failed_agent_ids=["x"]
        )
        undated = _entry("undated", date=None)
        client = _client()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "agent_errors": {},
                    "backups": [undated, older, newer],
                    "next_automatic_backup": "2026-10-24T04:45:00+00:00",
                },
            }
        )
        result = await list_backups(client, limit=2)
        assert [b["backup_id"] for b in result["backups"]] == ["new", "old"]
        assert result["backups"][0]["failed_agent_ids"] == ["x"]
        assert result["next_automatic_backup"] == "2026-10-24T04:45:00+00:00"
        assert (result["count"], result["total"]) == (2, 3)
        assert "warnings" not in result

    @pytest.mark.asyncio
    async def test_partial_list_says_so(self) -> None:
        """A caller checking whether a backup landed must know the list is partial."""
        client = _client()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "agent_errors": {"hassio.nas": "unreachable"},
                    "backups": [],
                },
            }
        )
        result = await list_backups(client)
        assert result["agent_errors"] == {"hassio.nas": "unreachable"}
        assert result["warnings"] == [
            "Partial list: these backup agents failed to list their backups: hassio.nas"
        ]

    @pytest.mark.asyncio
    async def test_list_failure_raises(self) -> None:
        client = _client()
        client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": "boom"}
        )
        with pytest.raises(ToolError):
            await list_backups(client)


class TestBackupProtected:
    """``protected`` is per agent in ``backup/info``; any agent decides."""

    def test_any_agent_protected_makes_it_protected(self) -> None:
        entry = {"agents": {"a": {"protected": False}, "b": {"protected": True}}}
        assert _backup_protected(entry) is True

    @pytest.mark.parametrize(
        "entry",
        [
            pytest.param({"agents": {"a": {"protected": False}}}, id="unprotected"),
            pytest.param({}, id="missing-agents"),
            pytest.param({"agents": {"a": {}}}, id="no-flag"),
            pytest.param({"agents": ["a"]}, id="non-dict"),
        ],
    )
    def test_not_protected_unless_an_agent_says_so(self, entry) -> None:
        assert _backup_protected(entry) is False
