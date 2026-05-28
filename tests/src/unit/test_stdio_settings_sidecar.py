"""Unit tests for the stdio settings UI sidecar (issue #863).

Covers the parent-side spawn gates (env var, sentinel, pid liveness)
and the read-side helper that ``ha_get_overview`` uses. The full
end-to-end spawn-and-serve flow is exercised in the integration suite
because the subprocess + Starlette stack doesn't unit-test cleanly.
"""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ha_mcp import stdio_settings_sidecar as sidecar
from ha_mcp.settings_ui import (
    build_settings_handlers,
    dump_tool_metadata_cache,
    load_tool_metadata_cache,
)


@pytest.fixture
def tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path]:
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
            "get_feature_flags",
            "save_feature_flags",
            # Auto-backup handlers (#1288).
            "list_backups",
            "view_backup",
            "diff_backup",
            "restore_backup",
            "delete_backup",
            "delete_backups_bulk",
            "get_backup_config",
            "save_backup_config",
            # Tool security policies handlers (#966).
            "policy_get_config",
            "policy_put_config",
            "policy_get_pending",
            "policy_post_approve",
            "policy_post_deny",
            "policy_get_tool_schema",
            "policy_get_value_source",
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

        app = Starlette(routes=[Route("/api/settings/tools", handlers["get_tools"])])
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
        # "Alive" now means BOTH the recorded PID is live AND the URL
        # file is on disk — see ``_existing_sidecar_alive`` docstring
        # for the stale-PID / crashed-mid-startup self-heal rationale.
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        (tmp_data_dir / "ui.pid").write_text(f"{os.getpid()}\n")
        (tmp_data_dir / "ui.url").write_text(
            "http://127.0.0.1:9999/private_xx/settings\n"
        )
        with patch("subprocess.Popen") as popen:
            sidecar.maybe_spawn()
        popen.assert_not_called()

    def test_maybe_spawn_respawns_when_pid_alive_but_url_missing(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        """Stale PID without a URL file → respawn + warning log.

        Failure modes this guards: (a) PID reuse after a crash that
        didn't clean up ``ui.pid`` and the OS reassigned the PID to
        an unrelated process; (b) sidecar that wrote pid then crashed
        before writing url (port-bind race, see ``_pick_free_port``).
        Without this self-heal, ``_pid_alive(pid)`` returns True and
        every future ``maybe_spawn()`` silently skips spawning a real
        sidecar — user permanently has no UI until they manually
        ``rm ui.pid``.
        """
        import logging

        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        (tmp_data_dir / "ui.pid").write_text(f"{os.getpid()}\n")
        # ui.url deliberately not written.
        fake_proc = MagicMock()
        fake_proc.pid = 99999
        with (
            caplog.at_level(logging.WARNING, logger="ha_mcp.stdio_settings_sidecar"),
            patch("subprocess.Popen", return_value=fake_proc) as popen,
        ):
            sidecar.maybe_spawn()
        popen.assert_called_once()
        assert any(
            "treating as stale and respawning" in rec.message for rec in caplog.records
        ), f"expected stale-sidecar warning, got: {[r.message for r in caplog.records]}"

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
        # is_sidecar=True because _build_app passes is_sidecar=True to
        # build_settings_handlers; covered separately in TestSidecarSettingsInfo.
        # The endpoint also carries ``instance_id`` + ``started_at`` so the
        # restart-then-reload JS cycle can prove a restart actually happened
        # (covered by TestSettingsInfoEndpoint in test_settings_ui.py).
        # This test pins the deployment-mode fields only.
        body = resp.json()
        assert body["is_addon"] is False
        assert body["is_sidecar"] is True

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

    def test_shutdown_rolls_back_sentinel_when_stop_raises(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If ``stop()`` raises *after* the sentinel was written, the
        sentinel must be rolled back. Otherwise the user sees the worst
        possible state: current session keeps running (stop failed),
        next launch refuses to spawn (sentinel on disk), and they have
        no clue why.
        """

        def boom() -> None:
            raise RuntimeError("uvicorn server.should_exit assignment failed")

        app = sidecar._build_app(
            host="127.0.0.1", port=12345, secret_path="/private_xx"
        )
        app.state.shutdown_state["stop"] = boom
        client = TestClient(app)
        resp = client.post(
            "/private_xx/api/settings/shutdown",
            headers={
                "host": "127.0.0.1:12345",
                "origin": "http://127.0.0.1:12345",
            },
        )
        assert resp.status_code == 500
        body = resp.json()
        assert body["success"] is False
        assert "rolled back" in body["error"]["message"].lower()
        # Sentinel MUST be gone so the next maybe_spawn still spawns.
        assert not (tmp_data_dir / "settings_ui_disabled").exists()


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
        # Per-process identity fields (``instance_id`` / ``started_at``)
        # also land in this response — verified in
        # TestSettingsInfoEndpoint (test_settings_ui.py); this test pins
        # the deployment-mode override, not the full payload shape.
        body = resp.json()
        assert body["is_addon"] is False
        assert body["is_sidecar"] is True


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

        # Track whether uvicorn was touched — the disable check happens
        # BEFORE the ``import uvicorn`` line inside run_main, so a
        # honored sentinel must return cleanly without any uvicorn
        # attribute access. Use a tracking proxy rather than MagicMock's
        # restricted __getattr__ override (Python forbids setting magic
        # methods on a MagicMock instance).
        class _TrackingProxy:
            touched = False

            def __getattr__(self, name: str) -> object:
                _TrackingProxy.touched = True
                raise AssertionError(
                    f"uvicorn.{name} accessed despite disable sentinel"
                )

        monkeypatch.setitem(__import__("sys").modules, "uvicorn", _TrackingProxy())

        rc = sidecar.run_main()
        assert rc == 0
        assert _TrackingProxy.touched is False
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


class TestSpawnLock:
    """``_spawn_lock`` serializes concurrent ``maybe_spawn()`` callers.

    The race Patch76 flagged: two parent stdio processes starting in
    rapid succession can both clear the ``_existing_sidecar_alive()``
    check and ``Popen`` a child; the loser's child then races on
    ``bind()`` and crashes into ``sidecar.log``. The lock makes the
    alive-check-plus-Popen window mutually exclusive across parents.
    """

    def test_second_holder_sees_lock_unacquired(self, tmp_data_dir: Path) -> None:
        """A second context-manager entry while the first is open MUST yield False."""
        with sidecar._spawn_lock() as first_acquired:
            assert first_acquired is True
            with sidecar._spawn_lock() as second_acquired:
                assert second_acquired is False, (
                    "Second concurrent _spawn_lock() must NOT acquire — "
                    "two parents would both pass the alive-check and Popen, "
                    "racing on bind()"
                )

    def test_lock_releases_on_context_exit(self, tmp_data_dir: Path) -> None:
        """After the first holder exits, the lock is acquirable again."""
        with sidecar._spawn_lock() as first:
            assert first is True
        with sidecar._spawn_lock() as second:
            assert second is True

    def test_maybe_spawn_held_lock_short_circuits(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If another parent already holds the spawn lock, maybe_spawn() MUST NOT Popen."""
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        with sidecar._spawn_lock() as held:
            assert held is True
            with patch("subprocess.Popen") as popen:
                sidecar.maybe_spawn()
            popen.assert_not_called()


class TestDiscoverabilityFlow:
    """Pin the end-to-end flow ha_get_overview's settings_url participates in.

    Unit tests elsewhere check that ``ha_get_overview`` includes the URL
    when ``read_sidecar_url()`` returns one — but they don't verify the
    URL actually responds at ``/settings``. If the producer (run_main's
    URL-file writer) and the consumer (the Starlette routes built by
    _build_app) drift on the secret-path format or the route suffix,
    Claude would hand users a dead link without any test failing. This
    suite ties producer + consumer together.
    """

    def test_url_from_overview_serves_settings_page(self, tmp_data_dir: Path) -> None:
        """The URL ``ha_get_overview`` surfaces MUST hit a real /settings page.

        Equivalent to: spawn the sidecar, read ui.url like overview does,
        fetch the page, get HTML back. Done in-process via TestClient
        so we don't actually bind a port.
        """
        # Use a secret_path shape identical to what run_main() emits.
        secret_token = "test_token_xyz"
        secret_path = f"/private_{secret_token}"
        port = 54321
        url = f"http://127.0.0.1:{port}{secret_path}/settings"
        (tmp_data_dir / "ui.url").write_text(url + "\n")

        # The consumer side: ha_get_overview reads via read_sidecar_url.
        # Confirm the producer's URL round-trips through the consumer
        # unchanged — otherwise Claude hands the user a truncated URL.
        surfaced = sidecar.read_sidecar_url()
        assert surfaced == url

        # The producer side: build the same Starlette app the sidecar
        # would build with that secret_path, request the surfaced URL,
        # and verify it returns the settings page (HTML).
        from urllib.parse import urlparse

        app = sidecar._build_app(host="127.0.0.1", port=port, secret_path=secret_path)
        client = TestClient(app)
        parsed = urlparse(surfaced)
        resp = client.get(
            parsed.path,
            headers={"host": f"127.0.0.1:{port}"},
        )
        assert resp.status_code == 200, (
            f"URL surfaced by ha_get_overview returns {resp.status_code} "
            "from the actual sidecar Starlette app — the producer "
            "(run_main URL writer) and consumer (Starlette routes) drift"
        )
        # Settings page is HTML; if it returns JSON something is wrong.
        ctype = resp.headers.get("content-type", "")
        assert ctype.startswith("text/html"), (
            f"Settings URL must serve text/html; got {ctype!r}"
        )
        assert "<html" in resp.text.lower() or "<!doctype" in resp.text.lower()

    def test_url_format_match_between_writer_and_route(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The URL run_main writes to disk must be exactly the path the app routes.

        Catches: run_main changing the secret-path prefix from
        ``/private_`` to anything else without updating _build_app, or
        the suffix changing from ``/settings`` to anything else on
        either side. We extract both via the same code paths the
        sidecar uses at runtime.
        """
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.setattr(sidecar, "_pick_free_port", lambda: 41234)

        captured: dict[str, object] = {}

        class FakeServer:
            def __init__(self, _config: object) -> None:
                self.should_exit = False

            def run(self) -> None:
                # Snapshot the URL the writer chose + the app's routes
                # at the same instant the listener "starts".
                captured["url"] = (tmp_data_dir / "ui.url").read_text().strip()

        fake_uvicorn = MagicMock()
        fake_uvicorn.Server = FakeServer
        fake_uvicorn.Config = MagicMock()
        monkeypatch.setitem(__import__("sys").modules, "uvicorn", fake_uvicorn)

        rc = sidecar.run_main()
        assert rc == 0
        url = str(captured["url"])

        from urllib.parse import urlparse

        parsed = urlparse(url)
        # Rebuild the app with the secret_path the writer chose and
        # confirm the parsed URL path resolves on it.
        secret_path = parsed.path[: -len("/settings")]
        assert secret_path.startswith("/private_"), (
            f"writer emitted unexpected path shape: {parsed.path!r}"
        )
        app = sidecar._build_app(
            host="127.0.0.1", port=parsed.port or 0, secret_path=secret_path
        )
        client = TestClient(app)
        resp = client.get(
            parsed.path,
            headers={"host": f"127.0.0.1:{parsed.port}"},
        )
        assert resp.status_code == 200


class TestFeatureFlagsEndpoint:
    """``/api/settings/features`` GET + POST surface (issue #863).

    Pins the env-locked / file-editable / default-editable matrix end
    to end through the sidecar handlers so a future refactor of the
    config layer cannot silently drop the lock semantics that the UI
    relies on to disable env-controlled rows.
    """

    @pytest.fixture(autouse=True)
    def _reset_settings(self) -> Generator[None]:
        from ha_mcp.config import _reset_global_settings

        _reset_global_settings()
        yield
        _reset_global_settings()

    def test_get_returns_all_flags_with_defaults(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clean environment → every flag reports origin=default, editable=True."""
        from ha_mcp.config import FEATURE_FLAG_FIELDS
        from ha_mcp.settings_ui import build_settings_handlers

        # Strip any pre-existing env vars so the "default" branch is reachable.
        for _, env_name, _ in FEATURE_FLAG_FIELDS:
            monkeypatch.delenv(env_name, raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        from ha_mcp.config import _reset_global_settings

        _reset_global_settings()

        handlers = build_settings_handlers(server=None)
        app = Starlette(
            routes=[
                Route(
                    "/api/settings/features",
                    handlers["get_feature_flags"],
                    methods=["GET"],
                ),
            ]
        )
        resp = TestClient(app).get("/api/settings/features")
        assert resp.status_code == 200
        data = resp.json()
        assert "flags" in data
        for field_name, env_name, _ftype in FEATURE_FLAG_FIELDS:
            entry = data["flags"][field_name]
            assert entry["origin"] == "default", (
                f"{field_name} unexpectedly origin={entry['origin']!r}"
            )
            assert entry["editable"] is True
            assert entry["env_var"] == env_name

    def test_get_marks_env_var_locked_fields(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit env var → origin=env, editable=False, value from env."""
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers

        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.setenv("ENABLE_TOOL_SEARCH", "true")
        _reset_global_settings()

        handlers = build_settings_handlers(server=None)
        app = Starlette(
            routes=[
                Route(
                    "/api/settings/features",
                    handlers["get_feature_flags"],
                    methods=["GET"],
                ),
            ]
        )
        resp = TestClient(app).get("/api/settings/features")
        body = resp.json()
        assert body["flags"]["enable_tool_search"] == {
            "value": True,
            "origin": "env",
            "editable": False,
            "type": "bool",
            "env_var": "ENABLE_TOOL_SEARCH",
        }

    def test_post_writes_file_and_resets_singleton(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST a value → file persisted + Settings singleton invalidated."""
        from ha_mcp.config import (
            _FEATURE_FLAG_OVERRIDE_FILENAME,
            FEATURE_FLAG_FIELDS,
            _reset_global_settings,
            get_global_settings,
        )
        from ha_mcp.settings_ui import build_settings_handlers

        for _, env_name, _ in FEATURE_FLAG_FIELDS:
            monkeypatch.delenv(env_name, raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        _reset_global_settings()

        handlers = build_settings_handlers(server=None)
        app = Starlette(
            routes=[
                Route(
                    "/api/settings/features",
                    handlers["save_feature_flags"],
                    methods=["POST"],
                ),
            ]
        )
        client = TestClient(app)
        resp = client.post(
            "/api/settings/features",
            json={"flags": {"enable_tool_search": True, "tool_search_max_results": 7}},
        )
        assert resp.status_code == 200
        override = json.loads(
            (tmp_data_dir / _FEATURE_FLAG_OVERRIDE_FILENAME).read_text()
        )
        assert override == {"enable_tool_search": True, "tool_search_max_results": 7}
        settings = get_global_settings()
        assert settings.enable_tool_search is True
        assert settings.tool_search_max_results == 7

    def test_post_refuses_env_locked_fields(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env-locked field → 400 with the env var name in the message."""
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers

        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.setenv("ENABLE_TOOL_SEARCH", "false")
        _reset_global_settings()

        handlers = build_settings_handlers(server=None)
        app = Starlette(
            routes=[
                Route(
                    "/api/settings/features",
                    handlers["save_feature_flags"],
                    methods=["POST"],
                ),
            ]
        )
        resp = TestClient(app).post(
            "/api/settings/features",
            json={"flags": {"enable_tool_search": True}},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert "ENABLE_TOOL_SEARCH" in body["error"]["message"]

    def test_post_rejects_out_of_range_int(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Numeric field outside the pydantic bound → 400, not silent clamp."""
        from ha_mcp.config import FEATURE_FLAG_FIELDS, _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers

        for _, env_name, _ in FEATURE_FLAG_FIELDS:
            monkeypatch.delenv(env_name, raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        _reset_global_settings()

        handlers = build_settings_handlers(server=None)
        app = Starlette(
            routes=[
                Route(
                    "/api/settings/features",
                    handlers["save_feature_flags"],
                    methods=["POST"],
                ),
            ]
        )
        resp = TestClient(app).post(
            "/api/settings/features",
            json={"flags": {"tool_search_max_results": 999}},
        )
        assert resp.status_code == 400

    def _build_post_app(self, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        """Return a TestClient hitting only the POST features endpoint
        with all FEATURE_FLAG_FIELDS env vars cleared so origin defaults
        to ``file``/``default`` and writes are accepted.
        """
        from ha_mcp.config import FEATURE_FLAG_FIELDS, _reset_global_settings
        from ha_mcp.settings_ui import build_settings_handlers

        for _, env_name, _ in FEATURE_FLAG_FIELDS:
            monkeypatch.delenv(env_name, raising=False)
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        _reset_global_settings()
        handlers = build_settings_handlers(server=None)
        app = Starlette(
            routes=[
                Route(
                    "/api/settings/features",
                    handlers["save_feature_flags"],
                    methods=["POST"],
                ),
            ]
        )
        return TestClient(app)

    def test_post_rejects_non_json_body(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._build_post_app(monkeypatch)
        resp = client.post(
            "/api/settings/features",
            content=b"not json {{{",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400
        assert "Invalid JSON" in resp.json()["error"]["message"]

    def test_post_rejects_body_not_object(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JSON-valid but non-dict body must 400, not 500.

        Mirrors the ``_save_tools`` validation discipline so an LLM
        that wraps the payload as a list (``[{"enable_tool_search":
        true}]``) gets a clear error instead of a stack trace.
        """
        client = self._build_post_app(monkeypatch)
        for payload in ([1, 2, 3], "hello", 42, None):
            resp = client.post("/api/settings/features", json=payload)
            assert resp.status_code == 400, (
                f"payload {payload!r} should 400, got {resp.status_code}"
            )

    def test_post_rejects_flags_not_object(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._build_post_app(monkeypatch)
        resp = client.post(
            "/api/settings/features",
            json={"flags": ["enable_tool_search"]},
        )
        assert resp.status_code == 400
        assert "object" in resp.json()["error"]["message"].lower()

    def test_post_rejects_unknown_field(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown field name → 400 with the field in the message.

        Without this guard a misspelled field would be silently
        dropped and the UI would say "Saved" while persisting
        nothing — same failure mode the ``_save_tools`` typo guard
        addresses.
        """
        client = self._build_post_app(monkeypatch)
        resp = client.post(
            "/api/settings/features",
            json={"flags": {"nonexistent_flag": True}},
        )
        assert resp.status_code == 400
        assert "nonexistent_flag" in resp.json()["error"]["message"]

    def test_post_rejects_string_for_bool(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``"true"`` for a bool field must 400.

        Python's truthiness would silently accept the non-empty
        string ``"false"`` as True — exactly the foot-gun the
        explicit ``isinstance(raw, bool)`` check on the handler
        prevents.
        """
        client = self._build_post_app(monkeypatch)
        resp = client.post(
            "/api/settings/features",
            json={"flags": {"enable_tool_search": "true"}},
        )
        assert resp.status_code == 400

    def test_post_rejects_bool_for_int(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``True`` for an int field must 400 — bool subclasses int
        in Python, so without the explicit ``isinstance(raw, bool)``
        guard the handler would happily coerce ``True`` to ``1``.
        """
        client = self._build_post_app(monkeypatch)
        resp = client.post(
            "/api/settings/features",
            json={"flags": {"tool_search_max_results": True}},
        )
        assert resp.status_code == 400

    def test_post_refuses_to_overwrite_corrupt_existing_file(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Existing override file is corrupt JSON → 409, file untouched.

        Pre-fix behavior dropped to ``existing = {}`` then overwrote,
        silently erasing every prior toggle the user had set.
        """
        from ha_mcp.config import _FEATURE_FLAG_OVERRIDE_FILENAME

        client = self._build_post_app(monkeypatch)
        corrupt = tmp_data_dir / _FEATURE_FLAG_OVERRIDE_FILENAME
        corrupt.write_text("{not json")
        resp = client.post(
            "/api/settings/features",
            json={"flags": {"enable_tool_search": True}},
        )
        assert resp.status_code == 409
        # File on disk must be unchanged — we refused to overwrite.
        assert corrupt.read_text() == "{not json"

    def test_post_atomic_write_no_partial_file_left_behind(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Successful POST → no ``feature_flags.json.tmp`` leftover.

        The handler writes to a sibling .tmp then ``os.replace``s
        into place; the .tmp must not survive a successful write.
        """
        from ha_mcp.config import _FEATURE_FLAG_OVERRIDE_FILENAME

        client = self._build_post_app(monkeypatch)
        resp = client.post(
            "/api/settings/features",
            json={"flags": {"enable_tool_search": True}},
        )
        assert resp.status_code == 200
        final = tmp_data_dir / _FEATURE_FLAG_OVERRIDE_FILENAME
        leftover = final.with_suffix(final.suffix + ".tmp")
        assert final.exists()
        assert not leftover.exists()


class TestFeatureFlagAddonMode:
    """Addon-mode short-circuit on ``get_feature_flag_origin`` /
    ``_apply_feature_flag_overrides``. When ``SUPERVISOR_TOKEN`` is set
    the override file is intentionally ignored — addon ``start.py``
    is the only path that writes env vars in addon mode, and any
    override file present is a stale leftover that MUST NOT shadow
    addon config. Without these tests, a regression dropping the
    ``SUPERVISOR_TOKEN`` check would silently let
    ``~/.ha-mcp/feature_flags.json`` override what the addon's
    Configuration tab says.
    """

    @pytest.fixture(autouse=True)
    def _reset_settings(self) -> Generator[None]:
        from ha_mcp.config import _reset_global_settings

        _reset_global_settings()
        yield
        _reset_global_settings()

    def test_origin_returns_addon_when_supervisor_token_set(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ha_mcp.config import (
            _FEATURE_FLAG_OVERRIDE_FILENAME,
            get_feature_flag_origin,
        )

        # File present + env var set — neither path should win over addon.
        (tmp_data_dir / _FEATURE_FLAG_OVERRIDE_FILENAME).write_text(
            json.dumps({"enable_tool_search": True})
        )
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-supervisor-token")
        monkeypatch.setenv("ENABLE_TOOL_SEARCH", "true")
        assert get_feature_flag_origin("ENABLE_TOOL_SEARCH") == "addon"

    def test_apply_overrides_skipped_when_supervisor_token_set(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Override file present + addon mode → file value IGNORED.

        Asserts that the cached Settings reflects the *env-var* /
        default value, not the file value, even though the file
        was on disk.
        """
        from ha_mcp.config import (
            _FEATURE_FLAG_OVERRIDE_FILENAME,
            get_global_settings,
        )

        (tmp_data_dir / _FEATURE_FLAG_OVERRIDE_FILENAME).write_text(
            json.dumps(
                {
                    "enable_tool_search": True,
                    "tool_search_max_results": 9,
                }
            )
        )
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-supervisor-token")
        # No env var → pydantic default should remain, NOT the file value.
        monkeypatch.delenv("ENABLE_TOOL_SEARCH", raising=False)
        monkeypatch.delenv("TOOL_SEARCH_MAX_RESULTS", raising=False)

        settings = get_global_settings()
        assert settings.enable_tool_search is False  # default, not file's True
        assert settings.tool_search_max_results == 5  # default, not file's 9


class TestFeatureFlagOverrideReadErrors:
    """``_read_feature_flag_override_file`` must log loudly when the
    file exists-but-can't-be-read or exists-but-isn't-valid-JSON, so
    a user whose toggles silently stop taking effect has a log line
    to grep for. Silent fall-through on a missing file is fine.
    """

    @pytest.fixture(autouse=True)
    def _reset_settings(self) -> Generator[None]:
        from ha_mcp.config import _reset_global_settings

        _reset_global_settings()
        yield
        _reset_global_settings()

    def test_missing_file_does_not_log(self, tmp_data_dir: Path, caplog) -> None:
        import logging as _l

        from ha_mcp.config import _read_feature_flag_override_file

        with caplog.at_level(_l.WARNING, logger="ha_mcp.config"):
            result = _read_feature_flag_override_file()
        assert result == {}
        assert not caplog.records, (
            f"missing file should be silent, got: {[r.message for r in caplog.records]}"
        )

    def test_corrupt_json_logs_warning(self, tmp_data_dir: Path, caplog) -> None:
        import logging as _l

        from ha_mcp.config import (
            _FEATURE_FLAG_OVERRIDE_FILENAME,
            _read_feature_flag_override_file,
        )

        (tmp_data_dir / _FEATURE_FLAG_OVERRIDE_FILENAME).write_text("{not valid")
        with caplog.at_level(_l.WARNING, logger="ha_mcp.config"):
            result = _read_feature_flag_override_file()
        assert result == {}
        assert any("not valid JSON" in r.message for r in caplog.records), [
            r.message for r in caplog.records
        ]

    def test_non_object_root_logs_warning(self, tmp_data_dir: Path, caplog) -> None:
        import logging as _l

        from ha_mcp.config import (
            _FEATURE_FLAG_OVERRIDE_FILENAME,
            _read_feature_flag_override_file,
        )

        (tmp_data_dir / _FEATURE_FLAG_OVERRIDE_FILENAME).write_text("[1,2,3]")
        with caplog.at_level(_l.WARNING, logger="ha_mcp.config"):
            result = _read_feature_flag_override_file()
        assert result == {}
        assert any("not a JSON object" in r.message for r in caplog.records), [
            r.message for r in caplog.records
        ]
