"""Bring-ups that overlap a worker or an install still running (#1904, #1914, #2146).

Every bring-up builds a new manager, but the workers are threads of one process
and the install is one executor job. A worker still importing ``ha_mcp`` must
not have its modules purged or its package replaced under it; an install that a
cancelled bring-up left running must be waited for. The tests hold a worker
inside its import with ``ImportHook.block`` and an install inside ``uv`` with an
event, and start further managers meanwhile.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from types import ModuleType
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import embedded_server as es
from custom_components.ha_mcp_tools.const import (
    DATA_LAST_PIP_SPEC,
    DATA_REINSTALL_REQUESTED,
    OPT_PIP_SPEC,
    OPT_SERVER_PORT,
)

from ._embedded_server_core import (
    DEV,
    INDEX_VERSION,
    STABLE,
    ImportHook,
    PackageIndex,
    new_manager,
    package_failure,
    package_step,
    pin,
    running_server,
    server_options,
    shorten,
    worker_gone,
)
from ._embedded_server_core import embedded_server_env as embedded_server_env
from ._embedded_server_core import import_hook as import_hook
from ._embedded_server_core import package_index as package_index

pytestmark = pytest.mark.usefixtures("embedded_server_env", "socket_enabled")

PIN = f"{STABLE}=={INDEX_VERSION}"
OVERRIDE = {OPT_PIP_SPEC: "ha-mcp==99.0.0"}
SKIPPING = "Skipping the ha_mcp module purge"


def generation() -> Any:
    return sys.modules["ha_mcp"]


async def wait_for(hass: HomeAssistant, event: threading.Event) -> None:
    assert await hass.async_add_executor_job(event.wait, 30), "never happened"


async def wait_for_log(caplog: pytest.LogCaptureFixture, text: str) -> None:
    for _ in range(200):
        if text in caplog.text:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{text!r} was never logged")


async def cancelled(task: asyncio.Task[Any]) -> None:
    task.cancel()
    await asyncio.wait([task])
    with suppress(asyncio.CancelledError):
        task.result()


@asynccontextmanager
async def importing_worker(
    hass: HomeAssistant, hook: ImportHook
) -> AsyncIterator[es.EmbeddedServerManager]:
    """A worker held inside its import; let go and stopped on exit."""
    hook.block("ha_mcp.server")
    mgr, _entry = await new_manager(
        hass, token=True, options=server_options(**{OPT_PIP_SPEC: PIN})
    )
    start = asyncio.create_task(mgr.async_start())
    try:
        await wait_for(hass, hook.entered)
        yield mgr
    finally:
        hook.release.set()
        try:
            await asyncio.wait_for(start, timeout=60)
        finally:
            await mgr.async_stop()


# -- a worker that will not stop ---------------------------------------------


async def wedged_worker(
    hass: HomeAssistant, hook: ImportHook, monkeypatch: pytest.MonkeyPatch
) -> es.EmbeddedServerManager:
    """A worker wedged in its import and stopped: left behind after a bounded join."""
    shorten(monkeypatch, stop_join=0.3)
    hook.block("ha_mcp.server")
    mgr, _entry = await new_manager(
        hass, token=True, options=server_options(**OVERRIDE)
    )
    first = asyncio.create_task(mgr.async_start())
    await wait_for(hass, hook.entered)
    await mgr.async_stop()
    await cancelled(first)
    return mgr


async def test_stopping_a_worker_wedged_in_its_import_is_bounded(
    hass: HomeAssistant,
    import_hook: ImportHook,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Home Assistant shutdown must not wait for a worker stuck in a cold import."""
    mgr = await wedged_worker(hass, import_hook, monkeypatch)

    assert "did not stop within" in caplog.text
    assert not mgr.is_running
    import_hook.release.set()
    await worker_gone()


