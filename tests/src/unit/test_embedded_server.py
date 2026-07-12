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

# Install stubs + put the component package on sys.path before importing the
# integration modules below. Also an isort barrier so the imports below are never
# reordered above it (which would import embedded_server before the stubs exist).
install()

import custom_components.ha_mcp_tools.embedded_server as es  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    CHANNEL_DEV,
    CHANNEL_STABLE,
    DATA_ACCESS_TOKEN,
    DATA_LAST_PIP_SPEC,
    DATA_PENDING_INSTALL_VERSION,
    DATA_REFRESH_TOKEN_ID,
    DATA_SECRET_PATH,
    DATA_SERVER_USER_ID,
    DEFAULT_PIP_SPEC,
    DEV_PIP_SPEC,
    DIST_NAME_DEV,
    DIST_NAME_STABLE,
    OPT_AUTO_UPDATE,
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


def _stub_ha_mcp_surface(monkeypatch, *, mcp, landing_mod=None) -> None:
    """Install a minimal in-memory ``ha_mcp`` package so ``_serve`` runs hermetically.

    Wires a non-sentinel connection (so ``_serve`` passes its refuse-to-serve
    guard), a server whose ``.mcp`` is ``mcp``, no-op settings routes, and a stub
    uvicorn. Pass ``landing_mod`` to stub ``ha_mcp.browser_landing``; omit it to
    simulate an OLDER installed server without the landing helper — modeled as a
    module missing the ``register_browser_landing`` attribute, so the from-import
    in ``_serve`` raises the same ImportError class its guard catches. Injection
    (a sys.modules hit) is the only hermetic way to force that failure: deleting
    the entry is NOT enough, because the editable install (``uv sync``) adds a
    meta-path finder that resolves ``ha_mcp.*`` by name and would re-import the
    REAL module even though the parent ``ha_mcp`` is faked with an empty
    ``__path__`` (live-found in CI).
    """
    settings = SimpleNamespace(
        homeassistant_url="http://127.0.0.1:8123", homeassistant_token="jwt"
    )
    ha_mcp_mod = ModuleType("ha_mcp")
    ha_mcp_mod.__path__ = []  # package semantics for submodule imports
    cfg = ModuleType("ha_mcp.config")
    cfg.reset_global_settings = lambda: None
    cfg.set_embedded_connection = lambda u, t: None
    cfg.OAUTH_MODE_URL = "__sentinel_url__"
    cfg.OAUTH_MODE_TOKEN = "__sentinel_token__"
    cfg.get_global_settings = lambda: settings
    server_mod = ModuleType("ha_mcp.server")
    server_mod.HomeAssistantSmartMCPServer = lambda: SimpleNamespace(mcp=mcp)
    ui_mod = ModuleType("ha_mcp.settings_ui")
    ui_mod.register_settings_routes = lambda *a, **k: None
    uvicorn_mod = ModuleType("uvicorn")
    uvicorn_mod.Config = lambda *a, **k: SimpleNamespace()
    uvicorn_mod.Server = lambda config: SimpleNamespace(should_exit=False)
    ha_mcp_mod.config = cfg
    ha_mcp_mod.server = server_mod
    ha_mcp_mod.settings_ui = ui_mod
    mods = {
        "ha_mcp": ha_mcp_mod,
        "ha_mcp.config": cfg,
        "ha_mcp.server": server_mod,
        "ha_mcp.settings_ui": ui_mod,
        "uvicorn": uvicorn_mod,
    }
    if landing_mod is None:
        # Older-server stand-in: module present, helper attribute absent — the
        # from-import raises ImportError, same class as a missing module.
        landing_mod = ModuleType("ha_mcp.browser_landing")
    ha_mcp_mod.browser_landing = landing_mod
    mods["ha_mcp.browser_landing"] = landing_mod
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)


