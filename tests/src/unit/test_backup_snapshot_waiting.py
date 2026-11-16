"""Waiting for a snapshot backup, on Core's backup integration.

Completion comes from ``backup/subscribe_events``; the result is the backup
the job added to ``backup/info``. Core makes every backup
(``_backup_snapshot_core``). A dropped connection or a missing answer is the
one thing the test brings about on the tool's own WebSocket; the heartbeat
tests also cover restore.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import instance_id

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.errors import ErrorCode
from ha_mcp.tools import backup_snapshot
from ha_mcp.tools.backup_snapshot import create_backup
from ha_mcp.tools.backup_snapshot_restore import restore_backup

from ._backup_snapshot_core import (
    PASSWORD,
    REMOTE,
    BackupInstall,
    Heartbeats,
    after_core_receives,
    agent_backup,
    no_answer_from_now,
    spy_on_release,
    when_backup_event,
)
from ._backup_snapshot_core import backup_install as backup_install
from ._backup_snapshot_core import tool_sockets as tool_sockets
from ._ws_transport import abort_transport

AUTOMATIC = f"Automatic backup {HA_VERSION}"
NO_ANSWER = [
    pytest.param("closed", id="closed"),
    pytest.param("no-answer", id="no-answer"),
    pytest.param("send-failed", id="send-failed"),
    pytest.param("closed-mid-write", id="closed-mid-write"),
]


async def _older_automatic_backup(hass: HomeAssistant, install: BackupInstall) -> None:
    """An automatic backup this instance made earlier, held by the remote location."""
    own = await instance_id.async_get(hass)
    install.remote.hold(
        agent_backup(
            "older",
            AUTOMATIC,
            date="2020-01-01T00:00:00+00:00",
            extra_metadata={"instance_id": own, "with_automatic_settings": True},
        )
    )


class TestTheResult:
    async def test_result_is_the_backup_the_job_added(
        self,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        tool_sockets: list[HomeAssistantWebSocketClient],
    ) -> None:
        """On Supervisor the job id is not the backup id; the listing names the backup."""
        supervisor = await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD)

        result = await create_backup(core_client)

        assert result["backup_id"] in supervisor.backups
        assert result["backup_id"] not in supervisor.jobs
        assert list(result["agents"]) == ["hassio.local"]
        assert "warnings" not in result
        assert tool_sockets[0].websocket is None

    async def test_older_backup_with_the_same_name_is_not_taken(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """Home Assistant does not keep names unique; a reused name must not match."""
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)
        first = await create_backup(core_client, name="mine")

        second = await create_backup(core_client, name="mine")

        assert second["backup_id"] != first["backup_id"]
        assert set(await backup_install.listed()) == {
            first["backup_id"],
            second["backup_id"],
        }

    async def test_same_named_backup_arriving_meanwhile_is_not_guessed(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
    ) -> None:
        """Another client's backup of the same name lands during the job."""
        await backup_install.core(remote=True)
        await backup_install.configure(password=PASSWORD)
        other = agent_backup("other-client", "mine", extra_metadata={})

        with (
            after_core_receives(
                hass, "backup/generate", lambda: backup_install.remote.hold(other)
            ),
            pytest.raises(ToolError, match="lists 2 new backups") as exc_info,
        ):
            await create_backup(core_client, name="mine")

        assert "Check the snapshot list" in str(exc_info.value)

    async def test_failed_upload_does_not_take_an_older_backup_for_its_own(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
    ) -> None:
        """A backup on an agent that failed the first listing may be old."""
        await backup_install.core(remote=True)
        await backup_install.configure(agent_ids=[REMOTE], password=PASSWORD)
        await _older_automatic_backup(hass, backup_install)
        backup_install.remote.unreachable = "unreachable"
        backup_install.remote.upload_error = "upload refused"

        with pytest.raises(ToolError, match="Backup creation failed"):
            await create_backup(core_client)

    async def test_new_backup_is_found_next_to_one_an_unlisted_agent_held(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
    ) -> None:
        """A failing agent's older backup does not hide the job's own.

        The job's backup is also on that agent: one listed agent holding it
        is enough.
        """
        await backup_install.core(remote=True)
        await backup_install.configure(
            agent_ids=["backup.local", REMOTE], password=PASSWORD
        )
        await _older_automatic_backup(hass, backup_install)
        backup_install.remote.unreachable = "unreachable"

        result = await create_backup(core_client)

        assert result["backup_id"] != "older"
        assert set(result["agents"]) == {"backup.local", REMOTE}

    async def test_backup_only_unlisted_agents_hold_names_their_errors(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """It may predate the job, so it is not named; the error says why."""
        await backup_install.core(remote=True)
        await backup_install.configure(agent_ids=[REMOTE], password=PASSWORD)
        backup_install.remote.unreachable = "timeout"

        with pytest.raises(ToolError, match="lists 0 new backups") as exc_info:
            await create_backup(core_client)

        assert "agent_errors_before_job" in str(exc_info.value)
        assert "timeout" in str(exc_info.value)

    async def test_finished_job_whose_backup_is_not_listed_is_an_error(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
    ) -> None:
        """The only agent holding it stops answering once the job is done."""
        await backup_install.core(remote=True)
        await backup_install.configure(agent_ids=[REMOTE], password=PASSWORD)

        def lose_the_location() -> None:
            backup_install.remote.unreachable = "gone"

        when_backup_event(hass, "create_backup", "completed", lose_the_location)

        with pytest.raises(ToolError, match="lists 0 new backups") as exc_info:
            await create_backup(core_client)

        assert ErrorCode.SERVICE_CALL_FAILED.value in str(exc_info.value)

    async def test_failed_backup_is_reported_as_failure(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        supervisor = await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD)
        supervisor.job_error = "No space left on device"

        with pytest.raises(ToolError, match="Backup creation failed") as exc_info:
            await create_backup(core_client)

        assert ErrorCode.SERVICE_CALL_FAILED.value in str(exc_info.value)
        assert supervisor.backups == {}

    async def test_backup_kept_after_a_failed_upload_is_returned_with_a_warning(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """Core keeps the backup on the agents whose upload worked."""
        await backup_install.core(remote=True)
        await backup_install.configure(
            agent_ids=["backup.local", REMOTE], password=PASSWORD
        )
        backup_install.remote.upload_error = "upload refused"

        result = await create_backup(core_client)

        assert result["backup_id"] in await backup_install.listed()
        assert f"Not stored on backup agents: {REMOTE}" in result["warnings"]
        assert result["warnings"][-1] == (
            "Home Assistant reported the upload as failed; the backup is "
            "stored on: backup.local"
        )

    async def test_failed_upload_without_a_stored_backup_is_a_failure(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        await backup_install.core(remote=True)
        await backup_install.configure(agent_ids=[REMOTE], password=PASSWORD)
        backup_install.remote.upload_error = "upload refused"

        with pytest.raises(ToolError, match="Backup creation failed"):
            await create_backup(core_client)

    async def test_apps_and_folders_left_out_are_named_in_warnings(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """Core reports a backup as done when apps or folders failed."""
        supervisor = await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD)
        supervisor.failed_apps = ["core_ssh"]
        supervisor.failed_folders = ["media"]

        result = await create_backup(
            core_client, include_addons=["core_ssh"], include_folders=["media"]
        )

        assert result["warnings"] == [
            "Apps (add-ons) that could not be backed up: core_ssh",
            "Folders that could not be backed up: media",
        ]


class TestBusyOrSlow:
    async def test_another_running_backup_is_reported_before_starting(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        tool_sockets: list[HomeAssistantWebSocketClient],
    ) -> None:
        """Another operation's events must not be read as this job's."""
        supervisor = await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD)
        supervisor.hold = True
        await backup_install.command(
            "backup/generate", agent_ids=["hassio.local"], password=PASSWORD
        )

        with pytest.raises(ToolError, match="busy"):
            await create_backup(core_client)

        supervisor.release()
        await hass.async_block_till_done()
        assert len(supervisor.backup_requests) == 1
        assert tool_sockets[0].websocket is None

    async def test_backup_manager_still_starting_is_reported_before_starting(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
    ) -> None:
        """Core keeps the backup manager blocked until Home Assistant has started."""
        hass.set_state(CoreState.starting)
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)

        with pytest.raises(ToolError, match="busy"):
            await create_backup(core_client)

        assert await backup_install.listed() == {}

    async def test_timeout_says_the_backup_may_still_be_running(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A retry after a timeout must not start a second backup blindly."""
        supervisor = await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD)
        supervisor.hold = True
        monkeypatch.setattr(backup_snapshot, "_BACKUP_MAX_WAIT_S", 0.05)
        monkeypatch.setattr(backup_snapshot, "_BACKUP_PROGRESS_INTERVAL_S", 0.01)

        with pytest.raises(ToolError) as exc_info:
            await create_backup(core_client)

        assert ErrorCode.TIMEOUT_OPERATION.value in str(exc_info.value)
        assert "likely_in_progress" in str(exc_info.value)
        assert "Check the snapshot list" in str(exc_info.value)
        supervisor.release()
        await hass.async_block_till_done()

    async def test_closed_connection_says_the_backup_may_still_be_running(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        tool_sockets: list[HomeAssistantWebSocketClient],
    ) -> None:
        supervisor = await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD)
        supervisor.hold = True

        def drop_once_waiting(reports: int) -> None:
            if reports == 1:
                abort_transport(tool_sockets[-1])

        with pytest.raises(ToolError) as exc_info:
            await create_backup(core_client, ctx=Heartbeats(drop_once_waiting).context)

        assert ErrorCode.CONNECTION_FAILED.value in str(exc_info.value)
        assert "likely_in_progress" in str(exc_info.value)
        assert "Check the snapshot list" in str(exc_info.value)
        supervisor.release()
        await hass.async_block_till_done()

    @pytest.mark.parametrize("tool", ["create", "restore"])
    async def test_long_backup_sends_progress_heartbeats(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        monkeypatch: pytest.MonkeyPatch,
        tool: str,
    ) -> None:
        """#1861: without heartbeats a client drops a long-running call.

        The backup finishes only once three have been sent.
        """
        supervisor = await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD)
        target = supervisor.add_backup("old")
        supervisor.hold = True
        monkeypatch.setattr(backup_snapshot, "_BACKUP_PROGRESS_INTERVAL_S", 0.01)

        def finish_after_three(reports: int) -> None:
            if reports == 3:
                supervisor.hold = False
                supervisor.release()

        ctx = Heartbeats(finish_after_three).context
        if tool == "create":
            result: dict[str, Any] = await create_backup(core_client, ctx=ctx)
        else:
            result = await restore_backup(
                core_client, target, ctx=ctx, safety_backup=True
            )

        assert result["success"] is True
        await hass.async_block_till_done()


class TestConnectionLost:
    @pytest.mark.parametrize("how", NO_ANSWER)
    async def test_listing_failure_after_the_backup_finished_says_to_check_the_list(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        tool_sockets: list[HomeAssistantWebSocketClient],
        monkeypatch: pytest.MonkeyPatch,
        how: str,
    ) -> None:
        """The backup exists; a retry without checking would make a second one."""
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)

        with (
            after_core_receives(
                hass,
                "backup/generate",
                lambda: no_answer_from_now(monkeypatch, tool_sockets[-1], how),
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await create_backup(core_client)

        assert ErrorCode.CONNECTION_FAILED.value in str(exc_info.value)
        assert "Check the snapshot list" in str(exc_info.value)
        await hass.async_block_till_done()
        assert len(await backup_install.listed()) == 1

    @pytest.mark.parametrize("how", NO_ANSWER)
    async def test_failed_send_of_the_backup_request_says_to_check_the_list(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        tool_sockets: list[HomeAssistantWebSocketClient],
        monkeypatch: pytest.MonkeyPatch,
        how: str,
    ) -> None:
        """The request may have reached Home Assistant before the send failed."""
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)
        released: list[AsyncMock] = []

        def fail_the_request() -> None:
            released.append(spy_on_release(monkeypatch, tool_sockets[-1]))
            no_answer_from_now(monkeypatch, tool_sockets[-1], how)

        with (
            after_core_receives(hass, "backup/subscribe_events", fail_the_request),
            pytest.raises(ToolError) as exc_info,
        ):
            await create_backup(core_client)

        assert ErrorCode.CONNECTION_FAILED.value in str(exc_info.value)
        assert "may have started the backup" in str(exc_info.value)
        assert "Check the snapshot list" in str(exc_info.value)
        released[0].assert_awaited_once()

    async def test_request_never_sent_is_not_reported_as_possibly_started(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        tool_sockets: list[HomeAssistantWebSocketClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing reached Home Assistant, so there is no backup to look for."""
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)

        with (
            after_core_receives(
                hass,
                "backup/subscribe_events",
                lambda: no_answer_from_now(monkeypatch, tool_sockets[-1], "never-sent"),
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await create_backup(core_client)

        assert "may have started the backup" not in str(exc_info.value)
        assert await backup_install.listed() == {}

    async def test_subscription_is_released_when_the_backup_cannot_start(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        tool_sockets: list[HomeAssistantWebSocketClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Core keeps an unreleased subscription for the socket's lifetime."""
        supervisor = await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD)
        supervisor.refusal = "No space left on device"
        released: list[AsyncMock] = []

        with (
            after_core_receives(
                hass,
                "backup/subscribe_events",
                lambda: released.append(spy_on_release(monkeypatch, tool_sockets[-1])),
            ),
            pytest.raises(ToolError, match="No space left on device"),
        ):
            await create_backup(core_client)

        released[0].assert_awaited_once()
