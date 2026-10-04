"""Unit tests for :class:`EmbeddedServerManager` (issue #1527).

Covers package-ensure gating, worker-thread env staging, HA-token provisioning
(create / reuse / revoke), the readiness probe, and start/stop idempotency.

Home Assistant and aiohttp are stubbed via ``_embedded_stubs`` (imported first so
the fakes are installed before the component modules bind them). ``ha_mcp`` is
never imported here — the manager only imports it inside the worker thread, which
these tests never actually run.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlparse

import pytest

from ._embedded_stubs import install

# Install stubs + put the component package on sys.path before importing the
# integration modules below. Also an isort barrier so the imports below are never
# reordered above it (which would import embedded_server before the stubs exist).
install()

import custom_components.ha_mcp_tools.embedded_server as es  # noqa: E402

# HA's wheels index, as a HOST — the installer's extra-index retry drops a
# failing index by parsed hostname, and the fakes below match it the same way.
_WHEELS_HOST = "wheels.home-assistant.io"
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    CHANNEL_DEV,
    DATA_ACCESS_TOKEN,
    DATA_LAST_PIP_SPEC,
    DATA_REFRESH_TOKEN_ID,
    DATA_SECRET_PATH,
    DATA_SERVER_USER_ID,
    DEFAULT_PIP_SPEC,
    DIST_NAME_DEV,
    DIST_NAME_STABLE,
    OPT_BIND_HOST,
    OPT_CHANNEL,
    OPT_ENABLE_LLM_API,
    OPT_PIP_SPEC,
    OPT_SERVER_PORT,
    OPT_SERVER_URL,
    SERVER_TOKEN_CLIENT_NAME,
)

# GROUP_ID_ADMIN / the LLAT token-type come from the homeassistant stub the
# manager imports; the string values are pinned in _embedded_stubs.
_GROUP_ID_ADMIN = es.GROUP_ID_ADMIN
_TOKEN_TYPE_LLAT = es.TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN


@pytest.fixture(autouse=True)
def _sound_installed_dependency_graph(monkeypatch):
    """Start-up and import-failure paths audit the installed dependency graph.

    Unaudited, that audit walks the metadata of every package in the test
    runner's own environment. These tests supply a graph with no violations;
    the audit itself is covered by ``test_dependency_diagnostics.py``.
    """
    monkeypatch.setattr(es, "audit_dependency_graph", lambda _root: [])


_REAL_PAIRED_SERVER_REQUIREMENT = es._async_paired_server_requirement


@pytest.fixture(autouse=True)
def _no_manifest_pin(monkeypatch):
    """``async_start`` reads the manifest pin through the HA loader (a stub
    here); default to a source checkout's "no pin". Tests that need a pin
    patch it via ``_paired``; ``TestPairedServerRequirement`` calls the real
    reader."""
    monkeypatch.setattr(
        es, "_async_paired_server_requirement", AsyncMock(return_value=None)
    )


def _make_hass(tmp_path) -> MagicMock:
    hass = MagicMock(name="hass")
    hass.config.skip_pip = False
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


def _stub_ha_mcp_surface(
    monkeypatch, *, mcp, landing_mod=None, log_filters_mod=None
) -> None:
    """Install a minimal in-memory ``ha_mcp`` package so ``_serve`` runs hermetically.

    Wires a non-sentinel connection (so ``_serve`` passes its refuse-to-serve
    guard), a server whose ``.mcp`` is ``mcp``, no-op settings routes, and a stub
    uvicorn. Pass ``landing_mod`` to stub ``ha_mcp.browser_landing`` and
    ``log_filters_mod`` to stub ``ha_mcp.log_filters``; omit either to simulate
    an OLDER installed server without that helper — modeled as a module missing
    the ``register_browser_landing`` / ``install_sdk_log_filters`` attribute, so
    the from-import in ``_serve`` raises the same ImportError class its guard
    catches. Injection (a sys.modules hit) is the only hermetic way to force
    that failure: deleting the entry is NOT enough, because the editable install
    (``uv sync``) adds a meta-path finder that resolves ``ha_mcp.*`` by name and
    would re-import the REAL module even though the parent ``ha_mcp`` is faked
    with an empty ``__path__`` (live-found in CI).
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
    if log_filters_mod is None:
        # Older-server stand-in, same reasoning as landing_mod above.
        log_filters_mod = ModuleType("ha_mcp.log_filters")
    ha_mcp_mod.log_filters = log_filters_mod
    mods["ha_mcp.log_filters"] = log_filters_mod
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


class TestLoopbackDerivation:
    """Issue #1890: the default loopback URL honors the http integration's real
    port and SSL configuration (``hass.config.api``) instead of hardcoding
    ``http://127.0.0.1:8123`` — which spoke plaintext into a TLS socket on any
    instance with ``http.ssl_certificate`` configured, killing every HA
    round-trip while the MCP handshake kept working."""

    def test_no_api_object_falls_back_to_constant(self, tmp_path):
        hass = _make_hass(tmp_path)
        hass.config.api = None
        assert es._derive_loopback_url(hass) == ("http://127.0.0.1:8123", None)

    def test_ssl_enabled_derives_https_with_verify_off(self, tmp_path):
        hass = _make_hass(tmp_path)
        hass.config.api = SimpleNamespace(port=8123, use_ssl=True)
        assert es._derive_loopback_url(hass) == ("https://127.0.0.1:8123", False)

    def test_custom_port_is_honored(self, tmp_path):
        hass = _make_hass(tmp_path)
        hass.config.api = SimpleNamespace(port=8444, use_ssl=False)
        assert es._derive_loopback_url(hass) == ("http://127.0.0.1:8444", None)

    def test_unusable_port_falls_back_to_8123(self, tmp_path):
        hass = _make_hass(tmp_path)
        hass.config.api = SimpleNamespace(port=object(), use_ssl=True)
        assert es._derive_loopback_url(hass) == ("https://127.0.0.1:8123", False)

    def test_manager_derives_when_no_override(self, tmp_path):
        hass = _make_hass(tmp_path)
        hass.config.api = SimpleNamespace(port=8443, use_ssl=True)
        mgr = es.EmbeddedServerManager(hass, _make_entry())
        assert mgr._server_url == "https://127.0.0.1:8443"
        assert mgr._loopback_verify_ssl is False

    def test_manager_explicit_override_wins_verbatim(self, tmp_path):
        hass = _make_hass(tmp_path)
        hass.config.api = SimpleNamespace(port=8443, use_ssl=True)
        mgr = es.EmbeddedServerManager(
            hass, _make_entry(options={OPT_SERVER_URL: "http://ha.local:8123/"})
        )
        assert mgr._server_url == "http://ha.local:8123"
        assert mgr._loopback_verify_ssl is None

    def test_manager_treats_stored_default_as_no_override(self, tmp_path):
        # Older options forms pre-filled DEFAULT_LOOPBACK_URL as
        # suggested_value, so entries whose owner never chose an override
        # carry it verbatim — it must not pin the scheme/port.
        hass = _make_hass(tmp_path)
        hass.config.api = SimpleNamespace(port=8123, use_ssl=True)
        mgr = es.EmbeddedServerManager(
            hass, _make_entry(options={OPT_SERVER_URL: "http://127.0.0.1:8123"})
        )
        assert mgr._server_url == "https://127.0.0.1:8123"
        assert mgr._loopback_verify_ssl is False


class _PkgEnv:
    """In-memory stand-in for the installed server distributions.

    ``dists`` maps distribution name -> version. The metadata readers, the
    installer and the uninstaller all operate on it, so a test states what is
    on disk before ``_async_ensure_package`` and asserts what is after.
    """

    def __init__(self, monkeypatch, dists, *, install_version=None, uninstall_ok=True):
        self.dists = dict(dists)
        self.installs: list[tuple[str, dict]] = []
        self.uninstalls: list[str] = []
        self.calls: list[str] = []
        self.install_version = install_version
        self.uninstall_ok = uninstall_ok
        self.install_ok = True
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(es, "_installed_dist_version", self.dists.get)
        monkeypatch.setattr(es, "_dist_installed", lambda name: name in self.dists)
        monkeypatch.setattr(es, "_installed_ha_mcp_version", self._importable)
        monkeypatch.setattr(es, "_force_install_package", self._install)
        monkeypatch.setattr(es, "_uninstall_distribution", self._uninstall)
        monkeypatch.setattr(
            es,
            "async_process_requirements",
            AsyncMock(side_effect=AssertionError("Home Assistant's eager installer")),
        )

    def _importable(self, preferred=None):
        if preferred in self.dists:
            return self.dists[preferred]
        return next(iter(self.dists.values()), None)

    def _install(self, spec, **kwargs):
        self.calls.append("install")
        self.installs.append((spec, kwargs))
        if not self.install_ok:
            return False
        dist = kwargs.get("target_dist") or DIST_NAME_STABLE
        pinned = es._exact_pinned_version(spec)
        self.dists[dist] = self.install_version or pinned or "8.6.0"
        return True

    def _uninstall(self, name, **_kwargs):
        self.calls.append(f"uninstall {name}")
        self.uninstalls.append(name)
        if self.uninstall_ok:
            self.dists.pop(name, None)
        return self.uninstall_ok


def _paired(monkeypatch, requirement):
    """The manifest pin ``async_start`` reads, applied to a manager directly."""

    async def _read(_hass):
        return requirement

    monkeypatch.setattr(es, "_async_paired_server_requirement", _read)


async def _ensure(mgr, **kwargs):
    """Resolve the spec the way async_start does, then ensure the package."""
    mgr._paired_spec = await es._async_paired_server_requirement(mgr._hass)
    mgr._pip_spec = mgr._resolve_pip_spec()
    return await mgr._async_ensure_package(**kwargs)


class TestSpecResolution:
    def test_override_wins_over_the_paired_pin(self, tmp_path):
        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_PIP_SPEC: "ha-mcp @ https://example/t.tgz"}
        )
        mgr._paired_spec = "ha-mcp==8.6.0"
        assert mgr._resolve_pip_spec() == "ha-mcp @ https://example/t.tgz"

    def test_paired_pin_without_override(self, tmp_path):
        mgr, _hass, _entry = _manager(tmp_path)
        mgr._paired_spec = "ha-mcp==8.6.0"
        assert mgr._resolve_pip_spec() == "ha-mcp==8.6.0"

    def test_unpinned_source_checkout_falls_back_to_bare_dist(self, tmp_path):
        mgr, _hass, _entry = _manager(tmp_path)
        assert mgr._resolve_pip_spec() == DEFAULT_PIP_SPEC

    def test_default_pip_spec_is_not_an_override(self, tmp_path):
        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_PIP_SPEC: DEFAULT_PIP_SPEC}
        )
        mgr._paired_spec = "ha-mcp==8.6.0"
        assert mgr._resolve_pip_spec() == "ha-mcp==8.6.0"

    def test_retired_channel_option_is_ignored(self, tmp_path):
        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_CHANNEL: CHANNEL_DEV, "auto_update": False}
        )
        mgr._paired_spec = "ha-mcp==8.6.0"
        mgr._pip_spec = mgr._resolve_pip_spec()
        assert mgr._pip_spec == "ha-mcp==8.6.0"
        assert mgr._conflicting_dist_name() == DIST_NAME_DEV

    @pytest.mark.parametrize(
        ("spec", "target", "conflicting"),
        [
            ("ha-mcp==8.6.0", DIST_NAME_STABLE, DIST_NAME_DEV),
            ("ha-mcp-dev==8.7.0.dev3", DIST_NAME_DEV, DIST_NAME_STABLE),
            ("ha-mcp @ file:///config/w.whl", DIST_NAME_STABLE, DIST_NAME_DEV),
            ("https://example/t.tgz", None, None),
            ("acme-ha-mcp==1.0", None, None),
        ],
    )
    def test_target_and_conflicting_dist(self, tmp_path, spec, target, conflicting):
        mgr, _hass, _entry = _manager(tmp_path, options={OPT_PIP_SPEC: spec})
        assert mgr._target_dist() == target
        assert mgr._conflicting_dist_name() == conflicting


class TestPairedServerRequirement:
    async def _read(self, monkeypatch, requirements):
        integration = SimpleNamespace(requirements=requirements)
        monkeypatch.setattr(
            es, "async_get_integration", AsyncMock(return_value=integration)
        )
        return await _REAL_PAIRED_SERVER_REQUIREMENT(MagicMock())

    async def test_reads_the_ha_mcp_pin(self, monkeypatch):
        assert (
            await self._read(monkeypatch, ["mcp>=1.24.0", "ha-mcp==8.6.0"])
            == "ha-mcp==8.6.0"
        )

    async def test_reads_a_dev_dist_pin(self, monkeypatch):
        assert (
            await self._read(monkeypatch, ["ha_mcp_dev==8.7.0.dev3"])
            == "ha_mcp_dev==8.7.0.dev3"
        )

    async def test_source_checkout_has_no_pin(self, monkeypatch):
        assert await self._read(monkeypatch, ["mcp>=1.24.0"]) is None

    async def test_skips_unparseable_requirements(self, monkeypatch):
        assert (
            await self._read(monkeypatch, ["not a requirement ==", "ha-mcp==8.6.0"])
            == "ha-mcp==8.6.0"
        )


