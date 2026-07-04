"""Unit tests for :class:`EmbeddedServerManager` (issue #1527).

Covers package-ensure gating, worker-thread env staging, HA-token provisioning
(create / reuse / revoke), the readiness probe, and start/stop idempotency.

Home Assistant and aiohttp are stubbed via ``_embedded_stubs`` (imported first so
the fakes are installed before the component modules bind them). ``ha_mcp`` is
never imported here — the manager only imports it inside the worker thread, which
these tests never actually run.
"""

from __future__ import annotations

import importlib.metadata
import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import RequirementsNotFound, install

# Install stubs + put homeassistant-integration/ on sys.path before importing the
# integration modules below. Also an isort barrier so the imports below are never
# reordered above it (which would import embedded_server before the stubs exist).
install()

import ha_mcp_server.embedded_server as es  # noqa: E402
from ha_mcp_server.const import (  # noqa: E402
    CHANNEL_DEV,
    CHANNEL_STABLE,
    DATA_ACCESS_TOKEN,
    DATA_LAST_PIP_SPEC,
    DATA_REFRESH_TOKEN_ID,
    DATA_SECRET_PATH,
    DATA_SERVER_USER_ID,
    DEFAULT_PIP_SPEC,
    DEV_PIP_SPEC,
    DIST_NAME_DEV,
    DIST_NAME_STABLE,
    OPT_BIND_HOST,
    OPT_CHANNEL,
    OPT_PIP_SPEC,
    OPT_SERVER_PORT,
    OPT_SERVER_URL,
    SERVER_TOKEN_CLIENT_NAME,
)

# GROUP_ID_ADMIN / the LLAT token-type come from the homeassistant stub the
# manager imports; the string values are pinned in _embedded_stubs.
_GROUP_ID_ADMIN = es.GROUP_ID_ADMIN
_TOKEN_TYPE_LLAT = es.TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN


def _make_hass(tmp_path) -> MagicMock:
    hass = MagicMock(name="hass")
    hass.config.path = lambda sub: str(tmp_path / sub)

    async def _executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_executor)

    # Auth surface: async_get_user / async_create_user / async_create_refresh_token
    # / async_remove_user are coroutines; async_get_refresh_token /
    # async_create_access_token / async_remove_refresh_token are @callback (sync).
    hass.auth.async_get_user = AsyncMock(return_value=None)
    hass.auth.async_create_user = AsyncMock()
    hass.auth.async_create_refresh_token = AsyncMock()
    hass.auth.async_remove_user = AsyncMock()
    hass.auth.async_get_refresh_token = MagicMock(return_value=None)
    hass.auth.async_create_access_token = MagicMock(return_value="access-token-xyz")
    hass.auth.async_remove_refresh_token = MagicMock()

    def _update_entry(entry, *, data=None, **_kw):
        if data is not None:
            entry.data = data

    hass.config_entries.async_update_entry = MagicMock(side_effect=_update_entry)
    return hass


def _make_entry(*, options=None, data=None) -> MagicMock:
    entry = MagicMock(name="entry")
    entry.options = {} if options is None else dict(options)
    # ``data=None`` ⇒ the default (secret present); ``data={}`` ⇒ explicitly no
    # secret (distinct cases: ``{} or default`` would wrongly pick the default).
    entry.data = {DATA_SECRET_PATH: "/private_secret"} if data is None else dict(data)
    return entry


def _manager(tmp_path, *, options=None, data=None):
    hass = _make_hass(tmp_path)
    entry = _make_entry(options=options, data=data)
    return es.EmbeddedServerManager(hass, entry), hass, entry


def _user(uid="user-1", refresh_tokens=None):
    return SimpleNamespace(id=uid, refresh_tokens=refresh_tokens or {})


def _rt(rt_id="rt-1", user=None, client_name="", token_type=""):
    return SimpleNamespace(
        id=rt_id,
        user=user or _user(),
        client_name=client_name,
        token_type=token_type,
    )


