"""Fakes shared by the ``backup_snapshot`` and ``backup_snapshot_restore`` unit tests."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.tools import backup_snapshot, backup_snapshot_restore

COMPLETED = {"manager_state": "create_backup", "state": "completed"}
FAILED = {
    "manager_state": "create_backup",
    "state": "failed",
    "reason": "unknown_error",
}
UPLOAD_FAILED = {
    "manager_state": "create_backup",
    "state": "failed",
    "reason": "upload_failed",
}
IDLE = {"manager_state": "idle"}


@pytest.fixture(autouse=True)
def _short_backup_wait():
    """A missed terminal event fails within a second instead of 30 minutes."""
    with (
        patch.object(backup_snapshot, "_BACKUP_MAX_WAIT_S", 1),
        patch.object(backup_snapshot_restore, "_BACKUP_MAX_WAIT_S", 1),
    ):
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
        last: dict[str, Any] = IDLE,
        events: list[dict[str, Any]] | None = None,
        details: dict[str, Any] | None = None,
        new_backup: dict[str, Any] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.last = last
        self.events = events if events is not None else [COMPLETED]
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
        self.last, self.events = IDLE, []
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
