"""Unit tests for ``ha_manage_backup(scope="snapshot")`` in ``tools/backup_snapshot.py``.

The snapshot actions follow Home Assistant's own contracts (#2632, #2786):

* new backups go where Home Assistant puts its own backup before a Core
  update: the configured automatic-backup agents, else the default agent;
* completion comes from ``backup/subscribe_events``; the result is the
  backup the job added to ``backup/info``;
* a restore reads from an agent that holds the backup and takes the
  database, add-ons and folders from the backup itself;
* the list returns Core's ``backup/info`` reply.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.errors import ErrorCode
from ha_mcp.tools import backup_snapshot
from ha_mcp.tools.backup_snapshot import (
    _backup_protected,
    create_backup,
    list_backups,
    restore_backup,
)

_COMPLETED = {"manager_state": "create_backup", "state": "completed"}
_FAILED = {
    "manager_state": "create_backup",
    "state": "failed",
    "reason": "unknown_error",
}
_UPLOAD_FAILED = {
    "manager_state": "create_backup",
    "state": "failed",
    "reason": "upload_failed",
}
_IDLE = {"manager_state": "idle"}


@pytest.fixture(autouse=True)
def _short_backup_wait():
    """A missed terminal event fails within a second instead of 30 minutes."""
    with patch.object(backup_snapshot, "_BACKUP_MAX_WAIT_S", 1):
        yield


class FakeWS:
    """WebSocket stand-in: canned replies per command, a backup event stream.

    ``events`` is what ``backup/subscribe_events`` streams after ``last``, the
    manager's last event that Core sends first on every subscription. A
    generated backup gets an id other than the job id, as on Supervisor
    installs, and ``backup/info`` lists it next to an older backup.
    """

    def __init__(
        self,
        *,
        agent_ids: list[str] | None = None,
        password: str | None = "pw",
        last: dict[str, Any] = _IDLE,
        events: list[dict[str, Any]] | None = None,
        details: dict[str, Any] | None = None,
        new_backup: dict[str, Any] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.last = last
        self.events = events if events is not None else [_COMPLETED]
        self.agent_ids = agent_ids if agent_ids is not None else ["backup.local"]
        self.password = password
        self.details = details
        self.new_backup_fields = new_backup or {}
        self.stored = [_entry("older")]
        # Backups the first listing misses; with ``unlisted`` set, those agents
        # report a listing error until the job runs.
        self.late: list[dict[str, Any]] = []
        self.unlisted: dict[str, str] = {}
        self.errors = errors or {}
        self.details_agent_errors: dict[str, str] = {}
        self.release_subscription = AsyncMock()
        self.disconnect = AsyncMock()

    async def subscribe_command(self, command: str) -> tuple[int, Any]:
        self.calls.append((command, {}))
        for event in [self.last, *self.events]:
            self.queue.put_nowait({"type": "event", "event": event})
        # The job's events are streamed once; the manager is idle after it.
        self.last, self.events = _IDLE, []
        return 7, self.queue

    async def send_command(self, command: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((command, kwargs))
        await asyncio.sleep(0)  # a network round trip lets other tasks run
        if (err := self.errors.get(command)) is not None:
            raise err
        if command == "backup/config/info":
            create = {"agent_ids": self.agent_ids, "password": self.password}
            return {"success": True, "result": {"config": {"create_backup": create}}}
        if command == "backup/generate":
            fields = {"name": kwargs["name"], **self.new_backup_fields}
            self.stored.append(_entry("new-backup", **fields))
            self.stored.extend(self.late)
            return {"success": True, "result": {"backup_job_id": "job1"}}
        if command == "backup/info":
            errors = {} if self.sent("backup/generate") else self.unlisted
            result = {"backups": list(self.stored), "agent_errors": errors}
            return {"success": True, "result": result}
        if command == "backup/details":
            result = {"backup": self.details, "agent_errors": self.details_agent_errors}
            return {"success": True, "result": result}
        if command == "backup/restore":
            return {"success": True, "result": None}
        raise AssertionError(f"unexpected WS command: {command!r}")

    def sent(self, command: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == command]


def _entry(backup_id: str, **fields: Any) -> dict[str, Any]:
    """A ``backup/info`` / ``backup/details`` entry with a subset of Core's fields."""
    return {
        "backup_id": backup_id,
        "name": f"name-{backup_id}",
        "date": "2026-10-01T00:00:00+00:00",
        "agents": {"backup.local": {"protected": True, "size": 10}},
        "addons": [],
        "folders": [],
        "database_included": False,
        "failed_agent_ids": [],
        "failed_addons": [],
        "failed_folders": [],
        **fields,
    }