# ---------------------------------------------------------------------------
# Construction / option parsing
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_defaults(self, tmp_path):
        mgr, _hass, _entry = _manager(tmp_path)
        assert mgr.port == 9584
        assert mgr._bind_host == "127.0.0.1"
        assert mgr._server_url == "http://127.0.0.1:8123"
        assert mgr._pip_spec == "ha-mcp==7.9.0"
        assert mgr.is_running is False

    def test_option_overrides(self, tmp_path):
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={
                OPT_SERVER_PORT: 9999,
                OPT_BIND_HOST: "0.0.0.0",
                OPT_SERVER_URL: "http://ha.local:8123/",  # trailing slash trimmed
                OPT_PIP_SPEC: "ha-mcp @ https://example/tarball.tgz",
            },
        )
        assert mgr.port == 9999
        assert mgr._bind_host == "0.0.0.0"
        assert mgr._server_url == "http://ha.local:8123"
        assert mgr._pip_spec == "ha-mcp @ https://example/tarball.tgz"


class TestChannelResolution:
    def test_default_channel_is_stable_pinned(self, tmp_path):
        mgr, _hass, _entry = _manager(tmp_path)
        assert mgr._channel == CHANNEL_STABLE
        assert mgr._pip_spec == DEFAULT_PIP_SPEC

    def test_dev_channel_uses_dev_dist(self, tmp_path):
        mgr, _hass, _entry = _manager(tmp_path, options={OPT_CHANNEL: CHANNEL_DEV})
        assert mgr._pip_spec == DEV_PIP_SPEC

    def test_explicit_override_wins_over_channel(self, tmp_path):
        # A real override (a tarball URL) beats the channel selector even on dev.
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={
                OPT_CHANNEL: CHANNEL_DEV,
                OPT_PIP_SPEC: "ha-mcp @ https://example/tarball.tgz",
            },
        )
        assert mgr._pip_spec == "ha-mcp @ https://example/tarball.tgz"

    def test_default_pip_spec_is_not_an_override(self, tmp_path):
        # The pinned default in the pip-spec field means "no override": a dev
        # entry that stored it must still resolve to the dev distribution.
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_CHANNEL: CHANNEL_DEV, OPT_PIP_SPEC: DEFAULT_PIP_SPEC},
        )
        assert mgr._pip_spec == DEV_PIP_SPEC

    def test_conflicting_dist_name_by_channel(self, tmp_path):
        stable, _h, _e = _manager(tmp_path, options={OPT_CHANNEL: CHANNEL_STABLE})
        dev, _h2, _e2 = _manager(tmp_path, options={OPT_CHANNEL: CHANNEL_DEV})
        assert stable._conflicting_dist_name() == DIST_NAME_DEV
        assert dev._conflicting_dist_name() == DIST_NAME_STABLE

    def test_conflicting_dist_name_none_for_override(self, tmp_path):
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_CHANNEL: CHANNEL_DEV, OPT_PIP_SPEC: "ha-mcp==7.8.0"},
        )
        assert mgr._conflicting_dist_name() is None


# ---------------------------------------------------------------------------
# Package-ensure gating
# ---------------------------------------------------------------------------


