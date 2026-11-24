"""A real ``hass``, server entries, a package index and worker import hooks.

The manager under test runs on Core's ``hass`` with a real server config entry
and a real access token. What is replaced is this component's own collaborators:
``uv`` and the installed-distribution metadata (``PackageIndex``), the
dependency audit, and, for the worker thread, the import system hooks
(``ImportHook``) that make a cold ``ha_mcp`` import slow, fail or find an older
server. The server, uvicorn and the listener are the real ones.
"""

from __future__ import annotations

import asyncio
import importlib.abc
import importlib.machinery
import importlib.metadata
import importlib.util
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.core import HomeAssistant
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from pytest_homeassistant.common import MockModule, mock_integration

from custom_components.ha_mcp_tools import embedded_server as es
from custom_components.ha_mcp_tools.const import (
    BIND_HOST_LOOPBACK,
    DATA_ADMIN_TOKEN,
    DOMAIN,
    OPT_BIND_HOST,
    OPT_SERVER_PORT,
)
from ha_mcp.config import reset_embedded_connection, reset_global_settings

from ._core_client import free_port
from ._server_entry import add_server_entry

STABLE = "ha-mcp"
DEV = "ha-mcp-dev"
WORKER_THREAD = "ha-mcp-server"
# What the fake index installs when the requirement names no exact version.
INDEX_VERSION = "8.6.0"
# The version of the ha-mcp that is really installed here, and so really runs.
REAL_VERSION = importlib.metadata.version(STABLE)
_SERVER_DISTS = (STABLE, DEV)
_WORKER_JOIN_SECONDS = 10


@pytest.fixture
def embedded_server_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Keep what a started worker changes in this process from the next test.

    The dependency audit walks every installed package, which these tests do
    not need (``test_dependency_diagnostics`` covers it); its stand-in finds
    nothing and appends each root it is asked to audit to the yielded list. The worker writes two
    variables into ``os.environ``, registers an embedded connection and, on a
    cold start, replaces the ``ha_mcp`` modules in ``sys.modules``: all of it is
    undone here.
    """
    audited_roots: list[str] = []

    def audit(root: str) -> list[Any]:
        audited_roots.append(root)
        return []

    monkeypatch.setattr(es, "audit_dependency_graph", audit)
    # Readiness is polled every 0.5s by default: a test pays it on every start.
    shorten(monkeypatch, ready_poll=0.05)
    for name in ("HA_MCP_CONFIG_DIR", "HA_MCP_EMBEDDED"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    for name in ("HOMEASSISTANT_URL", "HOMEASSISTANT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "ha_mcp" or name.startswith("ha_mcp.")
    }
    yield audited_roots
    workers = [w for w in threading.enumerate() if w.name == WORKER_THREAD]
    for worker in workers:
        worker.join(timeout=_WORKER_JOIN_SECONDS)
    if any(worker.is_alive() for worker in workers):
        # Restoring the module cache under a worker still importing breaks it.
        pytest.fail(
            f"an {WORKER_THREAD} worker still runs after {_WORKER_JOIN_SECONDS}s; "
            "the ha_mcp module cache is left as it is",
            pytrace=False,
        )
    reset_embedded_connection()
    reset_global_settings()
    for name in [n for n in sys.modules if n == "ha_mcp" or n.startswith("ha_mcp.")]:
        del sys.modules[name]
    sys.modules.update(saved)


def shorten(monkeypatch: pytest.MonkeyPatch, **seconds: float) -> None:
    """Shrink the manager's waits (``stop_join``, ``ready_stall`` ...) for a test."""
    names = {
        "stop_join": "_STOP_JOIN_TIMEOUT_SECONDS",
        "ready_stall": "READY_STALL_TIMEOUT_SECONDS",
        "ready_cap": "READY_TOTAL_CAP_SECONDS",
        "ready_poll": "READY_POLL_INTERVAL_SECONDS",
        "pending_install": "_PENDING_PACKAGE_JOB_WAIT_SECONDS",
    }
    for key, value in seconds.items():
        monkeypatch.setattr(es, names[key], value)


def pin(hass: HomeAssistant, *requirements: str) -> None:
    """Make the component's manifest list ``requirements``, as a release does."""
    mock_integration(hass, MockModule(DOMAIN, requirements=list(requirements)), False)


async def issue_admin_token(hass: HomeAssistant) -> str:
    """A long-lived access token of an administrator, as the server runs with."""
    user = await hass.auth.async_create_user(
        "HA-MCP server", group_ids=[GROUP_ID_ADMIN]
    )
    refresh_token = await hass.auth.async_create_refresh_token(
        user,
        client_name="HA-MCP",
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=timedelta(days=365),
    )
    return hass.auth.async_create_access_token(refresh_token)


