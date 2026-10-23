"""Unit tests for ``ha_manage_backup(scope="snapshot")`` in ``tools/backup.py``.

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
from ha_mcp.errors import ErrorCode
from ha_mcp.tools import backup
from ha_mcp.tools.backup import (
    _backup_protected,
    create_backup,
    list_backups,
    restore_backup,
)

_COMPLETED = {"manager_state": "create_backup", "state": "completed"}
_FAILED = {
    "manager_state": "create_backup",
    "state": "failed",
    "reason": "upload_failed",
}
_IDLE = {"manager_state": "idle"}


@pytest.fixture(autouse=True)
def _short_backup_wait():
    """A missed terminal event fails within a second instead of 30 minutes."""
    with patch.object(backup, "_BACKUP_MAX_WAIT_S", 1):
        yield


class FakeWS:
    """WebSocket stand-in: canned replies per command, a backup event stream.

    ``events`` is what ``backup/subscribe_events`` streams: the manager's last
    event (idle) first, as Core sends it, then the job's events. A generated
    backup gets an id other than the job id, as on Supervisor installs, and
    ``backup/info`` lists it next to an older backup.
    """

    def __init__(
        self,
        *,
        agent_ids: list[str] | None = None,
        password: str | None = "pw",
        events: list[dict[str, Any]] | None = None,
        details: dict[str, Any] | None = None,
        new_backup: dict[str, Any] | None = None,
        generate_error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for event in [_IDLE, *(events if events is not None else [_COMPLETED])]:
            self.queue.put_nowait({"type": "event", "event": event})
        self.agent_ids = agent_ids if agent_ids is not None else ["backup.local"]
        self.password = password
        self.details = details
        self.new_backup_fields = new_backup or {}
        self.stored = [_entry("older")]
        # Backups the first listing misses (an agent failed to list them).
        self.late: list[dict[str, Any]] = []
        self.generate_error = generate_error
        self.release_subscription = AsyncMock()
        self.disconnect = AsyncMock()

    async def subscribe_command(self, command: str) -> tuple[int, Any]:
        self.calls.append((command, {}))
        return 7, self.queue

    async def send_command(self, command: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((command, kwargs))
        if command == "backup/config/info":
            create = {"agent_ids": self.agent_ids, "password": self.password}
            return {"success": True, "result": {"config": {"create_backup": create}}}
        if command == "backup/generate":
            if self.generate_error:
                raise self.generate_error
            fields = {"name": kwargs["name"], **self.new_backup_fields}
            self.stored.append(_entry("new-backup", **fields))
            self.stored.extend(self.late)
            return {"success": True, "result": {"backup_job_id": "job1"}}
        if command == "backup/info":
            return {"success": True, "result": {"backups": list(self.stored)}}
        if command == "backup/details":
            return {"success": True, "result": {"backup": self.details}}
        if command == "backup/restore":
            return {"success": True, "result": None}
        raise AssertionError(f"unexpected WS command: {command!r}")

    def sent(self, command: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == command]


def _entry(backup_id: str, **fields: Any) -> dict[str, Any]:
    """One ``backup/info`` / ``backup/details`` entry in Core's shape."""
    return {
        "backup_id": backup_id,
        "name": f"name-{backup_id}",
        "date": "2026-10-01T00:00:00+00:00",
        "agents": {"backup.local": {"protected": True, "size": 10}},
        "addons": [],
        "folders": [],
        "database_included": False,
        "failed_agent_ids": [],
        **fields,
    }


def _client(*, supervised: bool = False) -> MagicMock:
    client = MagicMock()
    client.base_url = "http://ha"
    client.token = "t"
    client.verify_ssl = False
    client._is_supervised_install = AsyncMock(return_value=supervised)
    return client


