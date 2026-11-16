"""Every stdio startup retires the sidecar a previous one left running (#2131).

``maybe_spawn`` posts the old sidecar's own ``/shutdown`` on the secret-path URL
it recorded, never ``os.kill``: only the real sidecar was told that path, so a
reused PID can never get an innocent process terminated. ``retire_sidecar`` is
the same retirement for a test process that spawned a stdio server.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from ha_mcp import stdio_settings_sidecar as sidecar

from ._core_client import free_port
from ._stdio_sidecar_core import (
    SENTINEL,
    OldSidecar,
    Spawner,
    exits_on_first_poll,
    live_process,
)
from ._stdio_sidecar_core import data_dir as data_dir
from ._stdio_sidecar_core import old_sidecar as old_sidecar
from ._stdio_sidecar_core import spawned as spawned

RETIRE = ("POST", "/private_old/api/settings/shutdown?mode=retire")
LOGGER = "ha_mcp.stdio_settings_sidecar"


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records]


@pytest.fixture
def no_exit_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give up on a still-running old sidecar at once, with the usual warning."""
    monkeypatch.setattr(sidecar, "_OLD_SIDECAR_EXIT_WAIT", 0.0)


def _waited(caplog: pytest.LogCaptureFixture) -> bool:
    return any("still alive" in message for message in _messages(caplog))


def _stop_clicked(sentinel: Path) -> Callable[[], None]:
    """The user's Stop on the page, which writes the disable sentinel."""

    def stop() -> None:
        sentinel.write_text("user clicked Stop\n")

    return stop


def test_startup_retires_the_old_sidecar_then_spawns_its_own(
    old_sidecar: OldSidecar, spawned: Spawner
) -> None:
    """A live sidecar is replaced, never reused: it would serve the code and
    environment of whatever parent spawned it, a 57-day-old orphan in #2131."""
    sidecar.maybe_spawn()

    assert old_sidecar.requests == [RETIRE]
    assert len(spawned.spawns) == 1