class TestEnsurePackage:
    async def test_fast_path_when_spec_unchanged_and_installed(
        self, tmp_path, monkeypatch
    ):
        # Stored spec == configured spec AND the package imports ⇒ delegate to
        # HA's requirements manager (fast path), no forced reinstall.
        mgr, _hass, _entry = _manager(
            tmp_path,
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: "ha-mcp==7.9.0"},
        )
        proc = AsyncMock()
        install_pkg = MagicMock(return_value=True)
        monkeypatch.setattr(es, "async_process_requirements", proc)
        monkeypatch.setattr(es, "install_package", install_pkg)
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.9.0")

        await mgr._async_ensure_package()

        proc.assert_awaited_once()
        assert proc.await_args.args[2] == ["ha-mcp==7.9.0"]
        install_pkg.assert_not_called()

    async def test_force_install_when_spec_changed(self, tmp_path, monkeypatch):
        # Configured spec differs from the last-installed one (the pre-release
        # test channel) ⇒ force a real reinstall (upgrade=True), not the fast path.
        mgr, _hass, entry = _manager(
            tmp_path,
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: "ha-mcp==7.8.0"},
            options={OPT_PIP_SPEC: "ha-mcp==7.9.0"},
        )
        proc = AsyncMock()
        install_pkg = MagicMock(return_value=True)
        monkeypatch.setattr(es, "async_process_requirements", proc)
        monkeypatch.setattr(es, "install_package", install_pkg)
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.9.0")

        await mgr._async_ensure_package()

        proc.assert_not_awaited()
        install_pkg.assert_called_once()
        assert install_pkg.call_args.args[0] == "ha-mcp==7.9.0"
        assert install_pkg.call_args.kwargs.get("upgrade") is True
        # The just-installed spec is persisted so the next start takes the fast path.
        assert entry.data[DATA_LAST_PIP_SPEC] == "ha-mcp==7.9.0"

    async def test_force_install_when_not_installed(self, tmp_path, monkeypatch):
        # First run: package absent ⇒ force install, then persist the spec.
        mgr, _hass, entry = _manager(tmp_path)  # no DATA_LAST_PIP_SPEC
        install_pkg = MagicMock(return_value=True)
        monkeypatch.setattr(es, "install_package", install_pkg)
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(
            es, "_installed_ha_mcp_version", MagicMock(side_effect=[None, "7.9.0"])
        )

        await mgr._async_ensure_package()

        install_pkg.assert_called_once()
        assert entry.data[DATA_LAST_PIP_SPEC] == "ha-mcp==7.9.0"

    async def test_requirements_not_found_raises_package_error(
        self, tmp_path, monkeypatch
    ):
        # Fast path but the requirements manager fails ⇒ package-kind error.
        mgr, _hass, _entry = _manager(
            tmp_path,
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: "ha-mcp==7.9.0"},
        )
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.9.0")
        monkeypatch.setattr(
            es,
            "async_process_requirements",
            AsyncMock(side_effect=RequirementsNotFound("ha_mcp_server", ["ha-mcp"])),
        )
        with pytest.raises(es.EmbeddedServerError) as exc:
            await mgr._async_ensure_package()
        assert exc.value.kind == "package"

    async def test_force_install_failure_raises_package_error(
        self, tmp_path, monkeypatch
    ):
        mgr, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(es, "install_package", MagicMock(return_value=False))
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: None)
        with pytest.raises(es.EmbeddedServerError) as exc:
            await mgr._async_ensure_package()
        assert exc.value.kind == "package"
        # A FAILED install must never persist the spec: a poisoned
        # DATA_LAST_PIP_SPEC would make the next reload take the fast path and
        # never self-heal (review finding).
        assert DATA_LAST_PIP_SPEC not in _entry.data

    async def test_installed_but_not_importable_raises_package_error(
        self, tmp_path, monkeypatch
    ):
        # Install "succeeds" but the package still doesn't import ⇒ package error.
        mgr, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(es, "install_package", MagicMock(return_value=True))
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: None)
        with pytest.raises(es.EmbeddedServerError) as exc:
            await mgr._async_ensure_package()
        assert exc.value.kind == "package"
        assert DATA_LAST_PIP_SPEC not in _entry.data

    async def test_dev_channel_always_forces_install(self, tmp_path, monkeypatch):
        # Dev channel skips the fast path even when the stored spec matches and
        # the package is present, so every reload pulls the newest dev build.
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_CHANNEL: CHANNEL_DEV},
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: DEV_PIP_SPEC},
        )
        proc = AsyncMock()
        install_pkg = MagicMock(return_value=True)
        monkeypatch.setattr(es, "async_process_requirements", proc)
        monkeypatch.setattr(es, "install_package", install_pkg)
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.9.0.dev5")
        monkeypatch.setattr(es, "_dist_installed", lambda name: False)
        uninstall = MagicMock()
        monkeypatch.setattr(es, "_uninstall_distribution", uninstall)

        await mgr._async_ensure_package()

        proc.assert_not_awaited()
        install_pkg.assert_called_once()
        assert install_pkg.call_args.args[0] == DEV_PIP_SPEC
        uninstall.assert_not_called()  # the other dist was absent

    async def test_channel_switch_uninstalls_other_dist(self, tmp_path, monkeypatch):
        # Switching to dev while stable's distribution is installed uninstalls
        # ha-mcp first (shared ha_mcp import package) before installing dev.
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_CHANNEL: CHANNEL_DEV},
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: DEFAULT_PIP_SPEC},
        )
        install_pkg = MagicMock(return_value=True)
        monkeypatch.setattr(es, "install_package", install_pkg)
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.9.0")
        monkeypatch.setattr(
            es, "_dist_installed", lambda name: name == DIST_NAME_STABLE
        )
        uninstall = MagicMock()
        monkeypatch.setattr(es, "_uninstall_distribution", uninstall)

        await mgr._async_ensure_package()

        uninstall.assert_called_once_with(DIST_NAME_STABLE)
        install_pkg.assert_called_once()
        assert install_pkg.call_args.args[0] == DEV_PIP_SPEC

    async def test_channel_switch_downgrade_uninstalls_dev_dist(
        self, tmp_path, monkeypatch
    ):
        # The motivating case for _async_remove_conflicting_dist: dev -> stable
        # must uninstall ha-mcp-dev BEFORE the pinned stable reinstall, or the
        # orphaned dev metadata makes the pin look satisfied and the user keeps
        # running dev builds (review finding: only stable -> dev was wired-tested).
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_CHANNEL: CHANNEL_STABLE},
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: DEV_PIP_SPEC},
        )
        install_pkg = MagicMock(return_value=True)
        monkeypatch.setattr(es, "install_package", install_pkg)
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.9.0")
        monkeypatch.setattr(es, "_dist_installed", lambda name: name == DIST_NAME_DEV)
        uninstall = MagicMock()
        monkeypatch.setattr(es, "_uninstall_distribution", uninstall)

        await mgr._async_ensure_package()

        uninstall.assert_called_once_with(DIST_NAME_DEV)
        install_pkg.assert_called_once()
        assert install_pkg.call_args.args[0] == DEFAULT_PIP_SPEC

    async def test_no_uninstall_for_explicit_override(self, tmp_path, monkeypatch):
        # An explicit override has an unknown distribution name, so nothing is
        # uninstalled even when the other channel's package is present.
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_CHANNEL: CHANNEL_DEV, OPT_PIP_SPEC: "ha-mcp==7.8.0"},
            data={DATA_SECRET_PATH: "/p"},
        )
        monkeypatch.setattr(es, "install_package", MagicMock(return_value=True))
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.8.0")
        monkeypatch.setattr(es, "_dist_installed", lambda name: True)
        uninstall = MagicMock()
        monkeypatch.setattr(es, "_uninstall_distribution", uninstall)

        await mgr._async_ensure_package()
        uninstall.assert_not_called()


