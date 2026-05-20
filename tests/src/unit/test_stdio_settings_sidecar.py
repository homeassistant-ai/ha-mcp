"""Unit tests for the stdio settings UI sidecar (issue #863).

Covers the parent-side spawn gates (env var, sentinel, pid liveness)
and the read-side helper that ``ha_get_overview`` uses. The full
end-to-end spawn-and-serve flow is exercised in the integration suite
because the subprocess + Starlette stack doesn't unit-test cleanly.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from ha_mcp import stdio_settings_sidecar as sidecar
from ha_mcp.settings_ui import (
    build_settings_handlers,
    dump_tool_metadata_cache,
    load_tool_metadata_cache,
)


@pytest.fixture
def tmp_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Path]:
    """Redirect ha-mcp's data dir to ``tmp_path`` for the test.

    Uses the public ``HA_MCP_CONFIG_DIR`` env-var override rather than
    monkeypatching the function, so every importer of
    ``get_data_dir`` (settings_ui, sidecar, etc.) sees the same dir
    regardless of how they imported the name. The lru_cache must be
    cleared before AND after so adjacent tests don't see this tmp dir.
    """
    from ha_mcp.utils.data_paths import get_data_dir

    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    get_data_dir.cache_clear()
    try:
        yield tmp_path
    finally:
        get_data_dir.cache_clear()


class TestToolMetadataCache:
    """Round-trip tests for the on-disk metadata cache."""

    def test_dump_and_load(self, tmp_data_dir: Path) -> None:
        payload = [{"name": "ha_get_state", "primary_tag": "Entity Operations"}]
        assert dump_tool_metadata_cache(payload) is True
        assert load_tool_metadata_cache() == payload

    def test_load_missing_file_returns_empty(self, tmp_data_dir: Path) -> None:
        assert load_tool_metadata_cache() == []

    def test_load_malformed_returns_empty(self, tmp_data_dir: Path) -> None:
        (tmp_data_dir / "tool_metadata.json").write_text("not json {{{")
        assert load_tool_metadata_cache() == []

    def test_load_wrong_shape_returns_empty(self, tmp_data_dir: Path) -> None:
        (tmp_data_dir / "tool_metadata.json").write_text('{"not": "a list"}')
        assert load_tool_metadata_cache() == []


class TestBuildSettingsHandlers:
    """build_settings_handlers behaves correctly without a live server."""

    def test_returns_all_handler_keys(self) -> None:
        handlers = build_settings_handlers(server=None)
        assert set(handlers.keys()) == {
            "root_page",
            "settings_page",
            "get_tools",
            "save_tools",
            "restart_addon",
            "settings_info",
        }

    def test_get_tools_reads_cache_when_server_is_none(
        self, tmp_data_dir: Path
    ) -> None:
        payload = [{"name": "ha_call_event", "primary_tag": "System"}]
        dump_tool_metadata_cache(payload)
        handlers = build_settings_handlers(server=None)

        # Mount on a Starlette app so we can probe via TestClient (which
        # provides a valid Host header for the response code paths).
        from starlette.routing import Route

        app = Starlette(
            routes=[Route("/api/settings/tools", handlers["get_tools"])]
        )
        client = TestClient(app)
        resp = client.get("/api/settings/tools")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tools"] == payload

    def test_restart_addon_returns_400_without_server(self) -> None:
        from starlette.routing import Route

        handlers = build_settings_handlers(server=None)
        app = Starlette(
            routes=[
                Route(
                    "/api/settings/restart",
                    handlers["restart_addon"],
                    methods=["POST"],
                )
            ]
        )
        client = TestClient(app)
        # Even with SUPERVISOR_TOKEN set, restart must refuse when there's
        # no server to read verify_ssl from.
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "x"}):
            resp = client.post("/api/settings/restart")
        assert resp.status_code == 400


class TestSidecarDisableGates:
    """``_is_disabled`` honors env var + sentinel."""

    def test_env_var_truthy_disables(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", "1")
        assert sidecar._is_disabled() is True

    def test_env_var_falsy_does_not_disable(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", "0")
        assert sidecar._is_disabled() is False

    def test_sentinel_file_disables(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        (tmp_data_dir / "settings_ui_disabled").write_text("user disabled\n")
        assert sidecar._is_disabled() is True

    def test_neither_set(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        assert sidecar._is_disabled() is False


class TestReadSidecarUrl:
    """``read_sidecar_url`` is what ha_get_overview consumes."""

    def test_returns_url_when_file_present(self, tmp_data_dir: Path) -> None:
        url = "http://127.0.0.1:54321/private_abc/settings"
        (tmp_data_dir / "ui.url").write_text(url + "\n")
        assert sidecar.read_sidecar_url() == url

    def test_returns_none_when_file_missing(self, tmp_data_dir: Path) -> None:
        assert sidecar.read_sidecar_url() is None

    def test_returns_none_when_file_empty(self, tmp_data_dir: Path) -> None:
        (tmp_data_dir / "ui.url").write_text("")
        assert sidecar.read_sidecar_url() is None


class TestPidLiveness:
    """``_pid_alive`` and ``_existing_sidecar_alive`` correctness."""

    def test_pid_alive_for_self(self) -> None:
        assert sidecar._pid_alive(os.getpid()) is True

    def test_pid_alive_for_invalid(self) -> None:
        # PID 0 / negative are not valid process targets and must be
        # treated as "not alive" so a corrupt pidfile doesn't block
        # respawn forever.
        assert sidecar._pid_alive(0) is False
        assert sidecar._pid_alive(-1) is False

    def test_existing_sidecar_missing_pidfile(self, tmp_data_dir: Path) -> None:
        assert sidecar._existing_sidecar_alive() is False

    def test_existing_sidecar_garbage_pidfile(self, tmp_data_dir: Path) -> None:
        (tmp_data_dir / "ui.pid").write_text("not a number\n")
        assert sidecar._existing_sidecar_alive() is False

    def test_existing_sidecar_dead_pid(self, tmp_data_dir: Path) -> None:
        # PID 999999 is almost certainly not running. If by some
        # miracle it is on the test box, the assertion will fail loudly
        # — better than silently passing on a stale check.
        (tmp_data_dir / "ui.pid").write_text("999999\n")
        assert sidecar._existing_sidecar_alive() is False


class TestMaybeSpawnGates:
    """``maybe_spawn`` short-circuits in the documented cases."""

    def test_maybe_spawn_skips_when_disabled(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", "1")
        with patch("subprocess.Popen") as popen:
            sidecar.maybe_spawn()
        popen.assert_not_called()

    def test_maybe_spawn_skips_when_sidecar_alive(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        (tmp_data_dir / "ui.pid").write_text(f"{os.getpid()}\n")
        with patch("subprocess.Popen") as popen:
            sidecar.maybe_spawn()
        popen.assert_not_called()

    def test_maybe_spawn_invokes_popen_when_no_existing(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        # No pid file, no sentinel — spawn path should trigger.
        fake_proc = MagicMock()
        fake_proc.pid = 12345
        with patch("subprocess.Popen", return_value=fake_proc) as popen:
            sidecar.maybe_spawn()
        popen.assert_called_once()
        # The child command must invoke the sidecar entrypoint via -m
        # so it doesn't depend on console_scripts being on PATH.
        called_cmd = popen.call_args[0][0]
        assert called_cmd[-2:] == ["-m", "ha_mcp.stdio_settings_sidecar"]


class TestSecurityMiddleware:
    """End-to-end: build_app rejects bad Host / Origin headers."""

    def test_host_header_rejected(self, tmp_data_dir: Path) -> None:
        app = sidecar._build_app(
            host="127.0.0.1", port=12345, secret_path="/private_xx"
        )
        client = TestClient(app)
        # TestClient sets Host=testserver by default, which is not in
        # the allowed set {127.0.0.1:12345, localhost:12345}.
        resp = client.get("/private_xx/api/settings/info")
        assert resp.status_code == 400
        assert "Host header not allowed" in resp.text

    def test_host_header_accepted(self, tmp_data_dir: Path) -> None:
        app = sidecar._build_app(
            host="127.0.0.1", port=12345, secret_path="/private_xx"
        )
        client = TestClient(app)
        resp = client.get(
            "/private_xx/api/settings/info",
            headers={"host": "127.0.0.1:12345"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"is_addon": False}

    def test_post_origin_rejected(self, tmp_data_dir: Path) -> None:
        app = sidecar._build_app(
            host="127.0.0.1", port=12345, secret_path="/private_xx"
        )
        client = TestClient(app)
        resp = client.post(
            "/private_xx/api/settings/tools",
            headers={
                "host": "127.0.0.1:12345",
                "origin": "http://evil.example.com",
            },
            json={"states": {}},
        )
        assert resp.status_code == 403

    def test_post_origin_accepted(self, tmp_data_dir: Path) -> None:
        app = sidecar._build_app(
            host="127.0.0.1", port=12345, secret_path="/private_xx"
        )
        client = TestClient(app)
        resp = client.post(
            "/private_xx/api/settings/tools",
            headers={
                "host": "127.0.0.1:12345",
                "origin": "http://127.0.0.1:12345",
            },
            json={"states": {}},
        )
        # 200 (save succeeded) or 500 (cache dir unwritable) is fine —
        # the contract is that the request wasn't rejected at the
        # security middleware. 4xx/403 here would mean the origin check
        # mis-fired.
        assert resp.status_code in (200, 500)

    def test_shutdown_writes_sentinel(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = sidecar._build_app(
            host="127.0.0.1", port=12345, secret_path="/private_xx"
        )
        # Wire a no-op stop so the endpoint can complete.
        app.state.shutdown_state["stop"] = lambda: None
        client = TestClient(app)
        resp = client.post(
            "/private_xx/api/settings/shutdown",
            headers={
                "host": "127.0.0.1:12345",
                "origin": "http://127.0.0.1:12345",
            },
        )
        assert resp.status_code == 200
        # Sentinel must have been written so the next maybe_spawn skips.
        assert (tmp_data_dir / "settings_ui_disabled").exists()


class TestSidecarSettingsInfo:
    """Sidecar's settings_info MUST report is_addon=False regardless of env.

    The sidecar inherits parent env unchanged (subprocess.Popen with
    default env=None). If the parent stdio process happens to have
    SUPERVISOR_TOKEN set (e.g. a debug shell inside the add-on
    container), the served HTML would otherwise show the "Restart
    Add-on" button that POSTs to a route the sidecar doesn't expose —
    surfacing as a broken UI. The is_sidecar=True flag pins the answer.
    """

    def test_sidecar_settings_info_forces_is_addon_false(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-supervisor-token")
        app = sidecar._build_app(
            host="127.0.0.1", port=12345, secret_path="/private_xx"
        )
        client = TestClient(app)
        resp = client.get(
            "/private_xx/api/settings/info",
            headers={"host": "127.0.0.1:12345"},
        )
        assert resp.status_code == 200
        # Even with SUPERVISOR_TOKEN set, sidecar forces is_addon=False
        # AND advertises is_sidecar=True (drives the in-page Stop button).
        assert resp.json() == {"is_addon": False, "is_sidecar": True}


class TestRunMainWiring:
    """``run_main`` glue: port pick → URL build → app build → file writes.

    Mocks ``uvicorn.Server`` so the test doesn't bind a real socket or
    block on ``server.run()``. The contract verified here is that the
    URL file lands with the expected shape and the pid file matches
    the current process — same paths ``ha_get_overview`` and the
    parent's ``maybe_spawn`` read at runtime.
    """

    def test_run_main_writes_pid_and_url_with_secret_path(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.setattr(sidecar, "_pick_free_port", lambda: 54321)

        # Mock uvicorn.Server so ``server.run()`` is a no-op and the
        # test doesn't bind a port or block. The test exercises the
        # pre-run() wiring; the cleanup block in ``run_main`` would
        # unlink ui.url/ui.pid on exit, so we snapshot before that
        # runs by patching server.run to verify the files exist there.
        snapshot: dict[str, str] = {}

        class FakeServer:
            def __init__(self, _config: object) -> None:
                self.should_exit = False

            def run(self) -> None:
                # Capture state at the moment the listener "starts".
                snapshot["url"] = (tmp_data_dir / "ui.url").read_text().strip()
                snapshot["pid"] = (tmp_data_dir / "ui.pid").read_text().strip()

        fake_uvicorn = MagicMock()
        fake_uvicorn.Server = FakeServer
        fake_uvicorn.Config = MagicMock()
        monkeypatch.setitem(__import__("sys").modules, "uvicorn", fake_uvicorn)

        rc = sidecar.run_main()
        assert rc == 0

        url = snapshot["url"]
        assert url.startswith("http://127.0.0.1:54321/private_"), (
            f"URL missing host/port/secret prefix: {url!r}"
        )
        assert url.endswith("/settings"), f"URL missing /settings suffix: {url!r}"
        assert snapshot["pid"] == str(os.getpid()), (
            f"pid file must record current process: got {snapshot['pid']!r}"
        )
        # Cleanup path runs in run_main's finally; both files must be gone.
        assert not (tmp_data_dir / "ui.url").exists()
        assert not (tmp_data_dir / "ui.pid").exists()

    def test_run_main_respects_disable_sentinel(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        (tmp_data_dir / "settings_ui_disabled").write_text("manual\n")
        # If uvicorn is imported the disable path wasn't honored. Set
        # sys.modules["uvicorn"] to a sentinel that raises on attribute
        # access so any path through the rest of run_main blows up
        # visibly instead of silently passing.
        forbidden = MagicMock()
        forbidden.__getattr__ = MagicMock(
            side_effect=AssertionError("uvicorn touched despite disable sentinel")
        )
        monkeypatch.setitem(__import__("sys").modules, "uvicorn", forbidden)

        rc = sidecar.run_main()
        assert rc == 0
        # No pid/url files written.
        assert not (tmp_data_dir / "ui.pid").exists()
        assert not (tmp_data_dir / "ui.url").exists()


class TestMaybeSpawnStaleCleanup:
    """``maybe_spawn`` MUST unlink stale pid+url files BEFORE spawning.

    Catches a reordering regression where Popen is called first,
    leaving the child to inherit and overwrite stale files instead of
    starting from a clean state — which would mean the next
    ``read_sidecar_url`` call could briefly return the OLD URL pointing
    at a dead listener.
    """

    def test_stale_pid_and_url_unlinked_before_popen(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        stale_pid = tmp_data_dir / "ui.pid"
        stale_url = tmp_data_dir / "ui.url"
        # Pre-create stale files with a CLEARLY DEAD pid so the
        # "_existing_sidecar_alive" guard returns False and we proceed
        # to the cleanup-then-spawn path.
        stale_pid.write_text("999999\n")
        stale_url.write_text("http://127.0.0.1:1/stale\n")

        # Snapshot the files' existence at the moment Popen is called.
        # The contract is "files gone before Popen runs"; recording the
        # state via Popen.side_effect lets us assert on it directly.
        snapshot: dict[str, bool] = {}

        def _record_state(*_args: object, **_kwargs: object) -> MagicMock:
            snapshot["pid_exists"] = stale_pid.exists()
            snapshot["url_exists"] = stale_url.exists()
            proc = MagicMock()
            proc.pid = 12345
            return proc

        with patch("subprocess.Popen", side_effect=_record_state):
            sidecar.maybe_spawn()

        # Both stale files must have been unlinked before Popen ran;
        # if either is True the cleanup loop was reordered after spawn.
        assert snapshot.get("pid_exists") is False, (
            "stale ui.pid still present at Popen call — cleanup regressed"
        )
        assert snapshot.get("url_exists") is False, (
            "stale ui.url still present at Popen call — cleanup regressed"
        )


class TestDumpCacheFailurePath:
    """``dump_tool_metadata_cache`` documented contract: returns False on OSError."""

    def test_dump_returns_false_on_oserror(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force Path.write_text to raise; the helper must catch + return
        # False without escaping the exception (callers in __main__.py
        # rely on this to avoid blocking stdio startup on cache I/O).
        def _raise(*_args: object, **_kwargs: object) -> None:
            raise OSError("simulated disk full")

        monkeypatch.setattr(Path, "write_text", _raise)
        assert dump_tool_metadata_cache([{"name": "x"}]) is False
