"""``ha_manage_backup(scope="snapshot", action="delete")`` on Core's backup integration (#1861).

A full snapshot may be the last recovery point after an agent broke
something, so deleting one is layered:

L0. ``enable_snapshot_delete`` (default False): a human-only opt-in.
L1. A backup Home Assistant cannot confirm as manual (a scheduled one, or one
    another install made) is never deletable.
L2. An age floor (``snapshot_delete_min_age_days``, default 7; 0 turns it off).
L3. The single newest snapshot is never deletable, checked before L2.
Plus ``confirm=True``.

Core lists and deletes every backup (``_backup_snapshot_core``): the backups
sit at another integration's location (``RemoteAgent``) with the date and
origin a test gives them, and the tool's clock reads ``NOW``.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import instance_id

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import (
    DEFAULT_COMMAND_WAIT_TIMEOUT,
    HomeAssistantWebSocketClient,
)
from ha_mcp.config import reset_global_settings
from ha_mcp.errors import ErrorCode
from ha_mcp.tools import backup_snapshot
from ha_mcp.tools.backup_snapshot import delete_backup

from ._backup_snapshot_core import (
    REMOTE,
    BackupInstall,
    Heartbeats,
    after_core_receives,
    agent_backup,
    before_core_handles,
    no_answer_from_now,
)
from ._backup_snapshot_core import backup_install as backup_install
from ._backup_snapshot_core import tool_sockets as tool_sockets
from ._client_spies import sent_commands
from ._module_clock import set_module_clock
from ._tool_errors import rejected_call_body

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
OLD = timedelta(days=100)


def allow_deletes(monkeypatch: pytest.MonkeyPatch, min_age_days: int = 7) -> None:
    """A human's opt-in, as the server reads it from its environment."""
    monkeypatch.setenv("ENABLE_SNAPSHOT_DELETE", "true")
    monkeypatch.setenv("SNAPSHOT_DELETE_MIN_AGE_DAYS", str(min_age_days))
    reset_global_settings()


class Snapshots:
    """The backups at the remote location, as Core lists them."""

    def __init__(self, install: BackupInstall, our_instance: str) -> None:
        self.install = install
        self.remote = install.remote
        self._our_instance = our_instance

    def add(
        self,
        backup_id: str,
        *,
        age: timedelta = OLD,
        automatic: bool = False,
        instance: str | None = None,
        **fields: Any,
    ) -> None:
        """A backup made ``age`` before ``NOW`` by this install or ``instance``."""
        metadata = {
            "instance_id": instance or self._our_instance,
            "with_automatic_settings": automatic,
        }
        values = {
            "date": (NOW - age).isoformat(),
            "extra_metadata": metadata,
            **fields,
        }
        self.remote.hold(agent_backup(backup_id, f"{backup_id} backup", **values))

    async def listed(self) -> set[str]:
        return set(await self.install.listed())


@pytest.fixture
async def snapshots(
    hass: HomeAssistant,
    backup_install: BackupInstall,
    monkeypatch: pytest.MonkeyPatch,
) -> Snapshots:
    set_module_clock(monkeypatch, backup_snapshot, NOW)
    await backup_install.core(remote=True)
    return Snapshots(backup_install, await instance_id.async_get(hass))


async def refusal(core_client: HomeAssistantClient, backup_id: str) -> dict[str, Any]:
    """The tool's rejection of deleting ``backup_id``, confirmed."""
    return await rejected_call_body(delete_backup(core_client, backup_id, confirm=True))