class TestEnsurePairedPackage:
    async def test_pin_core_installed_needs_no_install(self, tmp_path, monkeypatch):
        # Home Assistant installed the manifest pin before setup: the normal
        # start installs nothing and records the spec.
        _paired(monkeypatch, "ha-mcp==8.6.0")
        env = _PkgEnv(monkeypatch, {DIST_NAME_STABLE: "8.6.0"})
        mgr, _hass, entry = _manager(tmp_path)

        assert await _ensure(mgr) == "8.6.0"

        assert env.installs == []
        assert env.uninstalls == []
        assert entry.data[DATA_LAST_PIP_SPEC] == "ha-mcp==8.6.0"

    async def test_pin_unchanged_writes_nothing(self, tmp_path, monkeypatch):
        _paired(monkeypatch, "ha-mcp==8.6.0")
        env = _PkgEnv(monkeypatch, {DIST_NAME_STABLE: "8.6.0"})
        mgr, hass, _entry = _manager(
            tmp_path, data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: "ha-mcp==8.6.0"}
        )

        await _ensure(mgr)

        assert env.installs == []
        hass.config_entries.async_update_entry.assert_not_called()

    async def test_bare_stored_spec_from_2x_counts_as_satisfied(
        self, tmp_path, monkeypatch
    ):
        # A component 2.x stable-channel install stored the bare dist; the
        # same distribution from the same index — nothing to replace.
        _paired(monkeypatch, "ha-mcp==8.6.0")
        env = _PkgEnv(monkeypatch, {DIST_NAME_STABLE: "8.6.0"})
        mgr, _hass, entry = _manager(
            tmp_path, data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: "ha-mcp"}
        )

        await _ensure(mgr)

        assert env.installs == []
        assert entry.data[DATA_LAST_PIP_SPEC] == "ha-mcp==8.6.0"

    async def test_unsatisfied_pin_upgrades_only_our_dist(self, tmp_path, monkeypatch):
        _paired(monkeypatch, "ha-mcp==8.6.0")
        env = _PkgEnv(monkeypatch, {DIST_NAME_STABLE: "8.5.0"})
        mgr, _hass, _entry = _manager(tmp_path)

        assert await _ensure(mgr) == "8.6.0"

        [(spec, kwargs)] = env.installs
        assert spec == "ha-mcp==8.6.0"
        assert kwargs["target_dist"] == DIST_NAME_STABLE
        assert kwargs["reinstall"] is False

    async def test_other_dist_beside_the_pin_is_removed_then_reinstalled(
        self, tmp_path, monkeypatch
    ):
        # Reproduced on Core 2026.10.0b0: both distributions own the ha_mcp
        # files, so removing the other one deletes the target's code too. The
        # target must then be REINSTALLED, not upgraded (a version-satisfied
        # upgrade is a no-op and left "No module named 'ha_mcp.config'").
        _paired(monkeypatch, "ha-mcp==8.6.0")
        env = _PkgEnv(
            monkeypatch, {DIST_NAME_STABLE: "8.6.0", DIST_NAME_DEV: "8.6.0.dev2893"}
        )
        mgr, _hass, entry = _manager(
            tmp_path,
            options={OPT_CHANNEL: CHANNEL_DEV},
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: "ha-mcp-dev"},
        )

        await _ensure(mgr)

        # Exactly one removal (the other dist) and no #1914 removal of the
        # target: the reinstall replaces its files whatever the version says.
        assert env.calls == [f"uninstall {DIST_NAME_DEV}", "install"]
        [(spec, kwargs)] = env.installs
        assert spec == "ha-mcp==8.6.0"
        assert kwargs["reinstall"] is True
        assert entry.data[DATA_LAST_PIP_SPEC] == "ha-mcp==8.6.0"

    async def test_override_on_the_other_dist_removes_the_pin_dist(
        self, tmp_path, monkeypatch
    ):
        # Home Assistant put the manifest pin back at boot; a dev-dist
        # override then replaces it the same way.
        _paired(monkeypatch, "ha-mcp==8.6.0")
        env = _PkgEnv(monkeypatch, {DIST_NAME_STABLE: "8.6.0"})
        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_PIP_SPEC: "ha-mcp-dev==8.7.0.dev3"}
        )

        assert await _ensure(mgr) == "8.7.0.dev3"

        assert env.uninstalls == [DIST_NAME_STABLE]
        [(spec, kwargs)] = env.installs
        assert spec == "ha-mcp-dev==8.7.0.dev3"
        assert kwargs["target_dist"] == DIST_NAME_DEV

    async def test_satisfied_index_override_needs_no_install(
        self, tmp_path, monkeypatch
    ):
        _paired(monkeypatch, "ha-mcp==8.6.0")
        env = _PkgEnv(monkeypatch, {DIST_NAME_STABLE: "8.5.0"})
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_PIP_SPEC: "ha-mcp==8.5.0"},
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: "ha-mcp==8.5.0"},
        )

        await _ensure(mgr)

        assert env.installs == []

    async def test_override_reapplied_after_core_restored_the_pin(
        self, tmp_path, monkeypatch
    ):
        # An HA restart with an override set: Core reinstalled the manifest pin
        # at boot, so the stored override no longer matches what is on disk.
        _paired(monkeypatch, "ha-mcp==8.6.0")
        env = _PkgEnv(monkeypatch, {DIST_NAME_STABLE: "8.6.0"})
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_PIP_SPEC: "ha-mcp==8.5.0"},
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: "ha-mcp==8.5.0"},
        )

        assert await _ensure(mgr) == "8.5.0"

        [(spec, _kwargs)] = env.installs
        assert spec == "ha-mcp==8.5.0"

    async def test_url_override_is_always_reinstalled(self, tmp_path, monkeypatch):
        wheel = "ha-mcp @ file:///config/ha_mcp-8.6.0-py3-none-any.whl"
        _paired(monkeypatch, "ha-mcp==8.6.0")
        env = _PkgEnv(monkeypatch, {DIST_NAME_STABLE: "8.6.0"})
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_PIP_SPEC: wheel},
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: wheel},
        )

        await _ensure(mgr)

        [(spec, _kwargs)] = env.installs
        assert spec == wheel
        assert env.uninstalls == []

    async def test_cleared_url_override_replaces_the_same_version(
        self, tmp_path, monkeypatch
    ):
        # Issue #1914: a PR tarball installs with the release's version string,
        # so after clearing the override the pin is "satisfied" by the PR code.
        # The replaced source is removed first so the reinstall is real.
        tarball = (
            "https://github.com/homeassistant-ai/ha-mcp/archive/refs/pull/1/head.tar.gz"
        )
        _paired(monkeypatch, "ha-mcp==8.6.0")
        env = _PkgEnv(monkeypatch, {DIST_NAME_STABLE: "8.6.0"})
        mgr, _hass, entry = _manager(
            tmp_path, data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: tarball}
        )

        await _ensure(mgr)

        assert env.calls == [f"uninstall {DIST_NAME_STABLE}", "install"]
        assert env.installs[0][0] == "ha-mcp==8.6.0"
        assert entry.data[DATA_LAST_PIP_SPEC] == "ha-mcp==8.6.0"

    async def test_failed_replaced_source_uninstall_raises(self, tmp_path, monkeypatch):
        tarball = (
            "https://github.com/homeassistant-ai/ha-mcp/archive/refs/pull/1/head.tar.gz"
        )
        _paired(monkeypatch, "ha-mcp==8.6.0")
        env = _PkgEnv(monkeypatch, {DIST_NAME_STABLE: "8.6.0"}, uninstall_ok=False)
        mgr, _hass, entry = _manager(
            tmp_path, data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: tarball}
        )

        with pytest.raises(es.EmbeddedServerError) as exc:
            await _ensure(mgr)

        assert exc.value.kind == "package"
        assert env.installs == []
        assert entry.data[DATA_LAST_PIP_SPEC] == tarball

    async def test_deferred_mutations_install_nothing(self, tmp_path, monkeypatch):
        _paired(monkeypatch, "ha-mcp==8.6.0")
        env = _PkgEnv(monkeypatch, {DIST_NAME_STABLE: "8.5.0"})
        mgr, _hass, entry = _manager(
            tmp_path, data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: "ha-mcp==8.5.0"}
        )

        assert await _ensure(mgr, defer_mutations=True) == "8.5.0"

        assert env.installs == []
        # Not recorded: the next undeferred reload must still apply the pin.
        assert entry.data[DATA_LAST_PIP_SPEC] == "ha-mcp==8.5.0"

    async def test_first_install_without_a_pin(self, tmp_path, monkeypatch):
        # A source checkout (no manifest pin) with nothing installed.
        _paired(monkeypatch, None)
        env = _PkgEnv(monkeypatch, {})
        mgr, _hass, entry = _manager(tmp_path)

        assert await _ensure(mgr) == "8.6.0"

        assert env.installs[0][0] == DEFAULT_PIP_SPEC
        assert entry.data[DATA_LAST_PIP_SPEC] == DEFAULT_PIP_SPEC

    async def test_install_failure_raises_and_records_nothing(
        self, tmp_path, monkeypatch
    ):
        _paired(monkeypatch, "ha-mcp==8.6.0")
        env = _PkgEnv(monkeypatch, {})
        env.install_ok = False
        mgr, _hass, entry = _manager(tmp_path)

        with pytest.raises(es.EmbeddedServerError) as exc:
            await _ensure(mgr)

        assert exc.value.kind == "package"
        assert DATA_LAST_PIP_SPEC not in entry.data

    async def test_legacy_server_is_removed_before_install(self, tmp_path, monkeypatch):
        _paired(monkeypatch, "ha-mcp==8.6.0")
        env = _PkgEnv(monkeypatch, {DIST_NAME_STABLE: "6.2.0"})
        mgr, _hass, _entry = _manager(tmp_path)

        assert await _ensure(mgr) == "8.6.0"

        assert env.calls == [f"uninstall {DIST_NAME_STABLE}", "install"]

    async def test_post_install_legacy_version_is_rejected(self, tmp_path, monkeypatch):
        _paired(monkeypatch, None)
        env = _PkgEnv(monkeypatch, {}, install_version="6.2.0")
        mgr, _hass, entry = _manager(tmp_path)

        with pytest.raises(es.EmbeddedServerError) as exc:
            await _ensure(mgr)

        assert env.installs
        assert exc.value.kind == "package"
        assert "installed ha-mcp 6.2.0" in str(exc.value)
        assert "custom_components.ha_mcp_tools.embedded_server" in str(exc.value)
        assert DATA_LAST_PIP_SPEC not in entry.data

    async def test_installed_but_not_importable_raises_package_error(
        self, tmp_path, monkeypatch
    ):
        _paired(monkeypatch, None)
        _PkgEnv(monkeypatch, {})
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda *_a: None)
        mgr, _hass, entry = _manager(tmp_path)

        with pytest.raises(es.EmbeddedServerError) as exc:
            await _ensure(mgr)

        assert exc.value.kind == "package"
        assert DATA_LAST_PIP_SPEC not in entry.data