# ---------------------------------------------------------------------------
# Construction / option parsing
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_defaults(self, tmp_path):
        mgr, _hass, _entry = _manager(tmp_path)
        assert mgr.port == 9584
        # LAN-reachable by default (owner decision: add-on parity - the
        # secret path is the credential, same as the add-on's port).
        assert mgr._bind_host == "0.0.0.0"
        assert mgr._server_url == "http://127.0.0.1:8123"
        # Stable is unpinned now: the bare distribution name (auto-updates).
        assert mgr._pip_spec == "ha-mcp"
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
    def test_default_channel_is_stable_unpinned(self, tmp_path):
        mgr, _hass, _entry = _manager(tmp_path)
        assert mgr._channel == CHANNEL_STABLE
        assert mgr._pip_spec == DEFAULT_PIP_SPEC
        # Unpinned: the stable channel installs the bare distribution name so
        # each install resolves the newest stable release.
        assert mgr._pip_spec == DIST_NAME_STABLE

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

    def test_auto_update_off_pins_stable_to_installed(self, tmp_path):
        # Auto-update off: a non-override channel pins to the passed installed
        # version so reloads keep exactly that build. The version is passed in
        # (not read) so _resolve_pip_spec never blocks the event loop.
        mgr, _hass, _entry = _manager(tmp_path, options={OPT_AUTO_UPDATE: False})
        # Construction defers the read: the initial spec is the bare dist.
        assert mgr._pip_spec == DIST_NAME_STABLE
        assert mgr._resolve_pip_spec("7.9.0") == f"{DIST_NAME_STABLE}==7.9.0"

    def test_auto_update_off_pins_dev_to_installed(self, tmp_path):
        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_CHANNEL: CHANNEL_DEV, OPT_AUTO_UPDATE: False}
        )
        assert mgr._pip_spec == DEV_PIP_SPEC
        assert mgr._resolve_pip_spec("7.9.0.dev5") == f"{DEV_PIP_SPEC}==7.9.0.dev5"

    async def test_ensure_package_repins_stable_from_installed_when_auto_off(
        self, tmp_path, monkeypatch
    ):
        # The executor-read version of the TARGET dist re-pins the spec inside
        # _async_ensure_package (off-loop), so the forced install targets the
        # exact installed build.
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_AUTO_UPDATE: False},
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: "stale"},
        )
        monkeypatch.setattr(es, "async_process_requirements", AsyncMock())
        monkeypatch.setattr(es, "install_package", MagicMock(return_value=True))
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.12.1")
        monkeypatch.setattr(es, "_installed_dist_version", lambda dist: "7.12.1")
        monkeypatch.setattr(
            es, "_dist_installed", lambda name: name == DIST_NAME_STABLE
        )
        monkeypatch.setattr(es, "_uninstall_distribution", MagicMock())

        await mgr._async_ensure_package()

        assert mgr._pip_spec == f"{DIST_NAME_STABLE}==7.12.1"

    async def test_ensure_package_channel_switch_auto_off_stays_unpinned(
        self, tmp_path, monkeypatch
    ):
        # Regression: dev->stable with auto-update off. The old dev dist is still
        # installed when the re-pin reads, but the pin must come from the TARGET
        # (stable) dist — which is not installed yet — so the spec stays unpinned
        # and installs the newest stable, rather than pinning ha-mcp to a
        # dev-only version that does not exist (a failed bring-up).
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_CHANNEL: CHANNEL_STABLE, OPT_AUTO_UPDATE: False},
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: "stale"},
        )
        monkeypatch.setattr(es, "async_process_requirements", AsyncMock())
        monkeypatch.setattr(es, "install_package", MagicMock(return_value=True))
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        # Whichever-present read sees the old dev build; the target-dist read
        # sees nothing (stable not installed on this machine yet).
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "8.0.0.dev3")
        monkeypatch.setattr(
            es,
            "_installed_dist_version",
            lambda dist: "8.0.0.dev3" if dist == DEV_PIP_SPEC else None,
        )
        monkeypatch.setattr(es, "_dist_installed", lambda name: True)
        monkeypatch.setattr(es, "_uninstall_distribution", MagicMock())

        await mgr._async_ensure_package()

        assert mgr._pip_spec == DIST_NAME_STABLE

    def test_auto_update_off_falls_back_to_unpinned_on_first_setup(
        self, tmp_path, monkeypatch
    ):
        # Nothing installed yet ⇒ no version to pin to ⇒ install the unpinned
        # dist once (the newest), then later reloads pin to whatever landed.
        monkeypatch.setattr(es, "_installed_dist_version", lambda dist: None)
        mgr, _hass, _entry = _manager(tmp_path, options={OPT_AUTO_UPDATE: False})
        assert mgr._pip_spec == DIST_NAME_STABLE

    def test_auto_update_default_on_never_pins(self, tmp_path, monkeypatch):
        # Default (option absent) is auto-update ON: the spec stays unpinned even
        # when a version is installed, and no version is read at construction.
        reader = MagicMock(return_value="7.9.0")
        monkeypatch.setattr(es, "_installed_dist_version", reader)
        mgr, _hass, _entry = _manager(tmp_path)
        assert mgr._pip_spec == DIST_NAME_STABLE
        reader.assert_not_called()

    def test_explicit_override_wins_over_auto_update_off(self, tmp_path, monkeypatch):
        # An override still wins even with auto-update off (no pinning applied).
        monkeypatch.setattr(es, "_installed_dist_version", lambda dist: "7.9.0")
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_AUTO_UPDATE: False, OPT_PIP_SPEC: "ha-mcp==7.8.0"},
        )
        assert mgr._pip_spec == "ha-mcp==7.8.0"


# ---------------------------------------------------------------------------
# Package-ensure gating
# ---------------------------------------------------------------------------


