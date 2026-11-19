"""A stdio startup spawns the settings sidecar once, and only when it may.

``maybe_spawn`` is skipped by the disable switches, serialized across parents
by the spawn lock, and holds that lock until its child has published the URL.
The stdio entry point dumps the tool list for that child first.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

from ha_mcp import stdio_settings_sidecar as sidecar
from ha_mcp.settings_ui import load_tool_metadata_cache

from ._stdio_sidecar_core import (
    DISABLE_ENV,
    NEW_URL,
    SENTINEL,
    OldSidecar,
    Spawner,
    another_parent_spawning,
    run_stdio_main,
)
from ._stdio_sidecar_core import data_dir as data_dir
from ._stdio_sidecar_core import old_sidecar as old_sidecar
from ._stdio_sidecar_core import spawned as spawned

LOGGER = "ha_mcp.stdio_settings_sidecar"


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records]


class TestSpawnGates:
    def test_disabled_ui_retires_the_old_sidecar_but_spawns_none(
        self,
        old_sidecar: OldSidecar,
        spawned: Spawner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Disabling must also stop a sidecar spawned before the switch (#2954)."""
        monkeypatch.setenv(DISABLE_ENV, "1")

        sidecar.maybe_spawn()

        assert [method for method, _path in old_sidecar.requests] == ["POST"]
        assert spawned.spawns == []

    def test_child_runs_the_sidecar_module(self, spawned: Spawner) -> None:
        """``-m`` keeps the child independent of console scripts on PATH."""
        sidecar.maybe_spawn()

        [spawn] = spawned.spawns
        assert spawn.cmd[-2:] == ["-m", "ha_mcp.stdio_settings_sidecar"]

    def test_stale_pid_and_url_are_gone_before_the_child_starts(
        self, data_dir: Path, spawned: Spawner
    ) -> None:
        """Otherwise ``read_sidecar_url`` could briefly hand out the old URL,
        pointing at a dead listener."""
        (data_dir / "ui.pid").write_text("999999\n")
        (data_dir / "ui.url").write_text("http://127.0.0.1:1/stale\n")

        sidecar.maybe_spawn()

        [spawn] = spawned.spawns
        assert not {"ui.pid", "ui.url"} & spawn.files

    def test_remembered_url_survives_the_stale_cleanup(
        self, data_dir: Path, spawned: Spawner
    ) -> None:
        """ui.state carries the stable URL to the child; cleaning it with the
        stale files would change the URL on every startup."""
        state = {"port": 54321, "secret_path": "/private_keep"}
        (data_dir / "ui.pid").write_text("999999\n")
        (data_dir / "ui.url").write_text("http://127.0.0.1:1/stale\n")
        (data_dir / "ui.state").write_text(json.dumps(state))

        sidecar.maybe_spawn()

        [spawn] = spawned.spawns
        assert "ui.state" in spawn.files
        assert json.loads((data_dir / "ui.state").read_text()) == state