class TestEnsureExternallyManagedPackage:
    async def test_skip_pip_uses_compatible_externally_managed_package(
        self, tmp_path, monkeypatch
    ):
        """skip_pip must bypass every package mutation and preserve markers."""
        data = {DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: "ha-mcp==7.11.0"}
        mgr, hass, entry = _manager(
            tmp_path, options={OPT_PIP_SPEC: "ha-mcp==99.0.0"}, data=data
        )
        hass.config.skip_pip = True
        monkeypatch.setattr(
            es,
            "async_process_requirements",
            AsyncMock(side_effect=AssertionError("requirements mutation")),
        )
        monkeypatch.setattr(
            es,
            "_force_install_package",
            MagicMock(side_effect=AssertionError("package install")),
        )
        monkeypatch.setattr(
            es,
            "_uninstall_distribution",
            MagicMock(side_effect=AssertionError("package uninstall")),
        )
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda *_a: "7.12.1")
        monkeypatch.setattr(
            es,
            "_installed_dist_version",
            lambda dist: "7.12.1" if dist == DIST_NAME_STABLE else None,
        )

        assert await mgr._async_ensure_package() == "7.12.1"
        assert entry.data == data

    async def test_skip_pip_rejects_the_other_dist(self, tmp_path, monkeypatch):
        mgr, hass, _entry = _manager(tmp_path)
        hass.config.skip_pip = True
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda *_a: "7.12.1.dev1")
        monkeypatch.setattr(
            es, "_installed_dist_version", {DIST_NAME_DEV: "7.12.1.dev1"}.get
        )

        with pytest.raises(
            es.EmbeddedServerError, match=r"expects ha-mcp, but only ha-mcp-dev"
        ):
            await mgr._async_ensure_package()

    async def test_skip_pip_reports_missing_externally_managed_package(
        self, tmp_path, monkeypatch
    ):
        mgr, hass, _entry = _manager(tmp_path)
        hass.config.skip_pip = True
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda *_a: None)
        monkeypatch.setattr(es, "_installed_dist_version", lambda _dist: None)

        with pytest.raises(
            es.EmbeddedServerError,
            match=r"skip_pip.*system package manager.*7\.10\.0",
        ) as exc_info:
            await mgr._async_ensure_package()

        assert exc_info.value.kind == "package"

    async def test_skip_pip_reports_incompatible_externally_managed_package(
        self, tmp_path, monkeypatch
    ):
        mgr, hass, _entry = _manager(tmp_path)
        hass.config.skip_pip = True
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda *_a: "7.9.0")
        monkeypatch.setattr(
            es,
            "_installed_dist_version",
            lambda dist: "7.9.0" if dist == DIST_NAME_STABLE else None,
        )

        with pytest.raises(
            es.EmbeddedServerError,
            match=r"externally managed ha-mcp 7\.9\.0.*7\.10\.0 or newer",
        ) as exc_info:
            await mgr._async_ensure_package()

        assert exc_info.value.kind == "package"

    async def test_skip_pip_reports_ambiguous_externally_managed_packages(
        self, tmp_path, monkeypatch
    ):
        mgr, hass, _entry = _manager(tmp_path)
        hass.config.skip_pip = True
        versions = {DIST_NAME_STABLE: "7.12.1", DIST_NAME_DEV: "7.13.0.dev1"}
        monkeypatch.setattr(es, "_installed_ha_mcp_version", lambda *_a: "7.12.1")
        monkeypatch.setattr(es, "_installed_dist_version", versions.get)

        with pytest.raises(
            es.EmbeddedServerError,
            match=r"Both ha-mcp 7\.12\.1 and ha-mcp-dev 7\.13\.0\.dev1",
        ) as exc_info:
            await mgr._async_ensure_package()

        assert exc_info.value.kind == "package"


class TestPinMovesOffInstalled:
    """The predicate that lets the source-change uninstall be skipped.

    A True here means "the forced install is guaranteed to be real", so a
    wrong True reopens #1914: no uninstall, and an install the installer
    then no-ops as already satisfied.
    """

    def test_local_version_label_still_satisfies_the_pin(self):
        # PEP 440: ==1.0 matches an installed 1.0+local, even though
        # Version("1.0") != Version("1.0+local"). Comparing parsed versions
        # would call this "moved" and skip the uninstall.
        assert not es._pin_moves_off_installed("ha-mcp==1.0", "1.0+local")

    def test_different_version_moves(self):
        assert es._pin_moves_off_installed("ha-mcp==8.1.0", "8.0.0")

    def test_same_version_does_not_move(self):
        assert not es._pin_moves_off_installed("ha-mcp==8.1.0", "8.1.0")

    def test_non_pin_specs_never_move(self):
        assert not es._pin_moves_off_installed("ha-mcp", "8.1.0")
        assert not es._pin_moves_off_installed("ha-mcp>=8.0.0", "8.1.0")

    def test_inapplicable_marker_never_counts_as_moving(self):
        # The installer skips a requirement whose marker is false, so the pin
        # cannot make the install real no matter which version it names.
        assert not es._pin_moves_off_installed(
            "ha-mcp==2.0; python_version < '3.0'", "8.1.0"
        )

    def test_unprovable_inputs_keep_the_uninstall(self):
        # Unparseable either side -> "unknown", which must not be reported
        # as a guaranteed move.
        assert not es._pin_moves_off_installed("ha-mcp==8.1.0", "not-a-version")
        assert not es._pin_moves_off_installed("!!! not a requirement", "8.1.0")


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


class TestSafeInvalidateCaches:
    """Guard for the Python-3.14 setuptools editable-finder KeyError (#1891, #1985).

    On Python 3.14, ``homeassistant`` as a setuptools *editable* install makes
    ``PathFinder.invalidate_caches()`` raise ``KeyError`` at its
    ``del sys.path_importer_cache[name]`` line, aborting
    ``importlib.invalidate_caches()`` and crashing in-process bring-up. The
    helper prunes the stale entries CPython choked on, then re-runs the call so
    the full sweep (finder invalidation + namespace-path epoch + metadata)
    completes.
    """

    _EDITABLE_KEY = "__editable__.homeassistant-2026.7.2.finder.__path_hook__"

    @pytest.fixture(autouse=True)
    def _isolate_path_cache(self, monkeypatch):
        # Recovery prunes sys.path_importer_cache entries; hand each test a
        # private copy so it never mutates the real process cache.
        monkeypatch.setattr(sys, "path_importer_cache", dict(sys.path_importer_cache))

    def _raise_once(self, monkeypatch, *, then=None):
        """Patch invalidate_caches to raise the editable KeyError on the first
        call, then delegate to ``then`` (a no-op by default) on the retry."""
        calls = []

        def _invalidate():
            calls.append(1)
            if len(calls) == 1:
                raise KeyError(self._EDITABLE_KEY)
            if then is not None:
                then()

        monkeypatch.setattr(es.importlib, "invalidate_caches", _invalidate)
        return calls

    def test_no_recovery_when_first_call_succeeds(self, monkeypatch):
        calls = []
        monkeypatch.setattr(es.importlib, "invalidate_caches", lambda: calls.append(1))
        es._safe_invalidate_caches()
        assert calls == [1]  # succeeded first try, no retry

    def test_prunes_stale_entries_then_retries(self, monkeypatch):
        calls = self._raise_once(monkeypatch)
        sys.path_importer_cache.clear()
        sys.path_importer_cache["/abs/deps/dir"] = object()  # abs + live → kept
        sys.path_importer_cache[self._EDITABLE_KEY] = None  # non-abs None → pruned
        sys.path_importer_cache[""] = None  # relative None → pruned
        # Non-absolute key with a LIVE (non-None) finder: the None disjunct is
        # False here, so ONLY the ``not os.path.isabs`` disjunct can prune it —
        # this independently exercises the abspath branch (a real editable
        # placeholder has a live finder, not None).
        sys.path_importer_cache["rel/not/absolute"] = object()

        es._safe_invalidate_caches()

        assert len(calls) == 2  # aborted once, retried once
        assert self._EDITABLE_KEY not in sys.path_importer_cache
        assert "" not in sys.path_importer_cache
        assert "rel/not/absolute" not in sys.path_importer_cache
        assert "/abs/deps/dir" in sys.path_importer_cache

    def test_propagates_non_keyerror(self, monkeypatch):
        def _boom():
            raise RuntimeError("unrelated import-system failure")

        monkeypatch.setattr(es.importlib, "invalidate_caches", _boom)
        with pytest.raises(RuntimeError):
            es._safe_invalidate_caches()

    def test_retry_keyerror_is_tolerated(self, monkeypatch):
        # If a concurrent import re-adds a stale placeholder between the prune and
        # the retry, the retry can raise KeyError a SECOND time. That must not
        # re-crash bring-up (the helper's whole purpose) — it is logged
        # best-effort and swallowed, not re-raised.
        def _always_keyerror():
            raise KeyError(self._EDITABLE_KEY)

        monkeypatch.setattr(es.importlib, "invalidate_caches", _always_keyerror)
        es._safe_invalidate_caches()  # must not raise despite the retry also failing

    def test_recovery_advances_namespace_epoch(self, monkeypatch):
        # Codex (#1987): recovery must preserve PathFinder's namespace-path epoch
        # bump so a newly-installed PEP 420 namespace portion stays discoverable.
        # The retry runs the REAL invalidate_caches, which advances the epoch.
        from importlib._bootstrap_external import _NamespacePath

        real = importlib.invalidate_caches
        self._raise_once(monkeypatch, then=real)

        before = _NamespacePath._epoch
        es._safe_invalidate_caches()
        assert _NamespacePath._epoch > before

    def test_installed_version_survives_editable_finder_keyerror(self, monkeypatch):
        # End-to-end regression for the exact reported traceback:
        # _installed_ha_mcp_version() -> importlib.invalidate_caches() raised
        # KeyError and crashed bring-up. With the guard the version still
        # resolves.
        self._raise_once(monkeypatch)
        monkeypatch.setattr(es.importlib.util, "find_spec", lambda name: object())
        monkeypatch.setattr(importlib.metadata, "version", lambda name: "7.14.1")
        assert es._installed_ha_mcp_version() == "7.14.1"


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

        # config_dir rides along (#2329) so the in-process server can read
        # blueprint files directly; it is HA's own config dir, captured on the
        # event loop at construction, never a caller-supplied path.
        set_conn.assert_called_once_with(
            "http://ha.local:8123",
            "tok-xyz",
            config_dir=mgr._hass_config_dir,
            llm_api_enabled=True,
        )
        # _serve raised on the ha_mcp.server import → captured, thread didn't hang.
        assert mgr._thread_exc is not None

    def test_serve_passes_verify_ssl_for_derived_https_loopback(
        self, tmp_path, monkeypatch
    ):
        # Issue #1890 end-to-end: an SSL-enabled instance (no URL override)
        # must register the derived https loopback WITH verify_ssl=False —
        # dropping the kwarg would re-introduce the cert-verification failure
        # the derivation exists to fix.
        hass = _make_hass(tmp_path)
        hass.config.api = SimpleNamespace(port=8123, use_ssl=True)
        mgr = es.EmbeddedServerManager(hass, _make_entry())
        set_conn = MagicMock(name="set_embedded_connection")
        ha_mcp_mod = ModuleType("ha_mcp")
        ha_mcp_config = ModuleType("ha_mcp.config")
        ha_mcp_config.set_embedded_connection = set_conn
        monkeypatch.setitem(sys.modules, "ha_mcp", ha_mcp_mod)
        monkeypatch.setitem(sys.modules, "ha_mcp.config", ha_mcp_config)
        monkeypatch.delitem(sys.modules, "ha_mcp.server", raising=False)

        mgr._thread_main("tok-xyz")

        set_conn.assert_called_once_with(
            "https://127.0.0.1:8123",
            "tok-xyz",
            config_dir=mgr._hass_config_dir,
            llm_api_enabled=True,
            verify_ssl=False,
        )

    def test_serve_passes_disabled_llm_api_option(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={
                OPT_SERVER_URL: "http://ha.local:8123",
                OPT_ENABLE_LLM_API: False,
            },
        )
        set_conn = MagicMock(name="set_embedded_connection")
        ha_mcp_mod = ModuleType("ha_mcp")
        ha_mcp_config = ModuleType("ha_mcp.config")
        ha_mcp_config.set_embedded_connection = set_conn
        monkeypatch.setitem(sys.modules, "ha_mcp", ha_mcp_mod)
        monkeypatch.setitem(sys.modules, "ha_mcp.config", ha_mcp_config)
        monkeypatch.delitem(sys.modules, "ha_mcp.server", raising=False)

        mgr._thread_main("tok-xyz")

        set_conn.assert_called_once_with(
            "http://ha.local:8123",
            "tok-xyz",
            config_dir=mgr._hass_config_dir,
            llm_api_enabled=False,
        )

    def test_serve_drops_only_config_dir_on_a_server_without_it(
        self, tmp_path, monkeypatch
    ):
        # A server that knows verify_ssl (#1890) but predates config_dir
        # (#2329) must lose ONLY config_dir: falling all the way back to the
        # two-arg call would re-open the loopback certificate failure just
        # because an unrelated newer keyword is missing.
        hass = _make_hass(tmp_path)
        hass.config.api = SimpleNamespace(port=8123, use_ssl=True)
        mgr = es.EmbeddedServerManager(hass, _make_entry())
        calls: list[tuple[str, str, bool | None]] = []

        def mid_set_conn(url, token, verify_ssl=None):  # no config_dir kwarg
            calls.append((url, token, verify_ssl))

        ha_mcp_mod = ModuleType("ha_mcp")
        ha_mcp_config = ModuleType("ha_mcp.config")
        ha_mcp_config.set_embedded_connection = mid_set_conn
        monkeypatch.setitem(sys.modules, "ha_mcp", ha_mcp_mod)
        monkeypatch.setitem(sys.modules, "ha_mcp.config", ha_mcp_config)
        monkeypatch.delitem(sys.modules, "ha_mcp.server", raising=False)

        mgr._thread_main("tok-xyz")

        assert calls == [("https://127.0.0.1:8123", "tok-xyz", False)]

    def test_serve_falls_back_to_two_arg_registration_on_old_server(
        self, tmp_path, monkeypatch
    ):
        # An installed server predating the verify_ssl parameter rejects the
        # three-arg call with TypeError; the manager must fall back to the
        # legacy two-arg registration (still starts, TLS verification stays
        # on) instead of crashing the worker thread.
        hass = _make_hass(tmp_path)
        hass.config.api = SimpleNamespace(port=8123, use_ssl=True)
        mgr = es.EmbeddedServerManager(hass, _make_entry())
        calls: list[tuple[str, str]] = []

        def old_set_conn(url, token):  # 2-arg signature: verify_ssl= raises
            calls.append((url, token))

        ha_mcp_mod = ModuleType("ha_mcp")
        ha_mcp_config = ModuleType("ha_mcp.config")
        ha_mcp_config.set_embedded_connection = old_set_conn
        monkeypatch.setitem(sys.modules, "ha_mcp", ha_mcp_mod)
        monkeypatch.setitem(sys.modules, "ha_mcp.config", ha_mcp_config)
        monkeypatch.delitem(sys.modules, "ha_mcp.server", raising=False)

        mgr._thread_main("tok-xyz")

        assert calls == [("https://127.0.0.1:8123", "tok-xyz")]

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