def test_retire_ignores_the_environment_proxy(
    old_sidecar: OldSidecar, spawned: Spawner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The POST embeds the secret path: a proxy would see the secret, and the
    request would never reach the loopback listener."""
    proxy = f"http://127.0.0.1:{free_port()}"
    for name in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, proxy)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    sidecar.maybe_spawn()

    assert old_sidecar.requests == [RETIRE]


def test_retire_does_not_follow_a_redirect(
    old_sidecar: OldSidecar, spawned: Spawner
) -> None:
    """A loopback listener must not bounce the secret-bearing request elsewhere."""
    old_sidecar.status = 302
    old_sidecar.headers = {"Location": f"http://127.0.0.1:{old_sidecar.port}/moved"}

    sidecar.maybe_spawn()

    assert old_sidecar.requests == [RETIRE]


@pytest.mark.parametrize(
    "recorded",
    [
        pytest.param("http://localhost:{port}/private_old/settings", id="not-127"),
        pytest.param("http://127.0.0.1:80/private_old/settings", id="privileged"),
    ],
)
def test_untrusted_recorded_url_gets_no_request(
    old_sidecar: OldSidecar,
    spawned: Spawner,
    data_dir: Path,
    caplog: pytest.LogCaptureFixture,
    recorded: str,
) -> None:
    """Only the URL shape the sidecar itself writes is trusted with the POST."""
    (data_dir / "ui.url").write_text(recorded.format(port=old_sidecar.port))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        sidecar.maybe_spawn()

    assert old_sidecar.requests == []
    assert any("URL is invalid" in message for message in _messages(caplog))
    assert len(spawned.spawns) == 1


def test_no_recorded_url_spawns_without_retiring(
    old_sidecar: OldSidecar, spawned: Spawner, data_dir: Path
) -> None:
    """A live pid with no URL (a crash left ui.pid behind, the pid since reused)
    has no listener to retire and no proof it is a sidecar: it is left alone."""
    (data_dir / "ui.url").unlink()
    (data_dir / "ui.pid").write_text(f"{os.getpid()}\n")

    sidecar.maybe_spawn()

    assert old_sidecar.requests == []
    assert len(spawned.spawns) == 1


def test_corrupt_recorded_url_does_not_stop_the_spawn(
    data_dir: Path, spawned: Spawner
) -> None:
    """A garbage ui.url must not raise out of startup, or every later startup
    fails the same way with the stale files still in place."""
    (data_dir / "ui.url").write_text("garbage\n")
    (data_dir / "ui.pid").write_text("4242\n")

    sidecar.maybe_spawn()

    assert len(spawned.spawns) == 1


def test_a_dead_legacy_sidecars_url_carries_over(
    data_dir: Path, spawned: Spawner, socket_enabled: None
) -> None:
    """Releases before ui.state kept port and secret only in ui.url, which the
    replace deletes: the first upgraded startup must keep the bookmarked URL,
    even when the old sidecar is already gone."""
    port = free_port()
    (data_dir / "ui.url").write_text(
        f"http://127.0.0.1:{port}/private_legacy/settings\n"
    )

    sidecar.maybe_spawn()

    state = json.loads((data_dir / "ui.state").read_text())
    assert state == {"port": port, "secret_path": "/private_legacy"}


@pytest.mark.parametrize(
    "recorded",
    [
        pytest.param("http://127.0.0.1:1023/private_x/settings", id="below-floor"),
        pytest.param("https://127.0.0.1:54321/private_x/settings", id="not-its-shape"),
    ],
)
def test_an_untrusted_legacy_url_carries_nothing_over(
    data_dir: Path, spawned: Spawner, recorded: str
) -> None:
    (data_dir / "ui.url").write_text(recorded)

    sidecar.maybe_spawn()

    assert not (data_dir / "ui.state").exists()


def test_existing_state_wins_over_the_recorded_url(
    old_sidecar: OldSidecar, spawned: Spawner, data_dir: Path
) -> None:
    state = {"port": 44444, "secret_path": "/private_state"}
    (data_dir / "ui.state").write_text(json.dumps(state))

    sidecar.maybe_spawn()

    assert json.loads((data_dir / "ui.state").read_text()) == state


def test_a_legacy_sidecars_retire_does_not_disable_its_successor(
    old_sidecar: OldSidecar, spawned: Spawner, data_dir: Path
) -> None:
    """A legacy /shutdown writes the disable sentinel before it exits; left on
    disk, the successor would honor it and the settings UI would be gone."""
    sentinel = data_dir / SENTINEL
    old_sidecar.body = {"success": True}
    old_sidecar.on_post = lambda: sentinel.write_text("Disabled via /shutdown\n")

    sidecar.maybe_spawn()

    assert not sentinel.exists()


def test_a_stop_reported_by_the_old_sidecar_survives_the_retire(
    old_sidecar: OldSidecar, spawned: Spawner, data_dir: Path
) -> None:
    """The user clicked Stop just before the retire POST arrived: the old
    sidecar reports it did not create the sentinel, which outranks the
    parent's older "no sentinel" snapshot."""
    sentinel = data_dir / SENTINEL
    old_sidecar.on_post = lambda: sentinel.write_text("Disabled via /shutdown\n")

    sidecar.maybe_spawn()

    assert sentinel.exists()


def test_a_stop_before_the_retire_survives_a_legacy_sidecar(
    old_sidecar: OldSidecar, spawned: Spawner, data_dir: Path
) -> None:
    """A Stop landing between the startup's disable check and its POST must
    stick; a legacy endpoint does not say who wrote the sentinel."""
    sentinel = data_dir / SENTINEL
    old_sidecar.body = {"success": True}

    sidecar.maybe_spawn(prepare=_stop_clicked(sentinel))

    assert sentinel.exists()
    assert old_sidecar.requests == [RETIRE]


def test_successor_waits_for_the_old_pid_even_after_its_cleanup(
    old_sidecar: OldSidecar,
    spawned: Spawner,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dying sidecar holds the port its successor rebinds. A current one
    unlinks ui.pid as it acknowledges, so the pid read after the POST would be
    gone and the successor would race it for the port."""
    with live_process() as old_process:
        (data_dir / "ui.pid").write_text(f"{old_process.pid}\n")
        old_sidecar.on_post = (data_dir / "ui.pid").unlink
        polls = exits_on_first_poll(monkeypatch, old_process)
        exited_at_spawn: list[bool] = []
        spawned.on_spawn = lambda: exited_at_spawn.append(
            old_process.poll() is not None
        )

        sidecar.maybe_spawn()

    assert polls
    assert exited_at_spawn == [True]


@pytest.mark.usefixtures("no_exit_wait")
def test_an_old_sidecar_that_never_exits_does_not_stall_startup(
    old_sidecar: OldSidecar,
    spawned: Spawner,
    data_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (data_dir / "ui.pid").write_text(f"{os.getpid()}\n")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        sidecar.maybe_spawn()

    assert any(
        "still alive" in message and "spawning replacement anyway" in message
        for message in _messages(caplog)
    ), _messages(caplog)
    assert len(spawned.spawns) == 1


@pytest.mark.usefixtures("no_exit_wait")
def test_refused_retire_says_so_and_leaves_the_sentinel(
    old_sidecar: OldSidecar,
    spawned: Spawner,
    data_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 500 means "alive and NOT shutting down", not "no sidecar there": it
    wrote no sentinel to clean up and will not exit, so nothing is waited on."""
    sentinel = data_dir / SENTINEL
    (data_dir / "ui.pid").write_text(f"{os.getpid()}\n")
    old_sidecar.status = 500

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        sidecar.maybe_spawn(prepare=_stop_clicked(sentinel))

    assert sentinel.exists()
    assert any(
        "refused shutdown" in message and "500" in message
        for message in _messages(caplog)
    ), _messages(caplog)
    assert not _waited(caplog)


@pytest.mark.usefixtures("no_exit_wait")
def test_unanswered_retire_warns_it_may_still_be_running(
    old_sidecar: OldSidecar,
    spawned: Spawner,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A slow but live sidecar is not "no responsive sidecar"."""
    monkeypatch.setattr(sidecar, "_SHUTDOWN_HTTP_TIMEOUT", 0.05)
    old_sidecar.on_post = old_sidecar.released.wait
    (data_dir / "ui.pid").write_text(f"{os.getpid()}\n")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        sidecar.maybe_spawn()

    assert any(
        "did not answer" in message and "may still be running" in message
        for message in _messages(caplog)
    ), _messages(caplog)
    assert not _waited(caplog)
    assert len(spawned.spawns) == 1


@pytest.mark.usefixtures("no_exit_wait")
def test_no_listener_at_the_recorded_url_skips_the_exit_wait(
    data_dir: Path,
    spawned: Spawner,
    caplog: pytest.LogCaptureFixture,
    socket_enabled: None,
) -> None:
    """The recorded pid is not proven to be a sidecar (pids are reused), so
    with nothing answering on the secret path there is nothing safe to wait on."""
    (data_dir / "ui.url").write_text(
        f"http://127.0.0.1:{free_port()}/private_gone/settings\n"
    )
    (data_dir / "ui.pid").write_text(f"{os.getpid()}\n")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        sidecar.maybe_spawn()

    assert not _waited(caplog)
    assert len(spawned.spawns) == 1


def test_garbage_pid_file_does_not_stop_the_replace(
    old_sidecar: OldSidecar, spawned: Spawner, data_dir: Path
) -> None:
    (data_dir / "ui.pid").write_text("not a number\n")

    sidecar.maybe_spawn()

    assert old_sidecar.requests == [RETIRE]
    assert len(spawned.spawns) == 1


class TestRetireSidecar:
    """``retire_sidecar``: a test process's cleanup says what it left behind."""

    def test_missing_url_file_is_debug_logged(
        self, data_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            acknowledged = sidecar.retire_sidecar(data_dir)

        assert acknowledged is False
        assert any(
            str(data_dir / "ui.url") in message and "not found" in message
            for message in _messages(caplog)
        ), _messages(caplog)

    def test_non_loopback_url_gets_no_request(
        self, old_sidecar: OldSidecar, data_dir: Path
    ) -> None:
        (data_dir / "ui.url").write_text(
            f"http://localhost:{old_sidecar.port}/private_old/settings"
        )

        assert sidecar.retire_sidecar(data_dir) is False
        assert old_sidecar.requests == []

    def test_unacknowledged_retirement_names_target_and_consequence(
        self,
        old_sidecar: OldSidecar,
        data_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        old_sidecar.status = 500

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            acknowledged = sidecar.retire_sidecar(data_dir)

        assert acknowledged is False
        messages = _messages(caplog)
        assert any(old_sidecar.url in message for message in messages), messages
        assert any(str(data_dir) in message for message in messages), messages
        assert any("may still be running" in message for message in messages)

    def test_acknowledged_retirement_waits_for_the_recorded_pid(
        self,
        old_sidecar: OldSidecar,
        data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with live_process() as old_process:
            (data_dir / "ui.pid").write_text(str(old_process.pid))
            polls = exits_on_first_poll(monkeypatch, old_process)

            acknowledged = sidecar.retire_sidecar(data_dir)

            exited = old_process.poll() is not None

        assert acknowledged is True
        assert len(polls) == 1
        assert exited
