"""Unit tests for ``ha_manage_backup(scope="snapshot", action="restore")``.

A restore reads from an agent that holds the backup, follows the backup for
the database and, like Home Assistant's restore dialog, restores only the
apps (add-ons) and folders it is given (#2786). A safety backup of what the
restore replaces comes first; a request Home Assistant would refuse or
mishandle stops before it.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.errors import ErrorCode
from ha_mcp.tools import backup_snapshot_restore
from ha_mcp.tools.backup_snapshot_restore import restore_backup

from ._backup_snapshot_fakes import (
    FAILED,
    FakeWS,
    _client,
    _entry,
    _run,
    _short_backup_wait,  # noqa: F401 - pytest fixture
    _slow_restore,
)


class TestRestore:
    @pytest.mark.asyncio
    async def test_unknown_backup_is_not_found_and_nothing_runs(self) -> None:
        """An agent that failed to list the backup is named, not hidden."""
        ws = FakeWS(details=None)
        ws.details_agent_errors = {"hassio.nas": "unreachable"}
        with pytest.raises(ToolError) as exc_info:
            await _run(restore_backup, ws, _client(), "nope")
        assert ErrorCode.RESOURCE_NOT_FOUND.value in str(exc_info.value)
        assert "hassio.nas" in str(exc_info.value)
        assert not ws.sent("backup/generate")
        assert not ws.sent("backup/restore")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("agents", "expected"),
        [
            pytest.param({"s3.bucket": {}}, "s3.bucket", id="remote-only"),
            pytest.param(
                {"s3.bucket": {}, "hassio.nas": {}}, "hassio.nas", id="mount-copy"
            ),
            pytest.param(
                {"s3.bucket": {}, "backup.local": {}}, "backup.local", id="core-local"
            ),
        ],
    )
    async def test_restore_reads_from_an_agent_that_holds_the_backup(
        self, agents, expected
    ) -> None:
        """A backup stored only on a network mount restores from that mount."""
        ws = FakeWS(details=_entry("b1", agents=agents))
        await _run(restore_backup, ws, _client(), "b1")
        assert ws.sent("backup/restore")[0]["agent_id"] == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("included", "requested", "expected"),
        [
            pytest.param(True, None, True, id="default-with-database"),
            pytest.param(False, None, False, id="default-without-database"),
            pytest.param(None, None, False, id="default-field-absent"),
            pytest.param(True, False, False, id="core-may-keep-its-database"),
        ],
    )
    async def test_restore_database_follows_the_backup_unless_given(
        self, included, requested, expected
    ) -> None:
        """The database follows the backup unless given; a Core install may keep its own."""
        target = _entry("b1", database_included=included)
        if included is None:
            del target["database_included"]
        ws = FakeWS(details=target)
        result = await _run(
            restore_backup, ws, _client(), "b1", restore_database=requested
        )
        assert ws.sent("backup/restore")[0]["restore_database"] is expected
        assert result["restore_database"] is expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("given", "addons", "folders"),
        [
            pytest.param({}, [], [], id="default-none"),
            pytest.param(
                {"restore_addons": ["core_ssh"], "restore_folders": ["ssl"]},
                ["core_ssh"],
                ["ssl"],
                id="given-pass-through",
            ),
        ],
    )
    async def test_restore_takes_only_the_apps_and_folders_it_is_given(
        self, given, addons, folders
    ) -> None:
        """Home Assistant's restore dialog starts with no apps or folders ticked.

        Restoring every one the backup holds would also roll back the
        configuration and data of each app.
        """
        target = _entry(
            "b1",
            addons=[
                {"slug": "core_ssh", "name": "SSH", "version": "1"},
                {"slug": "core_mosquitto", "name": "Mosquitto", "version": "1"},
            ],
            folders=["share", "ssl"],
        )
        ws = FakeWS(details=target)
        result = await _run(restore_backup, ws, _client(supervised=True), "b1", **given)
        sent = ws.sent("backup/restore")[0]
        assert sent["restore_addons"] == addons
        assert sent["restore_folders"] == folders
        assert (result["restore_addons"], result["restore_folders"]) == (
            addons,
            folders,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("supervised", "included", "given", "reasons"),
        [
            pytest.param(
                True,
                True,
                {"restore_database": False},
                ["requires the two to match"],
                id="supervisor-database-mismatch",
            ),
            pytest.param(
                True,
                False,
                {"restore_database": True},
                ["the backup has no database"],
                id="supervisor-no-database",
            ),
            pytest.param(
                False,
                False,
                {"restore_database": True},
                ["the backup has no database"],
                id="core-would-delete-database",
            ),
            pytest.param(
                False,
                False,
                {"restore_folders": ["share"]},
                ["need a Supervisor install"],
                id="core-folders",
            ),
            pytest.param(
                False,
                False,
                {"restore_database": True, "restore_addons": ["core_ssh"]},
                ["the backup has no database", "need a Supervisor install"],
                id="all-reasons-at-once",
            ),
            pytest.param(
                True,
                False,
                {
                    "restore_addons": ["core_ssh", "mosquitto"],
                    "restore_folders": ["media"],
                },
                ["the backup holds no apps ['mosquitto']", "no folders ['media']"],
                id="supervisor-skips-what-the-backup-lacks",
            ),
        ],
    )
    async def test_restore_home_assistant_would_refuse_stops_before_the_safety_backup(
        self, supervised, included, given, reasons
    ) -> None:
        """Home Assistant checks these only after the safety backup exists, or not at all.

        Each retry would leave another safety backup behind.
        """
        ws = FakeWS(
            details=_entry(
                "b1",
                database_included=included,
                addons=[{"slug": "core_ssh"}],
                folders=["share"],
            )
        )
        with pytest.raises(ToolError) as exc_info:
            await _run(
                restore_backup, ws, _client(supervised=supervised), "b1", **given
            )
        assert ErrorCode.VALIDATION_INVALID_PARAMETER.value in str(exc_info.value)
        assert '"backup_id": "b1"' in str(exc_info.value)
        for reason in reasons:
            assert reason in str(exc_info.value)
        assert not ws.sent("backup/generate")
        assert not ws.sent("backup/restore")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("protected", "forwarded"),
        [
            pytest.param(True, True, id="encrypted"),
            pytest.param(False, False, id="plain"),
        ],
    )
    async def test_password_is_sent_only_for_an_encrypted_backup(
        self, protected, forwarded
    ) -> None:
        target = _entry("b1", agents={"backup.local": {"protected": protected}})
        ws = FakeWS(details=target)
        await _run(restore_backup, ws, _client(), "b1")
        assert ("password" in ws.sent("backup/restore")[0]) is forwarded

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("supervised", "included", "given"),
        [
            pytest.param(
                True,
                True,
                {"restore_addons": ["core_ssh"], "restore_folders": ["share"]},
                id="supervisor-selection",
            ),
            pytest.param(True, True, {}, id="supervisor-database-only"),
            pytest.param(False, False, {}, id="core-configuration-only"),
        ],
    )
    async def test_safety_backup_holds_what_the_restore_replaces(
        self, supervised, included, given
    ) -> None:
        """Configuration, plus the database, apps and folders the restore takes."""
        target = _entry(
            "b1",
            database_included=included,
            addons=[{"slug": "core_ssh"}],
            folders=["share"],
        )
        ws = FakeWS(agent_ids=["hassio.nas"], details=target)
        result = await _run(
            restore_backup, ws, _client(supervised=supervised), "b1", **given
        )
        names = [name for name, _ in ws.calls]
        assert names.index("backup/restore") > names.index("backup/generate")
        safety = ws.sent("backup/generate")[0]
        assert safety["agent_ids"] == ["hassio.nas"]
        assert safety["include_homeassistant"] is True
        assert safety["include_database"] is included
        assert "include_all_addons" not in safety
        assert safety.get("include_addons", []) == given.get("restore_addons", [])
        assert safety.get("include_folders", []) == given.get("restore_folders", [])
        restore = ws.sent("backup/restore")[0]
        assert (restore["backup_id"], restore["restore_homeassistant"]) == ("b1", True)
        assert result["safety_backup_id"] == "new-backup"

    @pytest.mark.asyncio
    async def test_rejected_restore_names_the_backup_and_its_safety_backup(
        self,
    ) -> None:
        """A retry makes another safety backup; the error names the first."""
        rejected = HomeAssistantCommandError(
            "Command failed: boom", "home_assistant_error"
        )
        ws = FakeWS(details=_entry("b1"), errors={"backup/restore": rejected})
        with pytest.raises(ToolError) as exc_info:
            await _run(restore_backup, ws, _client(), "b1")
        assert '"backup_id": "b1"' in str(exc_info.value)
        assert '"safety_backup_id": "new-backup"' in str(exc_info.value)
        assert "Home Assistant may be restoring" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_busy_manager_after_the_safety_backup_names_it(self) -> None:
        """Another backup operation can start between the safety backup and the restore."""
        ws = FakeWS(details=_entry("b1"))
        subscribe = ws.subscribe_command

        async def _busy_after_safety(command: str) -> tuple[int, Any]:
            if ws.sent("backup/generate"):
                ws.last = {"manager_state": "create_backup", "state": "in_progress"}
            return await subscribe(command)

        ws.subscribe_command = _busy_after_safety
        with pytest.raises(ToolError, match="busy") as exc_info:
            await _run(restore_backup, ws, _client(), "b1")
        assert '"safety_backup_id": "new-backup"' in str(exc_info.value)
        assert not ws.sent("backup/restore")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failed",
        [
            pytest.param(
                {"failed_addons": [{"slug": "core_ssh", "name": "SSH"}]}, id="app"
            ),
            pytest.param({"failed_folders": ["share"]}, id="folder"),
        ],
    )
    async def test_safety_backup_missing_what_the_restore_replaces_stops_it(
        self, failed
    ) -> None:
        """Overwriting an app or folder the safety backup lacks could not be undone."""
        target = _entry("b1", addons=[{"slug": "core_ssh"}], folders=["share"])
        ws = FakeWS(details=target, new_backup=failed)
        with pytest.raises(ToolError) as exc_info:
            await _run(
                restore_backup,
                ws,
                _client(supervised=True),
                "b1",
                restore_addons=["core_ssh"],
                restore_folders=["share"],
            )
        assert "new-backup" in str(exc_info.value)
        assert not ws.sent("backup/restore")

    @pytest.mark.asyncio
    async def test_safety_backup_warnings_reach_the_restore_reply(self) -> None:
        ws = FakeWS(details=_entry("b1"), new_backup={"failed_agent_ids": ["s3.x"]})
        result = await _run(restore_backup, ws, _client(), "b1")
        assert "Not stored on backup agents: s3.x" in result["warnings"]

    @pytest.mark.asyncio
    async def test_failed_safety_backup_stops_the_restore(self) -> None:
        ws = FakeWS(details=_entry("b1"), events=[FAILED])
        with pytest.raises(ToolError) as exc_info:
            await _run(restore_backup, ws, _client(), "b1")
        assert ErrorCode.SERVICE_CALL_FAILED.value in str(exc_info.value)
        assert not ws.sent("backup/restore")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("events", "error", "warning"),
        [
            pytest.param(
                [{"manager_state": "restore_backup", "state": "core_restart"}],
                HomeAssistantConnectionError("closed"),
                "Home Assistant accepted the restore but did not confirm it "
                "before the connection closed",
                id="restart-for-the-restore",
            ),
            pytest.param(
                [{"manager_state": "restore_backup", "state": "in_progress"}],
                HomeAssistantCommandTimeout("no reply"),
                "Home Assistant took the restore but did not answer within",
                id="still-running",
            ),
            pytest.param(
                [], HomeAssistantConnectionError("closed"), None, id="no-event"
            ),
            pytest.param(
                [{"manager_state": "restore_backup", "state": "failed"}],
                HomeAssistantConnectionError("closed"),
                None,
                id="restore-failed",
            ),
            pytest.param(
                [], HomeAssistantCommandTimeout("no reply"), None, id="timeout-no-event"
            ),
            pytest.param(
                [{"manager_state": "restore_backup", "state": "in_progress"}],
                HomeAssistantCommandNotSent("closed before sending"),
                None,
                id="never-sent",
            ),
        ],
    )
    async def test_restore_home_assistant_took_is_not_a_failure(
        self, events, error, warning
    ) -> None:
        """Home Assistant restarts before it answers a real restore.

        On a long restore the command can time out first. A command that was
        never sent is a failure, whatever restore events arrive.

        Reporting that as a failure invites a retry: a second safety backup
        and a second restore.
        """
        ws = FakeWS(
            password=None,
            details=_entry("b1"),
            events=events,
            errors={"backup/restore": error},
        )
        if warning:
            result = await _run(restore_backup, ws, _client(), "b1")
            assert result["warnings"][-1].startswith(warning)
        else:
            with pytest.raises(ToolError) as exc_info:
                await _run(restore_backup, ws, _client(), "b1")
            assert str(error) in str(exc_info.value)
        assert ws.sent("backup/restore")
        ws.release_subscription.assert_awaited()

    @pytest.mark.asyncio
    async def test_restore_home_assistant_answered_carries_no_unconfirmed_warning(
        self,
    ) -> None:
        """A reply confirms the restore; warning otherwise invites a needless check."""
        ws = FakeWS(password=None, details=_entry("b1"))
        result = await _run(restore_backup, ws, _client(), "b1")
        assert not [w for w in result["warnings"] if "did not" in w]

    @pytest.mark.asyncio
    async def test_failure_before_the_safety_backup_names_no_safety_backup(
        self,
    ) -> None:
        """Nothing was backed up, so there is no safety backup to point at."""
        rejected = HomeAssistantCommandError("Command failed: boom", "unknown_error")
        ws = FakeWS(errors={"backup/details": rejected})
        with pytest.raises(ToolError) as exc_info:
            await _run(restore_backup, ws, _client(), "b1")
        assert "boom" in str(exc_info.value)
        assert "safety_backup_id" not in str(exc_info.value)
        assert not ws.sent("backup/generate")

    @pytest.mark.asyncio
    async def test_busy_backup_manager_stops_a_restore_without_safety_backup(
        self,
    ) -> None:
        """Another restore's replayed event must not read as this one's."""
        ws = FakeWS(
            password=None,
            details=_entry("b1"),
            last={"manager_state": "restore_backup", "state": "in_progress"},
            events=[],
            errors={"backup/restore": HomeAssistantConnectionError("closed")},
        )
        with pytest.raises(ToolError, match="busy"):
            await _run(restore_backup, ws, _client(), "b1")
        assert not ws.sent("backup/restore")
        ws.release_subscription.assert_awaited_once_with(7)

    @pytest.mark.asyncio
    async def test_long_restore_sends_progress_heartbeats(self) -> None:
        """#1861: a restore can run for minutes before Home Assistant replies.

        The heartbeats stop once it has.
        """
        ws = _slow_restore(FakeWS(password=None, details=_entry("b1")))
        ctx = MagicMock()
        ctx.report_progress = AsyncMock()
        with patch.object(backup_snapshot_restore, "_BACKUP_PROGRESS_INTERVAL_S", 0.01):
            await _run(restore_backup, ws, _client(), "b1", ctx=ctx)
            sent = ctx.report_progress.await_count
            await asyncio.sleep(0.05)
        assert sent >= 3
        assert ctx.report_progress.await_count == sent

    @pytest.mark.asyncio
    async def test_restore_progress_counts_from_the_start_of_the_call(
        self,
    ) -> None:
        """It never drops below the safety backup's, which started later."""
        ws = _slow_restore(FakeWS())
        ctx = MagicMock()
        ctx.report_progress = AsyncMock()
        started = asyncio.get_running_loop().time() - 100
        with patch.object(backup_snapshot_restore, "_BACKUP_PROGRESS_INTERVAL_S", 0.01):
            await backup_snapshot_restore._send_restore(
                ws, {"backup_id": "b1"}, ctx, started
            )
        progress = [c.kwargs["progress"] for c in ctx.report_progress.await_args_list]
        assert progress
        assert min(progress) >= 100

    @pytest.mark.asyncio
    async def test_without_password_restore_warns_there_is_no_safety_backup(
        self,
    ) -> None:
        """The apps and folders given are still restored."""
        target = _entry("b1", addons=[{"slug": "core_ssh"}], folders=["share"])
        ws = FakeWS(password=None, details=target)
        result = await _run(
            restore_backup,
            ws,
            _client(supervised=True),
            "b1",
            restore_addons=["core_ssh"],
            restore_folders=["share"],
        )
        assert not ws.sent("backup/generate")
        sent = ws.sent("backup/restore")[0]
        assert "password" not in sent
        assert (sent["restore_addons"], sent["restore_folders"]) == (
            ["core_ssh"],
            ["share"],
        )
        assert result["safety_backup_id"] is None
        assert any("No safety backup" in w for w in result["warnings"])
