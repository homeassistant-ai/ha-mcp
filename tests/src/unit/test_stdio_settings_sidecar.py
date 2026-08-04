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
from types import SimpleNamespace
from typing import Any
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
            # Theme / accessibility prefs (#1574 review).
            "get_theme_prefs",
            "save_theme_prefs",
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
            # Advanced settings handlers.
            "get_advanced_settings",
            "save_advanced_settings",
            # Custom filesystem directories (issue #1567).
            "get_fs_custom_paths",
            "save_fs_custom_paths",
            # Entity visibility filter (#1728).
            "visibility_get_config",
            "visibility_put_config",
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

    def test_env_var_unrecognized_disables(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fail closed on a mistyped value — parity with the HTTP transports'
        # _settings_ui_disabled.
        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", "disable")
        assert sidecar._is_disabled() is True

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
    """``_pid_alive`` correctness."""

    def test_pid_alive_for_self(self) -> None:
        assert sidecar._pid_alive(os.getpid()) is True

    def test_pid_alive_for_invalid(self) -> None:
        # PID 0 / negative are not valid process targets and must be
        # treated as "not alive" so a corrupt pidfile doesn't block
        # respawn forever.
        assert sidecar._pid_alive(0) is False
        assert sidecar._pid_alive(-1) is False


def _fake_opener(status: int = 200, side_effect: Exception | None = None) -> MagicMock:
    """Stand-in for ``_no_proxy_opener()``: canned status or raising open()."""
    opener = MagicMock()
    if side_effect is not None:
        opener.open.side_effect = side_effect
    else:
        opener.open.return_value.__enter__.return_value.status = status
    return opener


def _fake_listener(port: int) -> MagicMock:
    """Stand-in for the socket ``_bind_listener`` returns."""
    sock = MagicMock()
    sock.getsockname.return_value = ("127.0.0.1", port)
    return sock


class TestShutdownExistingSidecar:
    """``_shutdown_existing_sidecar`` retires the previous sidecar (issue #2131).

    Termination goes through the old sidecar's own ``/shutdown`` endpoint
    on its recorded secret-path URL — never ``os.kill``. Only the real
    sidecar answers on the secret path, so a reused PID can never get an
    innocent process terminated, and the endpoint has existed at the same
    path since the first sidecar release, so pre-fix orphans (the 57-day
    one in #2131) are retired too.
    """

    def test_posts_shutdown_to_secret_endpoint(self, tmp_data_dir: Path) -> None:
        (tmp_data_dir / "ui.url").write_text(
            "http://127.0.0.1:9999/private_xx/settings\n"
        )
        opener = _fake_opener()
        with patch.object(sidecar, "_no_proxy_opener", return_value=opener):
            sidecar._shutdown_existing_sidecar()
        opener.open.assert_called_once()
        req = opener.open.call_args[0][0]
        assert req.full_url == "http://127.0.0.1:9999/private_xx/api/settings/shutdown"
        assert req.get_method() == "POST"

    def test_opener_disables_environment_proxies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shutdown POST targets loopback and embeds the secret path —
        an HTTP_PROXY-honoring opener would leak the secret to the proxy
        and never reach the listener (urllib does not bypass 127.0.0.1
        unless no_proxy says so). The empty ProxyHandler suppresses the
        default env-reading one; with an empty dict it registers no
        protocol handlers at all, so the pin is "no proxy-carrying
        handler exists even with proxies in the environment"."""
        import urllib.request

        monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
        monkeypatch.setenv("http_proxy", "http://proxy.example:3128")
        assert not any(
            isinstance(h, urllib.request.ProxyHandler) and h.proxies
            for h in sidecar._no_proxy_opener().handlers
        ), "opener must not carry an env-configured ProxyHandler"

    def test_no_url_file_is_noop(self, tmp_data_dir: Path) -> None:
        opener = _fake_opener()
        with patch.object(sidecar, "_no_proxy_opener", return_value=opener):
            sidecar._shutdown_existing_sidecar()
        opener.open.assert_not_called()

    def test_malformed_url_is_nonfatal(self, tmp_data_dir: Path) -> None:
        """A corrupt ui.url must not raise out of the replace flow.

        ``Request("garbage/...")`` raises ValueError before any I/O; if it
        escaped, maybe_spawn() would abort before _do_spawn() and every
        later startup would repeat the failure with the stale files intact.
        """
        (tmp_data_dir / "ui.url").write_text("garbage\n")
        (tmp_data_dir / "ui.pid").write_text("4242\n")
        with patch.object(sidecar, "_pid_alive") as alive:
            sidecar._shutdown_existing_sidecar()
        alive.assert_not_called()

    def test_seeds_state_from_legacy_url(self, tmp_data_dir: Path) -> None:
        """Upgrading from a pre-ui.state release keeps the existing URL.

        Legacy installs carry port+secret only in ui.url, which the
        replace flow deletes — without seeding, the first upgraded
        startup would mint a fresh URL and break every bookmark. Seeding
        must happen even when the old sidecar is already dead.
        """
        (tmp_data_dir / "ui.url").write_text(
            "http://127.0.0.1:54321/private_legacy/settings\n"
        )
        import urllib.error

        opener = _fake_opener(side_effect=urllib.error.URLError("refused"))
        with patch.object(sidecar, "_no_proxy_opener", return_value=opener):
            sidecar._shutdown_existing_sidecar()
        state = json.loads((tmp_data_dir / "ui.state").read_text())
        assert state == {"port": 54321, "secret_path": "/private_legacy"}

    def test_seed_does_not_overwrite_existing_state(self, tmp_data_dir: Path) -> None:
        (tmp_data_dir / "ui.state").write_text(
            json.dumps({"port": 44444, "secret_path": "/private_state"})
        )
        (tmp_data_dir / "ui.url").write_text(
            "http://127.0.0.1:54321/private_other/settings\n"
        )
        opener = _fake_opener()
        with patch.object(sidecar, "_no_proxy_opener", return_value=opener):
            sidecar._shutdown_existing_sidecar()
        state = json.loads((tmp_data_dir / "ui.state").read_text())
        assert state == {"port": 44444, "secret_path": "/private_state"}

    def test_removes_sentinel_written_by_shutdown_endpoint(
        self, tmp_data_dir: Path
    ) -> None:
        """/shutdown drops the disable sentinel; a replace must clear it.

        Otherwise every replacement would permanently disable the
        settings UI — the freshly spawned child honors the sentinel and
        exits immediately.
        """
        (tmp_data_dir / "ui.url").write_text(
            "http://127.0.0.1:9999/private_xx/settings\n"
        )
        sentinel = tmp_data_dir / "settings_ui_disabled"

        def _fake_open(_req: object, timeout: float = 0) -> MagicMock:
            # The live endpoint writes the sentinel before signalling exit.
            sentinel.write_text("Disabled via /shutdown endpoint at pid 999\n")
            cm = MagicMock()
            cm.__enter__.return_value.status = 200
            return cm

        opener = MagicMock()
        opener.open.side_effect = _fake_open
        with patch.object(sidecar, "_no_proxy_opener", return_value=opener):
            sidecar._shutdown_existing_sidecar()
        assert not sentinel.exists(), (
            "replace flow must remove the disable sentinel the endpoint wrote"
        )

    def test_waits_for_recorded_pid_to_exit(self, tmp_data_dir: Path) -> None:
        """After a successful POST, poll the old pid until it exits.

        Two reasons the wait matters: the dying process holds the port
        the replacement wants to rebind (the sticky URL depends on it
        freeing in time), and sidecars from releases before the
        ownership-guarded cleanup unlink ui.pid/ui.url blindly on exit —
        during migration a still-dying legacy process could delete the
        replacement's freshly written files.
        """
        (tmp_data_dir / "ui.url").write_text(
            "http://127.0.0.1:9999/private_xx/settings\n"
        )
        (tmp_data_dir / "ui.pid").write_text("4242\n")
        liveness = iter([True, True, False])
        with (
            patch.object(sidecar, "_no_proxy_opener", return_value=_fake_opener()),
            patch.object(
                sidecar, "_pid_alive", side_effect=lambda _pid: next(liveness)
            ) as alive,
            patch("time.sleep"),
        ):
            sidecar._shutdown_existing_sidecar()
        assert alive.call_count == 3
        assert alive.call_args[0][0] == 4242

    def test_exit_wait_times_out_with_warning(
        self, tmp_data_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A pid that never exits must not stall startup past the bound."""
        import logging

        (tmp_data_dir / "ui.url").write_text(
            "http://127.0.0.1:9999/private_xx/settings\n"
        )
        (tmp_data_dir / "ui.pid").write_text("4242\n")
        # First monotonic() call sets the deadline; the next one exceeds it.
        clock = iter([1000.0, 1000.0 + sidecar._OLD_SIDECAR_EXIT_WAIT + 1])
        with (
            caplog.at_level(logging.WARNING, logger="ha_mcp.stdio_settings_sidecar"),
            patch.object(sidecar, "_no_proxy_opener", return_value=_fake_opener()),
            patch.object(sidecar, "_pid_alive", return_value=True),
            patch("time.monotonic", side_effect=lambda: next(clock)),
            patch("time.sleep"),
        ):
            sidecar._shutdown_existing_sidecar()
        assert any(
            "still alive" in rec.message
            and "spawning replacement anyway" in rec.message
            for rec in caplog.records
        ), f"expected timeout warning, got: {[r.message for r in caplog.records]}"

    def test_preexisting_sentinel_survives_the_replace(
        self, tmp_data_dir: Path
    ) -> None:
        """Only a sentinel the retire POST itself caused may be removed.

        A user can click the page's Stop button in the window between the
        parent's _is_disabled() check and its POST; the endpoint then
        writes the sentinel for THEM. Snapshotting existence before the
        POST distinguishes "the sentinel I just caused" from "the
        sentinel the user caused" — the latter must survive, or the UI
        the user just turned off silently resurrects.
        """
        (tmp_data_dir / "ui.url").write_text(
            "http://127.0.0.1:9999/private_xx/settings\n"
        )
        sentinel = tmp_data_dir / "settings_ui_disabled"
        sentinel.write_text("user clicked stop moments before the POST\n")
        with patch.object(sidecar, "_no_proxy_opener", return_value=_fake_opener()):
            sidecar._shutdown_existing_sidecar()
        assert sentinel.exists(), "a user-caused sentinel must not be unlinked"

    def test_refused_shutdown_logs_truthfully_and_keeps_sentinel_alone(
        self, tmp_data_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A 500 from /shutdown means "alive and NOT shutting down".

        urllib raises HTTPError for non-2xx (it is a URLError subclass),
        so a plain URLError arm mislabels an explicit refusal as "no
        responsive sidecar" — the opposite of the truth — and skips
        nothing it should skip. The refusal paths (sentinel write failed
        / stop() raised) leave no sentinel to clean and a process that
        will not exit, so: truthful warning, no sentinel unlink, no exit
        wait.
        """
        import logging
        import urllib.error

        (tmp_data_dir / "ui.url").write_text(
            "http://127.0.0.1:9999/private_xx/settings\n"
        )
        (tmp_data_dir / "ui.pid").write_text("4242\n")
        sentinel = tmp_data_dir / "settings_ui_disabled"
        sentinel.write_text("user clicked stop just now\n")
        refusal = urllib.error.HTTPError(
            "http://127.0.0.1:9999/private_xx/api/settings/shutdown",
            500,
            "Internal Server Error",
            None,  # type: ignore[arg-type]
            None,
        )
        with (
            caplog.at_level(logging.WARNING, logger="ha_mcp.stdio_settings_sidecar"),
            patch.object(
                sidecar,
                "_no_proxy_opener",
                return_value=_fake_opener(side_effect=refusal),
            ),
            patch.object(sidecar, "_pid_alive") as alive,
        ):
            sidecar._shutdown_existing_sidecar()
        alive.assert_not_called()
        assert sentinel.exists(), "refusal path must not unlink the sentinel"
        assert any(
            "refused shutdown" in rec.message and "500" in str(rec.args)
            for rec in caplog.records
        ), f"expected refusal warning, got: {[r.message for r in caplog.records]}"

    def test_timed_out_shutdown_warns_it_may_still_be_running(
        self, tmp_data_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A slow-but-alive sidecar is not "no responsive sidecar".

        socket timeouts are OSError subclasses; folding them into the
        dead-listener arm tells the operator nothing was there when a
        live process simply didn't answer in time.
        """
        import logging

        (tmp_data_dir / "ui.url").write_text(
            "http://127.0.0.1:9999/private_xx/settings\n"
        )
        with (
            caplog.at_level(logging.WARNING, logger="ha_mcp.stdio_settings_sidecar"),
            patch.object(
                sidecar,
                "_no_proxy_opener",
                return_value=_fake_opener(side_effect=TimeoutError("timed out")),
            ),
            patch.object(sidecar, "_pid_alive") as alive,
        ):
            sidecar._shutdown_existing_sidecar()
        alive.assert_not_called()
        assert any(
            "did not answer" in rec.message and "may still be running" in rec.message
            for rec in caplog.records
        ), f"expected timeout warning, got: {[r.message for r in caplog.records]}"

    def test_post_failure_is_nonfatal_and_skips_wait(self, tmp_data_dir: Path) -> None:
        """Dead listener → nothing to retire; don't raise, don't poll the pid.

        The recorded pid is NOT verified to be the sidecar (PID reuse),
        so with no listener on the secret path there is nothing safe to
        wait on — proceed straight to spawning the replacement.
        """
        import urllib.error

        (tmp_data_dir / "ui.url").write_text(
            "http://127.0.0.1:9999/private_xx/settings\n"
        )
        (tmp_data_dir / "ui.pid").write_text(f"{os.getpid()}\n")
        with (
            patch.object(
                sidecar,
                "_no_proxy_opener",
                return_value=_fake_opener(
                    side_effect=urllib.error.URLError("connection refused")
                ),
            ),
            patch.object(sidecar, "_pid_alive") as alive,
        ):
            sidecar._shutdown_existing_sidecar()
        alive.assert_not_called()

    def test_garbage_pid_file_skips_wait(self, tmp_data_dir: Path) -> None:
        (tmp_data_dir / "ui.url").write_text(
            "http://127.0.0.1:9999/private_xx/settings\n"
        )
        (tmp_data_dir / "ui.pid").write_text("not a number\n")
        with (
            patch.object(sidecar, "_no_proxy_opener", return_value=_fake_opener()),
            patch.object(sidecar, "_pid_alive") as alive,
        ):
            sidecar._shutdown_existing_sidecar()
        alive.assert_not_called()


class TestSidecarStateValidation:
    """``_load_sidecar_state`` rejects everything it cannot safely serve.

    The secret is interpolated into route paths and the port is bound
    verbatim, so any invalid shape must yield None (fresh values) — and,
    when a file was actually present, a warning: a silently regenerated
    URL looks like the bug this state file exists to prevent.
    """

    def test_valid_state_round_trips(self, tmp_data_dir: Path) -> None:
        (tmp_data_dir / "ui.state").write_text(
            json.dumps({"port": 54321, "secret_path": "/private_ok"})
        )
        assert sidecar._load_sidecar_state() == (54321, "/private_ok")

    def test_absent_file_is_silent(
        self, tmp_data_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="ha_mcp.stdio_settings_sidecar"):
            assert sidecar._load_sidecar_state() is None
        assert not caplog.records, "a normal first spawn must not warn"

    @pytest.mark.parametrize(
        "payload",
        [
            "not json {{{",
            json.dumps(["port", 54321]),  # non-dict root
            json.dumps({"port": "54321", "secret_path": "/private_x"}),  # str port
            json.dumps({"port": True, "secret_path": "/private_x"}),  # bool port
            json.dumps({"port": 0, "secret_path": "/private_x"}),  # out of range
            json.dumps({"port": 1023, "secret_path": "/private_x"}),  # below floor
            json.dumps({"port": 99999, "secret_path": "/private_x"}),  # above range
            json.dumps({"port": 54321, "secret_path": ""}),  # empty secret
            json.dumps({"port": 54321, "secret_path": "/"}),  # bare slash
            json.dumps({"port": 54321, "secret_path": "/private_a/../b"}),  # traversal
            json.dumps({"port": 54321}),  # missing secret
        ],
    )
    def test_invalid_state_yields_none_and_warns(
        self, tmp_data_dir: Path, caplog: pytest.LogCaptureFixture, payload: str
    ) -> None:
        import logging

        (tmp_data_dir / "ui.state").write_text(payload)
        with caplog.at_level(logging.WARNING, logger="ha_mcp.stdio_settings_sidecar"):
            assert sidecar._load_sidecar_state() is None
        assert any("ui.state" in rec.message for rec in caplog.records), (
            f"present-but-invalid state must warn; got {[r.message for r in caplog.records]}"
        )

    def test_floor_port_1024_is_accepted(self, tmp_data_dir: Path) -> None:
        (tmp_data_dir / "ui.state").write_text(
            json.dumps({"port": 1024, "secret_path": "/private_ok"})
        )
        assert sidecar._load_sidecar_state() == (1024, "/private_ok")

    def test_save_failure_is_nonfatal(self, tmp_data_dir: Path) -> None:
        with patch.object(
            sidecar, "_atomic_write_0600", side_effect=OSError("read-only fs")
        ):
            sidecar._save_sidecar_state(54321, "/private_x")  # must not raise


class TestSeedStateFromUrl:
    """Legacy-migration parser: only a well-formed loopback URL seeds state."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:0/private_x/settings",  # port 0
            "http://127.0.0.1:99999/private_x/settings",  # out of range
            "http://127.0.0.1:1023/private_x/settings",  # below floor
            "https://127.0.0.1:54321/private_x/settings",  # wrong scheme
            "http://192.168.1.10:54321/private_x/settings",  # not loopback
            "http://127.0.0.1:54321/private_x",  # missing /settings
            "garbage",
        ],
    )
    def test_invalid_urls_seed_nothing(self, tmp_data_dir: Path, url: str) -> None:
        sidecar._seed_state_from_url(url)
        assert not (tmp_data_dir / "ui.state").exists()

    def test_valid_legacy_url_seeds_state(self, tmp_data_dir: Path) -> None:
        sidecar._seed_state_from_url("http://127.0.0.1:54321/private_ok/settings")
        state = json.loads((tmp_data_dir / "ui.state").read_text())
        assert state == {"port": 54321, "secret_path": "/private_ok"}


class TestMaybeSpawnGates:
    """``maybe_spawn`` short-circuits in the documented cases."""

    def test_maybe_spawn_skips_when_disabled(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disabled means fully inert: no retire POST, no spawn.

        The disabled check must run BEFORE the retire flow — a reorder
        would have a deliberately disabled install still POSTing
        /shutdown (and clearing the sentinel that records the disable).
        """
        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", "1")
        with (
            patch.object(sidecar, "_shutdown_existing_sidecar") as shutdown,
            patch("subprocess.Popen") as popen,
        ):
            sidecar.maybe_spawn()
        popen.assert_not_called()
        shutdown.assert_not_called()

    def test_maybe_spawn_replaces_live_sidecar(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live sidecar is retired and replaced, never reused (issue #2131).

        Reuse froze the settings UI at the code + environment of whatever
        parent spawned the sidecar first — a 57-day-old orphan in the
        reported case. Every stdio startup now hands out a sidecar that
        matches the running server.
        """
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        (tmp_data_dir / "ui.pid").write_text(f"{os.getpid()}\n")
        (tmp_data_dir / "ui.url").write_text(
            "http://127.0.0.1:9999/private_xx/settings\n"
        )
        fake_proc = MagicMock()
        fake_proc.pid = 12345
        with (
            patch.object(sidecar, "_shutdown_existing_sidecar") as shutdown,
            patch("subprocess.Popen", return_value=fake_proc) as popen,
        ):
            sidecar.maybe_spawn()
        shutdown.assert_called_once()
        popen.assert_called_once()

    def test_maybe_spawn_respawns_when_pid_alive_but_url_missing(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Live PID without a URL file → nothing to retire, spawn fresh.

        Covers PID reuse after a crash that didn't clean up ``ui.pid``:
        with no recorded URL there is no listener to shut down (and no
        proof the PID is even a sidecar), so the replace flow must not
        touch the process — just spawn.
        """
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        (tmp_data_dir / "ui.pid").write_text(f"{os.getpid()}\n")
        # ui.url deliberately not written.
        fake_proc = MagicMock()
        fake_proc.pid = 99999
        opener = _fake_opener()
        with (
            patch.object(sidecar, "_no_proxy_opener", return_value=opener),
            patch("subprocess.Popen", return_value=fake_proc) as popen,
        ):
            sidecar.maybe_spawn()
        popen.assert_called_once()
        opener.open.assert_not_called()

    def test_do_spawn_holds_the_lock_until_the_child_publishes(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """maybe_spawn must not return before the child writes ui.url.

        The spawn lock is released when maybe_spawn returns; a second
        parent starting in the child's 1-3s import window would then see
        no ui.url, retire nothing, and spawn a second child against the
        same remembered port. Holding the lock until the URL is
        published makes the concurrent parent take the loser path
        instead.
        """
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        fake_proc = MagicMock()
        fake_proc.pid = 12345
        fake_proc.poll.return_value = None  # child still running
        url_file = tmp_data_dir / "ui.url"
        sleeps: list[float] = []

        def _sleep_then_publish(duration: float) -> None:
            sleeps.append(duration)
            if len(sleeps) == 2:
                url_file.write_text("http://127.0.0.1:54321/private_x/settings\n")

        with (
            patch("subprocess.Popen", return_value=fake_proc),
            patch("time.sleep", side_effect=_sleep_then_publish),
        ):
            sidecar.maybe_spawn()
        assert len(sleeps) == 2, (
            "maybe_spawn must poll until the child publishes its URL"
        )

    def test_lock_loser_reports_existing_url_to_stderr(
        self,
        tmp_data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The loser should tell the user where the settings UI lives."""
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        (tmp_data_dir / "ui.url").write_text(
            "http://127.0.0.1:9999/private_xx/settings\n"
        )
        with sidecar._spawn_lock() as held:
            assert held is True
            with patch("subprocess.Popen") as popen:
                sidecar.maybe_spawn()
            popen.assert_not_called()
        assert "http://127.0.0.1:9999/private_xx/settings" in capsys.readouterr().err

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


class TestMainSpawnPathDumpsCache:
    """``__main__._maybe_spawn_settings_sidecar`` cache-dump gating."""

    def test_dump_runs_even_when_sidecar_files_present(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live sidecar no longer short-circuits the metadata dump.

        The old fast path skipped the dump when a sidecar was alive
        (nothing to spawn for). With replace-on-startup the alive sidecar
        is about to be retired, and its successor must read a cache dumped
        by THIS parent — an old-version cache would resurface issue #2131
        as a stale Tools tab.
        """
        import ha_mcp.__main__ as main_module

        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        (tmp_data_dir / "ui.pid").write_text(f"{os.getpid()}\n")
        (tmp_data_dir / "ui.url").write_text(
            "http://127.0.0.1:9999/private_xx/settings\n"
        )

        async def _fake_metadata(_server: object) -> list[dict[str, str]]:
            return [{"name": "ha_get_state", "primary_tag": "Entity Operations"}]

        with (
            patch.object(main_module, "_get_server") as get_server,
            patch("ha_mcp.settings_ui._get_tool_metadata", new=_fake_metadata),
            patch.object(sidecar, "maybe_spawn") as spawn,
        ):
            main_module._maybe_spawn_settings_sidecar()
        get_server.assert_called_once()
        spawn.assert_called_once()
        assert load_tool_metadata_cache() == [
            {"name": "ha_get_state", "primary_tag": "Entity Operations"}
        ]

    def test_dump_skipped_when_disabled(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disabled sidecar → no server build, no dump; maybe_spawn still
        invoked for its skip-logging."""
        import ha_mcp.__main__ as main_module

        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", "1")
        with (
            patch.object(main_module, "_get_server") as get_server,
            patch.object(sidecar, "maybe_spawn") as spawn,
        ):
            main_module._maybe_spawn_settings_sidecar()
        get_server.assert_not_called()
        spawn.assert_called_once()


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
        monkeypatch.setattr(
            "ha_mcp.config.get_global_settings",
            lambda: SimpleNamespace(sidecar_pin_port=0),
        )
        listener = _fake_listener(54321)
        monkeypatch.setattr(
            sidecar, "_bind_listener", lambda preferred, source: listener
        )
        snapshot = self._install_fake_uvicorn(monkeypatch, tmp_data_dir)

        rc = sidecar.run_main()
        assert rc == 0
        # The pre-bound serving socket must be handed to uvicorn — a
        # host/port re-bind would reintroduce the probe-then-rebind race.
        assert snapshot["sockets"] == [listener]

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

    @staticmethod
    def _install_fake_uvicorn(
        monkeypatch: pytest.MonkeyPatch, tmp_data_dir: Path
    ) -> dict[str, str]:
        """Stub uvicorn.Server; capture ui.url/ui.pid at run() time.

        run_main's finally-block unlinks the files on exit, so asserting
        after return would always see them gone — the snapshot taken
        inside run() is the observable contract.
        """
        snapshot: dict[str, Any] = {}

        class FakeServer:
            def __init__(self, _config: object) -> None:
                self.should_exit = False

            def run(self, sockets: list[object] | None = None) -> None:
                snapshot["url"] = (tmp_data_dir / "ui.url").read_text().strip()
                snapshot["pid"] = (tmp_data_dir / "ui.pid").read_text().strip()
                snapshot["sockets"] = sockets

        fake_uvicorn = MagicMock()
        fake_uvicorn.Server = FakeServer
        fake_uvicorn.Config = MagicMock()
        monkeypatch.setitem(__import__("sys").modules, "uvicorn", fake_uvicorn)
        return snapshot

    def test_run_main_cleanup_spares_a_successors_files(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exit cleanup must only unlink files this process still owns.

        The retire wait is bounded: a slow-dying sidecar can still be
        exiting after its replacement wrote fresh ui.pid/ui.url. A blind
        unlink in the old process's finally-block would delete the NEW
        sidecar's discovery files and ha_get_overview would stop
        surfacing the URL. Simulated by swapping in a successor's files
        during run().
        """
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.setattr(
            "ha_mcp.config.get_global_settings",
            lambda: SimpleNamespace(sidecar_pin_port=0),
        )
        monkeypatch.setattr(
            sidecar, "_bind_listener", lambda preferred, source: _fake_listener(54321)
        )

        class FakeServer:
            def __init__(self, _config: object) -> None:
                self.should_exit = False

            def run(self, sockets: list[object] | None = None) -> None:
                # A replacement took over while this process was exiting.
                # With sticky ui.state the successor reuses the SAME port
                # and secret, so its ui.url content is byte-identical —
                # URL equality can never distinguish owners (#2134 review).
                same_url = (tmp_data_dir / "ui.url").read_text()
                (tmp_data_dir / "ui.pid").write_text("424242\n")
                (tmp_data_dir / "ui.url").write_text(same_url)
                self.successor_url = same_url

        fake_uvicorn = MagicMock()
        fake_uvicorn.Server = FakeServer
        fake_uvicorn.Config = MagicMock()
        monkeypatch.setitem(__import__("sys").modules, "uvicorn", fake_uvicorn)

        assert sidecar.run_main() == 0
        assert (tmp_data_dir / "ui.pid").read_text().strip() == "424242"
        assert (tmp_data_dir / "ui.url").exists(), (
            "exiting sidecar deleted its successor's identical ui.url"
        )

    def test_cleanup_guard_is_pid_based(self, tmp_data_dir: Path) -> None:
        """Ownership = ui.pid records this process; url content is moot.

        Two live processes can never share a pid, while with sticky state
        they always share the URL — the pid is the only valid token.
        """
        url = "http://127.0.0.1:54321/private_x/settings"
        # Successor's pid recorded → delete nothing, even with matching URL.
        (tmp_data_dir / "ui.pid").write_text("424242\n")
        (tmp_data_dir / "ui.url").write_text(url + "\n")
        sidecar._cleanup_owned_serving_files()
        assert (tmp_data_dir / "ui.pid").exists()
        assert (tmp_data_dir / "ui.url").exists()
        # Own pid recorded → delete both, regardless of url content.
        (tmp_data_dir / "ui.pid").write_text(f"{os.getpid()}\n")
        sidecar._cleanup_owned_serving_files()
        assert not (tmp_data_dir / "ui.pid").exists()
        assert not (tmp_data_dir / "ui.url").exists()
        # Missing pid file → nothing to own, nothing deleted.
        (tmp_data_dir / "ui.url").write_text(url + "\n")
        sidecar._cleanup_owned_serving_files()
        assert (tmp_data_dir / "ui.url").exists()

    def test_write_and_cleanup_share_the_files_lock(self, tmp_data_dir: Path) -> None:
        """Both critical sections run under the same inter-process lock,
        closing the write-between-check-and-unlink window."""
        entered: list[str] = []

        @__import__("contextlib").contextmanager
        def _tracking_lock() -> object:
            entered.append("locked")
            yield

        with patch.object(sidecar, "_serving_files_lock", _tracking_lock):
            sidecar._write_pid_url("http://127.0.0.1:54321/private_x/settings")
            sidecar._cleanup_owned_serving_files()
        assert len(entered) == 2

    def test_run_main_reuses_persisted_port_and_secret(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A prior spawn's port + secret are reused → the URL never changes.

        With replace-on-startup (issue #2131) the sidecar process no
        longer survives restarts, so URL stability must come from
        persisted state instead: the replacement rebinds the remembered
        port and serves the remembered secret path.
        """
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        # Stub the settings singleton: it reads the real data dir, so a
        # host-level pin (env or override file) would change ``requested``.
        monkeypatch.setattr(
            "ha_mcp.config.get_global_settings",
            lambda: SimpleNamespace(sidecar_pin_port=0),
        )
        (tmp_data_dir / "ui.state").write_text(
            json.dumps({"port": 54321, "secret_path": "/private_persisted"})
        )
        requested: list[int] = []

        def _fake_bind(preferred: int, source: str) -> MagicMock:
            requested.append(preferred)
            return _fake_listener(preferred or 49999)

        monkeypatch.setattr(sidecar, "_bind_listener", _fake_bind)
        snapshot = self._install_fake_uvicorn(monkeypatch, tmp_data_dir)

        assert sidecar.run_main() == 0
        assert requested == [54321], "remembered port must be requested for rebind"
        assert snapshot["url"] == "http://127.0.0.1:54321/private_persisted/settings"

    def test_run_main_persists_the_bound_port_not_the_requested_one(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ephemeral fallback must be what lands in ui.state.

        When the remembered port can't be bound, the sidecar serves on a
        fallback port; persisting the REQUESTED port instead would make
        ui.state disagree with the URL just served, and the settings URL
        would flip on every subsequent restart.
        """
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.setattr(
            "ha_mcp.config.get_global_settings",
            lambda: SimpleNamespace(sidecar_pin_port=0),
        )
        (tmp_data_dir / "ui.state").write_text(
            json.dumps({"port": 54321, "secret_path": "/private_persisted"})
        )
        monkeypatch.setattr(
            sidecar, "_bind_listener", lambda preferred, source: _fake_listener(49999)
        )
        snapshot = self._install_fake_uvicorn(monkeypatch, tmp_data_dir)

        assert sidecar.run_main() == 0
        assert snapshot["url"] == "http://127.0.0.1:49999/private_persisted/settings"
        state = json.loads((tmp_data_dir / "ui.state").read_text())
        assert state["port"] == 49999

    def test_run_main_bind_failure_exits_before_touching_files(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Total bind failure must not disturb a live sidecar's files.

        If even the ephemeral bind fails, this child can never serve —
        writing state/discovery files first (or cleaning existing ones
        on the way out) would corrupt or delete a LIVE predecessor's
        records for nothing.
        """
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.setattr(
            "ha_mcp.config.get_global_settings",
            lambda: SimpleNamespace(sidecar_pin_port=0),
        )
        (tmp_data_dir / "ui.pid").write_text("424242\n")
        (tmp_data_dir / "ui.url").write_text(
            "http://127.0.0.1:54321/private_live/settings\n"
        )
        (tmp_data_dir / "ui.state").write_text(
            json.dumps({"port": 54321, "secret_path": "/private_live"})
        )

        def _fail_bind(preferred: int, source: str) -> MagicMock:
            raise OSError("no ports available")

        monkeypatch.setattr(sidecar, "_bind_listener", _fail_bind)
        self._install_fake_uvicorn(monkeypatch, tmp_data_dir)

        assert sidecar.run_main() != 0
        assert (tmp_data_dir / "ui.pid").read_text().strip() == "424242"
        assert "private_live" in (tmp_data_dir / "ui.url").read_text()
        state = json.loads((tmp_data_dir / "ui.state").read_text())
        assert state["port"] == 54321

    def test_run_main_persists_state_for_next_spawn(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First run records port + secret; cleanup must NOT remove them.

        ui.url/ui.pid mean "a sidecar is serving right now" and die with
        the process; ui.state means "this install's stable URL" and must
        outlive it — it's what the NEXT spawn reads.
        """
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.setattr(
            "ha_mcp.config.get_global_settings",
            lambda: SimpleNamespace(sidecar_pin_port=0),
        )
        monkeypatch.setattr(
            sidecar, "_bind_listener", lambda preferred, source: _fake_listener(54321)
        )
        snapshot = self._install_fake_uvicorn(monkeypatch, tmp_data_dir)

        assert sidecar.run_main() == 0
        state = json.loads((tmp_data_dir / "ui.state").read_text())
        assert state["port"] == 54321
        secret = state["secret_path"]
        assert secret.startswith("/private_")
        assert snapshot["url"] == f"http://127.0.0.1:54321{secret}/settings"
        # Serving files cleaned up, state retained.
        assert not (tmp_data_dir / "ui.url").exists()
        assert not (tmp_data_dir / "ui.pid").exists()
        assert (tmp_data_dir / "ui.state").exists()

    def test_run_main_pin_setting_overrides_persisted_port(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit pin (#1587) wins over the remembered port; secret sticks."""
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        (tmp_data_dir / "ui.state").write_text(
            json.dumps({"port": 54321, "secret_path": "/private_persisted"})
        )
        monkeypatch.setattr(
            "ha_mcp.config.get_global_settings",
            lambda: SimpleNamespace(sidecar_pin_port=61234),
        )
        requested: list[int] = []

        def _fake_bind(preferred: int, source: str) -> MagicMock:
            requested.append(preferred)
            return _fake_listener(preferred)

        monkeypatch.setattr(sidecar, "_bind_listener", _fake_bind)
        snapshot = self._install_fake_uvicorn(monkeypatch, tmp_data_dir)

        assert sidecar.run_main() == 0
        assert requested == [61234]
        assert snapshot["url"] == "http://127.0.0.1:61234/private_persisted/settings"
        # State follows the actually bound port so the next spawn (with
        # the pin removed again) keeps the same origin.
        state = json.loads((tmp_data_dir / "ui.state").read_text())
        assert state["port"] == 61234

    def test_run_main_regenerates_on_garbage_state(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.setattr(
            "ha_mcp.config.get_global_settings",
            lambda: SimpleNamespace(sidecar_pin_port=0),
        )
        (tmp_data_dir / "ui.state").write_text("not json {{{")
        monkeypatch.setattr(
            sidecar, "_bind_listener", lambda preferred, source: _fake_listener(54321)
        )
        snapshot = self._install_fake_uvicorn(monkeypatch, tmp_data_dir)

        assert sidecar.run_main() == 0
        assert snapshot["url"].startswith("http://127.0.0.1:54321/private_")
        state = json.loads((tmp_data_dir / "ui.state").read_text())
        assert state["port"] == 54321
        assert state["secret_path"].startswith("/private_")

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
                # __getattr__ must raise AttributeError per the protocol;
                # the ``touched`` flag (asserted False below) is the real
                # detection mechanism if uvicorn is accessed at all.
                _TrackingProxy.touched = True
                raise AttributeError(
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

        # The stale URL has no live listener; stub the retire POST so the
        # test stays hermetic (no real socket connect).
        import urllib.error

        with (
            patch.object(
                sidecar,
                "_no_proxy_opener",
                return_value=_fake_opener(
                    side_effect=urllib.error.URLError("connection refused")
                ),
            ),
            patch("subprocess.Popen", side_effect=_record_state),
        ):
            sidecar.maybe_spawn()

        # Both stale files must have been unlinked before Popen ran;
        # if either is True the cleanup loop was reordered after spawn.
        assert snapshot.get("pid_exists") is False, (
            "stale ui.pid still present at Popen call — cleanup regressed"
        )
        assert snapshot.get("url_exists") is False, (
            "stale ui.url still present at Popen call — cleanup regressed"
        )

    def test_ui_state_survives_the_stale_cleanup(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ui.state must NOT join the stale-file unlink loop.

        It carries the stable URL to the next spawn — adding it to the
        cleanup (a plausible "clean stale state too" edit) would destroy
        URL stability, the PR's headline promise, with the suite green.
        """
        import urllib.error

        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        (tmp_data_dir / "ui.pid").write_text("999999\n")
        (tmp_data_dir / "ui.url").write_text("http://127.0.0.1:1/stale\n")
        state_file = tmp_data_dir / "ui.state"
        state_file.write_text(
            json.dumps({"port": 54321, "secret_path": "/private_keep"})
        )
        snapshot: dict[str, bool] = {}

        def _record_state(*_args: object, **_kwargs: object) -> MagicMock:
            snapshot["state_exists"] = state_file.exists()
            proc = MagicMock()
            proc.pid = 12345
            proc.poll.return_value = None
            return proc

        with (
            patch.object(
                sidecar,
                "_no_proxy_opener",
                return_value=_fake_opener(
                    side_effect=urllib.error.URLError("connection refused")
                ),
            ),
            patch("time.sleep"),
            patch("time.monotonic", side_effect=[0.0, 100.0, 200.0]),
            patch("subprocess.Popen", side_effect=_record_state),
        ):
            sidecar.maybe_spawn()

        assert snapshot.get("state_exists") is True
        assert state_file.exists()
        assert json.loads(state_file.read_text())["secret_path"] == "/private_keep"


class TestServingFilesLock:
    """``_serving_files_lock`` degrades to unlocked, loudly, never fatally."""

    def test_open_failure_degrades_to_unlocked(
        self,
        tmp_data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A data dir where the lock file can't be opened (read-only fs,
        flock-less network mount) must not take the sidecar down with it —
        the write/cleanup body still runs, with a warning trail."""
        import logging

        real_open = os.open

        def _fail_lock_open(path: object, *args: object, **kwargs: object) -> int:
            if str(path).endswith("files.lock"):
                raise OSError("read-only file system")
            return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "open", _fail_lock_open)
        ran = False
        with (
            caplog.at_level(logging.WARNING, logger="ha_mcp.stdio_settings_sidecar"),
            sidecar._serving_files_lock(),
        ):
            ran = True
        assert ran
        assert any("proceeding unlocked" in r.message for r in caplog.records)

    def test_acquire_failure_degrades_to_unlocked(
        self, tmp_data_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging
        import sys

        if sys.platform == "win32":
            import msvcrt

            lock_target, lock_name = msvcrt, "locking"
        else:
            import fcntl

            lock_target, lock_name = fcntl, "flock"
        ran = False
        with (
            caplog.at_level(logging.WARNING, logger="ha_mcp.stdio_settings_sidecar"),
            patch.object(lock_target, lock_name, side_effect=OSError("ENOLCK")),
            sidecar._serving_files_lock(),
        ):
            ran = True
        assert ran
        assert any("proceeding unlocked" in r.message for r in caplog.records)


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
    rapid succession can both run the retire-and-respawn window and
    ``Popen`` a child; the loser's child then races on ``bind()`` and
    crashes into ``sidecar.log``. The lock makes that window mutually
    exclusive across parents.
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
        """A lock loser must neither Popen NOR retire.

        The winner may have just spawned a fresh sidecar; a loser that
        still ran the retire POST would shut that brand-new sidecar
        down, leaving no sidecar at all.
        """
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        with sidecar._spawn_lock() as held:
            assert held is True
            with (
                patch.object(sidecar, "_shutdown_existing_sidecar") as shutdown,
                patch("subprocess.Popen") as popen,
            ):
                sidecar.maybe_spawn()
            popen.assert_not_called()
            shutdown.assert_not_called()


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

    def test_retire_post_reaches_the_real_shutdown_route(
        self, tmp_data_dir: Path
    ) -> None:
        """Producer/consumer tie for the retire path (#2134 review).

        The retire POST must survive the real route table AND the real
        SecurityMiddleware with exactly urllib's header shape: Host set
        to the loopback authority and NO Origin header (the CSRF check
        is opt-in for Origin-less clients — tightening it to require
        Origin would break every replace). A drift in the route path,
        the derivation arithmetic, or the middleware contract fails
        here first.
        """
        port = 54321
        secret_path = "/private_retire"
        url = f"http://127.0.0.1:{port}{secret_path}/settings"
        app = sidecar._build_app(host="127.0.0.1", port=port, secret_path=secret_path)
        client = TestClient(app)

        target = sidecar._shutdown_endpoint(url)
        prefix = f"http://127.0.0.1:{port}"
        assert target.startswith(prefix)
        resp = client.post(
            target.removeprefix(prefix),
            headers={"host": f"127.0.0.1:{port}"},
        )
        assert resp.status_code == 200
        assert resp.json().get("success") is True
        # The endpoint's contract the retire flow depends on: the disable
        # sentinel is written before it answers.
        assert (tmp_data_dir / "settings_ui_disabled").exists()

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
        monkeypatch.setattr(
            "ha_mcp.config.get_global_settings",
            lambda: SimpleNamespace(sidecar_pin_port=0),
        )
        monkeypatch.setattr(
            sidecar, "_bind_listener", lambda preferred, source: _fake_listener(41234)
        )

        captured: dict[str, object] = {}

        class FakeServer:
            def __init__(self, _config: object) -> None:
                self.should_exit = False

            def run(self, sockets: list[object] | None = None) -> None:
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


class TestVisibilityHandlers:
    """Entity visibility config GET/PUT handlers (#1728)."""

    @staticmethod
    def _client() -> TestClient:
        handlers = build_settings_handlers(server=None)
        app = Starlette(
            routes=[
                Route(
                    "/api/visibility/config",
                    handlers["visibility_get_config"],
                    methods=["GET"],
                ),
                Route(
                    "/api/visibility/config",
                    handlers["visibility_put_config"],
                    methods=["PUT"],
                ),
            ]
        )
        return TestClient(app)

    def test_get_returns_default_when_no_file(self, tmp_data_dir: Path) -> None:
        resp = self._client().get("/api/visibility/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is False
        assert body["version"] == 1

    def test_put_persists_and_bumps_version(self, tmp_data_dir: Path) -> None:
        client = self._client()
        resp = client.put(
            "/api/visibility/config",
            json={"version": 1, "enabled": True, "deny_entity_ids": ["light.x"]},
        )
        assert resp.status_code == 200
        assert resp.json()["version"] == 2
        got = client.get("/api/visibility/config").json()
        assert got["enabled"] is True
        assert got["deny_entity_ids"] == ["light.x"]
        assert got["version"] == 2

    def test_put_persists_enforce_flag(self, tmp_data_dir: Path) -> None:
        """The enforce flag (#2015) round-trips through the generic model."""
        client = self._client()
        resp = client.put(
            "/api/visibility/config",
            json={"version": 1, "enabled": True, "enforce": True},
        )
        assert resp.status_code == 200
        got = client.get("/api/visibility/config").json()
        assert got["enforce"] is True
        assert got["enabled"] is True

    def test_put_stale_version_conflicts(self, tmp_data_dir: Path) -> None:
        client = self._client()
        client.put("/api/visibility/config", json={"version": 1, "enabled": True})
        resp = client.put(
            "/api/visibility/config", json={"version": 1, "enabled": False}
        )
        assert resp.status_code == 409
        assert resp.json()["current_version"] == 2

    def test_put_rejects_invalid_payload(self, tmp_data_dir: Path) -> None:
        resp = self._client().put("/api/visibility/config", json={"version": "abc"})
        assert resp.status_code == 400


class TestEmbeddedRestart:
    """The restart endpoint reloads the server config entry in embedded mode."""

    def _post_restart(self, server, monkeypatch, recorder):
        from starlette.routing import Route

        from ha_mcp.tools import tools_dev

        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        monkeypatch.setattr(tools_dev, "schedule_deferred_entry_reload", recorder)
        handlers = build_settings_handlers(server=server)
        app = Starlette(
            routes=[
                Route(
                    "/api/settings/restart",
                    handlers["restart_addon"],
                    methods=["POST"],
                )
            ]
        )
        return TestClient(app).post("/api/settings/restart")

    def test_embedded_restart_schedules_entry_reload(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        server = MagicMock()
        server.client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": [{"entry_id": "server-e"}]}
        )
        server.client.start_options_flow = AsyncMock(
            return_value={
                "type": "form",
                "flow_id": "f1",
                "data_schema": [{"name": "pip_spec", "default": ""}],
            }
        )
        server.client.abort_options_flow = AsyncMock(return_value={})
        scheduled: list[tuple] = []
        resp = self._post_restart(
            server, monkeypatch, lambda client, entry_id: scheduled.append(entry_id)
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert scheduled == ["server-e"]
        # The probe flow must not be left open.
        server.client.abort_options_flow.assert_awaited_once_with("f1")

    def test_embedded_restart_409_when_entry_missing(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        server = MagicMock()
        server.client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        scheduled: list[tuple] = []
        resp = self._post_restart(
            server, monkeypatch, lambda client, entry_id: scheduled.append(entry_id)
        )
        # Below 500 on purpose: the restart JS treats 5xx as
        # restart-in-flight and would start its poll-reload cycle.
        assert resp.status_code == 409
        assert scheduled == []


class TestSettingsInfoDeploymentMode:
    def test_sidecar_mode_reported(self):
        from starlette.routing import Route

        handlers = build_settings_handlers(server=None, is_sidecar=True)
        app = Starlette(routes=[Route("/api/settings/info", handlers["settings_info"])])
        resp = TestClient(app).get("/api/settings/info")
        assert resp.status_code == 200
        assert resp.json()["deployment_mode"] == "sidecar"

    def test_embedded_mode_reported(self, monkeypatch):
        from unittest.mock import MagicMock

        from starlette.routing import Route

        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        handlers = build_settings_handlers(server=MagicMock())
        app = Starlette(routes=[Route("/api/settings/info", handlers["settings_info"])])
        resp = TestClient(app).get("/api/settings/info")
        assert resp.status_code == 200
        assert resp.json()["deployment_mode"] == "embedded"


class TestEmbeddedRestartDiscoveryFailure:
    def test_discovery_failure_is_not_masked_as_not_found(self, monkeypatch):
        # A WS/connection failure during entry discovery must answer as a
        # connection problem (and below 500 — no restart was initiated),
        # not as "entry not found" with a reinstall-flavored suggestion.
        from unittest.mock import AsyncMock, MagicMock

        from starlette.routing import Route

        from ha_mcp.tools import tools_dev

        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        server = MagicMock()
        server.client.send_websocket_message = AsyncMock(
            side_effect=Exception("ws down")
        )
        scheduled: list = []
        monkeypatch.setattr(
            tools_dev,
            "schedule_deferred_entry_reload",
            lambda client, entry_id: scheduled.append(entry_id),
        )
        handlers = build_settings_handlers(server=server)
        app = Starlette(
            routes=[
                Route(
                    "/api/settings/restart",
                    handlers["restart_addon"],
                    methods=["POST"],
                )
            ]
        )
        resp = TestClient(app).post("/api/settings/restart")
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"]["code"] == "CONNECTION_FAILED"
        assert "not locate" not in body["error"]["message"]
        assert scheduled == []
