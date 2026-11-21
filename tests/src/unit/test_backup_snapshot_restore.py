"""``ha_manage_backup(scope="snapshot", action="restore")`` on Core's backup integration.

A restore reads from an agent that holds the backup, follows the backup for
the database and, like Home Assistant's restore dialog, restores only the
apps (add-ons) and folders it is given (#2786). A safety backup of what the
restore replaces comes first only when asked for; a request Home Assistant
would refuse or mishandle stops before it.

Core runs every restore (``_backup_snapshot_core``): on a Core install it
leaves the restore for its restart (``.HA_RESTORE``), on a Supervisor install
it asks the Supervisor for it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time
from homeassistant.components.backup import async_get_manager
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.errors import ErrorCode
from ha_mcp.tools import backup_snapshot, backup_snapshot_restore
from ha_mcp.tools.backup_snapshot_restore import restore_backup

from ._backup_snapshot_core import (
    PASSWORD,
    REMOTE,
    BackupInstall,
    Heartbeats,
    after_core_receives,
    agent_backup,
    before_core_handles,
    drop_after_what_core_sent,
    instead_of_core,
    no_answer_from_now,
    spy_on_release,
)
from ._backup_snapshot_core import backup_install as backup_install
from ._backup_snapshot_core import tool_sockets as tool_sockets
from ._command_faults import received
from ._held_command import held_command
from ._ws_transport import abort_transport

ACCEPTED = "Home Assistant accepted the restore but did not confirm it"


async def _core_backup(install: BackupInstall, **fields: Any) -> str:
    """A backup Core's local agent holds; ``fields`` go to ``backup/generate``."""
    return await install.generate(
        **{"agent_ids": ["backup.local"], "password": PASSWORD, **fields}
    )


def _new_backup(listed: dict[str, Any], *known: str) -> dict[str, Any]:
    new = [backup for backup_id, backup in listed.items() if backup_id not in known]
    assert len(new) == 1, new
    return new[0]


