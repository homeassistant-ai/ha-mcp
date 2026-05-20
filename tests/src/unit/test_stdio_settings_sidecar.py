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
