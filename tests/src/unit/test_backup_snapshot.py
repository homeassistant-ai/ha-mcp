"""Unit tests for ``ha_manage_backup(scope="snapshot")`` in ``tools/backup_snapshot.py``.

The snapshot actions follow Home Assistant's own contracts (#2632, #2786):

* a full backup is made as Home Assistant makes its own before a Core
  update: with the automatic-backup settings, else on the default agent;
  a custom backup holds the parts given and goes to the default agent;
* completion comes from ``backup/subscribe_events``; the result is the
  backup the job added to ``backup/info``;
* the list returns Core's ``backup/info`` reply.

Restore-specific behaviour is in ``test_backup_snapshot_restore.py``; the
connection and heartbeat tests here also cover restore.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.websockets.exceptions import ConnectionClosed
from ha_mcp.client.rest_client import (
    HomeAssistantCommandNotSent,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
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


class TestFullBackup:
    @pytest.mark.asyncio
    async def test_with_automatic_agents_it_is_home_assistants_automatic_backup(
        self,
    ) -> None:
        """Contents, locations and name come from the settings; retention counts it."""
        ws = FakeWS(agent_ids=["hassio.nas", "cloud.cloud"])
        result = await _run(create_backup, ws, _client(supervised=True))
        assert ws.sent("backup/generate_with_automatic_settings") == [{}]
        assert not ws.sent("backup/generate")
        assert result["backup_id"] == "new-backup"
        assert result["with_automatic_settings"] is True
        assert "retention" in result["note"]
        assert "warnings" not in result

    @pytest.mark.asyncio
    async def test_another_instances_backup_appearing_meanwhile_is_not_taken(
        self,
    ) -> None:
        """A shared location lists other instances' backups too; Core marks only
        this instance's own automatic backups with with_automatic_settings."""
        ws = FakeWS(agent_ids=["hassio.nas"])
        ws.late = [
            _entry(
                "other-instance",
                name="Automatic backup 2026.10.4",
                with_automatic_settings=None,
            )
        ]
        result = await _run(create_backup, ws, _client())
        assert result["backup_id"] == "new-backup"

    @pytest.mark.asyncio
    async def test_a_name_an_automatic_backup_cannot_take_is_named_in_a_warning(
        self,
    ) -> None:
        ws = FakeWS(agent_ids=["hassio.nas"])
        result = await _run(create_backup, ws, _client(), name="mine")
        assert "the name 'mine' was not used" in result["warnings"][-1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("supervised", "mounts", "expected"),
        [
            pytest.param(
                True, {"default_backup_mount": "nas"}, "hassio.nas", id="mount"
            ),
            pytest.param(
                True, {"default_backup_mount": None}, "hassio.local", id="local"
            ),
            pytest.param(False, {}, "backup.local", id="core-install"),
        ],
    )
    async def test_without_automatic_agents_it_goes_to_the_default_agent(
        self, supervised, mounts, expected
    ) -> None:
        """Core rejects the automatic command without agents and raises a repair issue."""
        ws = FakeWS()
        await _run(create_backup, ws, _client(supervised=supervised), mounts=mounts)
        assert not ws.sent("backup/generate_with_automatic_settings")
        assert ws.sent("backup/generate")[0]["agent_ids"] == [expected]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("create", "expected"),
        [
            pytest.param(
                {},
                {"include_database": True, "include_all_addons": False},
                id="core-defaults",
            ),
            pytest.param(
                {
                    "include_database": False,
                    "include_addons": ["core_ssh"],
                    "include_folders": ["share"],
                },
                {
                    "include_database": False,
                    "include_all_addons": False,
                    "include_addons": ["core_ssh"],
                    "include_folders": ["share"],
                },
                id="chosen-parts",
            ),
            pytest.param(
                {"include_all_addons": True},
                {"include_database": True, "include_all_addons": True},
                id="every-app",
            ),
        ],
    )
    async def test_without_automatic_agents_it_holds_what_the_settings_hold(
        self, create, expected
    ) -> None:
        ws = FakeWS(create=create)
        await _run(create_backup, ws, _client(supervised=True))
        sent = ws.sent("backup/generate")[0]
        assert sent["password"] == "pw"
        parts = {k: v for k, v in sent.items() if k.startswith("include_")}
        assert parts == {"include_homeassistant": True, **expected}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("agent_ids", "supervised", "apps", "no_apps"),
        [
            pytest.param([], True, [], True, id="manual-without-apps"),
            pytest.param(["hassio.nas"], True, [], True, id="automatic-without-apps"),
            pytest.param([], True, [{"slug": "core_ssh"}], False, id="with-an-app"),
            pytest.param([], False, [], False, id="core-install"),
        ],
    )
    async def test_note_says_when_a_full_backup_holds_no_apps(
        self, agent_ids, supervised, apps, no_apps
    ) -> None:
        """Settings that select no apps give a full backup without apps."""
        ws = FakeWS(agent_ids=agent_ids, new_backup={"addons": apps})
        result = await _run(create_backup, ws, _client(supervised=supervised))
        assert ("holds no apps" in result["note"]) is no_apps

    @pytest.mark.asyncio
    async def test_note_says_where_a_manual_full_backup_went(self) -> None:
        ws = FakeWS()
        result = await _run(create_backup, ws, _client())
        assert "default location" in result["note"]
        assert "automatic-backup settings" in result["note"]

    @pytest.mark.asyncio
    async def test_failed_install_probe_fails_instead_of_guessing_the_install(
        self,
    ) -> None:
        """A Supervisor install read as Core would pick an agent it does not have."""
        ws = FakeWS()
        client = _client()
        client.get_config = AsyncMock(side_effect=OSError("config unreachable"))
        with pytest.raises(ToolError):
            await _run(create_backup, ws, client)
        assert not ws.generated()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "agent_ids",
        [pytest.param([], id="manual"), pytest.param(["x"], id="automatic")],
    )
    async def test_create_without_default_password_is_refused(self, agent_ids) -> None:
        ws = FakeWS(agent_ids=agent_ids, password=None)
        with pytest.raises(ToolError, match="No default backup password"):
            await _run(create_backup, ws, _client())
        assert not ws.generated()

    @pytest.mark.asyncio
    async def test_name_is_matched_as_home_assistant_stores_it(self) -> None:
        """Core strips the name; an unstripped match would miss the new backup."""
        ws = FakeWS()
        result = await _run(create_backup, ws, _client(), name="  mine ")
        assert ws.sent("backup/generate")[0]["name"] == "mine"
        assert result["backup_id"] == "new-backup"


