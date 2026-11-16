"""``ha_manage_backup(scope="snapshot")`` create and list, on Core's backup integration.

The snapshot actions follow Home Assistant's own contracts (#2632, #2786):

* a full backup is made as Home Assistant makes its own before a Core
  update: with the automatic-backup settings, else on the default agent;
  a custom backup holds the parts given and goes to the default agent;
* the list returns Core's ``backup/info`` reply.

Core makes every backup (``_backup_snapshot_core``): its local agent on a Core
install, a Supervisor answering Core on a Supervisor install. Waiting for the
backup is in ``test_backup_snapshot_waiting.py``, restore in
``test_backup_snapshot_restore.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.errors import ErrorCode
from ha_mcp.tools.backup_snapshot import create_backup, list_backups
from ha_mcp.tools.backup_snapshot_restore import restore_backup

from ._backup_snapshot_core import (
    APPS,
    PASSWORD,
    REMOTE,
    BackupInstall,
    after_core_receives,
    agent_backup,
)
from ._backup_snapshot_core import backup_install as backup_install
from ._backup_snapshot_core import tool_sockets as tool_sockets


def _parts(result: dict[str, Any]) -> tuple[bool, bool, list[str]]:
    """Whether a listed backup holds the configuration and database, and its apps."""
    apps = [app["slug"] for app in result["addons"]]
    return result["homeassistant_included"], result["database_included"], apps


class TestFullBackup:
    async def test_with_automatic_agents_it_is_home_assistants_automatic_backup(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """Contents, locations and name come from the settings; retention counts it."""
        await backup_install.core()
        await backup_install.configure(agent_ids=["backup.local"], password=PASSWORD)

        result = await create_backup(core_client)

        assert result["with_automatic_settings"] is True
        assert list(result["agents"]) == ["backup.local"]
        assert "retention" in result["note"]
        assert "warnings" not in result

    async def test_another_instances_backup_appearing_meanwhile_is_not_taken(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
    ) -> None:
        """A shared location lists other instances' backups too; Core marks only
        this instance's own automatic backups with with_automatic_settings."""
        await backup_install.core(remote=True)
        await backup_install.configure(agent_ids=[REMOTE], password=PASSWORD)
        other = agent_backup(
            "other-instance",
            f"Automatic backup {HA_VERSION}",
            extra_metadata={"instance_id": "another", "with_automatic_settings": True},
        )

        with after_core_receives(
            hass,
            "backup/generate_with_automatic_settings",
            lambda: backup_install.remote.hold(other),
        ):
            result = await create_backup(core_client)

        assert result["backup_id"] != "other-instance"
        assert result["with_automatic_settings"] is True
        assert "other-instance" in await backup_install.listed()

    async def test_a_name_an_automatic_backup_cannot_take_is_named_in_a_warning(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        await backup_install.core()
        await backup_install.configure(agent_ids=["backup.local"], password=PASSWORD)

        result = await create_backup(core_client, name="mine")

        assert result["name"] != "mine"
        assert "the name 'mine' was not used" in result["warnings"][-1]

    @pytest.mark.parametrize(
        ("mounts", "default_mount", "expected"),
        [
            pytest.param(("nas",), "nas", "hassio.nas", id="mount"),
            pytest.param(("nas",), None, "hassio.local", id="local"),
            pytest.param(None, None, "backup.local", id="core-install"),
        ],
    )
    async def test_without_automatic_agents_it_goes_to_the_default_agent(
        self,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        mounts: tuple[str, ...] | None,
        default_mount: str | None,
        expected: str,
    ) -> None:
        """Core rejects the automatic command without agents and raises a repair issue."""
        if mounts is None:
            await backup_install.core()
        else:
            await backup_install.supervisor(mounts=mounts, default_mount=default_mount)
        await backup_install.configure(password=PASSWORD)

        result = await create_backup(core_client)

        assert result["with_automatic_settings"] is False
        assert list(result["agents"]) == [expected]

    @pytest.mark.parametrize(
        ("settings", "database", "apps", "folder"),
        [
            pytest.param({}, True, [], None, id="core-defaults"),
            pytest.param(
                {
                    "include_database": False,
                    "include_addons": ["core_ssh"],
                    "include_folders": ["share"],
                },
                False,
                ["core_ssh"],
                "share",
                id="chosen-parts",
            ),
            pytest.param(
                {"include_all_addons": True}, True, list(APPS), None, id="every-app"
            ),
        ],
    )
    async def test_without_automatic_agents_it_holds_what_the_settings_hold(
        self,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        settings: dict[str, Any],
        database: bool,
        apps: list[str],
        folder: str | None,
    ) -> None:
        await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD, **settings)

        result = await create_backup(core_client)

        assert _parts(result) == (True, database, apps)
        assert folder is None or folder in result["folders"]
        assert result["agents"]["hassio.local"]["protected"] is True

    @pytest.mark.parametrize(
        ("supervised", "settings", "no_apps"),
        [
            pytest.param(True, {}, True, id="supervisor-without-apps"),
            pytest.param(
                True, {"include_addons": ["core_ssh"]}, False, id="with-an-app"
            ),
            pytest.param(False, {}, False, id="core-install"),
        ],
    )
    async def test_note_says_when_a_full_backup_holds_no_apps(
        self,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        supervised: bool,
        settings: dict[str, Any],
        no_apps: bool,
    ) -> None:
        """Settings that select no apps give a full backup without apps."""
        if supervised:
            await backup_install.supervisor()
        else:
            await backup_install.core()
        await backup_install.configure(password=PASSWORD, **settings)

        result = await create_backup(core_client)

        assert ("holds no apps" in result["note"]) is no_apps

    async def test_note_says_where_a_manual_full_backup_went(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)

        result = await create_backup(core_client)

        assert "default location" in result["note"]
        assert "automatic-backup settings" in result["note"]

    async def test_failed_install_probe_fails_instead_of_guessing_the_install(
        self,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A Supervisor install read as Core would pick an agent it does not have."""
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)
        monkeypatch.setattr(
            core_client,
            "get_config",
            AsyncMock(side_effect=HomeAssistantConnectionError("config unreachable")),
        )

        with pytest.raises(ToolError):
            await create_backup(core_client)

        assert await backup_install.listed() == {}

    async def test_create_without_default_password_is_refused(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        await backup_install.core()

        with pytest.raises(ToolError, match="No default backup password"):
            await create_backup(core_client)

        assert await backup_install.listed() == {}

    async def test_name_is_matched_as_home_assistant_stores_it(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """Core strips the name; an unstripped match would miss the new backup."""
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)

        result = await create_backup(core_client, name="  mine ")

        assert result["name"] == "mine"
        assert result["backup_id"] in await backup_install.listed()


class TestCustomBackup:
    async def test_one_app_alone_goes_to_the_default_agent(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """Parts not given are left out; the automatic-backup locations are not used."""
        await backup_install.supervisor(mounts=("nas",))
        await backup_install.configure(agent_ids=["hassio.nas"], password=PASSWORD)

        result = await create_backup(core_client, include_addons=["core_mosquitto"])

        assert list(result["agents"]) == ["hassio.local"]
        assert result["agents"]["hassio.local"]["protected"] is True
        assert _parts(result) == (False, False, ["core_mosquitto"])
        assert result["folders"] == []

    async def test_every_app_alone(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """Losing the flag would report an empty backup as a success."""
        await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD)

        result = await create_backup(core_client, include_all_addons=True)

        assert _parts(result) == (False, False, list(APPS))

    async def test_configuration_with_its_database_and_a_folder(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD)

        result = await create_backup(
            core_client,
            include_homeassistant=True,
            include_database=True,
            include_folders=["share"],
        )

        assert _parts(result) == (True, True, [])
        assert "share" in result["folders"]

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
        self,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        supervised: bool,
        given: dict[str, Any],
        reasons: list[str],
    ) -> None:
        """Supervisor leaves an app that is not installed out without an error."""
        if supervised:
            await backup_install.supervisor()
        else:
            await backup_install.core()
        await backup_install.configure(password=PASSWORD)

        with pytest.raises(ToolError) as exc_info:
            await create_backup(core_client, **given)

        assert ErrorCode.VALIDATION_INVALID_PARAMETER.value in str(exc_info.value)
        for reason in reasons:
            assert reason in str(exc_info.value)
        assert await backup_install.listed() == {}

    async def test_unreadable_app_list_starts_no_backup(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        supervisor = await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD)
        supervisor.apps_unreadable = True

        with pytest.raises(ToolError):
            await create_backup(core_client, include_addons=["core_ssh"])

        assert supervisor.backup_requests == []

    async def test_note_of_a_custom_backup_names_only_the_password(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """No app list is read without include_addons, and the full-backup
        sentences do not describe a custom backup."""
        supervisor = await backup_install.supervisor()
        await backup_install.configure(password=PASSWORD)
        supervisor.apps_unreadable = True

        result = await create_backup(core_client, include_homeassistant=True)

        assert "password" in result["note"]
        assert "automatic-backup" not in result["note"]


class TestConnection:
    # Core closes a socket whose token it refused; the client's close reply then
    # re-arms aiohttp's heartbeat timer on that closed server socket, which
    # nothing cancels (as in test_backup_auth_refusal.py).
    @pytest.mark.parametrize("expected_lingering_timers", [True])
    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            pytest.param(create_backup, (), id="create"),
            pytest.param(restore_backup, ("b1",), id="restore"),
        ],
    )
    async def test_connect_failure_keeps_the_reason(
        self,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        tool: Any,
        args: tuple[str, ...],
    ) -> None:
        """A refused token is reported as one, not as an unreachable Home Assistant."""
        await backup_install.core()
        refused = HomeAssistantClient(
            base_url=core_client.base_url, token="not-a-token", verify_ssl=False
        )
        try:
            with pytest.raises(ToolError, match="rejected the WebSocket token"):
                await tool(refused, *args)
        finally:
            await refused.close()

    async def test_cleanup_transport_error_keeps_a_finished_backup(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_install: BackupInstall,
        tool_sockets: list[HomeAssistantWebSocketClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A retry after a false failure would start a second backup."""
        await backup_install.core()
        await backup_install.configure(password=PASSWORD)

        def break_disconnect() -> None:
            monkeypatch.setattr(
                tool_sockets[-1],
                "disconnect",
                AsyncMock(side_effect=OSError("socket gone")),
            )

        with after_core_receives(hass, "backup/generate", break_disconnect):
            result = await create_backup(core_client)

        assert result["backup_id"] in await backup_install.listed()


class TestList:
    async def test_list_keeps_cores_fields_newest_first(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """Per-backup details and Core's top-level fields reach the caller."""
        await backup_install.core(remote=True)
        # Same second: as strings "...00Z" would sort after "...00.500000+00:00".
        for backup_id, date in (
            ("undated", "not a date"),
            ("old", "2026-10-01T00:00:00Z"),
            ("new", "2026-10-01T00:00:00.500000+00:00"),
        ):
            backup_install.remote.hold(agent_backup(backup_id, backup_id, date=date))

        result = await list_backups(core_client, limit=2)

        assert [b["backup_id"] for b in result["backups"]] == ["new", "old"]
        assert list(result["backups"][0]["agents"]) == [REMOTE]
        info = await backup_install.command("backup/info")
        for field in (
            "next_automatic_backup",
            "last_attempted_automatic_backup",
            "state",
        ):
            assert result[field] == info[field]
        assert (result["count"], result["total"]) == (2, 3)
        assert "warnings" not in result

    async def test_partial_list_says_so(
        self, core_client: HomeAssistantClient, backup_install: BackupInstall
    ) -> None:
        """A caller checking whether a backup landed must know the list is partial."""
        await backup_install.core(remote=True)
        backup_install.remote.unreachable = "unreachable"

        result = await list_backups(core_client)

        assert result["agent_errors"] == {REMOTE: "unreachable"}
        assert result["warnings"] == [
            f"Partial list: these backup agents failed to list their backups: {REMOTE}"
        ]

    async def test_list_without_the_backup_integration_raises(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Without ``backup`` Core does not answer ``backup/info``."""
        with pytest.raises(ToolError):
            await list_backups(core_client)