class TestAuditDistName:
    """The dependency audit walks the graph of the dist actually installed."""

    def test_override_names_the_audit_root(self, tmp_path, monkeypatch):
        """A stable-dist override audits the override's dist even when stale
        ``ha-mcp-dev`` metadata coexists with it (Codex review on #2245)."""
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_PIP_SPEC: "ha-mcp==9.9.9"},
        )
        monkeypatch.setattr(es, "_dist_installed", lambda name: True)
        assert mgr._audit_dist_name() == DIST_NAME_STABLE

    def test_bare_url_override_audits_the_stable_dist(self, tmp_path, monkeypatch):
        """A repository tarball installs as ha-mcp.

        A bare URL parses as no requirement, so _replaced_dist_name() is
        None — without the explicit stable preference, stale ha-mcp-dev
        metadata would win the audit root (CodeRabbit outside-diff-range
        finding on #2245).
        """
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={
                OPT_PIP_SPEC: "https://example.invalid/ha-mcp.tar.gz",
            },
        )
        monkeypatch.setattr(es, "_dist_installed", lambda name: True)
        assert mgr._audit_dist_name() == DIST_NAME_STABLE

    def test_bare_url_override_prefers_the_installed_known_dist(
        self, tmp_path, monkeypatch
    ):
        """A dev wheel URL installs ha-mcp-dev; audit what is actually there.

        Hardcoding stable for every bare URL would report a phantom missing
        ha-mcp on each healthy bring-up of such an install (Codex on #2245).
        """
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={
                OPT_PIP_SPEC: "https://example.invalid/ha-mcp-dev.whl",
            },
        )
        monkeypatch.setattr(es, "_dist_installed", lambda name: name == DIST_NAME_DEV)
        assert mgr._audit_dist_name() == DIST_NAME_DEV

    def test_named_url_override_audits_its_own_distribution(
        self, tmp_path, monkeypatch
    ):
        """An override may install an ARBITRARY distribution; audit that one.

        ``name @ url`` parses, so the bare-URL stable fallback never fires,
        and mapping onto the two known dists would fall back to a stale
        graph (CodeRabbit on #2245).
        """
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={
                OPT_PIP_SPEC: "acme-ha-mcp @ file:///package.whl",
            },
        )
        monkeypatch.setattr(es, "_dist_installed", lambda name: True)
        assert mgr._audit_dist_name() == "acme-ha-mcp"

    def test_explicit_root_survives_missing_metadata(self, tmp_path, monkeypatch):
        """An explicit root is audited even when its metadata is absent.

        Swapping to the other known installed dist would walk a stale
        graph; auditing the explicit root instead reports it as missing —
        the true story when its install failed (CodeRabbit on #2245).
        """
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_PIP_SPEC: "ha-mcp==9.9.9"},
        )
        monkeypatch.setattr(es, "_dist_installed", lambda name: name == DIST_NAME_DEV)
        assert mgr._audit_dist_name() == DIST_NAME_STABLE

    def test_pinned_dist_wins_without_an_override(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(tmp_path)
        mgr._paired_spec = "ha-mcp==8.6.0"
        mgr._pip_spec = mgr._resolve_pip_spec()
        monkeypatch.setattr(es, "_dist_installed", lambda name: True)
        assert mgr._audit_dist_name() == DIST_NAME_STABLE

    def test_falls_back_to_the_installed_dist(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(es, "_dist_installed", lambda name: name == DIST_NAME_DEV)
        assert mgr._audit_dist_name() == DIST_NAME_DEV


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
        # _worker_startup_failure wraps a non-EmbeddedServerError crash; the
        # "failed to start" prefix moved to embedded_setup's log line (#2239).
        with pytest.raises(es.EmbeddedServerError, match="worker thread crashed"):
            await mgr._async_wait_until_ready()

    async def test_wait_ready_surfaces_worker_composed_error_verbatim(self, tmp_path):
        """A worker-composed EmbeddedServerError passes through UNWRAPPED.

        The passthrough branch of _worker_startup_failure carries the
        dependency diagnosis and its failure kind; re-wrapping is what
        produced the doubled "failed to start:" prefix (#2239, Patch76
        review: this branch had no test that goes red on regression).
        """
        mgr, hass, _entry = _manager(tmp_path)
        hass.loop.time = MagicMock(return_value=0.0)
        composed = es.EmbeddedServerError(
            "Installed mcp 1.14.1 does not satisfy 'mcp>=1.24.0'.",
            kind="package",
        )
        mgr._thread_exc = composed
        with pytest.raises(es.EmbeddedServerError) as excinfo:
            await mgr._async_wait_until_ready()
        assert excinfo.value is composed
        assert excinfo.value.kind == "package"
        assert "failed to start" not in str(excinfo.value)

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

        def _restore_env() -> None:
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

    def test_thread_main_unwraps_uvicorn_systemexit(self, tmp_path, monkeypatch):
        # Real uvicorn does NOT let a bind failure escape as OSError: startup()
        # catches it and calls sys.exit(STARTUP_FAILURE). SystemExit is a
        # BaseException, so the worker's `except Exception` missed it and the
        # component reported a bare readiness timeout while the actual cause
        # (port in use) only surfaced in HA's generic task-exception log
        # (issue #1904). The handler must unwrap the original error.
        _saved = {
            k: os.environ.get(k) for k in ("HA_MCP_CONFIG_DIR", "HA_MCP_EMBEDDED")
        }

        def _restore_env() -> None:
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
                # Mirror uvicorn.Server.startup(): the bind error is caught
                # and converted to sys.exit(STARTUP_FAILURE), leaving the
                # OSError only as SystemExit.__context__.
                try:
                    raise OSError(98, "address already in use")
                except OSError:
                    # uvicorn calls bare sys.exit(STARTUP_FAILURE), leaving
                    # the OSError only in __context__ — no `from` chaining.
                    raise SystemExit(3)  # noqa: B904

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
        monkeypatch.setitem(
            sys.modules,
            "ha_mcp.browser_landing",
            ModuleType("ha_mcp.browser_landing"),
        )

        try:
            mgr._thread_main("tok")
        finally:
            _restore_env()

        assert isinstance(mgr._thread_exc, es.EmbeddedServerError)
        assert "exited during startup" in str(mgr._thread_exc)
        assert "address already in use" in str(mgr._thread_exc)

    def test_thread_main_wraps_bare_systemexit(self, tmp_path, monkeypatch):
        # A SystemExit with no chained exception (bare sys.exit) must still
        # land in _thread_exc as an EmbeddedServerError naming the exit -
        # not escape the worker, and not read as an empty failure message.
        _saved = {
            k: os.environ.get(k) for k in ("HA_MCP_CONFIG_DIR", "HA_MCP_EMBEDDED")
        }

        def _restore_env() -> None:
            for k, v in _saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        mgr, _hass, _entry = _manager(tmp_path)

        async def _exit_now(access_token, stop_event):
            raise SystemExit(3)

        monkeypatch.setattr(mgr, "_serve", _exit_now)
        try:
            mgr._thread_main("tok")
        finally:
            _restore_env()

        assert isinstance(mgr._thread_exc, es.EmbeddedServerError)
        assert "exited during startup" in str(mgr._thread_exc)
        assert "SystemExit(3)" in str(mgr._thread_exc)

    async def test_wait_ready_stall_stops_thread_and_raises(
        self, tmp_path, monkeypatch
    ):
        # No observable progress (pinned signature) past the stall budget.
        mgr, hass, _entry = _manager(tmp_path)
        hass.loop.time = MagicMock(side_effect=[0.0, 100.0])
        mgr._thread = SimpleNamespace(is_alive=lambda: True)
        mgr._startup_phase = "pinned"
        monkeypatch.setattr(mgr, "_progress_signature", lambda: (0, "pinned"))
        monkeypatch.setattr(mgr, "_async_probe_port", AsyncMock(return_value=False))
        stop = AsyncMock()
        monkeypatch.setattr(mgr, "async_stop", stop)
        monkeypatch.setattr(es.asyncio, "sleep", AsyncMock())

        with pytest.raises(
            es.EmbeddedServerError, match="no startup progress"
        ) as excinfo:
            await mgr._async_wait_until_ready()
        stop.assert_awaited_once()
        # The failure names the phase the worker was last seen in.
        assert "pinned" in str(excinfo.value)

    async def test_wait_ready_progress_extends_past_stall_budget(
        self, tmp_path, monkeypatch
    ):
        # 150s elapsed (past the 90s stall budget) but the worker kept
        # importing (signature moves) - the wait must NOT give up (#1904).
        mgr, hass, _entry = _manager(tmp_path)
        hass.loop.time = MagicMock(side_effect=[0.0, 150.0])
        mgr._thread = SimpleNamespace(is_alive=lambda: True)
        ticks = iter(range(100))
        monkeypatch.setattr(
            mgr, "_progress_signature", lambda: (next(ticks), "importing")
        )
        monkeypatch.setattr(
            mgr, "_async_probe_port", AsyncMock(side_effect=[False, True])
        )
        monkeypatch.setattr(es.asyncio, "sleep", AsyncMock())

        await mgr._async_wait_until_ready()  # must not raise

    async def test_wait_ready_total_cap_fires_despite_progress(
        self, tmp_path, monkeypatch
    ):
        # Endless "progress" cannot extend the wait past the absolute cap.
        mgr, hass, _entry = _manager(tmp_path)
        hass.loop.time = MagicMock(side_effect=[0.0, 700.0])
        mgr._thread = SimpleNamespace(is_alive=lambda: True)
        ticks = iter(range(100))
        monkeypatch.setattr(
            mgr, "_progress_signature", lambda: (next(ticks), "importing")
        )
        monkeypatch.setattr(mgr, "_async_probe_port", AsyncMock(return_value=False))
        stop = AsyncMock()
        monkeypatch.setattr(mgr, "async_stop", stop)
        monkeypatch.setattr(es.asyncio, "sleep", AsyncMock())

        with pytest.raises(es.EmbeddedServerError, match="within 600s"):
            await mgr._async_wait_until_ready()
        stop.assert_awaited_once()

    def test_progress_signature_tracks_modules_and_phase(self, tmp_path, monkeypatch):
        # The production progress source itself (review gap): module-count
        # growth and phase advances must each change the signature - this is
        # the mechanism that keeps a slow cold import alive (#1904).
        mgr, _hass, _entry = _manager(tmp_path)
        base = mgr._progress_signature()
        monkeypatch.setitem(
            sys.modules, "_pr1908_progress_probe", ModuleType("_pr1908_progress_probe")
        )
        after_import = mgr._progress_signature()
        assert after_import != base
        mgr._startup_phase = "further along"
        assert mgr._progress_signature() != after_import


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
# _serve log-filter installation
# ---------------------------------------------------------------------------


class TestServeLogFilters:
    """Parity with the CLI HTTP runner and the add-on's start.py: _serve must
    install the shared SDK/fastmcp log-noise filters (routine stateless
    teardown, benign tool-validation tracebacks, disconnect-caused "session
    crashed" tracebacks) so the in-process embedded server doesn't log
    alarming noise the other two launchers already suppress."""

    @pytest.fixture(autouse=True)
    def _isolate_env(self):
        """Snapshot/restore the env vars _thread_main stages, per-test."""
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

    def test_serve_installs_log_filters(self, tmp_path, monkeypatch):
        """_serve calls install_sdk_log_filters() when it is available."""
        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_SERVER_URL: "http://ha.local:8123"}
        )

        class _StopServe(Exception):
            pass

        class _FakeMcp:
            def http_app(self, path, stateless_http):
                raise _StopServe

        fake_mcp = _FakeMcp()
        install_calls: list = []
        log_filters_mod = ModuleType("ha_mcp.log_filters")
        log_filters_mod.install_sdk_log_filters = lambda: install_calls.append(True)
        _stub_ha_mcp_surface(monkeypatch, mcp=fake_mcp, log_filters_mod=log_filters_mod)

        mgr._thread_main("tok")

        assert install_calls == [True]
        assert isinstance(mgr._thread_exc, _StopServe)

    def test_serve_tolerates_missing_log_filters_module(self, tmp_path, monkeypatch):
        """Backward-compat: an OLDER bundled ha-mcp (the component reaches
        users ahead of the server) has no log_filters module. _serve must
        swallow the ImportError and keep serving, same as the
        browser-landing guard."""
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

        # log_filters_mod omitted ⇒ ha_mcp.log_filters is present but the
        # helper attribute is absent (import fails).
        _stub_ha_mcp_surface(monkeypatch, mcp=_FakeMcp())

        mgr._thread_main("tok")

        assert reached == ["/private_secret"]
        assert isinstance(mgr._thread_exc, _StopServe)


