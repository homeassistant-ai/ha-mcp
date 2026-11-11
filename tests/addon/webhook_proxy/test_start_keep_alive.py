"""The running app (add-on): its keep-alive loop and its shutdown.

Each run is the app's ``main()`` against the real Core, from start to the
stop signal the Supervisor sends. While it runs the app echoes the
component's inbound-request mirror into its own log (#1694, #2220); a stop
must run the cleanup, and take the OAuth surface dark before anything slow.
"""

from __future__ import annotations

import signal
from pathlib import Path
from types import ModuleType

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._proxy import Upstream
from ._start import (
    App,
    CoreLink,
    connect_core,
    entry_states,
    in_core,
    make_app,
    run_main,
)
from ._variants import Variant

MIRRORED = {"old line", "line A", "line B", "partial", "partial done", "fresh"}


@pytest.fixture
def app(variant: Variant, fs_root: Path, monkeypatch: pytest.MonkeyPatch) -> App:
    return make_app(variant, fs_root, monkeypatch, installed=True)


@pytest.fixture
async def core(
    app: App,
    component: ModuleType,
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
    socket_enabled: None,
) -> CoreLink:
    return await connect_core(app, hass, hass_client, hass_access_token, monkeypatch)


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def _append(path: Path, data: bytes) -> None:
    with path.open("ab") as fh:
        fh.write(data)


async def _mirrored_lines(
    hass: HomeAssistant,
    app: App,
    upstream: Upstream,
) -> list[str]:
    """Run the app with "Log inbound requests" on; the mirror lines it echoed."""
    app.set_options(mcp_server_url=upstream.target_url(), debug_logging=True)
    assert await run_main(hass, app) == 0
    return [line for line in app.info_lines() if line in MIRRORED]


async def test_restart_does_not_replay_old_mirror_lines(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
) -> None:
    mirror = app.path(variant.inbound_log)
    mirror.write_bytes(b"old line\n")
    app.clock.polls = [lambda: _append(mirror, b"line A\n")]

    assert await _mirrored_lines(hass, app, upstream) == ["line A"]


async def test_mirror_tail_echoes_whole_lines_once_and_follows_rotation(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
) -> None:
    """The app echoes only complete lines, holds a partial one for the next
    poll, never repeats a line, and starts over when the component truncates
    the file."""
    mirror = app.path(variant.inbound_log)
    app.clock.polls = [
        lambda: _write(mirror, b"line A\nline B\npartial"),
        lambda: _append(mirror, b" done\n"),
        lambda: _write(mirror, b"fresh\n"),
    ]

    assert await _mirrored_lines(hass, app, upstream) == [
        "line A",
        "line B",
        "partial done",
        "fresh",
    ]


@pytest.mark.parametrize("stop", [signal.SIGTERM, signal.SIGINT], ids=lambda s: s.name)
async def test_stop_signal_runs_the_cleanup_and_logs_why(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
    stop: signal.Signals,
) -> None:
    """Without a handler a Supervisor stop killed the process mid-loop, so the
    webhook stayed registered and the log never said why it exited (#1694)."""
    app.set_options(mcp_server_url=upstream.target_url())
    app.clock.stop_signal = stop
    running: list[list[str]] = []
    app.clock.while_running = lambda: running.append(
        in_core(hass, lambda: entry_states(hass, variant))
    )

    assert await run_main(hass, app) == 0

    assert running == [["loaded"]]
    assert entry_states(hass, variant) == []
    assert f"Shutting down (reason: {stop.name})" in app.out()


async def test_stop_takes_oauth_dark_before_removing_the_entry(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
) -> None:
    """The heartbeat goes first, so a kill during the slow Core calls cannot
    leave the OAuth views serving for a stopped app; then the entry is
    removed, the mirror dropped, and a second signal can no longer abort the
    cleanup."""
    app.set_options(
        mcp_server_url=upstream.target_url(), enable_oauth=True, debug_logging=True
    )
    mirror = app.path(variant.inbound_log)
    app.clock.while_running = lambda: _write(mirror, b"x\n")
    document = f"{variant.oauth_base}/authorization-server"
    served_at_removal: list[int] = []

    def client_calls_at_removal(method: str, path: str) -> None:
        if method == "DELETE":
            served_at_removal.append(core.get(document))

    core.on_request = client_calls_at_removal

    assert await run_main(hass, app) == 0

    assert served_at_removal == [404]
    assert entry_states(hass, variant) == []
    assert not mirror.exists()
    assert app.signals.handlers == {
        signal.SIGTERM: signal.SIG_DFL,
        signal.SIGINT: signal.SIG_DFL,
    }