class TestEnsurePackage:
    async def test_fast_path_only_for_unchanged_override(self, tmp_path, monkeypatch):
        # The fast path is reserved for an explicit pip-spec override: an
        # unchanged, already-installed pin delegates the "already satisfied?"
        # decision to HA's requirements manager (a pin does not move, so no
        # forced reinstall).
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_PIP_SPEC: "ha-mcp==7.12.1"},
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: "ha-mcp==7.12.1"},
        )
        proc = AsyncMock()
        install_pkg = MagicMock(return_value=True)
        monkeypatch.setattr(es, "async_process_requirements", proc)
        monkeypatch.setattr(es, "install_package", install_pkg)
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.12.1")

        await mgr._async_ensure_package()

        proc.assert_awaited_once()
        assert proc.await_args.args[2] == ["ha-mcp==7.12.1"]
        install_pkg.assert_not_called()

    async def test_auto_update_off_takes_fast_path_when_pinned_unchanged(
        self, tmp_path, monkeypatch
    ):
        # Auto-update off pins to the installed version; an unchanged pin takes
        # the fast path (like an explicit override) — no forced upgrade churn.
        monkeypatch.setattr(es, "_installed_dist_version", lambda dist: "7.12.1")
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_AUTO_UPDATE: False},
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: "ha-mcp==7.12.1"},
        )
        proc = AsyncMock()
        install_pkg = MagicMock(return_value=True)
        monkeypatch.setattr(es, "async_process_requirements", proc)
        monkeypatch.setattr(es, "install_package", install_pkg)
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.12.1")

        await mgr._async_ensure_package()

        proc.assert_awaited_once()
        assert proc.await_args.args[2] == ["ha-mcp==7.12.1"]
        install_pkg.assert_not_called()

    async def test_stable_non_override_always_forces_install(
        self, tmp_path, monkeypatch
    ):
        # Stable is unpinned and auto-updates: even when the stored spec matches
        # and the package is present, a non-override spec takes the force-install
        # path (upgrade=True) so every reload pulls the newest stable build.
        mgr, _hass, _entry = _manager(
            tmp_path,
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: DEFAULT_PIP_SPEC},
        )
        proc = AsyncMock()
        install_pkg = MagicMock(return_value=True)
        monkeypatch.setattr(es, "async_process_requirements", proc)
        monkeypatch.setattr(es, "install_package", install_pkg)
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.12.1")
        monkeypatch.setattr(es, "_dist_installed", lambda name: False)
        monkeypatch.setattr(es, "_uninstall_distribution", MagicMock())

        await mgr._async_ensure_package()

        proc.assert_not_awaited()
        install_pkg.assert_called_once()
        assert install_pkg.call_args.args[0] == DEFAULT_PIP_SPEC
        assert install_pkg.call_args.kwargs.get("upgrade") is True

    async def test_force_install_when_spec_changed(self, tmp_path, monkeypatch):
        # Configured spec differs from the last-installed one (the pre-release
        # test channel) ⇒ force a real reinstall (upgrade=True), not the fast path.
        mgr, _hass, entry = _manager(
            tmp_path,
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: "ha-mcp==7.11.0"},
            options={OPT_PIP_SPEC: "ha-mcp==7.12.1"},
        )
        proc = AsyncMock()
        install_pkg = MagicMock(return_value=True)
        monkeypatch.setattr(es, "async_process_requirements", proc)
        monkeypatch.setattr(es, "install_package", install_pkg)
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.12.1")

        await mgr._async_ensure_package()

        proc.assert_not_awaited()
        install_pkg.assert_called_once()
        assert install_pkg.call_args.args[0] == "ha-mcp==7.12.1"
        assert install_pkg.call_args.kwargs.get("upgrade") is True
        # The just-installed spec is persisted so the next start takes the fast path.
        assert entry.data[DATA_LAST_PIP_SPEC] == "ha-mcp==7.12.1"

    async def test_force_install_when_not_installed(self, tmp_path, monkeypatch):
        # First run: package absent ⇒ force install, then persist the spec (the
        # unpinned stable distribution name).
        mgr, _hass, entry = _manager(tmp_path)  # no DATA_LAST_PIP_SPEC
        install_pkg = MagicMock(return_value=True)
        monkeypatch.setattr(es, "install_package", install_pkg)
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(
            es, "_installed_ha_mcp_version", MagicMock(side_effect=[None, "7.12.1"])
        )

        await mgr._async_ensure_package()

        install_pkg.assert_called_once()
        assert entry.data[DATA_LAST_PIP_SPEC] == DEFAULT_PIP_SPEC

    async def test_requirements_not_found_raises_package_error(
        self, tmp_path, monkeypatch
    ):
        # Fast path (unchanged override) but the requirements manager fails ⇒
        # package-kind error.
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_PIP_SPEC: "ha-mcp==7.9.0"},
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: "ha-mcp==7.9.0"},
        )
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.9.0")
        monkeypatch.setattr(
            es,
            "async_process_requirements",
            AsyncMock(side_effect=RequirementsNotFound("ha_mcp_tools", ["ha-mcp"])),
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

    async def test_legacy_server_is_removed_before_install(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(tmp_path)
        install_pkg = MagicMock(return_value=True)
        uninstall = MagicMock(return_value=True)
        monkeypatch.setattr(es, "install_package", install_pkg)
        monkeypatch.setattr(
            es,
            "pip_kwargs",
            lambda cfg: {"target": "/config/deps", "constraints": "/constraints"},
        )
        monkeypatch.setattr(
            es,
            "_installed_ha_mcp_version",
            MagicMock(side_effect=["6.2.0", "7.12.1"]),
        )
        monkeypatch.setattr(
            es, "_dist_installed", lambda name: name == DIST_NAME_STABLE
        )
        monkeypatch.setattr(es, "_uninstall_distribution", uninstall)

        await mgr._async_ensure_package()

        uninstall.assert_called_once_with(DIST_NAME_STABLE, target="/config/deps")
        install_pkg.assert_called_once()

    async def test_legacy_server_overrides_disabled_auto_update_with_bare_stored_spec(
        self, tmp_path, monkeypatch
    ):
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_AUTO_UPDATE: False},
            data={
                DATA_SECRET_PATH: "/p",
                DATA_LAST_PIP_SPEC: DIST_NAME_STABLE,
            },
        )
        install_pkg = MagicMock(return_value=True)
        uninstall = MagicMock(return_value=True)
        monkeypatch.setattr(es, "install_package", install_pkg)
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(
            es,
            "_installed_ha_mcp_version",
            MagicMock(side_effect=["6.2.0", "7.12.1"]),
        )
        monkeypatch.setattr(es, "_installed_dist_version", lambda name: "6.2.0")
        monkeypatch.setattr(
            es, "_dist_installed", lambda name: name == DIST_NAME_STABLE
        )
        monkeypatch.setattr(es, "_uninstall_distribution", uninstall)

        await mgr._async_ensure_package()

        assert mgr._pip_spec == DIST_NAME_STABLE
        uninstall.assert_called_once_with(DIST_NAME_STABLE)
        assert install_pkg.call_args.args[0] == DIST_NAME_STABLE

    async def test_post_install_legacy_version_is_rejected(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(es, "install_package", MagicMock(return_value=True))
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(
            es,
            "_installed_ha_mcp_version",
            MagicMock(side_effect=["6.2.0", "6.2.0"]),
        )
        monkeypatch.setattr(es, "_dist_installed", lambda name: False)
        monkeypatch.setattr(es, "_uninstall_distribution", MagicMock(return_value=True))

        with pytest.raises(es.EmbeddedServerError) as exc:
            await mgr._async_ensure_package()

        assert exc.value.kind == "package"
        assert "installed ha-mcp 6.2.0" in str(exc.value)
        assert f"requires {es.MIN_EMBEDDED_SERVER_VERSION} or newer" in str(exc.value)
        assert "Home Assistant 2026.6.0 or newer" in str(exc.value)
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
        monkeypatch.setattr(
            es, "_installed_ha_mcp_version", lambda preferred=None: "7.12.1.dev5"
        )
        monkeypatch.setattr(es, "_dist_installed", lambda name: False)
        uninstall = MagicMock()
        monkeypatch.setattr(es, "_uninstall_distribution", uninstall)

        await mgr._async_ensure_package()

        proc.assert_not_awaited()
        install_pkg.assert_called_once()
        assert install_pkg.call_args.args[0] == DEV_PIP_SPEC
        uninstall.assert_not_called()  # the other dist was absent

    async def test_dev_install_validates_target_when_stale_stable_metadata_remains(
        self, tmp_path, monkeypatch
    ):
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_CHANNEL: CHANNEL_DEV},
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: DEFAULT_PIP_SPEC},
        )
        install_pkg = MagicMock(return_value=True)
        uninstall = MagicMock(return_value=False)

        def installed_version(preferred_dist=None):
            if preferred_dist == DIST_NAME_DEV:
                return "7.12.1.dev5"
            return "6.2.0"

        monkeypatch.setattr(es, "install_package", install_pkg)
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(es, "_installed_ha_mcp_version", installed_version)
        monkeypatch.setattr(
            es, "_dist_installed", lambda name: name == DIST_NAME_STABLE
        )
        monkeypatch.setattr(es, "_uninstall_distribution", uninstall)

        await mgr._async_ensure_package()

        uninstall.assert_called_once_with(DIST_NAME_STABLE)
        install_pkg.assert_called_once()

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
        monkeypatch.setattr(
            es, "_installed_ha_mcp_version", lambda preferred=None: "7.12.1"
        )
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
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.12.1")
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
            options={OPT_CHANNEL: CHANNEL_DEV, OPT_PIP_SPEC: "ha-mcp==7.11.0"},
            data={DATA_SECRET_PATH: "/p"},
        )
        monkeypatch.setattr(es, "install_package", MagicMock(return_value=True))
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.11.0")
        monkeypatch.setattr(es, "_dist_installed", lambda name: True)
        uninstall = MagicMock()
        monkeypatch.setattr(es, "_uninstall_distribution", uninstall)

        await mgr._async_ensure_package()
        uninstall.assert_not_called()