async def _run(coro_fn, ws: FakeWS, *args: Any, mounts: dict | None = None, **kw):
    supervisor = AsyncMock(return_value={"success": True, "result": mounts or {}})
    with (
        patch.object(
            backup, "get_connected_ws_client", AsyncMock(return_value=(ws, None))
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
        """A backup no longer lands on the local disk when HA backs up elsewhere."""
        ws = FakeWS(agent_ids=configured)
        await _run(create_backup, ws, _client(supervised=supervised), mounts=mounts)
        assert ws.sent("backup/generate")[0]["agent_ids"] == expected

    @pytest.mark.asyncio
    async def test_addons_are_only_requested_on_supervisor_installs(self) -> None:
        """Core rejects add-ons in a backup, so a Core install must not ask."""
        ws = FakeWS()
        await _run(create_backup, ws, _client(supervised=False))
        assert ws.sent("backup/generate")[0]["include_all_addons"] is False

    @pytest.mark.asyncio
    async def test_create_without_default_password_is_refused(self) -> None:
        ws = FakeWS(password=None)
        with pytest.raises(ToolError, match="No default backup password"):
            await _run(create_backup, ws, _client())
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
        with pytest.raises(ToolError, match="lists 2 new backups"):
            await _run(create_backup, ws, _client(), name="mine")

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
        assert "upload_failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_unavailable_agents_are_named_in_warnings(self) -> None:
        ws = FakeWS(new_backup={"failed_agent_ids": ["hassio.gone"]})
        result = await _run(create_backup, ws, _client())
        assert result["warnings"] == [
            "Not stored on unavailable backup agents: hassio.gone"
        ]

    @pytest.mark.asyncio
    async def test_timeout_says_the_backup_may_still_be_running(self) -> None:
        """A retry after a timeout must not start a second backup blindly."""
        ws = FakeWS(events=[])
        with (
            patch.object(backup, "_BACKUP_MAX_WAIT_S", 0.05),
            patch.object(backup, "_BACKUP_PROGRESS_INTERVAL_S", 0.01),
            pytest.raises(ToolError) as exc_info,
        ):
            await _run(create_backup, ws, _client())
        assert ErrorCode.TIMEOUT_OPERATION.value in str(exc_info.value)
        assert "likely_in_progress" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_closed_connection_says_the_backup_may_still_be_running(
        self,
    ) -> None:
        ws = FakeWS(events=[])
        ws.queue.shutdown(immediate=True)
        with pytest.raises(ToolError) as exc_info:
            await _run(create_backup, ws, _client())
        assert ErrorCode.CONNECTION_FAILED.value in str(exc_info.value)
        assert "likely_in_progress" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_long_backup_sends_progress_heartbeats(self) -> None:
        """#1861: without heartbeats a client drops a long-running call."""
        ws = FakeWS(events=[])
        ctx = MagicMock()
        ctx.report_progress = AsyncMock()

        async def _finish_later() -> None:
            await asyncio.sleep(0.05)
            ws.queue.put_nowait({"type": "event", "event": _COMPLETED})

        with patch.object(backup, "_BACKUP_PROGRESS_INTERVAL_S", 0.01):
            finisher = asyncio.create_task(_finish_later())
            await _run(create_backup, ws, _client(), ctx=ctx)
            await finisher
        assert ctx.report_progress.await_count >= 3

    @pytest.mark.asyncio
    async def test_subscription_is_released_when_the_backup_cannot_start(
        self,
    ) -> None:
        """Core keeps an unreleased subscription for the socket's lifetime."""
        ws = FakeWS(generate_error=RuntimeError("Backup manager busy"))
        with pytest.raises(ToolError):
            await _run(create_backup, ws, _client())
        ws.release_subscription.assert_awaited_once_with(7)


class TestRestore:
    @pytest.mark.asyncio
    async def test_unknown_backup_is_not_found_and_nothing_runs(self) -> None:
        ws = FakeWS(details=None)
        with pytest.raises(ToolError) as exc_info:
            await _run(restore_backup, ws, _client(), "nope")
        assert ErrorCode.RESOURCE_NOT_FOUND.value in str(exc_info.value)
        assert not ws.sent("backup/generate")
        assert not ws.sent("backup/restore")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("agents", "expected"),
        [
            pytest.param({"hassio.nas": {}}, "hassio.nas", id="remote-only"),
            pytest.param(
                {"hassio.nas": {}, "hassio.local": {}}, "hassio.local", id="local-copy"
            ),
        ],
    )
    async def test_restore_reads_from_an_agent_that_holds_the_backup(
        self, agents, expected
    ) -> None:
        """A backup stored only on a network mount used to be unrestorable."""
        ws = FakeWS(details=_entry("b1", agents=agents))
        await _run(restore_backup, ws, _client(), "b1")
        assert ws.sent("backup/restore")[0]["agent_id"] == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("requested", "expected"),
        [
            pytest.param(None, True, id="default-follows-backup"),
            pytest.param(False, False, id="explicit-passes-through"),
        ],
    )
    async def test_restore_database_follows_the_backup_unless_given(
        self, requested, expected
    ) -> None:
        ws = FakeWS(details=_entry("b1", database_included=True))
        await _run(restore_backup, ws, _client(), "b1", restore_database=requested)
        assert ws.sent("backup/restore")[0]["restore_database"] is expected

    @pytest.mark.asyncio
    async def test_restore_names_the_backups_own_addons_and_folders(self) -> None:
        target = _entry(
            "b1",
            addons=[{"slug": "core_ssh", "name": "SSH", "version": "1"}],
            folders=["share"],
        )
        ws = FakeWS(details=target)
        await _run(restore_backup, ws, _client(supervised=True), "b1")
        sent = ws.sent("backup/restore")[0]
        assert sent["restore_addons"] == ["core_ssh"]
        assert sent["restore_folders"] == ["share"]

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
        """Core rejects a password for an unencrypted backup."""
        target = _entry("b1", agents={"backup.local": {"protected": protected}})
        ws = FakeWS(details=target)
        await _run(restore_backup, ws, _client(), "b1")
        assert ("password" in ws.sent("backup/restore")[0]) is forwarded

    @pytest.mark.asyncio
    async def test_safety_backup_finishes_before_the_restore_starts(self) -> None:
        ws = FakeWS(agent_ids=["hassio.nas"], details=_entry("b1"))
        result = await _run(restore_backup, ws, _client(supervised=True), "b1")
        names = [name for name, _ in ws.calls]
        assert names.index("backup/restore") > names.index("backup/generate")
        safety = ws.sent("backup/generate")[0]
        assert safety["agent_ids"] == ["hassio.nas"]
        assert safety["include_database"] is True
        assert result["safety_backup_id"] == "new-backup"

    @pytest.mark.asyncio
    async def test_failed_safety_backup_stops_the_restore(self) -> None:
        ws = FakeWS(details=_entry("b1"), events=[_FAILED])
        with pytest.raises(ToolError) as exc_info:
            await _run(restore_backup, ws, _client(), "b1")
        assert ErrorCode.SERVICE_CALL_FAILED.value in str(exc_info.value)
        assert not ws.sent("backup/restore")

    @pytest.mark.asyncio
    async def test_without_password_restore_warns_there_is_no_safety_backup(
        self,
    ) -> None:
        ws = FakeWS(password=None, details=_entry("b1"))
        result = await _run(restore_backup, ws, _client(), "b1")
        assert not ws.sent("backup/generate")
        assert result["safety_backup_id"] is None
        assert any("No safety backup" in w for w in result["warnings"])