class TestDistHelpers:
    def test_dist_installed_true(self, monkeypatch):
        monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.0")
        assert es._dist_installed(DIST_NAME_DEV) is True

    def test_dist_installed_false(self, monkeypatch):
        def _version(name):
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "version", _version)
        assert es._dist_installed(DIST_NAME_DEV) is False

    def test_uninstall_builds_uv_pip_command_no_shell(self, monkeypatch):
        calls = {}

        def _run(args, **kwargs):
            calls["args"] = args
            calls["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stderr="")

        monkeypatch.setattr(es.subprocess, "run", _run)
        es._uninstall_distribution(DIST_NAME_DEV)

        args = calls["args"]
        assert args[0] == sys.executable
        assert args[1:6] == ["-m", "uv", "pip", "uninstall", "--python"]
        assert args[-1] == DIST_NAME_DEV
        # No shell, and a non-zero exit is tolerated rather than raising.
        assert calls["kwargs"]["check"] is False

    def test_uninstall_nonzero_exit_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(
            es.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=1, stderr="boom"),
        )
        es._uninstall_distribution(DIST_NAME_DEV)  # must not raise

    def test_uninstall_subprocess_error_is_swallowed(self, monkeypatch):
        def _boom(*a, **k):
            raise OSError("no uv")

        monkeypatch.setattr(es.subprocess, "run", _boom)
        es._uninstall_distribution(DIST_NAME_DEV)  # must not raise