# ---------------------------------------------------------------------------
# Pending-install marker (issue #1760): the update entity's Install button
# ---------------------------------------------------------------------------


class TestPendingInstallMarker:
    async def test_pinned_to_pending_version_regardless_of_auto_update(
        self, tmp_path, monkeypatch
    ):
        # auto_update ON (default): the pending marker still forces a PINNED
        # install, not the unpinned, auto-updating channel spec.
        mgr, _hass, _entry = _manager(
            tmp_path,
            data={DATA_SECRET_PATH: "/p", DATA_PENDING_INSTALL_VERSION: "7.11.0"},
        )
        install_pkg = MagicMock(return_value=True)
        monkeypatch.setattr(es, "install_package", install_pkg)
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.11.0")
        monkeypatch.setattr(es, "_dist_installed", lambda name: False)
        monkeypatch.setattr(es, "_uninstall_distribution", MagicMock())

        await mgr._async_ensure_package()

        assert mgr._pip_spec == f"{DIST_NAME_STABLE}==7.11.0"
        install_pkg.assert_called_once()
        assert install_pkg.call_args.args[0] == f"{DIST_NAME_STABLE}==7.11.0"

    async def test_pinned_to_pending_version_overrides_auto_update_off_repin(
        self, tmp_path, monkeypatch
    ):
        # auto_update OFF would normally re-pin to the CURRENTLY installed
        # version (the whole reason the marker exists — see async_install's
        # docstring: a bare reload would otherwise be a no-op). The pending
        # marker must win over that re-pin.
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_AUTO_UPDATE: False},
            data={DATA_SECRET_PATH: "/p", DATA_PENDING_INSTALL_VERSION: "7.12.1"},
        )
        install_pkg = MagicMock(return_value=True)
        monkeypatch.setattr(es, "install_package", install_pkg)
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        # Currently-installed version differs from the requested pending one -
        # proves the marker, not the auto-update-off re-pin, decided the spec.
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.11.0")
        monkeypatch.setattr(es, "_installed_dist_version", lambda dist: "7.11.0")
        monkeypatch.setattr(es, "_dist_installed", lambda name: False)
        monkeypatch.setattr(es, "_uninstall_distribution", MagicMock())

        await mgr._async_ensure_package()

        assert mgr._pip_spec == f"{DIST_NAME_STABLE}==7.12.1"

    async def test_explicit_override_wins_over_pending_marker(
        self, tmp_path, monkeypatch
    ):
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_PIP_SPEC: "ha-mcp==7.10.0"},
            data={
                DATA_SECRET_PATH: "/p",
                DATA_PENDING_INSTALL_VERSION: "7.12.1",
                DATA_LAST_PIP_SPEC: "ha-mcp==7.10.0",
            },
        )
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.10.0")

        await mgr._async_ensure_package()

        assert mgr._pip_spec == "ha-mcp==7.10.0"

    async def test_marker_cleared_after_successful_install(self, tmp_path, monkeypatch):
        mgr, _hass, entry = _manager(
            tmp_path,
            data={DATA_SECRET_PATH: "/p", DATA_PENDING_INSTALL_VERSION: "7.12.1"},
        )
        monkeypatch.setattr(es, "install_package", MagicMock(return_value=True))
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "7.12.1")
        monkeypatch.setattr(es, "_dist_installed", lambda name: False)
        monkeypatch.setattr(es, "_uninstall_distribution", MagicMock())

        await mgr._async_ensure_package()

        assert DATA_PENDING_INSTALL_VERSION not in entry.data

    async def test_marker_cleared_even_when_install_fails(self, tmp_path, monkeypatch):
        # Review finding: the marker is consumed BEFORE the install attempt
        # (one-shot means one ATTEMPT, not "until it succeeds"). Clearing only
        # on success would let a marker for a failing version re-pin every
        # later reload - including the periodic auto-update ones - to that
        # same broken version, looping the failure forever.
        mgr, _hass, entry = _manager(
            tmp_path,
            data={DATA_SECRET_PATH: "/p", DATA_PENDING_INSTALL_VERSION: "7.12.1"},
        )
        monkeypatch.setattr(es, "install_package", MagicMock(return_value=False))
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: None)

        with pytest.raises(es.EmbeddedServerError):
            await mgr._async_ensure_package()

        assert DATA_PENDING_INSTALL_VERSION not in entry.data


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

    def test_uninstall_targets_same_dependency_directory(self, monkeypatch):
        calls = {}

        def _run(args, **kwargs):
            calls["args"] = args
            return SimpleNamespace(returncode=0, stderr="")

        monkeypatch.setattr(es.subprocess, "run", _run)

        assert (
            es._uninstall_distribution(DIST_NAME_STABLE, target="/config/deps") is True
        )

        args = calls["args"]
        assert "--target" in args
        assert args[args.index("--target") + 1] == "/config/deps"
        assert "--python" not in args

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

    def test_prefers_requested_dist(self, monkeypatch):
        monkeypatch.setattr(
            importlib.metadata,
            "version",
            lambda name: "7.9.0.dev5" if name == DIST_NAME_DEV else "6.2.0",
        )
        assert es._installed_ha_mcp_version(DIST_NAME_DEV) == "7.9.0.dev5"

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