class TestSpawnLock:
    """Two parents starting together must not both retire and spawn: the
    loser's child would race the winner's for the port and die."""

    def test_a_second_parent_neither_retires_nor_spawns(
        self, old_sidecar: OldSidecar, spawned: Spawner, data_dir: Path
    ) -> None:
        """A retire from the loser would stop the winner's brand-new sidecar."""
        with another_parent_spawning(data_dir):
            sidecar.maybe_spawn()

        assert old_sidecar.requests == []
        assert spawned.spawns == []

    def test_the_lock_is_free_again_for_the_next_startup(
        self, spawned: Spawner
    ) -> None:
        spawned.publishes = False
        spawned.exit_code = 0
        sidecar.maybe_spawn()
        sidecar.maybe_spawn()

        assert len(spawned.spawns) == 2

    def test_a_second_parent_points_the_user_at_the_published_url(
        self,
        data_dir: Path,
        spawned: Spawner,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (data_dir / "ui.url").write_text(NEW_URL + "\n")

        with another_parent_spawning(data_dir):
            sidecar.maybe_spawn()

        assert NEW_URL in capsys.readouterr().err

    def test_lock_is_held_until_the_child_publishes_its_url(
        self, data_dir: Path, spawned: Spawner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second parent starting in the child's import window would find no
        ui.url, retire nothing, and spawn a duplicate for the same port."""
        spawned.publishes = False
        lock_free: list[bool] = []

        def sleep(_seconds: float) -> None:
            try:
                with another_parent_spawning(data_dir):
                    lock_free.append(True)
            except OSError:
                lock_free.append(False)
            if len(lock_free) == 2:
                (data_dir / "ui.url").write_text(NEW_URL + "\n")

        monkeypatch.setattr(time, "sleep", sleep)

        sidecar.maybe_spawn()

        assert lock_free == [False, False]

    def test_a_child_that_dies_before_publishing_ends_the_wait_loudly(
        self, spawned: Spawner, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Startup moves on without a URL; the warning is the only trace."""
        spawned.publishes = False
        spawned.exit_code = 1

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            sidecar.maybe_spawn()

        assert any(
            "exited" in message and "before publishing" in message
            for message in _messages(caplog)
        ), _messages(caplog)

    def test_a_silent_child_ends_the_wait_loudly(
        self,
        spawned: Spawner,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An import hang in the child must not wedge stdio startup."""
        monkeypatch.setattr(sidecar, "_CHILD_PUBLISH_WAIT", 0.0)
        spawned.publishes = False

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            sidecar.maybe_spawn()

        assert any(
            "has not published its URL" in message for message in _messages(caplog)
        ), _messages(caplog)


class TestPrepareHook:
    """``prepare`` (the tool-list dump) runs for the winner only, before the
    retire: the old sidecar keeps the remembered port while it runs."""

    def test_prepare_runs_before_the_retire(
        self, old_sidecar: OldSidecar, spawned: Spawner
    ) -> None:
        seen_at_prepare: list[list[tuple[str, str]]] = []

        sidecar.maybe_spawn(
            prepare=lambda: seen_at_prepare.append(list(old_sidecar.requests))
        )

        assert seen_at_prepare == [[]]
        assert len(old_sidecar.requests) == 1

    def test_a_second_parent_never_prepares(
        self, data_dir: Path, spawned: Spawner
    ) -> None:
        """Its environment may differ from the winner's sidecar."""
        prepared: list[str] = []

        with another_parent_spawning(data_dir):
            sidecar.maybe_spawn(prepare=lambda: prepared.append("loser"))

        assert prepared == []

    def test_a_disabled_ui_never_prepares(
        self, spawned: Spawner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(DISABLE_ENV, "1")
        prepared: list[str] = []

        sidecar.maybe_spawn(prepare=lambda: prepared.append("disabled"))

        assert prepared == []


class TestStdioStartupDumpsTheToolList:
    """The stdio entry point dumps the tool list the child's Tools tab reads."""

    def test_the_dump_runs_when_a_sidecar_is_already_serving(
        self,
        old_sidecar: OldSidecar,
        spawned: Spawner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The serving sidecar is about to be replaced; its successor must read
        a list from THIS parent, or an old version's tools resurface (#2131)."""
        run_stdio_main(monkeypatch)

        assert "ha_get_state" in {tool["name"] for tool in load_tool_metadata_cache()}
        assert len(old_sidecar.requests) == 1
        assert len(spawned.spawns) == 1

    def test_a_second_parent_does_not_dump(
        self, data_dir: Path, spawned: Spawner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sidecar re-reads the shared file per request, so a loser's dump
        would replace the winner's list with its own environment's."""
        with another_parent_spawning(data_dir):
            run_stdio_main(monkeypatch)

        assert load_tool_metadata_cache() == []

    def test_a_disabled_ui_still_retires_but_does_not_dump(
        self,
        old_sidecar: OldSidecar,
        spawned: Spawner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(DISABLE_ENV, "1")

        run_stdio_main(monkeypatch)

        assert load_tool_metadata_cache() == []
        assert len(old_sidecar.requests) == 1
        assert spawned.spawns == []


class TestOverviewUrlGate:
    """``live_sidecar_url``: the URL reaches the agent only while it is served."""

    @pytest.fixture
    def published(self, data_dir: Path) -> Path:
        (data_dir / "ui.pid").write_text(f"{os.getpid()}\n")
        (data_dir / "ui.url").write_text(NEW_URL + "\n")
        return data_dir

    @pytest.mark.parametrize(
        ("switch", "surfaced"),
        [
            pytest.param(None, True, id="enabled"),
            pytest.param("disable", False, id="unrecognized-value-fails-closed"),
        ],
    )
    def test_disable_switch_value(
        self,
        published: Path,
        monkeypatch: pytest.MonkeyPatch,
        switch: str | None,
        surfaced: bool,
    ) -> None:
        """A mistyped value disables, as the HTTP transports' switch does."""
        if switch is not None:
            monkeypatch.setenv(DISABLE_ENV, switch)

        assert (sidecar.live_sidecar_url() == NEW_URL) is surfaced

    def test_stop_sentinel_hides_the_url(self, published: Path) -> None:
        (published / SENTINEL).write_text("user disabled\n")

        assert sidecar.live_sidecar_url() is None

    def test_a_corrupt_pid_is_not_a_live_sidecar(self, published: Path) -> None:
        """``kill(-1, 0)`` signals every process and succeeds: a corrupt pidfile
        would otherwise keep a dead URL surfaced forever."""
        (published / "ui.pid").write_text("-1\n")

        assert sidecar.live_sidecar_url() is None


class TestReadSidecarUrl:
    def test_returns_url_as_written(self, data_dir: Path) -> None:
        (data_dir / "ui.url").write_text(NEW_URL + "\n")

        assert sidecar.read_sidecar_url() == NEW_URL

    def test_returns_none_when_file_missing(self, data_dir: Path) -> None:
        assert sidecar.read_sidecar_url() is None

    def test_returns_none_when_file_empty(self, data_dir: Path) -> None:
        (data_dir / "ui.url").write_text("")

        assert sidecar.read_sidecar_url() is None