class TestInstalledVersion:
    @pytest.fixture(autouse=True)
    def _importable(self, monkeypatch):
        # The guard now also requires the import machinery to resolve ha_mcp
        # (orphaned-metadata hazard); default to importable, tests override.
        monkeypatch.setattr(
            es.importlib.util, "find_spec", lambda name: object(), raising=True
        )

    def test_returns_stable_dist_version(self, monkeypatch):
        monkeypatch.setattr(importlib.metadata, "version", lambda name: "7.9.0")
        assert es._installed_ha_mcp_version() == "7.9.0"

    def test_falls_back_to_dev_dist(self, monkeypatch):
        def _version(name):
            if name == "ha-mcp":
                raise importlib.metadata.PackageNotFoundError(name)
            return "7.9.0.dev5"

        monkeypatch.setattr(importlib.metadata, "version", _version)
        assert es._installed_ha_mcp_version() == "7.9.0.dev5"

    def test_returns_none_when_absent(self, monkeypatch):
        def _version(name):
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "version", _version)
        assert es._installed_ha_mcp_version() is None

    def test_orphaned_metadata_without_importable_package_is_none(self, monkeypatch):
        # Regression (review finding): a channel switch's best-effort uninstall
        # can leave the OTHER dist's .dist-info while the shared ha_mcp/ files
        # are gone — metadata alone must not count as installed, or the
        # post-install guard passes and the worker thread crashes on import
        # (surfaced as the WRONG repair issue).
        monkeypatch.setattr(importlib.metadata, "version", lambda name: "7.9.0")
        monkeypatch.setattr(es.importlib.util, "find_spec", lambda name: None)
        assert es._installed_ha_mcp_version() is None


# ---------------------------------------------------------------------------
# Worker-thread env staging
# ---------------------------------------------------------------------------