class TestWhatIsRestored:
    async def test_unknown_backup_is_not_found_and_nothing_runs(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """An agent that failed to look for the backup is named, not hidden."""
        await backup_install.core(remote=True)
        backup_install.remote.unreachable = "unreachable"

        with pytest.raises(ToolError) as exc_info:
            await restore_backup(core_client, "nope", safety_backup=True)

        assert ErrorCode.RESOURCE_NOT_FOUND.value in str(exc_info.value)
        assert REMOTE in str(exc_info.value)
        assert await backup_install.listed() == {}
        assert backup_install.restarts == 0

    @pytest.mark.parametrize(
        ("agent_ids", "expected"),
        [
            pytest.param([REMOTE], REMOTE, id="remote-only"),
            pytest.param([REMOTE, "backup.local"], "backup.local", id="core-local"),
        ],
    )
    async def test_restore_reads_from_an_agent_that_holds_the_backup(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        agent_ids: list[str],
        expected: str,
    ) -> None:
        """Local storage needs no download; a remote-only backup restores from there."""
        await backup_install.core(remote=True)
        await backup_install.configure(password=PASSWORD)
        target = await _core_backup(backup_install, agent_ids=agent_ids)

        with received(hass, "backup/restore") as seen:
            await restore_backup(core_client, target)

        assert seen["backup/restore"][0]["agent_id"] == expected
        assert backup_install.restarts == 1

    async def test_restore_reads_a_mount_copy_from_the_mount(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """A backup stored on a network mount and off-site restores from the mount."""
        supervisor = await backup_install.supervisor(mounts=("nas",), remote=True)
        await backup_install.configure(password=PASSWORD)
        target = supervisor.add_backup("old", locations=("nas",))
        backup_install.remote.hold(agent_backup(target, "old"))

        await restore_backup(core_client, target)

        assert [r["location"] for r in supervisor.restores] == ["nas"]

    @pytest.mark.parametrize(
        ("included", "requested", "expected"),
        [
            pytest.param(True, None, True, id="default-with-database"),
            pytest.param(False, None, False, id="default-without-database"),
            pytest.param(True, False, False, id="core-may-keep-its-database"),
        ],
    )
    async def test_restore_database_follows_the_backup_unless_given(
        self,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        included: bool,
        requested: bool | None,
        expected: bool,
    ) -> None:
        """The database follows the backup unless given; a Core install may keep its own."""
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)
        target = await _core_backup(backup_install, include_database=included)

        result = await restore_backup(core_client, target, restore_database=requested)

        restore = await backup_install.restore_request()
        assert restore is not None
        assert restore["restore_database"] is expected
        assert result["restore_database"] is expected

    @pytest.mark.parametrize(
        ("given", "apps", "folders"),
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
        self,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        given: dict[str, Any],
        apps: list[str],
        folders: list[str],
    ) -> None:
        """Home Assistant's restore dialog starts with no apps or folders ticked.

        Restoring every one the backup holds would also roll back the
        configuration and data of each app.
        """
        supervisor = await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD)
        target = supervisor.add_backup(
            "old", apps=("core_ssh", "core_mosquitto"), folders=("share", "ssl")
        )

        result = await restore_backup(core_client, target, **given)

        restore = supervisor.restores[0]
        assert (restore.get("addons") or [], restore.get("folders") or []) == (
            apps,
            folders,
        )
        assert (result["restore_addons"], result["restore_folders"]) == (apps, folders)

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
                    "restore_addons": ["core_ssh", "core_mosquitto"],
                    "restore_folders": ["media"],
                },
                ["the backup holds no apps ['core_mosquitto']", "no folders ['media']"],
                id="supervisor-skips-what-the-backup-lacks",
            ),
        ],
    )
    async def test_restore_home_assistant_would_refuse_stops_before_the_safety_backup(
        self,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        supervised: bool,
        included: bool,
        given: dict[str, Any],
        reasons: list[str],
    ) -> None:
        """Home Assistant checks these only after the safety backup exists, or not at all.

        Each retry would leave another safety backup behind.
        """
        if supervised:
            supervisor = await backup_install.supervisor()
            target = supervisor.add_backup(
                "old", database=included, apps=("core_ssh",), folders=("share",)
            )
        else:
            await backup_install.core()
            target = await _core_backup(backup_install, include_database=included)
        await backup_install.configure(password=PASSWORD)

        with pytest.raises(ToolError) as exc_info:
            await restore_backup(core_client, target, safety_backup=True, **given)

        assert ErrorCode.VALIDATION_INVALID_PARAMETER.value in str(exc_info.value)
        assert f'"backup_id": "{target}"' in str(exc_info.value)
        for reason in reasons:
            assert reason in str(exc_info.value)
        assert list(await backup_install.listed()) == [target]
        assert await backup_install.restore_request() is None
        if supervised:
            assert supervisor.operations == []

    @pytest.mark.parametrize("encrypted", [True, False], ids=["encrypted", "plain"])
    async def test_password_is_sent_only_for_an_encrypted_backup(
        self,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        encrypted: bool,
    ) -> None:
        """Home Assistant refuses a restore whose password does not fit the backup."""
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)
        if not encrypted:
            await backup_install.command(
                "backup/config/update", agents={"backup.local": {"protected": False}}
            )
        target = await _core_backup(backup_install)

        await restore_backup(core_client, target)

        restore = await backup_install.restore_request()
        assert restore is not None
        assert restore["password"] == (PASSWORD if encrypted else None)

    async def test_restore_without_safety_backup_needs_no_password(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """The apps and folders given are still restored."""
        supervisor = await backup_install.supervisor()
        target = supervisor.add_backup(
            "old", apps=("core_ssh",), folders=("share",), password=None
        )

        result = await restore_backup(
            core_client, target, restore_addons=["core_ssh"], restore_folders=["share"]
        )

        assert supervisor.operations == ["restore"]
        restore = supervisor.restores[0]
        assert "password" not in restore
        assert (restore["addons"], restore["folders"]) == (["core_ssh"], ["share"])
        assert result["safety_backup_id"] is None


class TestSafetyBackup:
    async def test_no_backup_is_made_unless_asked_for(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """A full backup the user did not ask for takes a retention slot."""
        await backup_install.core()
        await backup_install.configure(agent_ids=["backup.local"], password=PASSWORD)
        target = await _core_backup(backup_install)

        result = await restore_backup(core_client, target)

        assert list(await backup_install.listed()) == [target]
        assert backup_install.restarts == 1
        assert result["safety_backup_id"] is None
        assert "safety_backup=false" in result["note"]

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
        self,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        supervised: bool,
        included: bool,
        given: dict[str, Any],
    ) -> None:
        """Configuration, plus the database, apps and folders the restore takes."""
        if supervised:
            supervisor = await backup_install.supervisor()
            target = supervisor.add_backup(
                "old", database=included, apps=("core_ssh",), folders=("share",)
            )
        else:
            await backup_install.core()
            target = await _core_backup(backup_install, include_database=included)
        await backup_install.configure(password=PASSWORD)

        result = await restore_backup(core_client, target, safety_backup=True, **given)

        safety = (await backup_install.listed())[result["safety_backup_id"]]
        default_agent = "hassio.local" if supervised else "backup.local"
        assert list(safety["agents"]) == [default_agent]
        assert safety["agents"][default_agent]["protected"] is True
        assert safety["name"].startswith("PreRestore_Safety_")
        assert (safety["homeassistant_included"], safety["database_included"]) == (
            True,
            included,
        )
        assert [app["slug"] for app in safety["addons"]] == given.get(
            "restore_addons", []
        )
        assert set(given.get("restore_folders", [])) <= set(safety["folders"])
        if supervised:
            assert supervisor.operations == ["backup", "restore"]
        else:
            assert backup_install.restarts == 1

    async def test_safety_backup_without_default_password_is_refused(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """ha-mcp makes no backup without the default password; nothing runs."""
        await backup_install.core()
        target = await _core_backup(backup_install, password=None)

        with pytest.raises(ToolError, match="no default backup password"):
            await restore_backup(core_client, target, safety_backup=True)

        assert list(await backup_install.listed()) == [target]
        assert backup_install.restarts == 0

    async def test_safety_backup_is_never_an_automatic_backup(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """An automatic backup could get the backup being restored deleted.

        Home Assistant's retention trims the automatic backups beyond the
        configured copies, oldest first, and Home Assistant Cloud keeps a single
        backup per instance.
        """
        await backup_install.core(remote=True)
        await backup_install.configure(agent_ids=[REMOTE], password=PASSWORD)
        target = await _core_backup(backup_install)

        result = await restore_backup(core_client, target, safety_backup=True)

        safety = (await backup_install.listed())[result["safety_backup_id"]]
        assert safety["with_automatic_settings"] is False
        assert list(safety["agents"]) == ["backup.local"]

    async def test_rejected_restore_names_the_backup_and_its_safety_backup(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """A retry makes another safety backup; the error names the first.

        The backup was made with another password than the default one.
        """
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)
        target = await _core_backup(backup_install, password="an-older-password")

        with pytest.raises(ToolError) as exc_info:
            await restore_backup(core_client, target, safety_backup=True)

        safety = _new_backup(await backup_install.listed(), target)
        assert f'"backup_id": "{target}"' in str(exc_info.value)
        assert f'"safety_backup_id": "{safety["backup_id"]}"' in str(exc_info.value)
        assert "Home Assistant may be restoring" in str(exc_info.value)
        assert backup_install.restarts == 0

    async def test_busy_manager_after_the_safety_backup_names_it(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
    ) -> None:
        """Another backup operation can start between the safety backup and the restore."""
        supervisor = await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD)
        target = supervisor.add_backup("old")
        subscriptions: list[str] = []

        def another_backup_on_the_restores_subscription() -> None:
            subscriptions.append("subscribe")
            if len(subscriptions) == 2:
                supervisor.hold = True
                hass.async_create_task(
                    async_get_manager(hass).async_initiate_backup(
                        agent_ids=["hassio.local"],
                        include_addons=None,
                        include_all_addons=False,
                        include_database=True,
                        include_folders=None,
                        include_homeassistant=True,
                        name="another",
                        password=None,
                    ),
                    eager_start=True,
                )

        with (
            before_core_handles(
                hass,
                "backup/subscribe_events",
                another_backup_on_the_restores_subscription,
            ),
            pytest.raises(ToolError, match="busy") as exc_info,
        ):
            await restore_backup(core_client, target, safety_backup=True)

        safety = _new_backup(await backup_install.listed(), target)
        assert f'"safety_backup_id": "{safety["backup_id"]}"' in str(exc_info.value)
        assert supervisor.restores == []
        supervisor.release()
        await hass.async_block_till_done()

    @pytest.mark.parametrize(
        "failed",
        [
            pytest.param({"failed_apps": ["core_ssh"]}, id="app"),
            pytest.param({"failed_folders": ["share"]}, id="folder"),
        ],
    )
    async def test_safety_backup_missing_what_the_restore_replaces_stops_it(
        self,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        failed: dict[str, list[str]],
    ) -> None:
        """Overwriting an app or folder the safety backup lacks could not be undone."""
        supervisor = await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD)
        target = supervisor.add_backup("old", apps=("core_ssh",), folders=("share",))
        for part, names in failed.items():
            setattr(supervisor, part, names)

        with pytest.raises(ToolError) as exc_info:
            await restore_backup(
                core_client,
                target,
                safety_backup=True,
                restore_addons=["core_ssh"],
                restore_folders=["share"],
            )

        safety = _new_backup(await backup_install.listed(), target)
        assert safety["backup_id"] in str(exc_info.value)
        assert supervisor.restores == []

    async def test_failed_safety_backup_stops_the_restore(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        supervisor = await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD)
        target = supervisor.add_backup("old")
        supervisor.job_error = "No space left on device"

        with pytest.raises(ToolError) as exc_info:
            await restore_backup(core_client, target, safety_backup=True)

        assert ErrorCode.SERVICE_CALL_FAILED.value in str(exc_info.value)
        assert supervisor.restores == []

    async def test_failure_before_the_safety_backup_names_no_safety_backup(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Nothing was backed up, so there is no safety backup to point at.

        Without ``backup`` Core does not answer ``backup/details``.
        """
        with pytest.raises(ToolError) as exc_info:
            await restore_backup(core_client, "b1", safety_backup=True)

        assert "Unknown command" in str(exc_info.value)
        assert "safety_backup_id" not in str(exc_info.value)


class TestTheAnswer:
    async def test_restore_home_assistant_answered_carries_no_unconfirmed_warning(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """A reply confirms the restore; warning otherwise invites a needless check."""
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)
        target = await _core_backup(backup_install)

        result = await restore_backup(core_client, target)

        assert not [w for w in result["warnings"] if "did not" in w]

    async def test_restart_for_the_restore_is_not_a_failure(
        self,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        tool_sockets: list[HomeAssistantWebSocketClient],
    ) -> None:
        """Home Assistant restarts before it answers a real restore.

        Reporting that as a failure invites a retry: a second safety backup
        and a second restore.
        """
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)
        target = await _core_backup(backup_install)
        backup_install.on_restart = lambda: drop_after_what_core_sent(tool_sockets[-1])

        result = await restore_backup(core_client, target)

        assert result["warnings"][-1].startswith(ACCEPTED)
        assert backup_install.restarts == 1

    async def test_restore_events_read_only_after_the_close_still_count(
        self,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        tool_sockets: list[HomeAssistantWebSocketClient],
    ) -> None:
        """The close can come before the tool read Home Assistant's events.

        Here it reads none while the restore runs: its client stopped taking
        heartbeats.
        """
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)
        target = await _core_backup(backup_install)
        backup_install.on_restart = lambda: drop_after_what_core_sent(tool_sockets[-1])

        result = await restore_backup(
            core_client, target, ctx=Heartbeats(stalled=True).context
        )

        assert result["warnings"][-1].startswith(ACCEPTED)

    async def test_restore_still_running_at_the_timeout_is_not_a_failure(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A long Supervisor restore can outlast the command timeout."""
        supervisor = await backup_install.supervisor()
        target = supervisor.add_backup("old", password=None)
        supervisor.hold = True
        monkeypatch.setattr(backup_snapshot_restore, "BACKUP_MAX_WAIT_S", 0.1)

        result = await restore_backup(core_client, target)

        assert result["warnings"][-1].startswith(
            "Home Assistant took the restore but did not answer within"
        )
        supervisor.release()
        await hass.async_block_till_done()

    async def test_restore_home_assistant_never_took_is_a_failure_when_closed(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        tool_sockets: list[HomeAssistantWebSocketClient],
    ) -> None:
        """Without a restore event the close does not show a restore started."""
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)
        target = await _core_backup(backup_install)

        with (
            instead_of_core(
                hass, "backup/restore", lambda: abort_transport(tool_sockets[-1])
            ),
            pytest.raises(ToolError),
        ):
            await restore_backup(core_client, target)

        assert backup_install.restarts == 0

    async def test_restore_home_assistant_never_took_is_a_failure_at_the_timeout(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)
        target = await _core_backup(backup_install)
        monkeypatch.setattr(backup_snapshot_restore, "BACKUP_MAX_WAIT_S", 0.1)

        async with held_command(hass, "backup/restore"):
            with pytest.raises(ToolError, match="timeout"):
                await restore_backup(core_client, target)

    async def test_restore_never_sent_is_a_failure(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        tool_sockets: list[HomeAssistantWebSocketClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Another client's restore event must not read as this one's."""
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)
        target = await _core_backup(backup_install)

        with (
            after_core_receives(
                hass,
                "backup/subscribe_events",
                lambda: no_answer_from_now(monkeypatch, tool_sockets[-1], "never-sent"),
            ),
            pytest.raises(ToolError),
        ):
            await restore_backup(core_client, target)

        assert backup_install.restarts == 0

    async def test_busy_backup_manager_stops_a_restore_without_safety_backup(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        tool_sockets: list[HomeAssistantWebSocketClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Another operation's replayed event must not read as this restore's."""
        supervisor = await backup_install.supervisor()
        target = supervisor.add_backup("old", password=None)
        supervisor.hold = True
        await backup_install.command("backup/generate", agent_ids=["hassio.local"])
        released: list[AsyncMock] = []

        with (
            after_core_receives(
                hass,
                "backup/subscribe_events",
                lambda: released.append(spy_on_release(monkeypatch, tool_sockets[-1])),
            ),
            pytest.raises(ToolError, match="busy"),
        ):
            await restore_backup(core_client, target)

        assert supervisor.restores == []
        released[0].assert_awaited_once()
        supervisor.release()
        await hass.async_block_till_done()


class TestHeartbeats:
    async def test_long_restore_sends_progress_heartbeats_until_it_is_answered(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1861: a restore can run for minutes before Home Assistant replies.

        It finishes only once three heartbeats have been sent; with no gap
        between heartbeats any left running would show at once.
        """
        supervisor = await backup_install.supervisor()
        target = supervisor.add_backup("old", password=None)
        supervisor.hold = True
        monkeypatch.setattr(backup_snapshot_restore, "BACKUP_PROGRESS_INTERVAL_S", 0)

        def finish_after_three(reports: int) -> None:
            if reports == 3:
                supervisor.hold = False
                supervisor.release()

        heartbeats = Heartbeats(finish_after_three)
        await restore_backup(core_client, target, ctx=heartbeats.context)
        sent = len(heartbeats.reports)
        await hass.async_block_till_done()

        assert sent >= 3
        assert len(heartbeats.reports) == sent

    async def test_restore_progress_counts_from_the_start_of_the_call(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """It never drops below the safety backup's, which started later.

        The safety backup takes 100 seconds of the clock.
        """
        supervisor = await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD)
        target = supervisor.add_backup("old")
        supervisor.hold = True
        for module in (backup_snapshot, backup_snapshot_restore):
            monkeypatch.setattr(module, "BACKUP_MAX_WAIT_S", 1000)
        refresh_token = hass.auth.async_validate_access_token(core_client.token)
        assert refresh_token is not None

        with freeze_time("2026-11-01 00:00:00", tick=True) as frozen:

            def pass_100_seconds_then_finish(reports: int) -> None:
                if reports == 1:
                    frozen.tick(100)
                    supervisor.hold = False
                    supervisor.release()

            heartbeats = Heartbeats(pass_100_seconds_then_finish)
            # Core takes no token issued after the frozen clock's time.
            client = HomeAssistantClient(
                base_url=core_client.base_url,
                token=hass.auth.async_create_access_token(refresh_token),
                verify_ssl=False,
            )
            try:
                await restore_backup(
                    client, target, ctx=heartbeats.context, safety_backup=True
                )
            finally:
                await client.close()

        restore = [
            p for p, message in heartbeats.reports if "restore" in (message or "")
        ]
        assert restore
        assert min(restore) >= 100
