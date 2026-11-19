"""Home Assistant's backup integration on a real ``hass``, for the snapshot tests.

``backup_install`` sets the integration up as an install has it. ``core()``
is a Core install: Core's local agent (``backup.local``) writes real backups
into the test's config directory. ``supervisor()`` is a Supervisor install:
Core's own ``hassio`` agents and reader-writer talk to a Supervisor that
``aioclient_mock`` answers (``Supervisor``), as Core's hassio tests answer it.
Either can add another integration's agent, ``remote_storage.remote``
(``RemoteAgent``), set up through Core's ``mock_integration``.

The snapshot tools open a WebSocket of their own for each call;
``tool_sockets`` records each one, so a test can drop it or take its answers
away. Nothing here answers for Core.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiohasupervisor.models.backups import (
    Backup,
    BackupAddon,
    BackupComplete,
    BackupContent,
    BackupJob,
    BackupList,
    BackupLocationAttributes,
    BackupType,
    Folder,
)
from aiohasupervisor.models.jobs import Job, JobError
from aiohasupervisor.models.mounts import (
    CIFSMountResponse,
    MountsInfo,
    MountState,
    MountType,
    MountUsage,
)
from homeassistant.backup_restore import RESTORE_BACKUP_FILE
from homeassistant.components import websocket_api
from homeassistant.components.backup import (
    AgentBackup,
    BackupAgent,
    BackupAgentError,
    BackupAgentPlatformProtocol,
    BackupNotFound,
    OnProgressCallback,
    async_get_manager,
)
from homeassistant.components.hassio.const import EVENT_SUPERVISOR_EVENT
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import (
    MockModule,
    mock_component,
    mock_integration,
    mock_platform,
)
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from pytest_homeassistant.typing import WebSocketGenerator
from yarl import URL

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.websockets.exceptions import ConnectionClosed
from ha_mcp.client.rest_client import (
    HomeAssistantCommandNotSent,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools import backup_snapshot, backup_snapshot_restore

from ._supervisor_core import SUPERVISOR, load_supervisor_api
from ._tools_addons_core import installed_apps
from ._ws_transport import abort_transport

PASSWORD = "pw"
REMOTE = "remote_storage.remote"
APPS = ("core_mosquitto", "core_ssh")


@pytest.fixture(autouse=True)
def bounded_backup_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missed terminal event fails within seconds instead of 30 minutes."""
    monkeypatch.setattr(backup_snapshot, "BACKUP_MAX_WAIT_S", 10)
    monkeypatch.setattr(backup_snapshot_restore, "BACKUP_MAX_WAIT_S", 10)


class RemoteAgent(BackupAgent):
    """Another integration's off-site location: it keeps what is uploaded.

    ``unreachable`` makes it fail to list or read until the next upload, which
    reaches it again. ``upload_error`` fails uploads.
    """

    domain = "remote_storage"

    def __init__(self) -> None:
        super().__init__()
        self.name = self.unique_id = "remote"
        self.held: dict[str, tuple[AgentBackup, bytes]] = {}
        self.unreachable: str | None = None
        self.upload_error: str | None = None

    async def async_download_backup(
        self, backup_id: str, **kwargs: Any
    ) -> AsyncIterator[bytes]:
        data = self._held(backup_id)[1]

        async def stream() -> AsyncIterator[bytes]:
            yield data

        return stream()

    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        on_progress: OnProgressCallback,
        **kwargs: Any,
    ) -> None:
        self.unreachable = None
        if self.upload_error is not None:
            raise BackupAgentError(self.upload_error)
        data = b"".join([chunk async for chunk in await open_stream()])
        self.held[backup.backup_id] = (backup, data)

    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        self._reach()
        return [backup for backup, _ in self.held.values()]

    async def async_get_backup(self, backup_id: str, **kwargs: Any) -> AgentBackup:
        return self._held(backup_id)[0]

    async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
        self._held(backup_id)
        del self.held[backup_id]

    def _reach(self) -> None:
        if self.unreachable is not None:
            raise BackupAgentError(self.unreachable)

    def _held(self, backup_id: str) -> tuple[AgentBackup, bytes]:
        self._reach()
        if backup_id not in self.held:
            raise BackupNotFound(f"Backup {backup_id} not found")
        return self.held[backup_id]

    def hold(self, backup: AgentBackup) -> None:
        """Put ``backup`` at the location, as another client's upload would."""
        self.held[backup.backup_id] = (backup, b"")