class TestInstalledDistVersion:
    """``_installed_dist_version`` pins a SINGLE distribution name (the channel's),
    unlike ``_installed_ha_mcp_version`` which reports whichever is present — the
    auto-update check must compare against the version of the channel installed.
    """

    def test_returns_version_of_named_dist(self, monkeypatch):
        monkeypatch.setattr(
            importlib.metadata,
            "version",
            lambda name: "7.9.0.dev5" if name == DIST_NAME_DEV else "7.9.0",
        )
        assert es._installed_dist_version(DIST_NAME_DEV) == "7.9.0.dev5"
        assert es._installed_dist_version(DIST_NAME_STABLE) == "7.9.0"

    def test_returns_none_when_named_dist_absent(self, monkeypatch):
        def _version(name):
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "version", _version)
        assert es._installed_dist_version(DIST_NAME_STABLE) is None


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

        async def _fake_serve(_token, _stop_event):
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

    def test_serve_resets_cached_settings_before_registering_connection(
        self, tmp_path, monkeypatch
    ):
        # Entry-reload parity with an add-on restart (live-found): the same
        # Python process keeps ha_mcp imported, so without an explicit reset
        # the settings singleton built on the FIRST start serves stale
        # feature-flag/override values to every later start. _serve must call
        # reset_global_settings() BEFORE set_embedded_connection.
        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_SERVER_URL: "http://ha.local:8123"}
        )
        order: list[str] = []
        ha_mcp_mod = ModuleType("ha_mcp")
        ha_mcp_config = ModuleType("ha_mcp.config")
        ha_mcp_config.reset_global_settings = lambda: order.append("reset")
        ha_mcp_config.set_embedded_connection = lambda url, tok: order.append("connect")
        monkeypatch.setitem(sys.modules, "ha_mcp", ha_mcp_mod)
        monkeypatch.setitem(sys.modules, "ha_mcp.config", ha_mcp_config)
        monkeypatch.delitem(sys.modules, "ha_mcp.server", raising=False)

        mgr._thread_main("tok-xyz")

        assert order == ["reset", "connect"]

    def test_serve_falls_back_to_private_reset_seam(self, tmp_path, monkeypatch):
        # Releases predating the public alias only have _reset_global_settings;
        # the manager must still reset (this is what runs against ha-mcp 7.9.0).
        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_SERVER_URL: "http://ha.local:8123"}
        )
        private_reset = MagicMock(name="_reset_global_settings")
        ha_mcp_mod = ModuleType("ha_mcp")
        ha_mcp_config = ModuleType("ha_mcp.config")
        ha_mcp_config._reset_global_settings = private_reset
        ha_mcp_config.set_embedded_connection = MagicMock()
        monkeypatch.setitem(sys.modules, "ha_mcp", ha_mcp_mod)
        monkeypatch.setitem(sys.modules, "ha_mcp.config", ha_mcp_config)
        monkeypatch.delitem(sys.modules, "ha_mcp.server", raising=False)

        mgr._thread_main("tok-xyz")

        private_reset.assert_called_once_with()

    @pytest.mark.parametrize(
        ("url", "token", "half"),
        [
            ("__oauth_mode_url__", "real-jwt", "url"),
            ("http://127.0.0.1:8123", "__oauth_mode_token__", "token"),
        ],
    )
    def test_serve_refuses_sentinel_connection_either_half(
        self, tmp_path, monkeypatch, url, token, half
    ):
        # The guard must refuse to serve when EITHER half of the in-memory
        # channel resolved to a sentinel (review finding: only the URL half
        # raised while the log already computed the token half). Fake the
        # whole ha_mcp surface so _serve reaches the guard hermetically.
        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_SERVER_URL: "http://ha.local:8123"}
        )
        settings = SimpleNamespace(homeassistant_url=url, homeassistant_token=token)
        ha_mcp_mod = ModuleType("ha_mcp")
        ha_mcp_mod.__path__ = []  # package semantics for submodule imports
        cfg = ModuleType("ha_mcp.config")
        cfg.reset_global_settings = lambda: None
        cfg.set_embedded_connection = lambda u, t: None
        cfg.OAUTH_MODE_URL = "__oauth_mode_url__"
        cfg.OAUTH_MODE_TOKEN = "__oauth_mode_token__"
        cfg.get_global_settings = lambda: settings
        server_mod = ModuleType("ha_mcp.server")
        server_mod.HomeAssistantSmartMCPServer = lambda: SimpleNamespace(mcp=None)
        ui_mod = ModuleType("ha_mcp.settings_ui")
        ui_mod.register_settings_routes = lambda *a, **k: pytest.fail(
            f"served despite sentinel {half}"
        )
        ha_mcp_mod.config = cfg
        ha_mcp_mod.server = server_mod
        ha_mcp_mod.settings_ui = ui_mod
        monkeypatch.setitem(sys.modules, "ha_mcp", ha_mcp_mod)
        monkeypatch.setitem(sys.modules, "ha_mcp.config", cfg)
        monkeypatch.setitem(sys.modules, "ha_mcp.server", server_mod)
        monkeypatch.setitem(sys.modules, "ha_mcp.settings_ui", ui_mod)

        mgr._thread_main("tok-xyz")

        assert isinstance(mgr._thread_exc, es.EmbeddedServerError)
        assert "sentinel" in str(mgr._thread_exc).lower()

    def test_thread_crash_is_captured_not_raised(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(tmp_path)

        async def _boom(_token, _stop_event):
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
        # Only the REUSE ids are persisted; the access token stays in
        # memory (review finding: it was stored but never read, leaving an
        # unused admin JWT at rest + a config-entry rewrite every start).
        assert entry.data[DATA_SERVER_USER_ID] == "new-user"
        assert entry.data[DATA_REFRESH_TOKEN_ID] == "rt-new"
        assert DATA_ACCESS_TOKEN not in entry.data

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

    async def test_wait_ready_returns_when_probe_succeeds(self, tmp_path, monkeypatch):
        # The SUCCESS path (review gap): live thread + successful port probe
        # returns normally - no raise, no repair issue.
        mgr, hass, _entry = _manager(tmp_path)
        hass.loop.time = MagicMock(return_value=0.0)
        mgr._thread = SimpleNamespace(is_alive=lambda: True)

        async def _probe():
            return True

        monkeypatch.setattr(mgr, "_async_probe_port", _probe)
        await mgr._async_wait_until_ready()  # must not raise
        assert mgr._thread_exc is None

    def test_serve_surfaces_self_exited_server(self, tmp_path, monkeypatch):
        # The race branch that mirrors the live EADDRINUSE bug (review gap):
        # when uvicorn's serve() exits on its own (bind failure) while the
        # stop event was never set, the failure must propagate to
        # _thread_exc, not be swallowed by the wait/cancel choreography.
        #
        # _thread_main stages HA_MCP_CONFIG_DIR/HA_MCP_EMBEDDED into
        # os.environ; this class has no _isolate_env fixture and
        # monkeypatch.delenv on an ABSENT key snapshots nothing (verified), so
        # restore explicitly or the flag leaks into unrelated suites on this
        # worker (live-found: flipped is_running_in_addon() for test_errors).
        _saved = {
            k: os.environ.get(k) for k in ("HA_MCP_CONFIG_DIR", "HA_MCP_EMBEDDED")
        }

        def _restore_env():
            for k, v in _saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_SERVER_URL: "http://ha.local:8123"}
        )
        from contextlib import asynccontextmanager

        settings = SimpleNamespace(
            homeassistant_url="http://127.0.0.1:8123", homeassistant_token="jwt"
        )
        ha_mcp_mod = ModuleType("ha_mcp")
        ha_mcp_mod.__path__ = []
        cfg = ModuleType("ha_mcp.config")
        cfg.reset_global_settings = lambda: None
        cfg.set_embedded_connection = lambda u, t: None
        cfg.OAUTH_MODE_URL = "__sentinel_url__"
        cfg.OAUTH_MODE_TOKEN = "__sentinel_token__"
        cfg.get_global_settings = lambda: settings

        @asynccontextmanager
        async def _lifespan():
            yield

        class _FakeMcp:
            def http_app(self, path, stateless_http):
                return object()

            _lifespan_manager = staticmethod(_lifespan)

        server_mod = ModuleType("ha_mcp.server")
        server_mod.HomeAssistantSmartMCPServer = lambda: SimpleNamespace(mcp=_FakeMcp())
        ui_mod = ModuleType("ha_mcp.settings_ui")
        ui_mod.register_settings_routes = lambda *a, **k: None

        class _FakeUvServer:
            def __init__(self, config):
                self.should_exit = False

            async def serve(self):
                raise OSError(98, "address already in use")

        uvicorn_mod = ModuleType("uvicorn")
        uvicorn_mod.Config = lambda *a, **k: SimpleNamespace()
        uvicorn_mod.Server = _FakeUvServer

        for name, mod in (
            ("ha_mcp", ha_mcp_mod),
            ("ha_mcp.config", cfg),
            ("ha_mcp.server", server_mod),
            ("ha_mcp.settings_ui", ui_mod),
            ("uvicorn", uvicorn_mod),
        ):
            monkeypatch.setitem(sys.modules, name, mod)
        ha_mcp_mod.config = cfg
        ha_mcp_mod.server = server_mod
        ha_mcp_mod.settings_ui = ui_mod
        # Inject an attributeless ha_mcp.browser_landing so _serve's landing
        # import raises ImportError and is skipped (this test is about the
        # OSError choreography). A sys.modules hit is the only hermetic way:
        # without it, the editable install's meta-path finder resolves the
        # REAL module by name and its register call would hit _FakeMcp
        # (no custom_route), masking the OSError (live-found in CI).
        monkeypatch.setitem(
            sys.modules,
            "ha_mcp.browser_landing",
            ModuleType("ha_mcp.browser_landing"),
        )

        try:
            mgr._thread_main("tok")
        finally:
            _restore_env()

        assert isinstance(mgr._thread_exc, OSError)
        assert "address already in use" in str(mgr._thread_exc)

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
# _serve browser-landing registration
# ---------------------------------------------------------------------------