class TestInstallLogFiltersIfAvailable:
    """Direct unit coverage of _install_log_filters_if_available()'s branch
    logic. CodeRabbit review finding: the _serve()-level tests above only
    assert that serving continues either way, never that the SILENT
    "older ha-mcp" case and the WARNING cases are actually distinguishable
    by their log output -- dropping the err.name check entirely would leave
    the suite green while every older install started logging noise on
    every startup. These tests call the function directly (no need for the
    full _stub_ha_mcp_surface/_thread_main harness) and assert on caplog.
    """

    def setup_method(self):
        """Snapshot ha_mcp.* sys.modules entries so each test's fakes don't
        leak into the next -- ha_mcp is genuinely importable in this test
        process (unlike the fully-stubbed harness the heavier _serve()-level
        tests above build)."""
        self._saved = {
            name: sys.modules.get(name) for name in ("ha_mcp", "ha_mcp.log_filters")
        }

    def teardown_method(self):
        """Restore the snapshotted sys.modules entries exactly."""
        for name, mod in self._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def test_genuinely_missing_module_is_silent(self, monkeypatch, caplog):
        """The true 'older ha-mcp, module doesn't exist yet' case. Patches
        builtins.__import__ to deterministically raise
        ModuleNotFoundError(name='ha_mcp.log_filters') for that exact name,
        the same technique test_different_missing_dependency_warns_and_names_it
        uses below -- NOT faking the parent ha_mcp package's __path__: this
        repo's editable install (uv sync) adds a meta-path finder that
        resolves ha_mcp.* by name and would re-import the REAL module even
        with an empty __path__ on the faked parent ("live-found in CI" per
        _stub_ha_mcp_surface's own docstring above), which would make this
        test pass vacuously -- no exception raised at all, not the silent
        branch actually exercised. This is exactly the branch CodeRabbit
        found untested."""
        sys.modules.pop("ha_mcp.log_filters", None)
        real_import = __import__

        def _fake_import(name, *args, **kwargs):
            if name == "ha_mcp.log_filters":
                raise ModuleNotFoundError(
                    "No module named 'ha_mcp.log_filters'", name="ha_mcp.log_filters"
                )
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", _fake_import)

        with caplog.at_level("WARNING", logger=es._LOGGER.name):
            es._install_log_filters_if_available()

        assert caplog.records == []

    def test_module_present_but_attribute_missing_warns(self, caplog):
        """The existing ImportError case (module exists, helper attribute
        doesn't) must actually log a WARNING -- the discriminating half of
        the same gap: a prior test proved serving continues, not that the
        output differs from the silent case above."""
        sys.modules["ha_mcp.log_filters"] = ModuleType("ha_mcp.log_filters")

        with caplog.at_level("WARNING", logger=es._LOGGER.name):
            es._install_log_filters_if_available()

        assert "Could not install MCP SDK log-noise filters" in caplog.text

    def test_different_missing_dependency_warns_and_names_it(self, monkeypatch, caplog):
        """A ModuleNotFoundError for anything OTHER than ha_mcp.log_filters
        itself (e.g. a stale fastmcp/pydantic left over from a previous
        install -- _purge_ha_mcp_modules deliberately never reinstalls
        third-party dependencies) is not the older-server case and must
        warn, naming the actual missing dependency rather than staying
        silent."""
        sys.modules.pop("ha_mcp.log_filters", None)
        real_import = __import__

        def _fake_import(name, *args, **kwargs):
            if name == "ha_mcp.log_filters":
                raise ModuleNotFoundError("No module named 'fastmcp'", name="fastmcp")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", _fake_import)

        with caplog.at_level("WARNING", logger=es._LOGGER.name):
            es._install_log_filters_if_available()

        assert "fastmcp" in caplog.text
        assert "Could not install MCP SDK log-noise filters" in caplog.text


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
            match=r"requires Home Assistant 2026\.8\.0 or newer",
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
            AsyncMock(side_effect=lambda **kwargs: calls.append("ensure")),
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
        monkeypatch.setattr(mgr, "_thread_main", started.append)

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

    def test_purge_clears_cached_import_version(self, monkeypatch):
        monkeypatch.setattr(es, "_CACHED_IMPORT_VERSION", "9.9.9")
        monkeypatch.setitem(sys.modules, "ha_mcp", ModuleType("ha_mcp"))
        es._purge_ha_mcp_modules()
        assert es._CACHED_IMPORT_VERSION is None


class TestRunningVersionStalenessWarning:
    async def test_start_prefers_the_override_dev_distribution(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            es, "_async_paired_server_requirement", AsyncMock(return_value=None)
        )
        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_PIP_SPEC: "ha-mcp-dev==7.13.0.dev1"}
        )
        monkeypatch.setattr(mgr, "_async_ensure_package", AsyncMock())
        monkeypatch.setattr(
            mgr, "_async_provision_token", AsyncMock(return_value="tok")
        )
        monkeypatch.setattr(mgr, "_prepare_config_dir", lambda: None)
        monkeypatch.setattr(es, "_purge_ha_mcp_modules", lambda: None)
        monkeypatch.setattr(mgr, "_thread_main", lambda token: None)
        installed = MagicMock(return_value="7.13.0.dev1")
        monkeypatch.setattr(es, "_installed_ha_mcp_version", installed)

        def _ready_with_current_dev_worker():
            mgr._running_version = "7.13.0.dev1"

        monkeypatch.setattr(
            mgr,
            "_async_wait_until_ready",
            AsyncMock(side_effect=_ready_with_current_dev_worker),
        )

        with caplog.at_level("WARNING"):
            await mgr.async_start()
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)

        installed.assert_called_once_with(DIST_NAME_DEV)
        assert "restart Home Assistant" not in caplog.text

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
        monkeypatch.setattr(
            es, "_installed_ha_mcp_version", lambda preferred_dist=None: "9.9.9"
        )

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
        monkeypatch.setattr(
            es, "_installed_ha_mcp_version", lambda preferred_dist=None: "1.1.1"
        )

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

    def _start_kwargs(
        self,
        mgr: es.EmbeddedServerManager,
        monkeypatch: pytest.MonkeyPatch,
        purges: list[bool],
    ) -> None:
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

    async def test_purge_skipped_while_foreign_worker_importing(
        self, tmp_path, monkeypatch, caplog
    ):
        # The orphan guard is per-manager, but every bring-up constructs a
        # FRESH manager - a still-importing worker abandoned by a PREVIOUS
        # manager must also block the purge (issue #1904: the reload-era
        # purge crashed the old worker mid-import with KeyError).
        mgr, _hass, _entry = _manager(tmp_path)
        purges: list[bool] = []
        self._start_kwargs(mgr, monkeypatch, purges)

        class _AliveWorker:
            def is_alive(self):
                return True

        foreign = _AliveWorker()
        monkeypatch.setattr(es, "_IMPORTING_WORKERS", {foreign})

        with caplog.at_level("WARNING"):
            await mgr.async_start()
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)

        assert purges == []
        assert "Skipping the ha_mcp module purge" in caplog.text
        assert foreign in es._IMPORTING_WORKERS  # live entry retained

    async def test_purge_resumes_once_foreign_worker_died(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(tmp_path)
        purges: list[bool] = []
        self._start_kwargs(mgr, monkeypatch, purges)

        class _DeadWorker:
            def is_alive(self):
                return False

        dead = _DeadWorker()
        monkeypatch.setattr(es, "_IMPORTING_WORKERS", {dead})

        await mgr.async_start()
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)

        assert purges == [True]
        assert dead not in es._IMPORTING_WORKERS  # dead entry pruned

    async def test_prune_keeps_live_worker_while_dropping_dead_one(
        self, tmp_path, monkeypatch
    ):
        # The composite prune-then-check must drop only the dead entry and
        # still block the purge on the surviving live one.
        mgr, _hass, _entry = _manager(tmp_path)
        purges: list[bool] = []
        self._start_kwargs(mgr, monkeypatch, purges)

        class _AliveWorker:
            def is_alive(self):
                return True

        class _DeadWorker:
            def is_alive(self):
                return False

        alive = _AliveWorker()
        dead = _DeadWorker()
        monkeypatch.setattr(es, "_IMPORTING_WORKERS", {alive, dead})

        await mgr.async_start()
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)

        assert purges == []
        assert alive in es._IMPORTING_WORKERS
        assert dead not in es._IMPORTING_WORKERS


class TestImportingWorkerRegistry:
    """The worker must be registered for exactly its import window."""

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

    def test_thread_main_always_deregisters_on_exit(self, tmp_path, monkeypatch):
        # Registration happens in async_start (main thread) before start();
        # this drives _thread_main with the current thread pre-registered the
        # same way and proves the finally backstop clears it on exit even
        # though _serve never reached its own early deregistration point.
        mgr, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(es, "_IMPORTING_WORKERS", {threading.current_thread()})
        seen: list[bool] = []

        async def _probe(access_token, stop_event):
            seen.append(threading.current_thread() in es._IMPORTING_WORKERS)
            raise SystemExit(3)

        monkeypatch.setattr(mgr, "_serve", _probe)
        mgr._thread_main("tok")

        assert seen == [True]
        assert not es._IMPORTING_WORKERS

    def test_thread_main_deregisters_on_plain_exception_crash(
        self, tmp_path, monkeypatch
    ):
        # A worker that dies of an ordinary Exception mid-import (the
        # abandoned-worker case the registry exists for) must also leave the
        # registry via the shared finally.
        mgr, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(es, "_IMPORTING_WORKERS", {threading.current_thread()})

        async def _boom(access_token, stop_event):
            raise RuntimeError("boom mid-import")

        monkeypatch.setattr(mgr, "_serve", _boom)
        mgr._thread_main("tok")

        assert isinstance(mgr._thread_exc, RuntimeError)
        assert not es._IMPORTING_WORKERS

    async def test_live_worker_blocks_a_fresh_managers_purge(
        self, tmp_path, monkeypatch, caplog
    ):
        # The literal #1904 race, end to end: worker A registers itself via
        # the REAL _thread_main path, and a freshly constructed manager B
        # (a reload builds a new manager every time) must see A in the
        # process-global registry and skip its purge.
        monkeypatch.setattr(es, "_IMPORTING_WORKERS", set())
        release = threading.Event()

        mgr_a, _hass_a, _entry_a = _manager(tmp_path)

        async def _hold(access_token, stop_event):
            # threading.Event so the MAIN thread can release a coroutine
            # running on worker A's own loop; run_in_executor keeps the
            # worker loop unblocked while waiting.
            await asyncio.get_running_loop().run_in_executor(None, release.wait)

        monkeypatch.setattr(mgr_a, "_serve", _hold)
        worker_a = threading.Thread(target=mgr_a._thread_main, args=("tok",))
        # Mirror async_start: the SPAWNING thread registers the worker
        # before start(), the worker only deregisters.
        with es._IMPORTING_WORKERS_LOCK:
            es._IMPORTING_WORKERS.add(worker_a)
        worker_a.start()
        try:

            def _wait_registered() -> bool:
                deadline = time.monotonic() + 5
                while not es._IMPORTING_WORKERS and time.monotonic() < deadline:
                    time.sleep(0.02)
                return bool(es._IMPORTING_WORKERS)

            registered = await asyncio.get_running_loop().run_in_executor(
                None, _wait_registered
            )
            assert registered, "worker A never registered"

            mgr_b, _hass_b, _entry_b = _manager(tmp_path)
            purges: list[bool] = []
            monkeypatch.setattr(
                mgr_b, "_async_ensure_package", AsyncMock(return_value="1.2.3")
            )
            monkeypatch.setattr(
                mgr_b, "_async_provision_token", AsyncMock(return_value="tok")
            )
            monkeypatch.setattr(mgr_b, "_prepare_config_dir", lambda: None)
            monkeypatch.setattr(mgr_b, "_async_wait_until_ready", AsyncMock())
            monkeypatch.setattr(mgr_b, "_thread_main", lambda token: None)
            monkeypatch.setattr(
                es, "_purge_ha_mcp_modules", lambda: purges.append(True)
            )

            with caplog.at_level("WARNING"):
                await mgr_b.async_start()
            if mgr_b._thread is not None:
                mgr_b._thread.join(timeout=2)

            assert purges == []
            assert "Skipping the ha_mcp module purge" in caplog.text
        finally:
            release.set()
            worker_a.join(timeout=5)
        assert not worker_a.is_alive()
        # Worker A deregistered itself on exit; manager B's stubbed worker
        # (no real _thread_main, so no self-discard) may linger dead.
        assert worker_a not in es._IMPORTING_WORKERS

    def test_serve_deregisters_before_building_the_app(self, tmp_path, monkeypatch):
        # Once the import section completes, a concurrent purge is harmless -
        # the worker must leave the registry BEFORE the listener build so a
        # long-running healthy server never blocks later bring-ups' purges.
        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_SERVER_URL: "http://ha.local:8123"}
        )
        monkeypatch.setattr(es, "_IMPORTING_WORKERS", set())

        class _StopServe(Exception):
            pass

        membership: list[bool] = []

        class _FakeMcp:
            def http_app(self, path, stateless_http):
                membership.append(
                    ("http_app", threading.current_thread() in es._IMPORTING_WORKERS)
                )
                raise _StopServe

        _stub_ha_mcp_surface(monkeypatch, mcp=_FakeMcp())
        monkeypatch.setattr(es, "_installed_dist_version", lambda dist: None)

        # Recorder on the stubbed settings-routes hook: registration must
        # still be in effect there (imports not yet complete), making this
        # test self-contained rather than relying on the sibling test to
        # prove the add() happened at all.
        def _routes_probe(*args, **kwargs):
            membership.append(
                ("routes", threading.current_thread() in es._IMPORTING_WORKERS)
            )

        sys.modules["ha_mcp.settings_ui"].register_settings_routes = _routes_probe

        # Pre-register the current thread the way async_start does before
        # start(); _serve's early discard must clear it before http_app.
        with es._IMPORTING_WORKERS_LOCK:
            es._IMPORTING_WORKERS.add(threading.current_thread())
        mgr._thread_main("tok")

        assert membership == [("routes", True), ("http_app", False)]
        assert not es._IMPORTING_WORKERS
        assert isinstance(mgr._thread_exc, _StopServe)