def _slow_restore(ws: FakeWS) -> FakeWS:
    """Make ``backup/restore`` take a moment before Home Assistant replies."""
    reply = ws.send_command

    async def _send(command: str, **kwargs: Any) -> dict[str, Any]:
        if command == "backup/restore":
            await asyncio.sleep(0.05)
        return await reply(command, **kwargs)

    ws.send_command = _send
    return ws


def _client(*, supervised: bool = False) -> MagicMock:
    client = MagicMock()
    client.base_url = "http://ha"
    client.token = "t"
    client.verify_ssl = False
    client.get_config = AsyncMock(
        return_value={"components": ["hassio"] if supervised else ["backup"]}
    )
    return client


async def _run(coro_fn, ws: FakeWS, *args: Any, mounts: dict | None = None, **kw):
    supervisor = AsyncMock(return_value={"success": True, "result": mounts or {}})
    with (
        patch.object(
            backup_snapshot,
            "get_connected_ws_client",
            AsyncMock(return_value=(ws, None)),
        ),
        patch("ha_mcp.tools.tools_addons._supervisor_api_call", supervisor),
    ):
        return await coro_fn(*args, **kw)


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
        ws = FakeWS(events=[_UPLOAD_FAILED], new_backup={"name": "someone-else"})
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
        ws = FakeWS(events=[_FAILED])
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
            events=[_UPLOAD_FAILED], new_backup={"failed_agent_ids": failed_agents}
        )
        result = await _run(create_backup, ws, _client())
        assert result["backup_id"] == "new-backup"
        assert result["warnings"][-1] == (
            "Home Assistant reported the upload as failed; the backup is "
            "stored on: backup.local"
        )

    @pytest.mark.asyncio
    async def test_failed_upload_without_a_stored_backup_is_a_failure(self) -> None:
        ws = FakeWS(events=[_UPLOAD_FAILED], new_backup={"name": "someone-else"})
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
            ws.queue.put_nowait({"type": "event", "event": _COMPLETED})

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
            pytest.param(True, False, False, id="explicit-passes-through"),
        ],
    )
    async def test_restore_database_follows_the_backup_unless_given(
        self, included, requested, expected
    ) -> None:
        """Supervisor rejects a database restore that does not match the backup."""
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
        ("supervised", "addons", "folders"),
        [
            pytest.param(True, ["core_ssh"], ["share"], id="supervisor"),
            pytest.param(False, [], [], id="core-rejects-them"),
        ],
    )
    async def test_restore_names_the_backups_own_addons_and_folders(
        self, supervised, addons, folders
    ) -> None:
        target = _entry(
            "b1",
            addons=[{"slug": "core_ssh", "name": "SSH", "version": "1"}],
            folders=["share"],
        )
        ws = FakeWS(details=target)
        await _run(restore_backup, ws, _client(supervised=supervised), "b1")
        sent = ws.sent("backup/restore")[0]
        assert sent["restore_addons"] == addons
        assert sent["restore_folders"] == folders

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
    @pytest.mark.parametrize("supervised", [True, False])
    async def test_safety_backup_holds_what_the_restore_replaces(
        self, supervised
    ) -> None:
        """Config, database, add-ons and restored folders, before the restore starts."""
        folders = ["share"] if supervised else []
        ws = FakeWS(agent_ids=["hassio.nas"], details=_entry("b1", folders=folders))
        result = await _run(restore_backup, ws, _client(supervised=supervised), "b1")
        names = [name for name, _ in ws.calls]
        assert names.index("backup/restore") > names.index("backup/generate")
        safety = ws.sent("backup/generate")[0]
        assert safety["agent_ids"] == ["hassio.nas"]
        assert safety["include_homeassistant"] is True
        assert safety["include_database"] is True
        # Core rejects add-ons and folders; Supervisor must include them.
        assert safety["include_all_addons"] is supervised
        assert safety.get("include_folders", []) == folders
        restore = ws.sent("backup/restore")[0]
        assert (restore["backup_id"], restore["restore_homeassistant"]) == ("b1", True)
        assert result["safety_backup_id"] == "new-backup"

    @pytest.mark.asyncio
    async def test_rejected_restore_is_an_error_naming_the_backup(self) -> None:
        rejected = HomeAssistantCommandError(
            "Command failed: boom", "home_assistant_error"
        )
        ws = FakeWS(details=_entry("b1"), errors={"backup/restore": rejected})
        with pytest.raises(ToolError) as exc_info:
            await _run(restore_backup, ws, _client(), "b1")
        assert "b1" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_safety_backup_missing_addons_stops_the_restore(self) -> None:
        """Overwriting an add-on the safety backup lacks could not be undone."""
        ws = FakeWS(
            details=_entry("b1"),
            new_backup={"failed_addons": [{"slug": "core_ssh", "name": "SSH"}]},
        )
        with pytest.raises(ToolError) as exc_info:
            await _run(restore_backup, ws, _client(supervised=True), "b1")
        assert "new-backup" in str(exc_info.value)
        assert not ws.sent("backup/restore")

    @pytest.mark.asyncio
    async def test_safety_backup_warnings_reach_the_restore_reply(self) -> None:
        ws = FakeWS(details=_entry("b1"), new_backup={"failed_agent_ids": ["s3.x"]})
        result = await _run(restore_backup, ws, _client(), "b1")
        assert "Not stored on backup agents: s3.x" in result["warnings"]

    @pytest.mark.asyncio
    async def test_failed_safety_backup_stops_the_restore(self) -> None:
        ws = FakeWS(details=_entry("b1"), events=[_FAILED])
        with pytest.raises(ToolError) as exc_info:
            await _run(restore_backup, ws, _client(), "b1")
        assert ErrorCode.SERVICE_CALL_FAILED.value in str(exc_info.value)
        assert not ws.sent("backup/restore")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("events", "error", "initiated"),
        [
            pytest.param(
                [{"manager_state": "restore_backup", "state": "core_restart"}],
                HomeAssistantConnectionError("closed"),
                True,
                id="restart-for-the-restore",
            ),
            pytest.param(
                [{"manager_state": "restore_backup", "state": "in_progress"}],
                HomeAssistantCommandTimeout("no reply"),
                True,
                id="still-running",
            ),
            pytest.param(
                [], HomeAssistantConnectionError("closed"), False, id="no-event"
            ),
            pytest.param(
                [{"manager_state": "restore_backup", "state": "failed"}],
                HomeAssistantConnectionError("closed"),
                False,
                id="restore-failed",
            ),
        ],
    )
    async def test_connection_closed_by_the_restore_restart_is_not_a_failure(
        self, events, error, initiated
    ) -> None:
        """Home Assistant restarts before it answers a real restore.

        Reporting that as a failure invites a retry: a second safety backup
        and a second restore.
        """
        ws = FakeWS(
            password=None,
            details=_entry("b1"),
            events=events,
            errors={"backup/restore": error},
        )
        if initiated:
            result = await _run(restore_backup, ws, _client(), "b1")
            assert result["warnings"][-1].startswith(
                "Home Assistant accepted the restore but did not confirm it"
            )
        else:
            with pytest.raises(ToolError) as exc_info:
                await _run(restore_backup, ws, _client(), "b1")
            assert "closed" in str(exc_info.value)
        assert ws.sent("backup/restore")
        ws.release_subscription.assert_awaited()

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
        with patch.object(backup_snapshot, "_BACKUP_PROGRESS_INTERVAL_S", 0.01):
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
        with patch.object(backup_snapshot, "_BACKUP_PROGRESS_INTERVAL_S", 0.01):
            await backup_snapshot._send_restore(ws, {"backup_id": "b1"}, ctx, started)
        progress = [c.kwargs["progress"] for c in ctx.report_progress.await_args_list]
        assert progress
        assert min(progress) >= 100

    @pytest.mark.asyncio
    async def test_without_password_restore_warns_there_is_no_safety_backup(
        self,
    ) -> None:
        """The install type still counts: add-ons and folders are restored."""
        target = _entry("b1", addons=[{"slug": "core_ssh"}], folders=["share"])
        ws = FakeWS(password=None, details=target)
        result = await _run(restore_backup, ws, _client(supervised=True), "b1")
        assert not ws.sent("backup/generate")
        sent = ws.sent("backup/restore")[0]
        assert "password" not in sent
        assert (sent["restore_addons"], sent["restore_folders"]) == (
            ["core_ssh"],
            ["share"],
        )
        assert result["safety_backup_id"] is None
        assert any("No safety backup" in w for w in result["warnings"])


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