async def test_a_worker_left_behind_blocks_the_next_managers_purge_until_it_died(
    hass: HomeAssistant,
    import_hook: ImportHook,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await wedged_worker(hass, import_hook, monkeypatch)
    wedged = generation()
    blocked, _entry = await new_manager(
        hass, token=True, options=server_options(**OVERRIDE)
    )
    start = asyncio.create_task(blocked.async_start())
    await wait_for_log(caplog, SKIPPING)
    import_hook.release.set()
    await asyncio.wait_for(start, timeout=60)
    await blocked.async_stop()
    assert generation() is wedged

    await worker_gone()
    async with running_server(hass, options=OVERRIDE):
        assert generation() is not wedged


# -- a previous bring-up's worker still importing -----------------------------


async def test_a_worker_still_importing_blocks_the_purge_of_the_next_manager(
    hass: HomeAssistant, import_hook: ImportHook, caplog: pytest.LogCaptureFixture
) -> None:
    """Every bring-up builds a new manager, which cannot know of the old worker;
    the purge crashed it mid-import with ``KeyError: 'ha_mcp.config'`` (#1904)."""
    async with importing_worker(hass, import_hook):
        held = generation()
        second, _entry = await new_manager(
            hass, token=True, options=server_options(**OVERRIDE)
        )
        start = asyncio.create_task(second.async_start())
        await wait_for_log(caplog, SKIPPING)
        import_hook.release.set()
        await asyncio.wait_for(start, timeout=60)
        await second.async_stop()

    assert generation() is held


async def test_a_healthy_server_does_not_block_the_purge_of_a_later_start(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Only the import window is protected: a long-running server must not stop
    every later bring-up from loading new code."""
    async with running_server(hass, options=OVERRIDE):
        running = generation()
        async with running_server(hass, options=OVERRIDE):
            assert generation() is not running

    assert SKIPPING not in caplog.text


@pytest.mark.parametrize("failure", ["busy port", "crash"])
async def test_a_worker_that_failed_does_not_block_the_next_purge(
    hass: HomeAssistant,
    import_hook: ImportHook,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        options = {
            **server_options(**OVERRIDE),
            OPT_SERVER_PORT: taken.getsockname()[1],
        }
        if failure == "crash":
            import_hook.fail("ha_mcp.server", RuntimeError("boom mid-import"))
        mgr, _entry = await new_manager(hass, token=True, options=options)
        with pytest.raises(es.EmbeddedServerError):
            await mgr.async_start()
        await mgr.async_stop()
    import_hook.clear()
    failed = sys.modules.get("ha_mcp")

    async with running_server(hass, options=OVERRIDE):
        assert generation() is not failed

    assert SKIPPING not in caplog.text


# -- package changes while a worker is importing -----------------------------


async def test_an_install_is_deferred_while_a_worker_is_still_importing(
    hass: HomeAssistant,
    import_hook: ImportHook,
    package_index: PackageIndex,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Replacing the package files under a live importer corrupts it like a purge."""
    package_index.dists[STABLE] = INDEX_VERSION
    async with importing_worker(hass, import_hook):
        second, entry = await new_manager(
            hass,
            options={OPT_PIP_SPEC: "ha-mcp==8.7.0"},
            data={DATA_LAST_PIP_SPEC: PIN},
        )

        await package_step(second)

    assert package_index.installs == []
    assert package_index.uninstalls == []
    package_index.requirements.assert_not_awaited()
    assert "Deferring the ha-mcp install/upgrade" in caplog.text
    # Recording the new spec would make the next reload see no change.
    assert entry.data[DATA_LAST_PIP_SPEC] == PIN


async def test_a_requested_reinstall_waits_for_the_importing_worker(
    hass: HomeAssistant, import_hook: ImportHook, package_index: PackageIndex
) -> None:
    package_index.dists[STABLE] = INDEX_VERSION
    async with importing_worker(hass, import_hook):
        second, entry = await new_manager(
            hass, data={DATA_LAST_PIP_SPEC: PIN, DATA_REINSTALL_REQUESTED: True}
        )
        pin(hass, PIN)

        await package_step(second)

    assert package_index.installs == []
    assert entry.data[DATA_REINSTALL_REQUESTED] is True


async def test_a_build_too_old_to_serve_is_rejected_even_when_the_install_is_deferred(
    hass: HomeAssistant, import_hook: ImportHook, package_index: PackageIndex
) -> None:
    """The deferred start touches nothing, so it fails loudly instead of serving
    the legacy build; the next undeferred reload installs for real."""
    package_index.dists[STABLE] = INDEX_VERSION
    async with importing_worker(hass, import_hook):
        package_index.dists[STABLE] = "6.2.0"
        pin(hass, PIN)
        second, entry = await new_manager(hass)

        await package_failure(second)

    assert package_index.installs == []
    assert DATA_LAST_PIP_SPEC not in entry.data


async def test_a_deferred_start_with_nothing_installed_still_installs(
    hass: HomeAssistant, import_hook: ImportHook, package_index: PackageIndex
) -> None:
    """With no distribution files there is nothing to replace under the importer,
    and without an install this start cannot produce a server at all."""
    package_index.dists[STABLE] = INDEX_VERSION
    package_index.requirements.side_effect = lambda *_a, **_k: (
        package_index.dists.update({STABLE: INDEX_VERSION})
    )
    async with importing_worker(hass, import_hook):
        package_index.dists.clear()
        pin(hass, PIN)
        second, entry = await new_manager(hass)

        await package_step(second)

    package_index.requirements.assert_awaited_once()
    assert package_index.installs == []
    assert DATA_LAST_PIP_SPEC not in entry.data


# -- an install that a cancelled start left running --------------------------


class Held:
    """A ``uv`` call that does not return until released."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, *_args: Any) -> None:
        self.started.set()
        self.release.wait(30)


@pytest.mark.parametrize("job", ["install", "uninstall"])
async def test_a_start_waits_for_the_job_a_cancelled_start_left_on_the_executor(
    hass: HomeAssistant,
    package_index: PackageIndex,
    caplog: pytest.LogCaptureFixture,
    job: str,
) -> None:
    """Cancelling the awaiting start cannot cancel the executor job, and a second
    pip swapping the files under the first would corrupt the package."""
    held = Held()
    pin(hass, PIN)
    if job == "install":
        package_index.install_outcome = held
    else:
        package_index.dists.update({STABLE: INDEX_VERSION, DEV: "8.6.0.dev1"})
        package_index.uninstall_outcome = held
    first, _entry = await new_manager(hass)
    cancelled_start = asyncio.create_task(first.async_start())
    await wait_for(hass, held.started)
    await cancelled(cancelled_start)

    second, _entry = await new_manager(hass)
    next_start = asyncio.create_task(second.async_start())
    await wait_for_log(caplog, "install job is still running")
    assert len(package_index.calls) == 1, "the next start touched the package"
    held.release.set()
    with pytest.raises(es.EmbeddedServerError) as err:
        await asyncio.wait_for(next_start, timeout=60)

    assert err.value.kind == "token"
    assert package_index.dists[STABLE] == INDEX_VERSION
    assert len(package_index.calls) == 1, "the job was run again"


async def test_a_start_gives_up_on_a_job_that_never_finishes(
    hass: HomeAssistant, package_index: PackageIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    shorten(monkeypatch, pending_install=0.2)
    held = Held()
    package_index.install_outcome = held
    pin(hass, PIN)
    first, _entry = await new_manager(hass)
    cancelled_start = asyncio.create_task(first.async_start())
    await wait_for(hass, held.started)
    await cancelled(cancelled_start)
    second, _entry = await new_manager(hass)

    try:
        err = await package_failure(second)
    finally:
        held.release.set()

    assert "refusing to modify" in str(err)
    assert len(package_index.installs) == 1


async def test_a_job_that_raised_does_not_make_the_next_start_wait(
    hass: HomeAssistant, package_index: PackageIndex, caplog: pytest.LogCaptureFixture
) -> None:
    package_index.install_outcome = lambda _install: RuntimeError("pip exploded")
    pin(hass, PIN)
    first, _entry = await new_manager(hass)
    with pytest.raises(RuntimeError, match="pip exploded"):
        await first.async_start()
    package_index.install_outcome = None
    second, _entry = await new_manager(hass)

    await package_step(second)

    assert "install job is still running" not in caplog.text
    assert package_index.dists[STABLE] == INDEX_VERSION


# -- how long a start waits for its server -----------------------------------


@asynccontextmanager
async def stuck_in_import(
    hass: HomeAssistant,
    hook: ImportHook,
    monkeypatch: pytest.MonkeyPatch,
    **waits: float,
) -> AsyncIterator[es.EmbeddedServerManager]:
    shorten(monkeypatch, stop_join=0.3, ready_poll=0.05, **waits)
    hook.block("ha_mcp.server")
    mgr, _entry = await new_manager(
        hass, token=True, options=server_options(**OVERRIDE)
    )
    try:
        yield mgr
    finally:
        hook.release.set()
        await worker_gone()


async def test_a_start_with_no_progress_is_stopped_naming_its_phase(
    hass: HomeAssistant, import_hook: ImportHook, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged worker is told apart from a slow one by the absence of progress."""
    async with stuck_in_import(hass, import_hook, monkeypatch, ready_stall=0.5) as mgr:
        with pytest.raises(es.EmbeddedServerError, match="no startup progress") as err:
            await mgr.async_start()

    assert "importing the server package" in str(err.value)
    assert not mgr.is_running


@asynccontextmanager
async def progress(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Modules keep landing in ``sys.modules``, as a slow cold import's do."""

    async def land_modules() -> None:
        for count in range(1000):
            name = f"_embedded_progress_probe_{count}"
            monkeypatch.setitem(sys.modules, name, ModuleType(name))
            await asyncio.sleep(0.1)

    task = asyncio.create_task(land_modules())
    try:
        yield
    finally:
        await cancelled(task)


async def test_progress_keeps_a_slow_start_alive_past_the_stall_budget(
    hass: HomeAssistant, import_hook: ImportHook, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cold import took minutes on slow hardware; giving up on it created the
    orphaned worker and the port collision of #1904."""
    async with stuck_in_import(hass, import_hook, monkeypatch, ready_stall=0.6) as mgr:
        async with progress(monkeypatch):
            start = asyncio.create_task(mgr.async_start())
            await wait_for(hass, import_hook.entered)
            await asyncio.sleep(1.5)
            assert not start.done(), "gave up while the start was progressing"
            import_hook.release.set()
            await asyncio.wait_for(start, timeout=60)
        assert mgr.is_running
        await mgr.async_stop()


async def test_the_total_cap_fires_despite_progress(
    hass: HomeAssistant, import_hook: ImportHook, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        stuck_in_import(
            hass, import_hook, monkeypatch, ready_stall=10.0, ready_cap=1.0
        ) as mgr,
        progress(monkeypatch),
    ):
        with pytest.raises(es.EmbeddedServerError, match="within 1s"):
            await mgr.async_start()