class TestServeBrowserLanding:
    @pytest.fixture(autouse=True)
    def _isolate_env(self):
        # _thread_main stages HA_MCP_CONFIG_DIR/HA_MCP_EMBEDDED into os.environ;
        # snapshot + restore so the flags never leak into unrelated suites on
        # this worker (as TestThreadEnvStaging documents).
        keys = ("HA_MCP_CONFIG_DIR", "HA_MCP_EMBEDDED")
        saved = {k: os.environ.get(k) for k in keys}
        for key in keys:
            os.environ.pop(key, None)
        yield
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_serve_registers_browser_landing(self, tmp_path, monkeypatch):
        # Parity with the CLI HTTP runner (the reported bug): _serve must register
        # the friendly browser landing on the MCP app so a browser GET — direct or
        # forwarded by the ingress webhook — sees setup guidance, not a bare 405.
        # Stop _serve right after by having http_app raise.
        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_SERVER_URL: "http://ha.local:8123"}
        )

        class _StopServe(Exception):
            pass

        class _FakeMcp:
            def http_app(self, path, stateless_http):
                raise _StopServe

        fake_mcp = _FakeMcp()
        landing_calls: list = []
        landing_mod = ModuleType("ha_mcp.browser_landing")
        landing_mod.register_browser_landing = lambda mcp, path: landing_calls.append(
            (mcp, path)
        )
        _stub_ha_mcp_surface(monkeypatch, mcp=fake_mcp, landing_mod=landing_mod)

        mgr._thread_main("tok")

        # Registered on the real MCP app, at the server's secret path.
        assert landing_calls == [(fake_mcp, "/private_secret")]
        assert isinstance(mgr._thread_exc, _StopServe)

    def test_serve_tolerates_missing_browser_landing_module(
        self, tmp_path, monkeypatch
    ):
        # Backward-compat: an OLDER bundled ha-mcp (the component reaches users
        # ahead of the server) has no browser_landing module. _serve must swallow
        # the ImportError and keep serving — the landing is simply absent, as today.
        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_SERVER_URL: "http://ha.local:8123"}
        )

        class _StopServe(Exception):
            pass

        reached: list = []

        class _FakeMcp:
            def http_app(self, path, stateless_http):
                reached.append(path)
                raise _StopServe

        # landing_mod omitted ⇒ ha_mcp.browser_landing is absent (import fails).
        _stub_ha_mcp_surface(monkeypatch, mcp=_FakeMcp())

        mgr._thread_main("tok")

        # _serve got PAST the failed landing import to build the app (ImportError
        # was swallowed, not propagated).
        assert reached == ["/private_secret"]
        assert isinstance(mgr._thread_exc, _StopServe)