class TestPendingInstallTracking:
    """Package-mutating executor jobs must be waitable across bring-ups.

    asyncio cancellation of a bring-up detaches the awaiter but the executor
    pip job runs to completion; untracked, an orphaned pip could swap the
    package files under the next bring-up's install or its worker's cold
    import (review finding on the #1904 fixes).
    """

    async def test_tracked_job_registers_and_clears(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(es, "_PENDING_INSTALL_DONE", None)
        observed: list[bool] = []

        def _job() -> str:
            with es._PENDING_INSTALL_LOCK:
                observed.append(es._PENDING_INSTALL_DONE is not None)
            return "done"

        result = await mgr._async_run_tracked_install_job(_job)

        assert result == "done"
        assert observed == [True]
        with es._PENDING_INSTALL_LOCK:
            assert es._PENDING_INSTALL_DONE is None

    async def test_tracked_job_clears_even_when_the_job_raises(
        self, tmp_path, monkeypatch
    ):
        mgr, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(es, "_PENDING_INSTALL_DONE", None)

        def _job() -> None:
            raise RuntimeError("pip exploded")

        with pytest.raises(RuntimeError, match="pip exploded"):
            await mgr._async_run_tracked_install_job(_job)

        with es._PENDING_INSTALL_LOCK:
            assert es._PENDING_INSTALL_DONE is None

    async def test_wait_returns_once_orphan_finishes(
        self, tmp_path, monkeypatch, caplog
    ):
        mgr, _hass, _entry = _manager(tmp_path)
        pending = threading.Event()
        monkeypatch.setattr(es, "_PENDING_INSTALL_DONE", pending)
        threading.Timer(0.2, pending.set).start()

        with caplog.at_level("WARNING"):
            await mgr._async_wait_for_pending_install()  # must not raise

        assert "install job is still running on the executor" in caplog.text

    async def test_wait_raises_when_orphan_never_finishes(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(es, "_PENDING_INSTALL_DONE", threading.Event())
        monkeypatch.setattr(es, "_PENDING_INSTALL_WAIT_SECONDS", 0.1)

        with pytest.raises(
            es.EmbeddedServerError, match="refusing to modify"
        ) as excinfo:
            await mgr._async_wait_for_pending_install()
        assert excinfo.value.kind == "package"

    async def test_wait_noop_when_nothing_pending(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(es, "_PENDING_INSTALL_DONE", None)
        await mgr._async_wait_for_pending_install()  # must not raise or block

    async def test_finally_leaves_a_foreign_slot_untouched(self, tmp_path, monkeypatch):
        # The identity guard in the tracked job's finally: if a NEWER job has
        # already replaced the slot, the older job's cleanup must not clear
        # it — an unconditional clear would let the next bring-up skip the
        # wait and mutate the package under the newer, still-running pip.
        mgr, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(es, "_PENDING_INSTALL_DONE", None)
        foreign = threading.Event()

        def _job() -> str:
            with es._PENDING_INSTALL_LOCK:
                es._PENDING_INSTALL_DONE = foreign
            return "ok"

        await mgr._async_run_tracked_install_job(_job)

        with es._PENDING_INSTALL_LOCK:
            assert es._PENDING_INSTALL_DONE is foreign

    async def test_force_install_routes_through_tracking(self, tmp_path, monkeypatch):
        # The wiring IS the behavioral payload: a revert to a bare
        # async_add_executor_job would silently reintroduce the orphaned,
        # untrackable install. Observe the slot from inside the pip stub.
        mgr, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(es, "_PENDING_INSTALL_DONE", None)
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        observed: list[bool] = []

        def _fake_install(spec: str, **kwargs: object) -> bool:
            with es._PENDING_INSTALL_LOCK:
                observed.append(es._PENDING_INSTALL_DONE is not None)
            return True

        monkeypatch.setattr(es, "_force_install_package", _fake_install)

        await mgr._async_force_install()

        assert observed == [True]
        with es._PENDING_INSTALL_LOCK:
            assert es._PENDING_INSTALL_DONE is None

    async def test_remove_distribution_routes_through_tracking(
        self, tmp_path, monkeypatch
    ):
        mgr, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(es, "_PENDING_INSTALL_DONE", None)
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        observed: list[bool] = []

        def _fake_uninstall(dist_name: str, *, target: str | None = None) -> bool:
            with es._PENDING_INSTALL_LOCK:
                observed.append(es._PENDING_INSTALL_DONE is not None)
            return True

        monkeypatch.setattr(es, "_uninstall_distribution", _fake_uninstall)

        await mgr._async_remove_distribution("ha-mcp-dev")

        assert observed == [True]
        with es._PENDING_INSTALL_LOCK:
            assert es._PENDING_INSTALL_DONE is None

    async def test_cancelled_awaiter_does_not_cancel_the_executor_job(
        self, tmp_path, monkeypatch
    ):
        # The dispatch is shielded: cancelling the awaiting bring-up must
        # leave the executor job to run (a QUEUED job cancelled with the
        # awaiter would never run _run, stranding the registration forever).
        mgr, hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(es, "_PENDING_INSTALL_DONE", None)
        inner: asyncio.Future = asyncio.get_running_loop().create_future()
        monkeypatch.setattr(hass, "async_add_executor_job", lambda fn, *a: inner)

        task = asyncio.create_task(
            mgr._async_run_tracked_install_job(lambda: "never observed")
        )
        await asyncio.sleep(0)  # let the task reach the shielded await
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            # Assignment form only to keep CodeQL's ineffectual-statement
            # heuristic quiet: the await IS the effect (it delivers the
            # cancellation this context manager asserts) and raises before
            # the bind ever happens.
            _ = await task

        assert not inner.cancelled()  # the job survives the awaiter's cancel
        inner.set_result(None)
        await asyncio.sleep(0)

    async def test_dispatch_failure_clears_own_registration(
        self, tmp_path, monkeypatch
    ):
        # If dispatch itself raises (executor shut down), _run never starts
        # and nothing would ever set the event - the registration must be
        # rolled back or the next bring-up waits the full budget on a job
        # that does not exist.
        mgr, hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(es, "_PENDING_INSTALL_DONE", None)

        def _refuse(func, *args):
            raise RuntimeError("cannot schedule new futures after shutdown")

        monkeypatch.setattr(hass, "async_add_executor_job", _refuse)

        with pytest.raises(RuntimeError, match="cannot schedule"):
            await mgr._async_run_tracked_install_job(lambda: "never runs")

        with es._PENDING_INSTALL_LOCK:
            assert es._PENDING_INSTALL_DONE is None

    async def test_wait_noop_when_pending_already_set(
        self, tmp_path, monkeypatch, caplog
    ):
        mgr, _hass, _entry = _manager(tmp_path)
        done = threading.Event()
        done.set()
        monkeypatch.setattr(es, "_PENDING_INSTALL_DONE", done)

        with caplog.at_level("WARNING"):
            await mgr._async_wait_for_pending_install()  # must not raise

        assert "install job is still running" not in caplog.text

    async def test_ensure_package_waits_before_touching_anything(
        self, tmp_path, monkeypatch
    ):
        mgr, _hass, _entry = _manager(tmp_path)

        class _Sentinel(Exception):
            pass

        monkeypatch.setattr(
            mgr,
            "_async_wait_for_pending_install",
            AsyncMock(side_effect=_Sentinel),
        )
        with pytest.raises(_Sentinel):
            await mgr._async_ensure_package()


class TestImporterAwareBringUp:
    """async_start must register its worker itself and defer package
    mutations while a previous worker is still importing (review findings:
    the worker-side add left a pre-registration window, and ensure-package
    could replace files on disk under a live importer)."""

    def _stub_bring_up(self, mgr, monkeypatch, ensure: AsyncMock) -> None:
        monkeypatch.setattr(mgr, "_async_ensure_package", ensure)
        monkeypatch.setattr(
            mgr, "_async_provision_token", AsyncMock(return_value="tok")
        )
        monkeypatch.setattr(mgr, "_prepare_config_dir", lambda: None)
        monkeypatch.setattr(mgr, "_async_wait_until_ready", AsyncMock())
        monkeypatch.setattr(es, "_purge_ha_mcp_modules", lambda: None)

    async def test_async_start_registers_worker_before_it_runs(
        self, tmp_path, monkeypatch
    ):
        mgr, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(es, "_IMPORTING_WORKERS", set())
        ensure = AsyncMock(return_value="1.2.3")
        self._stub_bring_up(mgr, monkeypatch, ensure)
        recorded: list[bool] = []

        def _stub_main(token: str) -> None:
            recorded.append(threading.current_thread() in es._IMPORTING_WORKERS)

        monkeypatch.setattr(mgr, "_thread_main", _stub_main)

        await mgr.async_start()
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)

        assert recorded == [True]

    async def test_async_start_defers_install_while_importer_busy(
        self, tmp_path, monkeypatch
    ):
        mgr, _hass, _entry = _manager(tmp_path)

        class _AliveWorker:
            def is_alive(self):
                return True

        monkeypatch.setattr(es, "_IMPORTING_WORKERS", {_AliveWorker()})
        ensure = AsyncMock(return_value="1.2.3")
        self._stub_bring_up(mgr, monkeypatch, ensure)
        monkeypatch.setattr(mgr, "_thread_main", lambda token: None)

        await mgr.async_start()
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)

        assert ensure.await_args.kwargs == {"defer_mutations": True}

    async def test_async_start_allows_install_when_no_importer(
        self, tmp_path, monkeypatch
    ):
        mgr, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(es, "_IMPORTING_WORKERS", set())
        ensure = AsyncMock(return_value="1.2.3")
        self._stub_bring_up(mgr, monkeypatch, ensure)
        monkeypatch.setattr(mgr, "_thread_main", lambda token: None)

        await mgr.async_start()
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)

        assert ensure.await_args.kwargs == {"defer_mutations": False}

    async def test_ensure_package_defers_force_install(
        self, tmp_path, monkeypatch, caplog
    ):
        # A dev-dist override beside the stable dist would normally take the
        # uninstall + reinstall path; with defer_mutations and a build already
        # on disk it must not touch the package at all (not even the
        # requirements manager, which installs any unsatisfied spec) and say so.
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_PIP_SPEC: "ha-mcp-dev==7.12.1.dev6"},
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: "ha-mcp-dev"},
        )

        def installed_version(preferred_dist: str | None = None) -> str | None:
            return "7.12.1.dev5"

        monkeypatch.setattr(es, "_installed_ha_mcp_version", installed_version)
        fast = AsyncMock()
        force = AsyncMock()
        uninstall = MagicMock(side_effect=AssertionError("package uninstall"))
        monkeypatch.setattr(es, "_uninstall_distribution", uninstall)
        monkeypatch.setattr(es, "_dist_installed", lambda name: True)
        monkeypatch.setattr(es, "_installed_dist_version", lambda name: "7.12.1.dev5")
        remove_legacy = AsyncMock()
        remove_replaced = AsyncMock()
        monkeypatch.setattr(mgr, "_async_process_requirements_fast", fast)
        monkeypatch.setattr(mgr, "_async_force_install", force)
        monkeypatch.setattr(mgr, "_async_remove_legacy_target", remove_legacy)
        monkeypatch.setattr(mgr, "_async_remove_replaced_source", remove_replaced)

        with caplog.at_level("WARNING"):
            ready = await mgr._async_ensure_package(defer_mutations=True)

        assert ready == "7.12.1.dev5"
        fast.assert_not_awaited()
        force.assert_not_awaited()
        remove_legacy.assert_not_awaited()
        remove_replaced.assert_not_awaited()
        assert "Deferring the ha-mcp install/upgrade" in caplog.text

    async def test_deferred_spec_change_is_not_recorded_as_installed(
        self, tmp_path, monkeypatch
    ):
        # A deferred spec change must stay pending: recording the NEW spec as
        # installed would make the next reload see "unchanged", skip the
        # replaced-source uninstall (and, for a stable spec, take the fast
        # path), so the deferred change would silently never apply (#1914).
        tarball = (
            "https://github.com/homeassistant-ai/ha-mcp/archive/refs/pull/"
            "1234/head.tar.gz"
        )
        mgr, _hass, entry = _manager(
            tmp_path,
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: tarball},
        )
        monkeypatch.setattr(
            es, "_installed_ha_mcp_version", lambda preferred_dist=None: "7.13.0"
        )
        fast = AsyncMock()
        force = AsyncMock()
        monkeypatch.setattr(mgr, "_async_process_requirements_fast", fast)
        monkeypatch.setattr(mgr, "_async_force_install", force)

        await mgr._async_ensure_package(defer_mutations=True)

        fast.assert_not_awaited()
        force.assert_not_awaited()
        assert entry.data[DATA_LAST_PIP_SPEC] == tarball

    async def test_deferred_incompatible_build_raises_at_compat_gate(
        self, tmp_path, monkeypatch
    ):
        # defer_mutations with an importable-but-legacy build on disk: the
        # deferred branch touches nothing (replacing files under the live
        # importer is the corruption being avoided), so the post-branch
        # compatibility gate rejects the build loudly instead of silently
        # serving it. The next (undeferred) reload installs for real.
        mgr, _hass, entry = _manager(tmp_path)
        monkeypatch.setattr(
            es, "_installed_ha_mcp_version", lambda preferred_dist=None: "6.2.0"
        )
        fast = AsyncMock()
        force = AsyncMock()
        monkeypatch.setattr(mgr, "_async_process_requirements_fast", fast)
        monkeypatch.setattr(mgr, "_async_force_install", force)

        with pytest.raises(es.EmbeddedServerError) as exc:
            await mgr._async_ensure_package(defer_mutations=True)

        assert exc.value.kind == "package"
        fast.assert_not_awaited()
        force.assert_not_awaited()
        assert DATA_LAST_PIP_SPEC not in entry.data

    async def test_deferred_with_nothing_installed_still_installs(
        self, tmp_path, monkeypatch
    ):
        # defer_mutations with NO build on disk: there are no distribution
        # files to replace under the live importer, and without an install this
        # bring-up cannot produce a server at all — the requirements manager
        # must still run.
        mgr, _hass, entry = _manager(tmp_path)
        monkeypatch.setattr(
            es,
            "_installed_ha_mcp_version",
            MagicMock(side_effect=[None, "7.13.0"]),
        )
        fast = AsyncMock()
        force = AsyncMock()
        monkeypatch.setattr(mgr, "_async_process_requirements_fast", fast)
        monkeypatch.setattr(mgr, "_async_force_install", force)

        ready = await mgr._async_ensure_package(defer_mutations=True)

        assert ready == "7.13.0"
        fast.assert_awaited_once()
        force.assert_not_awaited()
        # Still a deferred bring-up: nothing is recorded as installed, so the
        # next (undeferred) reload applies the configured spec for real.
        assert DATA_LAST_PIP_SPEC not in entry.data