class TestCustomBackup:
    @pytest.mark.asyncio
    async def test_one_app_alone_goes_to_the_default_agent(self) -> None:
        """Parts not given are left out."""
        ws = FakeWS(agent_ids=["hassio.nas"])
        await _run(
            create_backup,
            ws,
            _client(supervised=True),
            include_addons=["core_mosquitto"],
            mounts={"default_backup_mount": None},
        )
        assert not ws.sent("backup/generate_with_automatic_settings")
        sent = ws.sent("backup/generate")[0]
        assert sent["agent_ids"] == ["hassio.local"]
        assert sent["password"] == "pw"
        parts = {k: v for k, v in sent.items() if k.startswith("include_")}
        assert parts == {
            "include_homeassistant": False,
            "include_database": False,
            "include_all_addons": False,
            "include_addons": ["core_mosquitto"],
        }

    @pytest.mark.asyncio
    async def test_every_app_alone(self) -> None:
        """Losing the flag would report an empty backup as a success."""
        ws = FakeWS()
        await _run(create_backup, ws, _client(supervised=True), include_all_addons=True)
        sent = ws.sent("backup/generate")[0]
        assert sent["include_all_addons"] is True
        assert "include_addons" not in sent

    @pytest.mark.asyncio
    async def test_configuration_with_its_database_and_a_folder(self) -> None:
        ws = FakeWS()
        await _run(
            create_backup,
            ws,
            _client(supervised=True),
            include_homeassistant=True,
            include_database=True,
            include_folders=["share"],
        )
        sent = ws.sent("backup/generate")[0]
        assert (sent["include_homeassistant"], sent["include_database"]) == (True, True)
        assert sent["include_folders"] == ["share"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("supervised", "given", "reasons"),
        [
            pytest.param(
                True,
                {"include_homeassistant": False},
                ["nothing is selected"],
                id="empty",
            ),
            pytest.param(
                True,
                {"include_database": True},
                ["set include_homeassistant=true with it"],
                id="database-alone",
            ),
            pytest.param(
                True,
                {"include_all_addons": True, "include_addons": ["core_ssh"]},
                ["exclude each other"],
                id="all-and-some-apps",
            ),
            pytest.param(
                False,
                {"include_homeassistant": True, "include_folders": ["share"]},
                ["need a Supervisor install"],
                id="core-folders",
            ),
            pytest.param(
                False,
                {"include_addons": ["core_ssh"]},
                ["need a Supervisor install"],
                id="core-apps",
            ),
            pytest.param(
                True,
                {"include_addons": ["core_mosquitto", "core_nope"]},
                ["apps not installed: core_nope"],
                id="app-not-installed",
            ),
        ],
    )
    async def test_selection_that_fails_holds_nothing_or_drops_an_app_is_refused(
        self, supervised, given, reasons
    ) -> None:
        """Supervisor leaves an app that is not installed out without an error."""
        ws = FakeWS()
        # A Core install has no Supervisor to ask for its apps.
        no_supervisor = None if supervised else OSError("no Supervisor")
        with pytest.raises(ToolError) as exc_info:
            await _run(
                create_backup,
                ws,
                _client(supervised=supervised),
                addons_error=no_supervisor,
                **given,
            )
        assert ErrorCode.VALIDATION_INVALID_PARAMETER.value in str(exc_info.value)
        for reason in reasons:
            assert reason in str(exc_info.value)
        assert not ws.generated()

    @pytest.mark.asyncio
    async def test_unreadable_app_list_starts_no_backup(self) -> None:
        ws = FakeWS()
        with pytest.raises(ToolError):
            await _run(
                create_backup,
                ws,
                _client(supervised=True),
                include_addons=["core_ssh"],
                addons_error=OSError("Supervisor unreachable"),
            )
        assert not ws.generated()

    @pytest.mark.asyncio
    async def test_note_of_a_custom_backup_names_only_the_password(self) -> None:
        """No app list is read without include_addons, and the full-backup
        sentences do not describe a custom backup."""
        ws = FakeWS()
        result = await _run(
            create_backup,
            ws,
            _client(supervised=True),
            include_homeassistant=True,
            addons_error=OSError("Supervisor unreachable"),
        )
        assert "password" in result["note"]
        assert "automatic-backup" not in result["note"]


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
        assert not ws.generated()


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
        assert not ws.generated()
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
        ("tool", "args", "kwargs"),
        [
            pytest.param(create_backup, (), {}, id="create"),
            pytest.param(
                restore_backup, ("b1",), {"safety_backup": True}, id="restore"
            ),
        ],
    )
    async def test_long_backup_sends_progress_heartbeats(
        self, tool, args, kwargs
    ) -> None:
        """#1861: without heartbeats a client drops a long-running call."""
        ws = FakeWS(events=[], details=_entry("b1"))
        ctx = MagicMock()
        ctx.report_progress = AsyncMock()

        async def _finish_later() -> None:
            await asyncio.sleep(0.05)
            ws.queue.put_nowait({"type": "event", "event": COMPLETED})

        with patch.object(backup_snapshot, "_BACKUP_PROGRESS_INTERVAL_S", 0.01):
            finisher = asyncio.create_task(_finish_later())
            await _run(tool, ws, _client(), *args, ctx=ctx, **kwargs)
            await asyncio.gather(finisher)
        assert ctx.report_progress.await_count >= 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(HomeAssistantConnectionError("closed"), id="closed"),
            pytest.param(HomeAssistantCommandTimeout("no reply"), id="no-answer"),
            pytest.param(OSError("broken pipe"), id="send-failed"),
            pytest.param(ConnectionClosed(None, None), id="closed-mid-write"),
        ],
    )
    async def test_listing_failure_after_the_backup_finished_says_to_check_the_list(
        self, error
    ) -> None:
        """The backup may exist; a retry without checking would make a second one."""
        ws = FakeWS()
        reply = ws.send_command

        async def _send(command: str, **kwargs: Any) -> dict[str, Any]:
            if command == "backup/info" and ws.generated():
                raise error
            return await reply(command, **kwargs)

        ws.send_command = _send
        with pytest.raises(ToolError) as exc_info:
            await _run(create_backup, ws, _client())
        assert ErrorCode.CONNECTION_FAILED.value in str(exc_info.value)
        assert "Check the snapshot list" in str(exc_info.value)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(OSError("broken pipe"), id="send-failed"),
            pytest.param(ConnectionClosed(None, None), id="closed-mid-write"),
            pytest.param(HomeAssistantConnectionError("closed"), id="closed"),
            pytest.param(HomeAssistantCommandTimeout("no reply"), id="no-answer"),
        ],
    )
    async def test_failed_send_of_the_backup_request_says_to_check_the_list(
        self, error
    ) -> None:
        """The request may have reached Home Assistant before the send failed."""
        ws = FakeWS(errors={"backup/generate": error})
        with pytest.raises(ToolError) as exc_info:
            await _run(create_backup, ws, _client())
        assert ErrorCode.CONNECTION_FAILED.value in str(exc_info.value)
        assert "Check the snapshot list" in str(exc_info.value)
        ws.release_subscription.assert_awaited_once_with(7)

    @pytest.mark.asyncio
    async def test_request_never_sent_is_not_reported_as_possibly_started(
        self,
    ) -> None:
        """Nothing reached Home Assistant, so there is no backup to look for."""
        ws = FakeWS(errors={"backup/generate": HomeAssistantCommandNotSent("closed")})
        with pytest.raises(ToolError) as exc_info:
            await _run(create_backup, ws, _client())
        assert "may have started the backup" not in str(exc_info.value)

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