def agent_backup(backup_id: str, name: str, **fields: Any) -> AgentBackup:
    """A backup as an agent lists it; ``fields`` override the defaults."""
    values: dict[str, Any] = {
        "addons": [],
        "backup_id": backup_id,
        "database_included": True,
        "date": "2026-10-01T00:00:00+00:00",
        "extra_metadata": {},
        "folders": [],
        "homeassistant_included": True,
        "homeassistant_version": HA_VERSION,
        "name": name,
        "protected": True,
        "size": 10,
        **fields,
    }
    return AgentBackup(**values)


def _reply(
    method: str, url: URL, status: int, body: dict[str, Any]
) -> AiohttpClientMockResponse:
    return AiohttpClientMockResponse(
        method,
        url,
        status=status,
        json=body,
        headers={"Content-Type": "application/json"},
    )


class Supervisor:
    """The Supervisor's mount, app, backup and job API, answering Core.

    A backup or restore job finishes at once, unless ``hold`` is set: it then
    runs until ``release()``. ``job_error`` fails the next backup job;
    ``failed_apps`` / ``failed_folders`` are left out of a backup that still
    finishes, as Supervisor reports them in the job's child jobs.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        mounts: tuple[str, ...],
        default_mount: str | None,
    ) -> None:
        self._hass = hass
        self.mounts = mounts
        self.default_mount = default_mount
        self.backups: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.hold = False
        self.job_error: str | None = None
        self.failed_apps: list[str] = []
        self.failed_folders: list[str] = []
        self.refusal: str | None = None
        self.apps_unreadable = False
        self.backup_requests: list[dict[str, Any]] = []
        self.restores: list[dict[str, Any]] = []
        self.operations: list[str] = []
        self._passwords: dict[str, str | None] = {}
        self._pending: dict[str, dict[str, Any] | None] = {}

    def register(self, aioclient_mock: AiohttpClientMocker) -> None:
        address = re.compile(rf"^http://{re.escape(SUPERVISOR)}/")
        for method in ("get", "post", "delete"):
            aioclient_mock.request(method, address, side_effect=self._answer)

    def add_backup(
        self,
        name: str,
        *,
        homeassistant: bool = True,
        database: bool = True,
        apps: tuple[str, ...] = (),
        folders: tuple[str, ...] = (),
        locations: tuple[str, ...] = (".local",),
        password: str | None = PASSWORD,
    ) -> str:
        """A backup the Supervisor already holds; its slug."""
        slug = uuid.uuid4().hex[:8]
        self.backups[slug] = self._backup(
            slug,
            name,
            homeassistant=homeassistant,
            database=database,
            apps=list(apps),
            folders=list(folders),
            locations=list(locations),
            protected=password is not None,
            extra={},
        )
        self._passwords[slug] = password
        return slug

    def release(self) -> None:
        """Finish every held job, telling Core as the Supervisor does."""
        for job_id, backup in list(self._pending.items()):
            del self._pending[job_id]
            reference = None
            if backup is not None:
                self.backups[backup["slug"]] = backup
                reference = backup["slug"]
            job = self._job(job_id, done=True, reference=reference)
            self.jobs[job_id] = job
            async_dispatcher_send(
                self._hass, EVENT_SUPERVISOR_EVENT, {"event": "job", "data": job}
            )

    async def _answer(
        self, method: str, url: URL, data: Any
    ) -> AiohttpClientMockResponse:
        verb = method.lower()
        match verb, url.path.strip("/").split("/"):
            case "get", ["mounts"]:
                return self._ok(verb, url, self._mount_info())
            case "get", ["addons"]:
                if self.apps_unreadable:
                    return self._error(verb, url, 500, "Supervisor is busy")
                return self._ok(verb, url, installed_apps(*APPS))
            case "get", ["backups"]:
                summaries = [self._summary(b) for b in self.backups.values()]
                return self._ok(verb, url, BackupList(backups=summaries).to_dict())
            case "get", ["backups", slug, "info"] if slug in self.backups:
                return self._ok(verb, url, self.backups[slug])
            case "post", ["backups", "new", "partial"]:
                return self._new_backup(verb, url, data)
            case "post", ["backups", slug, "restore", "partial"]:
                return self._restore(verb, url, slug, data)
            case "delete", ["backups", slug] if slug in self.backups:
                del self.backups[slug]
                return self._ok(verb, url, {})
            case "get", ["jobs", job_id]:
                job = self.jobs.get(str(uuid.UUID(job_id)))
                if job is not None:
                    return self._ok(verb, url, job)
        return self._error(verb, url, 404, f"{url.path} does not exist")

    def _new_backup(
        self, verb: str, url: URL, data: dict[str, Any]
    ) -> AiohttpClientMockResponse:
        self.backup_requests.append(data)
        self.operations.append("backup")
        if self.refusal is not None:
            return self._error(verb, url, 400, self.refusal)
        job_id = str(uuid.uuid4())
        if self.job_error is not None:
            error = JobError(type="BackupError", message=self.job_error, stage=None)
            self.jobs[job_id] = self._job(job_id, done=True, errors=[error])
            return self._ok(verb, url, BackupJob(job_id=uuid.UUID(job_id)).to_dict())
        slug = uuid.uuid4().hex[:8]
        apps = data.get("addons") or []
        locations = data.get("location") or [None]
        backup = self._backup(
            slug,
            data["name"],
            homeassistant=bool(data.get("homeassistant")),
            database=data.get("homeassistant_exclude_database") is False,
            apps=list(APPS) if apps == "ALL" else list(apps),
            folders=list(data.get("folders") or []),
            locations=[location or ".local" for location in locations],
            protected=data.get("password") is not None,
            extra=data.get("extra") or {},
        )
        self._passwords[slug] = data.get("password")
        self._start(job_id, backup)
        return self._ok(verb, url, BackupJob(job_id=uuid.UUID(job_id)).to_dict())

    def _restore(
        self, verb: str, url: URL, slug: str, data: dict[str, Any]
    ) -> AiohttpClientMockResponse:
        if slug not in self.backups:
            return self._error(verb, url, 404, f"Backup {slug} does not exist")
        if data.get("password") != self._passwords[slug]:
            return self._error(verb, url, 400, f"Invalid password for backup {slug}")
        self.restores.append({"slug": slug, **data})
        self.operations.append("restore")
        job_id = str(uuid.uuid4())
        self._start(job_id, None)
        return self._ok(verb, url, BackupJob(job_id=uuid.UUID(job_id)).to_dict())

    def _start(self, job_id: str, backup: dict[str, Any] | None) -> None:
        if self.hold:
            self._pending[job_id] = backup
            self.jobs[job_id] = self._job(job_id, done=False)
            return
        if backup is not None:
            self.backups[backup["slug"]] = backup
        self.jobs[job_id] = self._job(
            job_id, done=True, reference=backup["slug"] if backup else None
        )

    def _backup(
        self,
        slug: str,
        name: str,
        *,
        homeassistant: bool,
        database: bool,
        apps: list[str],
        folders: list[str],
        locations: list[str],
        protected: bool,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        requested = extra.get("supervisor.backup_request_date")
        date = datetime.fromisoformat(requested) if requested else dt_util.utcnow()
        return BackupComplete(
            slug=slug,
            name=name,
            date=date,
            type=BackupType.PARTIAL,
            location_attributes={
                location: BackupLocationAttributes(protected=protected, size_bytes=1000)
                for location in locations
            },
            compressed=True,
            supervisor_version="2026.11.0",
            homeassistant=HA_VERSION if homeassistant else None,
            addons=[
                BackupAddon(slug=app, name=app, version="1.0.0", size=1.0)
                for app in apps
                if app not in self.failed_apps
            ],
            repositories=[],
            folders=[Folder(f) for f in folders if f not in self.failed_folders],
            homeassistant_exclude_database=not database if homeassistant else None,
            extra=extra,
        ).to_dict()

    def _job(
        self,
        job_id: str,
        *,
        done: bool,
        reference: str | None = None,
        errors: list[JobError] | None = None,
    ) -> dict[str, Any]:
        children = [
            self._failed_parts(
                "backup_store_addons", "backup_addon_save", self.failed_apps
            ),
            self._failed_parts(
                "backup_store_folders", "backup_folder_save", self.failed_folders
            ),
        ]
        return Job(
            name="backup_manager_partial_backup",
            reference=reference,
            uuid=uuid.UUID(job_id),
            progress=100 if done else 0,
            stage=None,
            done=done,
            errors=errors or [],
            created=dt_util.utcnow(),
            child_jobs=children if done else [],
            extra=None,
        ).to_dict()

    @staticmethod
    def _failed_parts(name: str, part: str, references: list[str]) -> Job:
        error = JobError(type="BackupError", message="could not be saved", stage=None)
        jobs = [
            Job(
                name=part,
                reference=reference,
                uuid=uuid.uuid4(),
                progress=100,
                stage=None,
                done=True,
                errors=[error],
                created=dt_util.utcnow(),
                child_jobs=[],
                extra=None,
            )
            for reference in references
        ]
        return Job(
            name=name,
            reference=None,
            uuid=uuid.uuid4(),
            progress=100,
            stage=None,
            done=True,
            errors=[],
            created=dt_util.utcnow(),
            child_jobs=jobs,
            extra=None,
        )

    def _mount_info(self) -> dict[str, Any]:
        return MountsInfo(
            default_backup_mount=self.default_mount,
            mounts=[
                CIFSMountResponse(
                    name=name,
                    usage=MountUsage.BACKUP,
                    type=MountType.CIFS,
                    server=SUPERVISOR,
                    share=name,
                    read_only=False,
                    state=MountState.ACTIVE,
                    user_path=None,
                )
                for name in self.mounts
            ],
        ).to_dict()

    @staticmethod
    def _summary(backup: dict[str, Any]) -> Backup:
        detail = BackupComplete.from_dict(backup)
        return Backup(
            slug=detail.slug,
            name=detail.name,
            date=detail.date,
            type=detail.type,
            location_attributes=detail.location_attributes,
            compressed=detail.compressed,
            content=BackupContent(
                homeassistant=detail.homeassistant is not None,
                addons=[app.slug for app in detail.addons],
                folders=detail.folders,
            ),
        )

    @staticmethod
    def _ok(verb: str, url: URL, data: dict[str, Any]) -> AiohttpClientMockResponse:
        return _reply(verb, url, 200, {"result": "ok", "data": data})

    @staticmethod
    def _error(
        verb: str, url: URL, status: int, message: str
    ) -> AiohttpClientMockResponse:
        return _reply(verb, url, status, {"result": "error", "message": message})


class BackupInstall:
    """Core's backup integration on the test's ``hass``, read and set over Core's API."""

    def __init__(
        self,
        hass: HomeAssistant,
        ws_generator: WebSocketGenerator,
        aioclient_mock: AiohttpClientMocker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.hass = hass
        self._ws_generator = ws_generator
        self._aioclient_mock = aioclient_mock
        self._monkeypatch = monkeypatch
        self._ws: Any = None
        self.remote = RemoteAgent()
        self.restarts = 0
        self.on_restart: Callable[[], Awaitable[None]] | None = None

    async def core(self, *, remote: bool = False) -> None:
        """A Core install; a restore ends in Core's restart, which counts here."""
        self.hass.services.async_register("homeassistant", "restart", self._restart)
        await self._set_up(remote)

    async def supervisor(
        self,
        *,
        mounts: tuple[str, ...] = (),
        default_mount: str | None = None,
        remote: bool = False,
    ) -> Supervisor:
        """A Supervisor install whose backup mounts are ``mounts``."""
        mock_component(self.hass, "hassio")
        # load_supervisor_api sets SUPERVISOR_TOKEN, which alone sends the
        # server to the Supervisor directly; off the app it goes through Core.
        self._monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        load_supervisor_api(self.hass, self._aioclient_mock, self._monkeypatch, {})
        supervisor = Supervisor(self.hass, mounts, default_mount)
        supervisor.register(self._aioclient_mock)
        await self._set_up(remote)
        return supervisor

    async def _set_up(self, remote: bool) -> None:
        if remote:
            mock_integration(self.hass, MockModule(RemoteAgent.domain))
            mock_platform(
                self.hass,
                f"{RemoteAgent.domain}.backup",
                Mock(
                    spec_set=BackupAgentPlatformProtocol,
                    async_get_backup_agents=AsyncMock(return_value=[self.remote]),
                    async_register_backup_agents_listener=Mock(return_value=Mock()),
                ),
            )
            assert await async_setup_component(self.hass, RemoteAgent.domain, {})
        assert await async_setup_component(self.hass, "backup", {})
        await self.hass.async_block_till_done()

    async def command(self, command: str, **fields: Any) -> Any:
        """Send ``command`` to Core as an admin; Core's result."""
        if self._ws is None:
            self._ws = await self._ws_generator(self.hass)
        await self._ws.send_json_auto_id({"type": command, **fields})
        reply = await self._ws.receive_json()
        assert reply["success"], reply
        return reply["result"]

    async def configure(self, **create_backup: Any) -> None:
        """Set the ``create_backup`` part of the automatic-backup settings."""
        await self.command("backup/config/update", create_backup=create_backup)

    async def listed(self) -> dict[str, dict[str, Any]]:
        """Every backup Core lists, by id."""
        info = await self.command("backup/info")
        return {backup["backup_id"]: backup for backup in info["backups"]}

    async def generate(self, **fields: Any) -> str:
        """A backup Core makes with ``backup/generate``; its id."""
        before = await self.listed()
        await self.command("backup/generate", **fields)
        await self.hass.async_block_till_done()
        new = [b for b in await self.listed() if b not in before]
        assert len(new) == 1, new
        return new[0]

    async def restore_request(self) -> dict[str, Any] | None:
        """What a Core install's restore left for the restart to run, if anything."""
        path = Path(self.hass.config.path(RESTORE_BACKUP_FILE))
        if not await self.hass.async_add_executor_job(path.exists):
            return None
        return cast(
            dict[str, Any],
            json.loads(await self.hass.async_add_executor_job(path.read_text)),
        )

    async def _restart(self, call: ServiceCall) -> None:
        self.restarts += 1
        if self.on_restart is not None:
            await self.on_restart()


@pytest.fixture
def backup_install(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> BackupInstall:
    """Core's backup integration, set up by the test as ``core()`` or ``supervisor()``."""
    return BackupInstall(hass, hass_ws_client, aioclient_mock, monkeypatch)


@pytest.fixture
async def tool_sockets(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[list[HomeAssistantWebSocketClient]]:
    """Every WebSocket the snapshot tools open, in order; each is closed afterwards."""
    opened: list[HomeAssistantWebSocketClient] = []
    connect = backup_snapshot.get_connected_ws_client

    async def recording(
        *args: Any, **kwargs: Any
    ) -> tuple[HomeAssistantWebSocketClient | None, dict[str, Any] | None]:
        ws_client, error = await connect(*args, **kwargs)
        if ws_client is not None:
            opened.append(ws_client)
        return ws_client, error

    monkeypatch.setattr(backup_snapshot, "get_connected_ws_client", recording)
    yield opened
    for socket in opened:
        await HomeAssistantWebSocketClient.disconnect(socket)


@contextmanager
def _core_receives(
    hass: HomeAssistant,
    command: str,
    then: Callable[[], None],
    *,
    core_runs: str,
) -> Iterator[None]:
    """Re-register ``command``'s own handler with its own schema around ``then``."""
    core_handler, schema = hass.data[websocket_api.DOMAIN][command]

    def handle(core: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
        if core_runs == "before":
            core_handler(core, connection, msg)
        then()
        if core_runs == "after":
            core_handler(core, connection, msg)

    websocket_api.async_register_command(hass, command, handle, schema)
    try:
        yield
    finally:
        websocket_api.async_register_command(hass, command, core_handler, schema)


def after_core_receives(
    hass: HomeAssistant, command: str, then: Callable[[], None]
) -> contextlib.AbstractContextManager[None]:
    """Run ``then`` each time Core has handed ``command`` to its own handler."""
    return _core_receives(hass, command, then, core_runs="before")


def before_core_handles(
    hass: HomeAssistant, command: str, then: Callable[[], None]
) -> contextlib.AbstractContextManager[None]:
    """Run ``then`` each time Core receives ``command``, before its handler runs."""
    return _core_receives(hass, command, then, core_runs="after")


def instead_of_core(
    hass: HomeAssistant, command: str, then: Callable[[], None]
) -> contextlib.AbstractContextManager[None]:
    """Run ``then`` when Core receives ``command``; Core never handles it."""
    return _core_receives(hass, command, then, core_runs="never")


async def drop_after_what_core_sent(socket: HomeAssistantWebSocketClient) -> None:
    """Drop ``socket`` once everything Core sent on it so far has arrived.

    Core answers a ping after whatever it sent before, so the pong marks it.
    """
    await socket.send_command("ping")
    abort_transport(socket)


def when_backup_event(
    hass: HomeAssistant, manager_state: str, state: str, then: Callable[[], None]
) -> None:
    """Run ``then`` as Core's backup manager reports ``manager_state``/``state``."""

    def on_event(event: Any) -> None:
        if (
            event.manager_state == manager_state
            and getattr(event, "state", None) == state
        ):
            then()

    async_get_manager(hass).async_subscribe_events(on_event)


def spy_on_release(
    monkeypatch: pytest.MonkeyPatch, socket: HomeAssistantWebSocketClient
) -> AsyncMock:
    """A spy on ``socket``'s subscription release; it calls through."""
    monkeypatch.setattr(
        socket,
        "release_subscription",
        AsyncMock(wraps=socket.release_subscription),
    )
    return cast(AsyncMock, socket.release_subscription)


def no_answer_from_now(
    monkeypatch: pytest.MonkeyPatch, socket: HomeAssistantWebSocketClient, how: str
) -> None:
    """``socket``'s commands get no answer from now on; ``how`` is the failure."""
    if how == "closed":
        monkeypatch.setattr(
            socket,
            "send_command",
            AsyncMock(side_effect=HomeAssistantConnectionError("closed")),
        )
    elif how == "no-answer":
        monkeypatch.setattr(
            socket,
            "send_command",
            AsyncMock(side_effect=HomeAssistantCommandTimeout("no reply")),
        )
    elif how == "send-failed":
        monkeypatch.setattr(
            socket, "send_command", AsyncMock(side_effect=OSError("broken pipe"))
        )
    elif how == "closed-mid-write":
        monkeypatch.setattr(
            socket, "send_command", AsyncMock(side_effect=ConnectionClosed(None, None))
        )
    else:
        assert how == "never-sent", how
        monkeypatch.setattr(
            socket,
            "send_command",
            AsyncMock(side_effect=HomeAssistantCommandNotSent("closed")),
        )


class Heartbeats:
    """A FastMCP context that records the progress a tool reports.

    ``on_report`` runs after each report with the count so far. With
    ``stalled`` a report never returns, as a client that stopped reading.
    """

    def __init__(
        self, on_report: Callable[[int], None] | None = None, *, stalled: bool = False
    ) -> None:
        self.reports: list[tuple[float, str | None]] = []
        self._on_report = on_report
        self._stalled = stalled

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.reports.append((progress, message))
        if self._on_report is not None:
            self._on_report(len(self.reports))
        if self._stalled:
            await asyncio.Event().wait()

    @property
    def context(self) -> Context:
        return cast(Context, self)