class TestPurgeSkippedOnWarmCache:
    """A retry with an unchanged install must reuse the warm module cache.

    Issue #1904: purging on every attempt made each retry pay the full cold
    import again, so slow hardware that missed the readiness window once
    could never recover.
    """

    def _start_kwargs(
        self,
        mgr: es.EmbeddedServerManager,
        monkeypatch: pytest.MonkeyPatch,
        purges: list[bool],
        ready_version: str | None,
    ) -> None:
        monkeypatch.setattr(
            mgr, "_async_ensure_package", AsyncMock(return_value=ready_version)
        )
        monkeypatch.setattr(
            mgr, "_async_provision_token", AsyncMock(return_value="tok")
        )
        monkeypatch.setattr(mgr, "_prepare_config_dir", lambda: None)
        monkeypatch.setattr(mgr, "_async_wait_until_ready", AsyncMock())
        monkeypatch.setattr(mgr, "_thread_main", lambda token: None)
        monkeypatch.setattr(es, "_purge_ha_mcp_modules", lambda: purges.append(True))

    async def test_purge_skipped_when_cache_matches_installed(
        self, tmp_path, monkeypatch
    ):
        mgr, _hass, _entry = _manager(tmp_path)
        purges: list[bool] = []
        self._start_kwargs(mgr, monkeypatch, purges, "1.2.3")
        monkeypatch.setattr(es, "_CACHED_IMPORT_VERSION", "1.2.3")

        await mgr.async_start()
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)

        assert purges == []

    async def test_purge_runs_when_cache_differs(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(tmp_path)
        purges: list[bool] = []
        self._start_kwargs(mgr, monkeypatch, purges, "1.2.3")
        monkeypatch.setattr(es, "_CACHED_IMPORT_VERSION", "1.2.2")

        await mgr.async_start()
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)

        assert purges == [True]

    async def test_purge_runs_under_pip_spec_override_despite_match(
        self, tmp_path, monkeypatch
    ):
        # A pip-spec override can re-point to different code under the SAME
        # version string, so the warm-cache skip must never fire for it.
        mgr, _hass, _entry = _manager(tmp_path, options={OPT_PIP_SPEC: "ha-mcp==1.2.3"})
        purges: list[bool] = []
        self._start_kwargs(mgr, monkeypatch, purges, "1.2.3")
        monkeypatch.setattr(es, "_CACHED_IMPORT_VERSION", "1.2.3")

        await mgr.async_start()
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)

        assert purges == [True]

    async def test_purge_runs_when_cache_unknown(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(tmp_path)
        purges: list[bool] = []
        self._start_kwargs(mgr, monkeypatch, purges, "1.2.3")
        monkeypatch.setattr(es, "_CACHED_IMPORT_VERSION", None)

        await mgr.async_start()
        if mgr._thread is not None:
            mgr._thread.join(timeout=2)

        assert purges == [True]


class TestWarmCacheVersionAgreement:
    """The two sides of the warm-cache comparison must agree for a plain
    install, or the purge skip could never fire in production: async_start
    keys on _async_ensure_package's return while the worker records
    _running_ha_mcp_version into _CACHED_IMPORT_VERSION (review gap)."""

    def _stub_install_surface(
        self, monkeypatch: pytest.MonkeyPatch, versions: dict[str, str]
    ) -> None:
        def installed_version(preferred_dist: str | None = None) -> str | None:
            if preferred_dist is not None:
                return versions.get(preferred_dist)
            return versions.get(DIST_NAME_STABLE) or versions.get(DIST_NAME_DEV)

        monkeypatch.setattr(es, "_force_install_package", MagicMock(return_value=True))
        monkeypatch.setattr(es, "pip_kwargs", lambda cfg: {})
        monkeypatch.setattr(es, "_installed_ha_mcp_version", installed_version)
        monkeypatch.setattr(es, "_installed_dist_version", versions.get)
        monkeypatch.setattr(es, "_dist_installed", lambda name: False)
        monkeypatch.setattr(
            es, "_uninstall_distribution", MagicMock(return_value=False)
        )

    async def test_dev_dist_sides_agree_despite_stale_stable_metadata(
        self, tmp_path, monkeypatch
    ):
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={OPT_PIP_SPEC: "ha-mcp-dev==7.12.1.dev5"},
            data={
                DATA_SECRET_PATH: "/p",
                DATA_LAST_PIP_SPEC: "ha-mcp-dev==7.12.1.dev5",
            },
        )
        versions = {DIST_NAME_STABLE: "6.2.0", DIST_NAME_DEV: "7.12.1.dev5"}
        self._stub_install_surface(monkeypatch, versions)
        fake = ModuleType("ha_mcp")
        fake.__version__ = versions[DIST_NAME_STABLE]  # stale stable metadata
        monkeypatch.setitem(sys.modules, "ha_mcp", fake)

        ready_version = await mgr._async_ensure_package()

        assert ready_version == versions[DIST_NAME_DEV]
        assert es._running_ha_mcp_version(DIST_NAME_DEV) == ready_version

    async def test_stable_channel_sides_agree(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(
            tmp_path,
            data={DATA_SECRET_PATH: "/p", DATA_LAST_PIP_SPEC: DEFAULT_PIP_SPEC},
        )
        versions = {DIST_NAME_STABLE: "7.13.0"}
        self._stub_install_surface(monkeypatch, versions)
        fake = ModuleType("ha_mcp")
        fake.__version__ = versions[DIST_NAME_STABLE]
        monkeypatch.setitem(sys.modules, "ha_mcp", fake)

        ready_version = await mgr._async_ensure_package()

        assert ready_version == versions[DIST_NAME_STABLE]
        assert es._running_ha_mcp_version(DIST_NAME_STABLE) == ready_version


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
        monkeypatch.setattr(es, "_installed_dist_version", lambda dist: None)

        monkeypatch.setattr(es, "_CACHED_IMPORT_VERSION", None)
        mgr._thread_main("tok")

        assert mgr._running_version == "9.8.7"
        # The warm-cache purge skip keys on this recording (issue #1904).
        assert es._CACHED_IMPORT_VERSION == "9.8.7"
        # _serve advanced through its phase markers before http_app raised -
        # a dropped or mislabeled _note_startup_phase call surfaces here.
        assert mgr._startup_phase == "registering web routes"
        assert isinstance(mgr._thread_exc, _StopServe)

    def test_serve_prefers_the_override_dev_metadata(self, tmp_path, monkeypatch):
        mgr, _hass, _entry = _manager(
            tmp_path,
            options={
                OPT_PIP_SPEC: "ha-mcp-dev==7.13.0.dev1",
                OPT_SERVER_URL: "http://ha.local:8123",
            },
        )

        class _StopServe(Exception):
            pass

        class _FakeMcp:
            def http_app(self, path, stateless_http):
                raise _StopServe

        _stub_ha_mcp_surface(monkeypatch, mcp=_FakeMcp())
        # ha_mcp.__version__ checks stable metadata first, so a failed
        # best-effort uninstall can make freshly imported dev code report the
        # stale stable version.
        sys.modules["ha_mcp"].__version__ = "6.2.0"
        versions = {
            DIST_NAME_STABLE: "6.2.0",
            DIST_NAME_DEV: "7.13.0.dev1",
        }
        monkeypatch.setattr(es, "_installed_dist_version", versions.get)

        mgr._thread_main("tok")

        assert mgr._running_version == "7.13.0.dev1"
        assert isinstance(mgr._thread_exc, _StopServe)


# =============================================================================
# _force_install_package: scoped upgrade, never uv's eager --upgrade (#2146)
# =============================================================================


class TestForceInstallPackage:
    """The force install may only ever touch ha-mcp's own distribution.

    uv's bare ``--upgrade`` eagerly re-resolves the whole dependency graph
    and replaces packages the Home Assistant image already ships even when
    the installed versions satisfy our specs — the #2135/#2146 torn-install
    window. Which scoped flag replaces it depends on the SPEC SHAPE, and
    both directions are load-bearing (measured against uv 0.11.33, the
    version CI pins):

    * URL spec + ``--upgrade-package`` -> "Checked 1 package", installs
      NOTHING, so the old code keeps running while bring-up logs success.
      Only ``--reinstall-package`` actually replaces it.
    * Index spec + ``--reinstall-package`` -> forced uninstall-then-extract
      on every bring-up, reopening the very window this PR closes.
    """

    def _run_capture(self, monkeypatch):
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stderr="")

        monkeypatch.setattr(es.subprocess, "run", fake_run)
        return captured

    def _install(self, spec, target_dist="ha-mcp", **overrides):
        params = {
            "target_dist": target_dist,
            "constraints": None,
            "target": None,
            "timeout": None,
        }
        params.update(overrides)
        return es._force_install_package(spec, **params)

    def test_never_passes_the_eager_upgrade_flag(self, monkeypatch):
        captured = self._run_capture(monkeypatch)
        assert self._install("ha-mcp")
        assert "--upgrade" not in captured["args"], (
            "bare uv --upgrade eagerly replaces the whole graph — the exact "
            "#2135/#2146 mechanism this function exists to prevent"
        )

    def test_reinstall_replaces_an_index_spec_whose_version_matches(self, monkeypatch):
        # After the other server dist was removed (taking the shared ha_mcp
        # files with it) a version-satisfied upgrade would install nothing.
        captured = self._run_capture(monkeypatch)
        assert self._install("ha-mcp==8.6.0", reinstall=True)
        args = captured["args"]
        assert args[args.index("--reinstall-package") + 1] == "ha-mcp"
        assert "--upgrade-package" not in args

    def test_index_spec_upgrades_only_its_own_distribution(self, monkeypatch):
        captured = self._run_capture(monkeypatch)
        assert self._install("ha-mcp", target_dist="ha-mcp")
        args = captured["args"]
        assert args[:6] == [sys.executable, "-m", "uv", "pip", "install", "--quiet"]
        assert args[args.index("--upgrade-package") + 1] == "ha-mcp"
        assert "--reinstall-package" not in args, (
            "force-reinstalling an index requirement reopens the non-atomic "
            "replacement window for a spec that never needed it"
        )

    def test_forked_index_spec_is_scoped_to_the_fork_not_reinstalled(self, monkeypatch):
        """A pinned fork must be upgrade-scoped to ITSELF, never reinstalled.

        Regression guard: routing on "which distribution did we recognise"
        instead of "is this a URL" sent this spec down the reinstall path,
        forcing an uninstall-then-extract on every single bring-up.
        """
        captured = self._run_capture(monkeypatch)
        assert self._install("my-fork==1.0", target_dist="ha-mcp")
        args = captured["args"]
        assert "--reinstall-package" not in args
        assert args[args.index("--upgrade-package") + 1] == "my-fork"

    def test_url_spec_is_reinstalled_not_merely_upgraded(self, monkeypatch):
        """A URL spec MUST carry --reinstall-package.

        Regression guard for the inverted routing: with
        ``--upgrade-package`` (or no flag) uv audits an unchanged URL and
        installs nothing, so a rebuilt wheel at the same path leaves the
        old server running while the bring-up reports success — and
        ``_replaced_dist_name`` / ``_async_remove_replaced_source`` skip
        their uninstall precisely because a URL install is supposed to be
        real.
        """
        captured = self._run_capture(monkeypatch)
        assert self._install("ha-mcp @ file:///config/ha_mcp-1.0-py3-none-any.whl")
        args = captured["args"]
        assert args[args.index("--reinstall-package") + 1] == "ha-mcp"
        assert "--upgrade-package" not in args
        assert "--upgrade" not in args

    def test_bare_url_is_scoped_to_every_known_distribution(self, monkeypatch):
        """A bare URL names no distribution, so both candidates are named.

        The channel's dist alone is the wrong guess: a repository tarball
        installs as ``ha-mcp`` whatever channel is selected, so on the dev
        channel ``--reinstall-package ha-mcp-dev`` names a package the URL
        does not provide — uv reports success while the real ``ha-mcp``
        keeps running, and a mutable URL holds its version string so
        nothing downstream notices (Codex review on #2150).
        """
        captured = self._run_capture(monkeypatch)
        assert self._install(
            "https://example.invalid/ha_mcp.tar.gz", target_dist="ha-mcp-dev"
        )
        args = captured["args"]
        scoped = [
            args[i + 1]
            for i, value in enumerate(args)
            if value == "--reinstall-package"
        ]
        assert scoped == ["ha-mcp-dev", "ha-mcp"]

    def test_bare_url_without_a_target_dist_still_scopes_both(self, monkeypatch):
        captured = self._run_capture(monkeypatch)
        assert self._install("https://example.invalid/ha_mcp.tar.gz", target_dist=None)
        args = captured["args"]
        scoped = [
            args[i + 1]
            for i, value in enumerate(args)
            if value == "--reinstall-package"
        ]
        assert scoped == ["ha-mcp", "ha-mcp-dev"]
        assert "--upgrade-package" not in args

    def test_constraints_and_http_timeout_are_threaded(self, monkeypatch):
        captured = self._run_capture(monkeypatch)
        assert self._install("ha-mcp", constraints="/cons.txt", timeout=120)
        args = captured["args"]
        assert args[args.index("--constraint") + 1] == "/cons.txt"
        assert captured["kwargs"]["env"]["HTTP_TIMEOUT"] == "120"

    def test_subprocess_call_is_time_bounded(self, monkeypatch):
        """A wedged uv must not hold the tracked-install slot forever.

        The extra-index fallback can run uv twice inside that slot, so an
        unbounded call would block the next bring-up indefinitely.
        """
        captured = self._run_capture(monkeypatch)
        assert self._install("ha-mcp")
        assert captured["kwargs"]["timeout"] == es._UV_INSTALL_TIMEOUT_SECONDS

    def test_timeout_expiry_reports_failure(self, monkeypatch):
        monkeypatch.setattr(
            es.subprocess,
            "run",
            MagicMock(side_effect=es.subprocess.TimeoutExpired(cmd="uv", timeout=1)),
        )
        assert not self._install("ha-mcp")

    def test_target_install_uses_target_path(self, monkeypatch, tmp_path):
        captured = self._run_capture(monkeypatch)
        assert self._install("ha-mcp", target=str(tmp_path))
        args = captured["args"]
        assert args[args.index("--target") + 1] == os.path.abspath(str(tmp_path))

    def test_non_virtualenv_install_uses_the_user_site_workaround(
        self, monkeypatch, tmp_path
    ):
        """The branch that runs on a non-venv HA install (uv has no --user).

        The shared stub pins is_virtual_env to True for determinism, so
        without flipping it here this path — the one real HA Core installs
        take — would never execute in any test.
        """
        captured = self._run_capture(monkeypatch)
        monkeypatch.setattr(es, "is_virtual_env", lambda: False)
        monkeypatch.setattr(es.site, "getusersitepackages", lambda: str(tmp_path))
        monkeypatch.delenv("UV_SYSTEM_PYTHON", raising=False)
        monkeypatch.delenv("UV_PYTHON", raising=False)

        assert self._install("ha-mcp")

        args = captured["args"]
        assert args[args.index("--python") + 1] == sys.executable
        assert args[args.index("--target") + 1] == os.path.abspath(str(tmp_path))

    def test_explicit_uv_python_selection_skips_the_workaround(
        self, monkeypatch, tmp_path
    ):
        captured = self._run_capture(monkeypatch)
        monkeypatch.setattr(es, "is_virtual_env", lambda: False)
        monkeypatch.setattr(es.site, "getusersitepackages", lambda: str(tmp_path))
        monkeypatch.setenv("UV_SYSTEM_PYTHON", "1")

        assert self._install("ha-mcp")

        assert "--target" not in captured["args"]

    def test_nonzero_exit_reports_failure(self, monkeypatch):
        monkeypatch.setattr(
            es.subprocess,
            "run",
            MagicMock(return_value=SimpleNamespace(returncode=2, stderr="boom")),
        )
        assert not self._install("ha-mcp")

    def test_failing_extra_index_host_is_dropped_and_retried(self, monkeypatch):
        """A wheels-index outage must not fail an install PyPI can satisfy.

        uv treats a failing extra index as FATAL where pip merely skips it,
        so homeassistant.util.package.install_package retries with the
        offending host removed. This installer replaced install_package, so
        it has to preserve that fallback.
        """
        calls: list[dict[str, str]] = []

        def fake_run(args, **kwargs):
            env = kwargs["env"]
            calls.append(env)
            # Match the HOST by equality, the way the installer's own retry
            # does — a substring test over the whole index list would also
            # "match" a URL that merely mentions the host in a path or query
            # (and CodeQL's py/incomplete-url-substring-sanitization reads
            # any `in` against URL-derived data as exactly that mistake).
            if any(
                urlparse(url).hostname == _WHEELS_HOST
                for url in env.get("UV_EXTRA_INDEX_URL", "").split()
            ):
                return SimpleNamespace(
                    returncode=1,
                    stderr="error: failed to fetch https://wheels.home-assistant.io/x",
                )
            return SimpleNamespace(returncode=0, stderr="")

        monkeypatch.setattr(es.subprocess, "run", fake_run)
        monkeypatch.setenv(
            "UV_EXTRA_INDEX_URL",
            "https://wheels.home-assistant.io/simple https://healthy.example/simple",
        )

        assert self._install("ha-mcp")

        assert len(calls) == 2, "the failing extra index was not retried"
        retry_extra = calls[1].get("UV_EXTRA_INDEX_URL", "")
        assert "wheels.home-assistant.io" not in retry_extra
        assert "healthy.example" in retry_extra

    def test_sole_failing_extra_index_is_removed_entirely(self, monkeypatch):
        calls: list[dict[str, str]] = []

        def fake_run(args, **kwargs):
            env = kwargs["env"]
            calls.append(env)
            if "UV_EXTRA_INDEX_URL" in env:
                return SimpleNamespace(
                    returncode=1, stderr="failed to fetch https://wheels.example/x"
                )
            return SimpleNamespace(returncode=0, stderr="")

        monkeypatch.setattr(es.subprocess, "run", fake_run)
        monkeypatch.setenv("UV_EXTRA_INDEX_URL", "https://wheels.example/simple")

        assert self._install("ha-mcp")

        assert len(calls) == 2
        assert "UV_EXTRA_INDEX_URL" not in calls[1]

    def test_malformed_extra_index_url_does_not_break_the_failure_path(
        self, monkeypatch
    ):
        """A typo'd index URL must not replace uv's stderr with a traceback.

        ``urlparse("https://[bad").hostname`` raises ValueError (Invalid IPv6
        URL), and this code runs only AFTER an install has already failed —
        so an operator's malformed UV_EXTRA_INDEX_URL entry would turn a
        reportable install failure into an exception from the error handler.
        """
        calls: list[dict[str, str]] = []

        def fake_run(args, **kwargs):
            env = kwargs["env"]
            calls.append(env)
            if _WHEELS_HOST in env.get("UV_EXTRA_INDEX_URL", ""):
                return SimpleNamespace(
                    returncode=1,
                    stderr=f"error: failed to fetch https://{_WHEELS_HOST}/x",
                )
            return SimpleNamespace(returncode=0, stderr="")

        monkeypatch.setattr(es.subprocess, "run", fake_run)
        monkeypatch.setenv(
            "UV_EXTRA_INDEX_URL",
            f"https://[bad/simple https://{_WHEELS_HOST}/simple",
        )

        assert self._install("ha-mcp")

        assert len(calls) == 2, "the failing extra index was not retried"
        retry_extra = calls[1].get("UV_EXTRA_INDEX_URL", "")
        assert _WHEELS_HOST not in retry_extra
        # The unparseable entry is kept: we could not prove it was at fault.
        assert "https://[bad/simple" in retry_extra

    def test_unrelated_failure_is_not_retried(self, monkeypatch):
        """Only an extra-index failure earns a second attempt."""
        calls: list[dict[str, str]] = []

        def fake_run(args, **kwargs):
            calls.append(kwargs["env"])
            return SimpleNamespace(returncode=1, stderr="No matching distribution")

        monkeypatch.setattr(es.subprocess, "run", fake_run)
        monkeypatch.setenv("UV_EXTRA_INDEX_URL", "https://wheels.example/simple")

        assert not self._install("ha-mcp")

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_manager_threads_pip_kwargs_and_target_dist(
        self, tmp_path, monkeypatch
    ):
        """_async_force_install passes HA's constraints/target + the target dist."""
        manager, _hass, _entry = _manager(tmp_path)
        monkeypatch.setattr(
            es,
            "pip_kwargs",
            MagicMock(return_value={"constraints": "/hacons.txt", "timeout": 5}),
        )
        force = MagicMock(name="force_install", return_value=True)
        monkeypatch.setattr(es, "_force_install_package", force)

        await manager._async_force_install()

        force.assert_called_once()
        kwargs = force.call_args.kwargs
        assert kwargs["constraints"] == "/hacons.txt"
        assert kwargs["target"] is None
        assert kwargs["target_dist"] == DIST_NAME_STABLE
        assert kwargs["reinstall"] is False
        assert kwargs["timeout"] >= es._PIP_INSTALL_TIMEOUT_SECONDS
