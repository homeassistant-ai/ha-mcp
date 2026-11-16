"""The sidecar child: ``run_main`` binds, publishes its URL and serves the page.

``ui.url``/``ui.pid`` mean "a sidecar is serving right now" and go with the
process; ``ui.state`` keeps this install's port and secret so the URL survives
the replace on every startup (#2131). The app answers only on the secret path,
only for its own Host, and refuses cross-origin writes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import pytest
import uvicorn
from starlette.routing import BaseRoute, Route
from starlette.testclient import TestClient

from ha_mcp import stdio_settings_sidecar as sidecar
from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp.config import reset_global_settings
from ha_mcp.settings_ui import register_settings_routes

from ._core_client import free_port
from ._stdio_sidecar_core import (
    SENTINEL,
    Serving,
    quiet_uvicorn_logging,
    run_sidecar,
)
from ._stdio_sidecar_core import data_dir as data_dir
from ._stdio_sidecar_core import served as served

LOGGER = "ha_mcp.stdio_settings_sidecar"
SERVING_FILES = {"ui.pid", "ui.url"}


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def _remember(data_dir: Path, port: int, secret_path: str) -> None:
    (data_dir / "ui.state").write_text(
        json.dumps({"port": port, "secret_path": secret_path})
    )


def _serve(monkeypatch: pytest.MonkeyPatch) -> Serving:
    seen: list[Serving] = []
    assert run_sidecar(monkeypatch, seen.append) == 0
    return seen[0]


def _never_serves(_serving: Serving) -> None:
    pytest.fail("the sidecar served")


@pytest.mark.usefixtures("socket_enabled")
class TestRunMain:
    def test_published_url_is_the_listening_port_and_a_secret_path(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The socket listens before the URL is published, so a parent's retire
        POST is queued by the kernel, never refused as "no sidecar there"."""
        facts: dict[str, Any] = {}

        def on_serve(serving: Serving) -> None:
            [listener] = serving.sockets
            facts["port"] = listener.getsockname()[1]
            facts["url"] = serving.url
            facts["pid"] = (data_dir / "ui.pid").read_text().strip()
            with socket.create_connection(("127.0.0.1", facts["port"]), timeout=5):
                pass

        assert run_sidecar(monkeypatch, on_serve) == 0

        assert re.fullmatch(
            rf"http://127\.0\.0\.1:{facts['port']}/private_[A-Za-z0-9_-]+/settings",
            facts["url"],
        )
        assert facts["pid"] == str(os.getpid())
        assert not SERVING_FILES & {path.name for path in data_dir.iterdir()}

    def test_state_outlives_the_serving_files(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The next spawn reads ui.state for the same port and secret."""
        serving = _serve(monkeypatch)

        state = json.loads((data_dir / "ui.state").read_text())
        url = urlsplit(serving.url)
        assert (state["port"], state["secret_path"]) == (url.port, serving.prefix)

    def test_remembered_port_and_secret_are_reused(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        port = free_port()
        _remember(data_dir, port, "/private_persisted")

        serving = _serve(monkeypatch)

        assert serving.url == f"http://127.0.0.1:{port}/private_persisted/settings"

    def test_a_taken_remembered_port_moves_the_state_to_the_bound_one(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Remembering the requested port would make ui.state disagree with the
        URL just served, and the URL would flip on every restart."""
        with socket.socket() as squatter:
            squatter.bind(("127.0.0.1", 0))
            squatter.listen()
            taken = squatter.getsockname()[1]
            _remember(data_dir, taken, "/private_persisted")

            serving = _serve(monkeypatch)

        bound = urlsplit(serving.url).port
        assert bound != taken
        assert serving.prefix == "/private_persisted"
        assert json.loads((data_dir / "ui.state").read_text())["port"] == bound

    def test_a_pinned_port_wins_over_the_remembered_one(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pin (#1587) moves the port; the secret, and so the path, stays."""
        pin = free_port()
        monkeypatch.setenv("HA_MCP_SIDECAR_PORT", str(pin))
        reset_global_settings()
        _remember(data_dir, free_port(), "/private_persisted")

        serving = _serve(monkeypatch)

        assert serving.url == f"http://127.0.0.1:{pin}/private_persisted/settings"
        assert json.loads((data_dir / "ui.state").read_text())["port"] == pin

    def test_no_port_at_all_leaves_a_live_sidecars_files_alone(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A child that can never serve must not rewrite or clean the state and
        discovery files of the predecessor still serving."""
        files = {
            "ui.pid": "424242\n",
            "ui.url": "http://127.0.0.1:54321/private_live/settings\n",
            "ui.state": json.dumps({"port": 54321, "secret_path": "/private_live"}),
        }
        for name, text in files.items():
            (data_dir / name).write_text(text)

        def no_port(_preferred: int, _source: str) -> socket.socket:
            raise OSError("no ports available")

        monkeypatch.setattr(sidecar, "bind_listener", no_port)

        assert run_sidecar(monkeypatch, _never_serves) != 0
        assert {name: (data_dir / name).read_text() for name in files} == files

    def test_a_disabled_ui_does_not_serve(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A manual start after a Stop keeps the user's choice."""
        (data_dir / SENTINEL).write_text("manual\n")

        assert run_sidecar(monkeypatch, _never_serves) == 0
        assert not SERVING_FILES & {path.name for path in data_dir.iterdir()}


@pytest.mark.usefixtures("socket_enabled")
class TestExitCleanup:
    """On exit the sidecar removes only the files it still owns.

    The retire wait is bounded, so a slow-dying sidecar can still be exiting
    after its successor wrote its own files; with sticky ui.state the
    successor's ui.url is byte-identical, so only the pid tells them apart.
    """

    def test_a_successors_files_survive(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def successor_takes_over(serving: Serving) -> None:
            (data_dir / "ui.pid").write_text("424242\n")
            (data_dir / "ui.url").write_text(serving.url + "\n")

        assert run_sidecar(monkeypatch, successor_takes_over) == 0

        assert (data_dir / "ui.pid").read_text().strip() == "424242"
        assert (data_dir / "ui.url").exists()

    def test_a_url_without_a_pid_is_nobodys_to_remove(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def pid_gone(_serving: Serving) -> None:
            (data_dir / "ui.pid").unlink()

        assert run_sidecar(monkeypatch, pid_gone) == 0

        assert (data_dir / "ui.url").exists()


@pytest.mark.usefixtures("socket_enabled")
class TestRememberedState:
    """Anything ``ui.state`` holds that the sidecar cannot safely serve yields a
    fresh URL, and, since a file was there, a warning: a silently changed URL
    looks like the bug the state exists to prevent."""

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("not json {{{", id="not-json"),
            pytest.param(json.dumps(["port", 54321]), id="not-an-object"),
            pytest.param(
                json.dumps({"port": "54321", "secret_path": "/private_x"}),
                id="port-text",
            ),
            pytest.param(
                json.dumps({"port": True, "secret_path": "/private_x"}),
                id="port-bool",
            ),
            pytest.param(
                json.dumps({"port": 1023, "secret_path": "/private_x"}),
                id="port-below-floor",
            ),
            pytest.param(
                json.dumps({"port": 99999, "secret_path": "/private_x"}),
                id="port-above-range",
            ),
            pytest.param(
                json.dumps({"port": 54321, "secret_path": "/private_a/../b"}),
                id="secret-traversal",
            ),
            pytest.param(json.dumps({"port": 54321}), id="secret-missing"),
        ],
    )
    def test_unusable_state_gives_a_fresh_url_and_a_warning(
        self,
        data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        payload: str,
    ) -> None:
        (data_dir / "ui.state").write_text(payload)

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            serving = _serve(monkeypatch)

        assert serving.prefix not in {"/private_x", "/private_a/../b"}
        assert any("ui.state" in message for message in _messages(caplog))

    def test_a_first_start_does_not_warn(
        self,
        data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            _serve(monkeypatch)

        assert not any("ui.state" in message for message in _messages(caplog))

    def test_the_lowest_unprivileged_port_is_kept(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _remember(data_dir, 1024, "/private_ok")

        assert _serve(monkeypatch).prefix == "/private_ok"

    def test_state_that_cannot_be_written_does_not_stop_the_sidecar(
        self,
        data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        (data_dir / "ui.state.tmp").mkdir()

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            _serve(monkeypatch)

        assert any(
            "Failed to persist sidecar state" in message
            for message in _messages(caplog)
        ), _messages(caplog)


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl locks are POSIX")
@pytest.mark.usefixtures("socket_enabled")
class TestServingFilesLock:
    """The pid/url writes and the exit cleanup share one inter-process lock, so
    a predecessor cannot pass its ownership check and then unlink the files
    its successor wrote a moment later."""

    @pytest.fixture
    def lock_fds(self, monkeypatch: pytest.MonkeyPatch) -> set[int]:
        """The descriptors opened on ``files.lock``."""
        fds: set[int] = set()
        real_open = os.open

        def recording_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            fd = real_open(path, flags, *args, **kwargs)
            if str(path).endswith("files.lock"):
                fds.add(fd)
            return fd

        monkeypatch.setattr(os, "open", recording_open)
        return fds

    def test_pid_and_url_change_only_while_locked(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch, lock_fds: set[int]
    ) -> None:
        import fcntl

        real_flock = fcntl.flock
        steps: list[tuple[str, set[str]]] = []

        def present() -> set[str]:
            return SERVING_FILES & {path.name for path in data_dir.iterdir()}

        def recording_flock(fd: int, operation: int) -> None:
            if fd in lock_fds and operation == fcntl.LOCK_UN:
                steps.append(("unlock", present()))
            real_flock(fd, operation)
            if fd in lock_fds and operation != fcntl.LOCK_UN:
                steps.append(("lock", present()))

        monkeypatch.setattr(fcntl, "flock", recording_flock)

        _serve(monkeypatch)

        assert steps == [
            ("lock", set()),
            ("unlock", SERVING_FILES),
            ("lock", SERVING_FILES),
            ("unlock", set()),
        ]

    def test_a_lock_that_cannot_be_taken_degrades_loudly(
        self,
        data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        lock_fds: set[int],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A flock-less mount must not take the sidecar down with it."""
        import fcntl

        real_flock = fcntl.flock

        def failing_flock(fd: int, operation: int) -> None:
            if fd in lock_fds:
                raise OSError("ENOLCK")
            real_flock(fd, operation)

        monkeypatch.setattr(fcntl, "flock", failing_flock)

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            _serve(monkeypatch)

        assert any("proceeding unlocked" in m for m in _messages(caplog))

    def test_a_lock_file_that_cannot_be_opened_degrades_loudly(
        self,
        data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        real_open = os.open

        def failing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if str(path).endswith("files.lock"):
                raise OSError("read-only file system")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", failing_open)

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            _serve(monkeypatch)

        assert any("proceeding unlocked" in m for m in _messages(caplog))


class TestServedApp:
    """What the browser reaches at the published URL."""

    def test_the_published_url_serves_the_settings_page(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch, socket_enabled: None
    ) -> None:
        """The URL ``ha_get_overview`` surfaces must open the page: a drift
        between the URL writer and the route table hands out a dead link."""
        pages: list[tuple[int, str, str]] = []

        def open_surfaced_url(serving: Serving) -> None:
            surfaced = sidecar.live_sidecar_url()
            assert surfaced is not None
            resp = serving.client().get(urlsplit(surfaced).path)
            pages.append((resp.status_code, resp.headers["content-type"], resp.text))

        assert run_sidecar(monkeypatch, open_surfaced_url) == 0

        [(status, content_type, text)] = pages
        assert status == 200
        assert content_type.startswith("text/html")
        assert "<html" in text.lower()

    def test_a_foreign_host_is_rejected(self, served: Serving) -> None:
        """DNS rebinding: an attacker's name resolving to 127.0.0.1 still sends
        its own name as Host."""
        resp = TestClient(served.app).get(f"{served.prefix}/api/settings/info")

        assert resp.status_code == 400
        assert "Host header not allowed" in resp.text

    def test_its_own_host_is_served(self, served: Serving) -> None:
        resp = served.client().get(f"{served.prefix}/api/settings/info")

        assert resp.status_code == 200

    def test_a_cross_origin_write_is_refused(self, served: Serving) -> None:
        resp = served.client().post(
            f"{served.prefix}/api/settings/tools",
            headers={"origin": "http://evil.example.com"},
            json={"states": {}},
        )

        assert resp.status_code == 403

    def test_a_same_origin_write_is_served(self, served: Serving) -> None:
        resp = served.client().post(
            f"{served.prefix}/api/settings/tools",
            headers={"origin": f"http://{served.host}"},
            json={"states": {}},
        )

        assert resp.status_code == 200

    def test_the_page_never_offers_an_addon_restart(
        self, served: Serving, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The child inherits the parent's environment: with SUPERVISOR_TOKEN
        set (a debug shell in the add-on container) the page would show a
        Restart button whose route the sidecar does not mount."""
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-supervisor-token")

        body = served.client().get(f"{served.prefix}/api/settings/info").json()

        assert body["is_addon"] is False
        assert body["is_sidecar"] is True
        assert body["deployment_mode"] == "sidecar"

    def test_visibility_settings_round_trip(self, served: Serving) -> None:
        client = served.client()
        path = f"{served.prefix}/api/visibility/config"
        initial = client.get(path).json()
        assert initial["restrict_report_issue"] is False

        saved = client.put(
            path,
            json={
                "version": initial["version"],
                "enabled": True,
                "enforce": True,
                "deny_entity_ids": ["light.bed_light"],
                "restrict_report_issue": True,
            },
        )

        assert saved.status_code == 200
        current = client.get(path).json()
        assert current["deny_entity_ids"] == ["light.bed_light"]
        assert current["restrict_report_issue"] is True

    def test_every_shared_settings_route_but_restart_is_served(
        self, served: Serving, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sidecar mounts the HTTP modes' settings surface; a route added
        there and missing here is a dead button in stdio mode."""
        mcp = FastMCP("test")
        monkeypatch.setattr("ha_mcp.settings_ui._http_settings_mounted", False)
        register_settings_routes(
            mcp, MagicMock(), secret_path=served.prefix, advertise_prefix=False
        )

        def table(routes: list[BaseRoute]) -> set[tuple[str, frozenset[str]]]:
            return {
                (route.path, frozenset((route.methods or set()) - {"HEAD"}))
                for route in routes
                if isinstance(route, Route) and route.path.startswith(served.prefix)
            }

        shared = table(mcp.http_app().routes)
        mounted = table(served.app.routes)
        restart = (f"{served.prefix}/api/settings/restart", frozenset({"POST"}))
        shutdown = (f"{served.prefix}/api/settings/shutdown", frozenset({"POST"}))

        assert mounted - {shutdown} == shared - {restart}


class TestShutdownEndpoint:
    """``/shutdown``: the page's Stop button disables, a parent's retire does not."""

    def test_stop_disables_and_reports_only_its_own_sentinel(
        self, served: Serving, data_dir: Path
    ) -> None:
        """The sentinel is written before the stop, so a fast restart cannot
        race past it; a repeat Stop did not create it and says so."""
        client = served.client()
        stop = f"{served.prefix}/api/settings/shutdown"

        first = client.post(stop)
        second = client.post(stop)

        assert (first.status_code, second.status_code) == (200, 200)
        assert first.json()["sentinel_created"] is True
        assert second.json()["sentinel_created"] is False
        assert (data_dir / SENTINEL).exists()
        assert served.server.should_exit is True

    def test_a_failed_stop_rolls_its_sentinel_back(
        self, served: Serving, data_dir: Path
    ) -> None:
        """Still running with a sentinel on disk is the worst state: this
        session keeps the UI and the next launch silently has none."""

        def stop_fails() -> None:
            raise RuntimeError("uvicorn server.should_exit assignment failed")

        served.app.state.shutdown_state["stop"] = stop_fails

        resp = served.client().post(f"{served.prefix}/api/settings/shutdown")

        assert resp.status_code == 500
        assert resp.json()["success"] is False
        assert "rolled back" in resp.json()["error"]["message"].lower()
        assert not (data_dir / SENTINEL).exists()

    def test_a_retire_gives_no_re_enable_advice(
        self, served: Serving, data_dir: Path
    ) -> None:
        """A retire wrote no sentinel and a replacement follows at once."""
        resp = served.client().post(
            f"{served.prefix}/api/settings/shutdown?mode=retire"
        )

        assert resp.json()["sentinel_created"] is False
        assert "Delete" not in resp.json()["message"]
        assert not (data_dir / SENTINEL).exists()

    def test_a_parents_retire_stops_the_real_sidecar(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch, socket_enabled: None
    ) -> None:
        """The retire POST must pass the real route table and middleware with
        urllib's request shape (loopback Host, no Origin): requiring Origin
        would break every replace."""
        # The sidecar runs in this process, so its pid never exits.
        monkeypatch.setattr(sidecar, "_OLD_SIDECAR_EXIT_WAIT", 0.0)
        quiet_uvicorn_logging(monkeypatch)
        serving = threading.Event()
        servers: list[uvicorn.Server] = []
        real_run = uvicorn.Server.run

        def run(
            server: uvicorn.Server, sockets: list[socket.socket] | None = None
        ) -> None:
            servers.append(server)
            serving.set()
            real_run(server, sockets)

        monkeypatch.setattr(uvicorn.Server, "run", run)
        exit_codes: list[int] = []
        thread = threading.Thread(
            target=lambda: exit_codes.append(sidecar.run_main()), name="sidecar"
        )
        thread.start()
        try:
            assert serving.wait(timeout=30)
            acknowledged = sidecar.retire_sidecar(data_dir)
            thread.join(timeout=30)
        finally:
            if thread.is_alive() and servers:
                servers[0].should_exit = True
            thread.join()

        assert acknowledged is True
        assert exit_codes == [0]
        assert not (data_dir / SENTINEL).exists()
        assert not SERVING_FILES & {path.name for path in data_dir.iterdir()}