class TestThreadEnvStaging:
    @pytest.fixture(autouse=True)
    def _isolate_env(self):
        keys = (
            "HOMEASSISTANT_URL",
            "HOMEASSISTANT_TOKEN",
            "HA_MCP_CONFIG_DIR",
            "HA_MCP_EMBEDDED",
        )
        saved = {k: os.environ.get(k) for k in keys}
        for key in keys:
            os.environ.pop(key, None)
        yield
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_only_config_dir_and_embedded_env_are_staged(self, tmp_path, monkeypatch):
        # Security posture: the loopback URL + admin token go through
        # set_embedded_connection (in memory), NEVER through os.environ. Only the
        # two non-secret vars are staged as env, before the first ha_mcp import.
        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_SERVER_URL: "http://ha.local:8123"}
        )
        captured = {}

        async def _fake_serve(_token):
            for key in (
                "HOMEASSISTANT_URL",
                "HOMEASSISTANT_TOKEN",
                "HA_MCP_CONFIG_DIR",
                "HA_MCP_EMBEDDED",
            ):
                captured[key] = os.environ.get(key)

        monkeypatch.setattr(mgr, "_serve", _fake_serve)
        mgr._thread_main("tok-abc")

        assert captured["HA_MCP_CONFIG_DIR"] == mgr._config_dir
        assert captured["HA_MCP_EMBEDDED"] == "1"
        # The token / URL are never exported to the shared process environment.
        assert captured["HOMEASSISTANT_URL"] is None
        assert captured["HOMEASSISTANT_TOKEN"] is None
        # The worker loop + stop event were created for a stop request to reach.
        assert mgr._stop_event is not None

    def test_serve_hands_connection_in_memory_not_via_env(self, tmp_path, monkeypatch):
        # _serve registers the loopback URL + admin token via
        # ha_mcp.config.set_embedded_connection before building the server. Stub
        # ha_mcp + ha_mcp.config only; leaving ha_mcp.server absent makes the next
        # import fail so _serve stops right after the connection is registered.
        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_SERVER_URL: "http://ha.local:8123"}
        )
        set_conn = MagicMock(name="set_embedded_connection")
        ha_mcp_mod = ModuleType("ha_mcp")
        ha_mcp_config = ModuleType("ha_mcp.config")
        ha_mcp_config.set_embedded_connection = set_conn
        monkeypatch.setitem(sys.modules, "ha_mcp", ha_mcp_mod)
        monkeypatch.setitem(sys.modules, "ha_mcp.config", ha_mcp_config)
        monkeypatch.delitem(sys.modules, "ha_mcp.server", raising=False)

        mgr._thread_main("tok-xyz")

        set_conn.assert_called_once_with("http://ha.local:8123", "tok-xyz")
        # _serve raised on the ha_mcp.server import → captured, thread didn't hang.
        assert mgr._thread_exc is not None

    def test_thread_crash_is_captured_not_raised(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(tmp_path)

        async def _boom(_token):
            raise RuntimeError("serve failed")

        monkeypatch.setattr(mgr, "_serve", _boom)
        mgr._thread_main("tok")  # must not raise out of the thread body
        assert isinstance(mgr._thread_exc, RuntimeError)


# ---------------------------------------------------------------------------
# Token provisioning / reuse / revocation
# ---------------------------------------------------------------------------


class TestTokenProvisioning:
    async def test_first_run_creates_user_and_llat(self, tmp_path):
        mgr, hass, entry = _manager(tmp_path)
        user = _user("new-user")
        hass.auth.async_create_user.return_value = user
        hass.auth.async_create_refresh_token.return_value = _rt("rt-new", user=user)

        token = await mgr._async_provision_token()

        assert token == "access-token-xyz"
        hass.auth.async_create_user.assert_awaited_once()
        # Admin, local-only user.
        assert hass.auth.async_create_user.await_args.kwargs["group_ids"] == [
            _GROUP_ID_ADMIN
        ]
        assert hass.auth.async_create_user.await_args.kwargs["local_only"] is True
        # A long-lived refresh token was minted.
        rt_kwargs = hass.auth.async_create_refresh_token.await_args.kwargs
        assert rt_kwargs["client_name"] == SERVER_TOKEN_CLIENT_NAME
        assert rt_kwargs["token_type"] == _TOKEN_TYPE_LLAT
        # ids + access token persisted to entry.data.
        assert entry.data[DATA_SERVER_USER_ID] == "new-user"
        assert entry.data[DATA_REFRESH_TOKEN_ID] == "rt-new"
        assert entry.data[DATA_ACCESS_TOKEN] == "access-token-xyz"

    async def test_reuse_across_restart_mints_only_access_token(self, tmp_path):
        user = _user("stored-user")
        rt = _rt("stored-rt", user=user)
        mgr, hass, _entry = _manager(
            tmp_path,
            data={
                DATA_SECRET_PATH: "/private_secret",
                DATA_SERVER_USER_ID: "stored-user",
                DATA_REFRESH_TOKEN_ID: "stored-rt",
            },
        )
        hass.auth.async_get_user.return_value = user
        hass.auth.async_get_refresh_token.return_value = rt

        token = await mgr._async_provision_token()

        assert token == "access-token-xyz"
        hass.auth.async_create_user.assert_not_awaited()
        hass.auth.async_create_refresh_token.assert_not_awaited()
        hass.auth.async_create_access_token.assert_called_once_with(rt)

    async def test_recreates_when_stored_user_gone(self, tmp_path):
        mgr, hass, _entry = _manager(
            tmp_path,
            data={DATA_SECRET_PATH: "/p", DATA_SERVER_USER_ID: "ghost"},
        )
        hass.auth.async_get_user.return_value = None  # stored user vanished
        new_user = _user("fresh")
        hass.auth.async_create_user.return_value = new_user
        hass.auth.async_create_refresh_token.return_value = _rt(
            "fresh-rt", user=new_user
        )

        await mgr._async_provision_token()
        hass.auth.async_create_user.assert_awaited_once()

    async def test_discards_refresh_token_of_other_user(self, tmp_path):
        user = _user("stored-user")
        foreign_rt = _rt("foreign", user=_user("someone-else"))
        mgr, hass, _entry = _manager(
            tmp_path,
            data={
                DATA_SECRET_PATH: "/p",
                DATA_SERVER_USER_ID: "stored-user",
                DATA_REFRESH_TOKEN_ID: "foreign",
            },
        )
        hass.auth.async_get_user.return_value = user
        hass.auth.async_get_refresh_token.return_value = foreign_rt
        hass.auth.async_create_refresh_token.return_value = _rt("mine", user=user)

        await mgr._async_provision_token()
        # A new refresh token was minted for the correct user.
        hass.auth.async_create_refresh_token.assert_awaited_once()

    async def test_clears_stale_llat_before_creating(self, tmp_path):
        stale = _rt(
            "stale",
            client_name=SERVER_TOKEN_CLIENT_NAME,
            token_type=_TOKEN_TYPE_LLAT,
        )
        user = _user("u", refresh_tokens={"stale": stale})
        stale.user = user
        mgr, hass, _entry = _manager(tmp_path)
        hass.auth.async_create_user.return_value = user
        hass.auth.async_create_refresh_token.return_value = _rt("rt-new", user=user)

        await mgr._async_provision_token()
        hass.auth.async_remove_refresh_token.assert_called_once_with(stale)


class TestRevokeCredentials:
    async def test_removes_token_and_user_and_strips_entry_data(self, tmp_path):
        user = _user("u")
        rt = _rt("rt", user=user)
        mgr, hass, entry = _manager(
            tmp_path,
            data={
                DATA_SECRET_PATH: "/p",
                DATA_SERVER_USER_ID: "u",
                DATA_REFRESH_TOKEN_ID: "rt",
                DATA_ACCESS_TOKEN: "tok",
            },
        )
        hass.auth.async_get_refresh_token.return_value = rt
        hass.auth.async_get_user.return_value = user

        await mgr.async_revoke_credentials()

        hass.auth.async_remove_refresh_token.assert_called_once_with(rt)
        hass.auth.async_remove_user.assert_awaited_once_with(user)
        # The three provisioning keys are stripped; the secret path is kept.
        assert DATA_SERVER_USER_ID not in entry.data
        assert DATA_REFRESH_TOKEN_ID not in entry.data
        assert DATA_ACCESS_TOKEN not in entry.data
        assert entry.data[DATA_SECRET_PATH] == "/p"

    async def test_idempotent_when_ids_missing(self, tmp_path):
        mgr, hass, _entry = _manager(tmp_path, data={DATA_SECRET_PATH: "/p"})
        await mgr.async_revoke_credentials()  # must not raise
        hass.auth.async_remove_refresh_token.assert_not_called()
        hass.auth.async_remove_user.assert_not_awaited()

    async def test_missing_objects_treated_as_success(self, tmp_path):
        mgr, hass, _entry = _manager(
            tmp_path,
            data={
                DATA_SECRET_PATH: "/p",
                DATA_SERVER_USER_ID: "gone",
                DATA_REFRESH_TOKEN_ID: "gone",
            },
        )
        hass.auth.async_get_refresh_token.return_value = None
        hass.auth.async_get_user.return_value = None
        await mgr.async_revoke_credentials()
        hass.auth.async_remove_refresh_token.assert_not_called()
        hass.auth.async_remove_user.assert_not_awaited()


# ---------------------------------------------------------------------------
# Readiness probe
# ---------------------------------------------------------------------------


class TestReadinessProbe:
    async def test_probe_port_true_on_connect(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(tmp_path)
        writer = MagicMock()
        writer.wait_closed = AsyncMock()

        async def _open(host, port):
            return MagicMock(), writer

        monkeypatch.setattr(es.asyncio, "open_connection", _open)
        assert await mgr._async_probe_port() is True
        writer.close.assert_called_once()

    async def test_probe_port_false_on_refused(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(tmp_path)

        async def _open(host, port):
            raise ConnectionRefusedError

        monkeypatch.setattr(es.asyncio, "open_connection", _open)
        assert await mgr._async_probe_port() is False

    async def test_wait_ready_raises_on_early_thread_crash(self, tmp_path):
        mgr, hass, _entry = _manager(tmp_path)
        hass.loop.time = MagicMock(return_value=0.0)
        mgr._thread_exc = RuntimeError("bind failed")
        with pytest.raises(es.EmbeddedServerError, match="failed to start"):
            await mgr._async_wait_until_ready()

    async def test_wait_ready_raises_when_thread_exited(self, tmp_path):
        mgr, hass, _entry = _manager(tmp_path)
        hass.loop.time = MagicMock(return_value=0.0)
        mgr._thread = SimpleNamespace(is_alive=lambda: False)
        with pytest.raises(es.EmbeddedServerError, match="exited during startup"):
            await mgr._async_wait_until_ready()

    async def test_wait_ready_timeout_stops_thread_and_raises(
        self, tmp_path, monkeypatch
    ):
        mgr, hass, _entry = _manager(tmp_path)
        # loop.time advances past the deadline on the second read.
        hass.loop.time = MagicMock(side_effect=[0.0, 0.0, 9999.0, 9999.0])
        mgr._thread = SimpleNamespace(is_alive=lambda: True)
        monkeypatch.setattr(mgr, "_async_probe_port", AsyncMock(return_value=False))
        stop = AsyncMock()
        monkeypatch.setattr(mgr, "async_stop", stop)
        monkeypatch.setattr(es.asyncio, "sleep", AsyncMock())

        with pytest.raises(es.EmbeddedServerError, match="did not become reachable"):
            await mgr._async_wait_until_ready()
        stop.assert_awaited_once()


# ---------------------------------------------------------------------------
# start / stop lifecycle + idempotency
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_start_requires_secret_path(self, tmp_path):
        mgr, _hass, _entry = _manager(tmp_path, data={})  # no DATA_SECRET_PATH
        with pytest.raises(es.EmbeddedServerError, match="secret path missing"):
            await mgr.async_start()

    async def test_start_orders_steps_and_spawns_thread(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(tmp_path)
        calls = []
        monkeypatch.setattr(
            mgr,
            "_async_ensure_package",
            AsyncMock(side_effect=lambda: calls.append("ensure")),
        )
        monkeypatch.setattr(
            mgr,
            "_async_provision_token",
            AsyncMock(side_effect=lambda: (calls.append("token"), "tok")[1]),
        )
        monkeypatch.setattr(mgr, "_prepare_config_dir", lambda: calls.append("dir"))
        monkeypatch.setattr(
            mgr,
            "_async_wait_until_ready",
            AsyncMock(side_effect=lambda: calls.append("ready")),
        )
        # Replace the thread body so no real ha_mcp import happens.
        started = []
        monkeypatch.setattr(mgr, "_thread_main", lambda token: started.append(token))

        await mgr.async_start()
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)

        assert calls == ["ensure", "token", "dir", "ready"]
        assert started == ["tok"]

    async def test_stop_without_start_is_noop(self, tmp_path):
        mgr, _hass, _entry = _manager(tmp_path)
        await mgr.async_stop()  # must not raise
        assert mgr.is_running is False

    async def test_stop_signals_and_joins_bounded_then_orphans(self, tmp_path):
        # HA-shutdown safety contract (review finding: untested): async_stop
        # must schedule the stop event threadsafe, join with the BOUNDED
        # timeout, and when the thread refuses to die, orphan it (clearing all
        # worker state) instead of blocking Home Assistant shutdown.
        mgr, _hass, _entry = _manager(tmp_path)
        joins: list[object] = []

        class _FakeThread:
            def is_alive(self):
                return True  # wedged thread: join times out

            def join(self, timeout=None):
                joins.append(timeout)

        class _FakeLoop:
            def __init__(self):
                self.scheduled = []

            def is_closed(self):
                return False

            def call_soon_threadsafe(self, cb):
                self.scheduled.append(cb)

        class _FakeEvent:
            def set(self):
                pass

        loop = _FakeLoop()
        mgr._thread = _FakeThread()
        mgr._loop = loop
        mgr._stop_event = _FakeEvent()
        mgr._thread_exc = RuntimeError("stale")

        await mgr.async_stop()

        assert len(loop.scheduled) == 1  # stop event scheduled threadsafe
        assert joins == [es._STOP_JOIN_TIMEOUT_SECONDS]  # bounded join
        # Worker state fully cleared even for the orphaned thread.
        assert mgr._thread is None
        assert mgr._loop is None
        assert mgr._stop_event is None
        assert mgr._thread_exc is None

    async def test_stop_survives_loop_closing_race(self, tmp_path):
        # The loop can close between is_closed() and call_soon_threadsafe
        # (worker exiting) — the RuntimeError must not escape async_stop.
        mgr, _hass, _entry = _manager(tmp_path)

        class _RacyLoop:
            def is_closed(self):
                return False

            def call_soon_threadsafe(self, cb):
                raise RuntimeError("Event loop is closed")

        class _DeadThread:
            def is_alive(self):
                return False

            def join(self, timeout=None):
                pass

        class _FakeEvent:
            def set(self):
                pass

        mgr._thread = _DeadThread()
        mgr._loop = _RacyLoop()
        mgr._stop_event = _FakeEvent()

        await mgr.async_stop()  # must not raise
        assert mgr._thread is None

    def test_prepare_config_dir_creates_directory(self, tmp_path):
        mgr, _hass, _entry = _manager(tmp_path)
        mgr._prepare_config_dir()
        assert os.path.isdir(mgr._config_dir)