# ---------------------------------------------------------------------------
# start / stop lifecycle + idempotency
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_start_requires_secret_path(self, tmp_path):
        mgr, _hass, _entry = _manager(tmp_path, data={})  # no DATA_SECRET_PATH
        with pytest.raises(es.EmbeddedServerError, match="secret path missing"):
            await mgr.async_start()

    async def test_start_rejects_unsupported_home_assistant_before_install(
        self, tmp_path, monkeypatch
    ):
        mgr, _hass, _entry = _manager(tmp_path)
        ensure = AsyncMock()
        monkeypatch.setattr(es, "HA_VERSION", "2025.9.4")
        monkeypatch.setattr(mgr, "_async_ensure_package", ensure)

        with pytest.raises(
            es.EmbeddedServerError,
            match=r"requires Home Assistant 2026\.6\.0 or newer",
        ) as exc:
            await mgr.async_start()

        assert exc.value.kind == "package"
        ensure.assert_not_awaited()

    async def test_start_rejects_invalid_home_assistant_version_before_install(
        self, tmp_path, monkeypatch
    ):
        mgr, _hass, _entry = _manager(tmp_path)
        ensure = AsyncMock()
        monkeypatch.setattr(es, "HA_VERSION", "custom-build")
        monkeypatch.setattr(mgr, "_async_ensure_package", ensure)

        with pytest.raises(
            es.EmbeddedServerError,
            match="could not determine whether Home Assistant custom-build",
        ) as exc:
            await mgr.async_start()

        assert exc.value.kind == "package"
        ensure.assert_not_awaited()

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
        monkeypatch.setattr(es, "_purge_ha_mcp_modules", lambda: calls.append("purge"))
        # Replace the thread body so no real ha_mcp import happens.
        started = []
        monkeypatch.setattr(mgr, "_thread_main", lambda token: started.append(token))

        await mgr.async_start()
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)

        # The module purge must land between the pip install (so the fresh
        # code is on disk) and the thread spawn (so the worker's import
        # resolves from disk, not the process-wide module cache).
        assert calls == ["ensure", "token", "dir", "purge", "ready"]
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
        # Worker state fully cleared even for the orphaned thread...
        assert mgr._thread is None
        assert mgr._loop is None
        assert mgr._stop_event is None
        assert mgr._thread_exc is None
        # ...but the zombie itself is REMEMBERED so the next start skips
        # the module purge while it may still be importing.
        assert mgr._orphaned_thread is not None

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


class TestPurgeHaMcpModules:
    """The stale-worker fix: cached ha_mcp modules are dropped per start.

    Regression guard for the live-found bug where an entry reload
    reinstalled the package but the new worker silently reused the OLD
    code from ``sys.modules`` — updates only took effect after a full HA
    core restart.
    """

    @pytest.fixture(autouse=True)
    def _preserve_real_modules(self):
        """Restore any genuinely imported ha_mcp modules after each test.

        Other unit tests in the same pytest session import the real
        ``ha_mcp``; purging it here without restoring would change module
        identity for everything that runs afterwards.
        """
        saved = {
            name: mod
            for name, mod in sys.modules.items()
            if name == "ha_mcp" or name.startswith("ha_mcp.")
        }
        yield
        sys.modules.update(saved)

    def test_purges_only_ha_mcp_modules(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "ha_mcp", ModuleType("ha_mcp"))
        monkeypatch.setitem(sys.modules, "ha_mcp.config", ModuleType("ha_mcp.config"))
        unrelated = ModuleType("ha_mcp_other")
        monkeypatch.setitem(sys.modules, "ha_mcp_other", unrelated)

        es._purge_ha_mcp_modules()

        assert "ha_mcp" not in sys.modules
        assert "ha_mcp.config" not in sys.modules
        # Prefix match must not swallow lookalike top-level names.
        assert sys.modules["ha_mcp_other"] is unrelated

    def test_noop_when_nothing_cached(self):
        for name in [
            n for n in list(sys.modules) if n == "ha_mcp" or n.startswith("ha_mcp.")
        ]:
            sys.modules.pop(name)
        es._purge_ha_mcp_modules()  # must not raise