class TestSnapshotDeleteGate:
    async def test_disabled_by_default_refuses_without_reaching_home_assistant(
        self,
        core_client: HomeAssistantClient,
        snapshots: Snapshots,
        tool_sockets: list[HomeAssistantWebSocketClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ENABLE_SNAPSHOT_DELETE", raising=False)
        reset_global_settings()
        snapshots.add("target")
        snapshots.add("other", age=timedelta(days=1))

        body = await refusal(core_client, "target")

        assert body["error"]["code"] == ErrorCode.VALIDATION_INVALID_PARAMETER.value
        assert "enable_snapshot_delete" in body["error"]["message"]
        assert tool_sockets == []
        assert "target" in await snapshots.listed()

    async def test_unconfirmed_delete_is_refused_without_reaching_home_assistant(
        self,
        core_client: HomeAssistantClient,
        snapshots: Snapshots,
        tool_sockets: list[HomeAssistantWebSocketClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        allow_deletes(monkeypatch)
        snapshots.add("target")
        snapshots.add("other", age=timedelta(days=1))

        body = await rejected_call_body(
            delete_backup(core_client, "target", confirm=False)
        )

        assert body["error"]["code"] == ErrorCode.VALIDATION_INVALID_PARAMETER.value
        assert "confirm" in body["error"]["message"].lower()
        assert tool_sockets == []
        assert "target" in await snapshots.listed()


class TestSnapshotDeleteLookup:
    async def test_unknown_backup_is_reported_not_found(
        self,
        core_client: HomeAssistantClient,
        snapshots: Snapshots,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Core's own backup/delete does nothing for an unknown id and reports success."""
        allow_deletes(monkeypatch)
        snapshots.add("other")

        body = await refusal(core_client, "missing-id")

        assert body["error"]["code"] == ErrorCode.RESOURCE_NOT_FOUND.value


class TestSnapshotDeleteGuards:
    @pytest.mark.parametrize(
        ("automatic", "instance"),
        [
            pytest.param(True, None, id="scheduled"),
            # Core reports with_automatic_settings=None for it.
            pytest.param(False, "another-install", id="another-installs"),
        ],
    )
    async def test_backup_not_provably_manual_is_kept(
        self,
        core_client: HomeAssistantClient,
        snapshots: Snapshots,
        monkeypatch: pytest.MonkeyPatch,
        automatic: bool,
        instance: str | None,
    ) -> None:
        """L1: a scheduled backup is the user's safety net; one of unknown origin
        fails closed."""
        allow_deletes(monkeypatch)
        snapshots.add("target", automatic=automatic, instance=instance)
        snapshots.add("other", age=timedelta(days=1))

        body = await refusal(core_client, "target")

        assert body["error"]["code"] == ErrorCode.VALIDATION_INVALID_PARAMETER.value
        assert "automatic" in body["error"]["message"].lower()
        assert "target" in await snapshots.listed()

    async def test_backup_younger_than_the_age_floor_is_kept(
        self,
        core_client: HomeAssistantClient,
        snapshots: Snapshots,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """L2, for a backup that is not the newest."""
        allow_deletes(monkeypatch, min_age_days=7)
        snapshots.add("target", age=timedelta(days=1))
        snapshots.add("other", age=timedelta(0))

        body = await refusal(core_client, "target")

        assert body["error"]["code"] == ErrorCode.VALIDATION_INVALID_PARAMETER.value
        assert "days" in body["error"]["message"]
        assert "target" in await snapshots.listed()

    @pytest.mark.parametrize(
        ("min_age_days", "age"),
        [
            pytest.param(7, OLD, id="clears-the-age-floor"),
            pytest.param(7, timedelta(0), id="also-too-young"),
            pytest.param(0, timedelta(0), id="age-floor-off"),
        ],
    )
    async def test_newest_snapshot_is_kept(
        self,
        core_client: HomeAssistantClient,
        snapshots: Snapshots,
        monkeypatch: pytest.MonkeyPatch,
        min_age_days: int,
        age: timedelta,
    ) -> None:
        """L3 holds at any age floor, and a newest backup that is also too young
        is told to make a new snapshot, not to lower the floor."""
        allow_deletes(monkeypatch, min_age_days=min_age_days)
        snapshots.add("other", age=age + timedelta(days=100))
        snapshots.add("target", age=age)

        body = await refusal(core_client, "target")

        message = body["error"]["message"]
        assert body["error"]["code"] == ErrorCode.VALIDATION_INVALID_PARAMETER.value
        assert "newest" in message
        assert "snapshot_delete_min_age_days" not in message
        assert "target" in await snapshots.listed()

    async def test_backup_with_an_unreadable_date_is_kept(
        self,
        core_client: HomeAssistantClient,
        snapshots: Snapshots,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A date the tool cannot read never counts as old enough."""
        allow_deletes(monkeypatch)
        snapshots.add("target", date="not-a-date")
        snapshots.add("other")

        body = await refusal(core_client, "target")

        assert body["error"]["code"] == ErrorCode.VALIDATION_INVALID_PARAMETER.value
        assert "date" in body["error"]["message"]
        assert "target" in await snapshots.listed()


class TestSnapshotDeleteSuccess:
    async def test_old_manual_backup_that_is_not_the_newest_is_deleted(
        self,
        core_client: HomeAssistantClient,
        snapshots: Snapshots,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        allow_deletes(monkeypatch, min_age_days=7)
        snapshots.add("target")
        snapshots.add("other", age=timedelta(days=1))

        result = await delete_backup(core_client, "target", confirm=True)

        assert result["success"] is True
        assert result["backup_id"] == "target"
        assert result["name"] == "target backup"
        assert await snapshots.listed() == {"other"}

    async def test_age_floor_of_zero_lets_even_a_future_dated_backup_go(
        self,
        core_client: HomeAssistantClient,
        snapshots: Snapshots,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """0 turns the floor off, also when the install's clock is ahead of the
        server's."""
        allow_deletes(monkeypatch, min_age_days=0)
        snapshots.add("target", age=timedelta(hours=-1))
        snapshots.add("other", age=timedelta(hours=-2))

        result = await delete_backup(core_client, "target", confirm=True)

        assert result["success"] is True
        assert await snapshots.listed() == {"other"}

    async def test_delete_waits_longer_than_a_plain_command(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        snapshots: Snapshots,
        tool_sockets: list[HomeAssistantWebSocketClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Core answers backup/delete once every location has; a slow remote one
        must not turn a delete into a false timeout."""
        allow_deletes(monkeypatch)
        snapshots.add("target")
        snapshots.add("other", age=timedelta(days=1))
        spies: list[AsyncMock] = []

        with ExitStack() as stack:

            def spy_on_commands() -> None:
                spies.append(stack.enter_context(sent_commands(tool_sockets[-1])))

            with after_core_receives(hass, "backup/info", spy_on_commands):
                await delete_backup(core_client, "target", confirm=True)

        (delete_call,) = spies[0].await_args_list
        assert delete_call.args == ("backup/delete",)
        assert delete_call.kwargs["_wait_timeout"] > DEFAULT_COMMAND_WAIT_TIMEOUT

    async def test_one_heartbeat_is_sent_before_the_delete(
        self,
        core_client: HomeAssistantClient,
        snapshots: Snapshots,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Like create and restore, delete reports progress while Core works."""
        allow_deletes(monkeypatch)
        snapshots.add("target")
        snapshots.add("other", age=timedelta(days=1))
        still_held: list[bool] = []
        heartbeats = Heartbeats(
            lambda _: still_held.append("target" in snapshots.remote.held)
        )

        await delete_backup(core_client, "target", confirm=True, ctx=heartbeats.context)

        assert still_held == [True]
        assert "delet" in cast(str, heartbeats.reports[0][1])
        assert await snapshots.listed() == {"other"}


class TestSnapshotDeleteFailures:
    async def test_location_that_fails_to_delete_is_reported(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        snapshots: Snapshots,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        allow_deletes(monkeypatch)
        snapshots.add("target")
        snapshots.add("other", age=timedelta(days=1))

        def lose_the_location() -> None:
            snapshots.remote.unreachable = "boom"

        with before_core_handles(hass, "backup/delete", lose_the_location):
            body = await refusal(core_client, "target")

        assert body["error"]["code"] == ErrorCode.SERVICE_CALL_FAILED.value
        assert list(body["agent_errors"]) == [REMOTE]

    async def test_list_missing_a_location_refuses_the_delete(
        self,
        core_client: HomeAssistantClient,
        snapshots: Snapshots,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A location that fails to list may hold the newest or a scheduled
        backup, so the guards cannot be trusted on what is left."""
        allow_deletes(monkeypatch)
        snapshots.add("target")
        snapshots.add("other", age=timedelta(days=1))
        snapshots.remote.unreachable = "timeout"

        body = await refusal(core_client, "target")

        assert body["error"]["code"] == ErrorCode.SERVICE_CALL_FAILED.value
        assert list(body["agent_errors"]) == [REMOTE]
        snapshots.remote.unreachable = None
        assert "target" in await snapshots.listed()

    async def test_connection_lost_during_the_delete_names_the_backup(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        snapshots: Snapshots,
        tool_sockets: list[HomeAssistantWebSocketClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        allow_deletes(monkeypatch)
        snapshots.add("target")
        snapshots.add("other", age=timedelta(days=1))

        with after_core_receives(
            hass,
            "backup/info",
            lambda: no_answer_from_now(monkeypatch, tool_sockets[-1], "closed"),
        ):
            body = await refusal(core_client, "target")

        assert body["error"]["code"] == ErrorCode.CONNECTION_FAILED.value
        assert body["backup_id"] == "target"
        assert "target" in await snapshots.listed()