class TestList:
    @pytest.mark.asyncio
    async def test_list_keeps_cores_fields_newest_first(self) -> None:
        """A partial list (agent_errors) and per-backup details reach the caller."""
        older = _entry("old", date="2026-09-01T00:00:00Z")
        newer = _entry("new", date="2026-10-01T00:00:00+00:00", failed_agent_ids=["x"])
        undated = _entry("undated", date=None)
        client = _client()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "agent_errors": {"hassio.nas": "unreachable"},
                    "backups": [undated, older, newer],
                    "next_automatic_backup": "2026-10-24T04:45:00+00:00",
                },
            }
        )
        result = await list_backups(client, limit=2)
        assert [b["backup_id"] for b in result["backups"]] == ["new", "old"]
        assert result["backups"][0]["failed_agent_ids"] == ["x"]
        assert result["agent_errors"] == {"hassio.nas": "unreachable"}
        assert result["next_automatic_backup"] == "2026-10-24T04:45:00+00:00"
        assert (result["count"], result["total"]) == (2, 3)

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

    def test_all_agents_unprotected(self) -> None:
        assert _backup_protected({"agents": {"a": {"protected": False}}}) is False

    @pytest.mark.parametrize(
        "entry",
        [
            pytest.param({}, id="missing-agents"),
            pytest.param({"agents": {"a": {}}}, id="no-flag"),
            pytest.param({"agents": ["a"]}, id="non-dict"),
        ],
    )
    def test_unknown_when_no_agent_reports_it(self, entry) -> None:
        assert _backup_protected(entry) is None