def server_options(**extra: Any) -> dict[str, Any]:
    """Options that make the server listen on a free loopback port."""
    return {OPT_BIND_HOST: BIND_HOST_LOOPBACK, OPT_SERVER_PORT: free_port(), **extra}


async def new_manager(
    hass: HomeAssistant,
    *,
    token: bool = False,
    options: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> tuple[es.EmbeddedServerManager, Any]:
    """A manager on a server entry; ``token`` gives the entry a working credential."""
    entry_data = dict(data or {})
    if token:
        entry_data[DATA_ADMIN_TOKEN] = await issue_admin_token(hass)
    entry = add_server_entry(hass, data=entry_data, options=options)
    return es.EmbeddedServerManager(hass, entry), entry


@asynccontextmanager
async def running_server(
    hass: HomeAssistant,
    *,
    options: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> AsyncIterator[tuple[es.EmbeddedServerManager, Any]]:
    """A manager whose real worker serves on a free loopback port; stopped on exit."""
    manager, entry = await new_manager(
        hass, token=True, options=server_options(**(options or {})), data=data
    )
    try:
        await manager.async_start()
        yield manager, entry
    finally:
        await manager.async_stop()


async def http_get(port: int, path: str) -> tuple[int, str]:
    """Status and body of a plain GET on the loopback ``port``."""
    url = f"http://127.0.0.1:{port}{path}"
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        return response.status, await response.text()


async def package_step(manager: es.EmbeddedServerManager) -> None:
    """Run a start that passes the package step and stops at the missing token."""
    with pytest.raises(es.EmbeddedServerError) as err:
        await manager.async_start()
    assert err.value.kind == "token"


async def package_failure(manager: es.EmbeddedServerManager) -> es.EmbeddedServerError:
    """Run a start whose package step fails; return the error."""
    with pytest.raises(es.EmbeddedServerError) as err:
        await manager.async_start()
    assert err.value.kind == "package"
    return err.value


async def worker_gone() -> None:
    """Wait until no server worker thread is alive."""
    for _ in range(200):
        if not any(
            t.name == WORKER_THREAD and t.is_alive() for t in threading.enumerate()
        ):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("a server worker thread is still alive")


def flag_values(args: list[str], flag: str) -> list[str]:
    """The values that follow each ``flag`` in a command line."""
    return [args[i + 1] for i, value in enumerate(args) if value == flag]


@dataclass
class Install:
    """One ``uv pip install`` the fake index was asked to run."""

    spec: str
    args: list[str]
    env: dict[str, str]
    kwargs: dict[str, Any]


@dataclass
class PackageIndex:
    """The installed server distributions, and ``uv`` working on them.

    ``dists`` maps a distribution to its installed version and ``sources`` to
    the requirement that installed it, so a test sees whether a build was really
    replaced. Like uv, an upgrade is a no-op when the installed version already
    satisfies the requirement; only a reinstall swaps the files.
    """

    monkeypatch: pytest.MonkeyPatch
    dists: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    installs: list[Install] = field(default_factory=list)
    uninstalls: list[str] = field(default_factory=list)
    uninstall_args: list[list[str]] = field(default_factory=list)
    install_version: str | None = None
    install_outcome: Callable[[Install], str | BaseException | None] | None = None
    uninstall_outcome: Callable[[str], str | BaseException | None] | None = None
    uninstall_leaves_dist_gone: bool = False
    after_worker_read: tuple[str, str] | None = None
    requirements: AsyncMock = field(init=False)

    def __post_init__(self) -> None:
        self.requirements = AsyncMock(
            side_effect=AssertionError("Home Assistant's eager installer ran")
        )
        real_run = subprocess.run
        real_version = importlib.metadata.version

        def run(args: list[str], **kwargs: Any) -> Any:
            if args[1:4] != ["-m", "uv", "pip"]:
                return real_run(args, **kwargs)
            if args[4] == "install":
                return self._install(args, kwargs)
            return self._uninstall(args)

        def version(name: str) -> str:
            key = canonicalize_name(name)
            if key in self.dists:
                value = self.dists[key]
                flip = self.after_worker_read
                if (
                    flip is not None
                    and flip[0] == key
                    and threading.current_thread().name == WORKER_THREAD
                ):
                    self.dists[key] = flip[1]
                    self.after_worker_read = None
                return value
            if key in _SERVER_DISTS:
                raise importlib.metadata.PackageNotFoundError(name)
            return real_version(name)

        self.monkeypatch.setattr(subprocess, "run", run)
        self.monkeypatch.setattr(importlib.metadata, "version", version)
        self.monkeypatch.setattr(es, "async_process_requirements", self.requirements)

    def _install(self, args: list[str], kwargs: dict[str, Any]) -> Any:
        spec = args[args.index("--quiet") + 1]
        install = Install(spec, args, kwargs.get("env", {}), kwargs)
        self.installs.append(install)
        self.calls.append("install")
        outcome = self.install_outcome(install) if self.install_outcome else None
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is not None:
            return subprocess.CompletedProcess(args, 1, "", outcome)
        dist = _dist_of(spec)
        reinstall = bool(flag_values(args, "--reinstall-package"))
        installed = self.dists.get(dist)
        if reinstall or installed is None or not _satisfied(spec, installed):
            self.dists[dist] = self.install_version or _pinned(spec) or INDEX_VERSION
            self.sources[dist] = spec
        return subprocess.CompletedProcess(args, 0, "", "")

    def _uninstall(self, args: list[str]) -> Any:
        dist = args[-1]
        self.uninstalls.append(dist)
        self.uninstall_args.append(args)
        self.calls.append(f"uninstall {dist}")
        outcome = self.uninstall_outcome(dist) if self.uninstall_outcome else None
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None or self.uninstall_leaves_dist_gone:
            self.dists.pop(dist, None)
            self.sources.pop(dist, None)
        if outcome is not None:
            return subprocess.CompletedProcess(args, 1, "", outcome)
        return subprocess.CompletedProcess(args, 0, "", "")


def _dist_of(spec: str) -> str:
    try:
        return canonicalize_name(Requirement(spec).name)
    except InvalidRequirement:
        return DEV if "ha_mcp_dev" in spec else STABLE


def _pinned(spec: str) -> str | None:
    try:
        clauses = list(Requirement(spec).specifier)
    except InvalidRequirement:
        return None
    return (
        clauses[0].version
        if len(clauses) == 1 and clauses[0].operator == "=="
        else None
    )


def _satisfied(spec: str, installed: str) -> bool:
    try:
        requirement = Requirement(spec)
    except InvalidRequirement:
        return False
    if requirement.url:
        return False
    return requirement.specifier.contains(installed, prereleases=True)


@pytest.fixture
def package_index(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> PackageIndex:
    """A fake index and ``uv``; Home Assistant manages its own packages (no skip_pip)."""
    hass.config.skip_pip = False
    return PackageIndex(monkeypatch)


class _EmptyLoader(importlib.abc.Loader):
    def exec_module(self, module: ModuleType) -> None:
        return None


class _PatchingLoader(importlib.abc.Loader):
    def __init__(
        self, real: importlib.abc.Loader, patch: Callable[[Any], None]
    ) -> None:
        self._real = real
        self._patch = patch

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return self._real.create_module(spec)

    def exec_module(self, module: ModuleType) -> None:
        self._real.exec_module(module)
        self._patch(module)


class ImportHook(importlib.abc.MetaPathFinder):
    """Acts on the worker thread's first import of a chosen ``ha_mcp`` module.

    Only a cold import reaches it: the module is in ``sys.modules`` otherwise.
    A start with a package override always purges first, so a test that needs
    its hook to fire gives the entry an override.
    """

    def __init__(self) -> None:
        self._rules: dict[str, Callable[[str, Any], Any]] = {}
        self.entered = threading.Event()
        self.release = threading.Event()

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> Any:
        rule = self._rules.get(fullname)
        if rule is None or threading.current_thread().name != WORKER_THREAD:
            return None
        return rule(fullname, path)

    def clear(self) -> None:
        """Forget every rule."""
        self._rules.clear()

    def block(self, name: str) -> None:
        """Hold the first worker that imports ``name`` until ``release`` is set."""

        def rule(_fullname: str, _path: Any) -> Any:
            if not self.entered.is_set():
                self.entered.set()
                self.release.wait(timeout=30)
            return None

        self._rules[name] = rule

    def fail(self, name: str, error: BaseException) -> None:
        """Make importing ``name`` raise ``error``."""

        def rule(_fullname: str, _path: Any) -> Any:
            raise error

        self._rules[name] = rule

    def empty(self, name: str) -> None:
        """Make ``name`` an importable module with nothing in it (an older server)."""
        self._rules[name] = lambda fullname, _path: importlib.util.spec_from_loader(
            fullname, _EmptyLoader()
        )

    def after(self, name: str, patch: Callable[[Any], None]) -> None:
        """Run ``patch`` on the real module ``name`` right after it is imported."""

        def rule(fullname: str, path: Any) -> Any:
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is None or spec.loader is None:
                return None
            spec.loader = _PatchingLoader(spec.loader, patch)
            return spec

        self._rules[name] = rule


@pytest.fixture
def import_hook(monkeypatch: pytest.MonkeyPatch) -> Iterator[ImportHook]:
    """An ``ImportHook`` in front of every other import finder."""
    hook = ImportHook()
    monkeypatch.setattr(sys, "meta_path", [hook, *sys.meta_path])
    yield hook
    hook.release.set()