class TestRunningVersionStalenessWarning:
    async def test_start_warns_when_running_version_stale(
        self, tmp_path, monkeypatch, caplog
    ):
        mgr, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(mgr, "_async_ensure_package", AsyncMock())
        monkeypatch.setattr(
            mgr, "_async_provision_token", AsyncMock(return_value="tok")
        )
        monkeypatch.setattr(mgr, "_prepare_config_dir", lambda: None)
        # Stub the module purge: letting it run for real would drop every
        # live ha_mcp module and poison later tests in this process.
        monkeypatch.setattr(es, "_purge_ha_mcp_modules", lambda: None)
        monkeypatch.setattr(mgr, "_thread_main", lambda token: None)
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "9.9.9")

        def _ready_with_stale_worker():
            # Deterministic stand-in for the _serve stash: the worker
            # imported an older generation than what pip just installed.
            mgr._running_version = "1.1.1"

        monkeypatch.setattr(
            mgr,
            "_async_wait_until_ready",
            AsyncMock(side_effect=_ready_with_stale_worker),
        )

        with caplog.at_level("WARNING"):
            await mgr.async_start()
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)

        assert "running version 1.1.1" in caplog.text
        assert "restart Home Assistant" in caplog.text

    async def test_start_quiet_when_versions_match(self, tmp_path, monkeypatch, caplog):
        mgr, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(mgr, "_async_ensure_package", AsyncMock())
        monkeypatch.setattr(
            mgr, "_async_provision_token", AsyncMock(return_value="tok")
        )
        monkeypatch.setattr(mgr, "_prepare_config_dir", lambda: None)
        # Stub the module purge: letting it run for real would drop every
        # live ha_mcp module and poison later tests in this process.
        monkeypatch.setattr(es, "_purge_ha_mcp_modules", lambda: None)
        monkeypatch.setattr(mgr, "_thread_main", lambda token: None)
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda: "1.1.1")

        def _ready_with_current_worker():
            mgr._running_version = "1.1.1"

        monkeypatch.setattr(
            mgr,
            "_async_wait_until_ready",
            AsyncMock(side_effect=_ready_with_current_worker),
        )

        with caplog.at_level("WARNING"):
            await mgr.async_start()
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)

        assert "restart Home Assistant" not in caplog.text


class TestPurgeSkippedWhileOrphanAlive:
    """A wedged old worker must block the module purge, not crash it.

    Live-found on QEMU-slow HAOS: a cold import outlived both the
    readiness timeout and the stop-join budget; purging sys.modules
    under the still-importing zombie corrupted its import and the next
    bring-up never came up.
    """

    def _start_kwargs(self, mgr, monkeypatch, purges):
        monkeypatch.setattr(mgr, "_async_ensure_package", AsyncMock())
        monkeypatch.setattr(
            mgr, "_async_provision_token", AsyncMock(return_value="tok")
        )
        monkeypatch.setattr(mgr, "_prepare_config_dir", lambda: None)
        monkeypatch.setattr(mgr, "_async_wait_until_ready", AsyncMock())
        monkeypatch.setattr(mgr, "_thread_main", lambda token: None)
        monkeypatch.setattr(es, "_purge_ha_mcp_modules", lambda: purges.append(True))

    async def test_purge_skipped_when_orphan_still_alive(
        self, tmp_path, monkeypatch, caplog
    ):
        mgr, _hass, _entry = _manager(tmp_path)
        purges: list[bool] = []
        self._start_kwargs(mgr, monkeypatch, purges)

        class _AliveThread:
            def is_alive(self):
                return True

        mgr._orphaned_thread = _AliveThread()
        with caplog.at_level("WARNING"):
            await mgr.async_start()
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)

        assert purges == []
        assert "Skipping the ha_mcp module purge" in caplog.text
        assert mgr._orphaned_thread is not None  # still tracked

    async def test_purge_resumes_once_orphan_died(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(tmp_path)
        purges: list[bool] = []
        self._start_kwargs(mgr, monkeypatch, purges)

        class _DeadThread:
            def is_alive(self):
                return False

        mgr._orphaned_thread = _DeadThread()
        await mgr.async_start()
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)

        assert purges == [True]
        assert mgr._orphaned_thread is None  # bookkeeping cleared


class TestServeRunningVersionCapture:
    @pytest.fixture(autouse=True)
    def _isolate_env(self):
        # _thread_main stages HA_MCP_CONFIG_DIR/HA_MCP_EMBEDDED into
        # os.environ; snapshot + restore so the flags never leak into
        # unrelated suites on this worker.
        keys = ("HA_MCP_CONFIG_DIR", "HA_MCP_EMBEDDED")
        saved = {k: os.environ.get(k) for k in keys}
        for key in keys:
            os.environ.pop(key, None)
        yield
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_serve_captures_imported_version(self, tmp_path, monkeypatch):
        # The staleness feature hinges on this one line: the worker must
        # stash the __version__ of the ha_mcp it ACTUALLY imported.
        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_SERVER_URL: "http://ha.local:8123"}
        )

        class _StopServe(Exception):
            pass

        class _FakeMcp:
            def http_app(self, path, stateless_http):
                raise _StopServe

        _stub_ha_mcp_surface(monkeypatch, mcp=_FakeMcp())
        sys.modules["ha_mcp"].__version__ = "9.8.7"

        mgr._thread_main("tok")

        assert mgr._running_version == "9.8.7"
        assert isinstance(mgr._thread_exc, _StopServe)
