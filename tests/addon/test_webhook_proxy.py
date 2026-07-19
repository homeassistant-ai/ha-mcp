"""Tests for the webhook proxy addon.

Structure tests verify addon files and config.yaml.
Unit tests mock Supervisor API calls to test discovery logic in start.py.
"""

import importlib
import importlib.util
import json
import logging
import os
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

WEBHOOK_PROXY_VARIANTS = {
    "stable": {
        "key": "stable",
        "addon_dir": "homeassistant-addon-webhook-proxy",
        "component": "mcp_proxy",
        "domain": "mcp_proxy",
        "slug": "ha_mcp_webhook_proxy",
        "oauth_base": "/api/mcp_proxy/oauth",
        "config_file": "/config/.mcp_proxy_config.json",
        "inbound_log": "/config/.mcp_proxy_inbound.log",
        "oauth_marker": "/config/.mcp_proxy_oauth_restart_required",
        "sibling_base": "ha_mcp_webhook_proxy_dev",
        "mutex_id": "mcp_proxy_mutex",
    },
    "dev": {
        "key": "dev",
        "addon_dir": "homeassistant-addon-webhook-proxy-dev",
        "component": "mcp_proxy_dev",
        "domain": "mcp_proxy_dev",
        "slug": "ha_mcp_webhook_proxy_dev",
        "oauth_base": "/api/mcp_proxy_dev/oauth",
        "config_file": "/config/.mcp_proxy_dev_config.json",
        "inbound_log": "/config/.mcp_proxy_dev_inbound.log",
        "oauth_marker": "/config/.mcp_proxy_dev_oauth_restart_required",
        "sibling_base": "ha_mcp_webhook_proxy",
        "mutex_id": "mcp_proxy_dev_mutex",
    },
}

# Rebound per-variant by the autouse `_webhook_proxy_variant` fixture below.
PROXY_ADDON_DIR = WEBHOOK_PROXY_VARIANTS["stable"]["addon_dir"]
CURRENT = WEBHOOK_PROXY_VARIANTS["stable"]


@pytest.fixture(
    autouse=True,
    params=list(WEBHOOK_PROXY_VARIANTS.values()),
    ids=lambda v: v["key"],
)
def _webhook_proxy_variant(request, monkeypatch):
    """Rebind PROXY_ADDON_DIR/CURRENT so every test in this module runs once
    per addon flavor (stable, dev). monkeypatch auto-reverts after each test."""
    variant = request.param
    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "PROXY_ADDON_DIR", variant["addon_dir"])
    monkeypatch.setattr(mod, "CURRENT", variant)
    return variant


# ---------------------------------------------------------------------------
# Helper: import start.py from the addon directory
# ---------------------------------------------------------------------------


def _import_start():
    """Import the webhook proxy start.py as a module."""
    start_path = os.path.join(PROXY_ADDON_DIR, "start.py")
    spec = importlib.util.spec_from_file_location(
        f"webhook_proxy_start_{CURRENT['key']}", start_path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helper: import mcp_proxy/__init__.py with homeassistant imports stubbed
# ---------------------------------------------------------------------------


class _FakeConfigEntryError(Exception):
    pass


def _install_runtime_stubs():
    """Inject homeassistant.* and aiohttp stubs into sys.modules.

    The custom integration imports from these packages at module load.
    Neither is in our dev dependencies (homeassistant only exists inside
    HA Core at runtime; aiohttp ships with HA's own deps), so tests stub
    just enough surface area to satisfy the imports.
    """
    ha = types.ModuleType("homeassistant")
    ha_components = types.ModuleType("homeassistant.components")
    ha_webhook = types.ModuleType("homeassistant.components.webhook")
    ha_webhook.async_register = MagicMock(name="async_register")
    ha_webhook.async_unregister = MagicMock(name="async_unregister")
    ha_config_entries = types.ModuleType("homeassistant.config_entries")
    ha_config_entries.ConfigEntry = MagicMock
    ha_core = types.ModuleType("homeassistant.core")
    ha_core.HomeAssistant = MagicMock
    ha_core.ServiceCall = MagicMock
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_helpers_typing = types.ModuleType("homeassistant.helpers.typing")
    ha_helpers_typing.ConfigType = dict
    ha_exceptions = types.ModuleType("homeassistant.exceptions")
    ha_exceptions.ConfigEntryError = _FakeConfigEntryError

    aiohttp_mod = types.ModuleType("aiohttp")
    aiohttp_mod.ClientSession = MagicMock(name="ClientSession")
    aiohttp_mod.ClientTimeout = MagicMock(name="ClientTimeout")
    aiohttp_mod.ClientError = type("ClientError", (Exception,), {})
    aiohttp_web = types.ModuleType("aiohttp.web")
    aiohttp_web.Request = MagicMock
    aiohttp_web.Response = MagicMock
    aiohttp_web.StreamResponse = MagicMock
    aiohttp_web.json_response = MagicMock(name="json_response")
    aiohttp_mod.web = aiohttp_web

    # yarl ships with aiohttp; OAuth views import it lazily for redirect
    # construction. Use the real package if available, otherwise minimal stub.
    try:
        import yarl as _real_yarl

        yarl_mod = _real_yarl
    except ImportError:
        yarl_mod = types.ModuleType("yarl")

        class _StubURL:
            def __init__(self, url):
                self._url = url
                self._extra: dict[str, str] = {}

            def update_query(self, params):
                new = _StubURL(self._url)
                new._extra = {**self._extra, **dict(params)}
                return new

            def __str__(self):
                if not self._extra:
                    return self._url
                from urllib.parse import urlencode

                sep = "&" if "?" in self._url else "?"
                return f"{self._url}{sep}{urlencode(self._extra)}"

        yarl_mod.URL = _StubURL

    # Stub the HA HTTP module just enough for the OAuth views to import
    ha_components_http = types.ModuleType("homeassistant.components.http")
    ha_components_http.HomeAssistantView = type(
        "HomeAssistantView", (), {"requires_auth": True, "cors_allowed": False}
    )

    # repairs.py imports — stub the surface area it needs
    ha_components_repairs = types.ModuleType("homeassistant.components.repairs")
    ha_components_repairs.RepairsFlow = type("RepairsFlow", (), {})
    ha_data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")
    ha_data_entry_flow.FlowResult = dict
    ha_helpers_issue_registry = types.ModuleType("homeassistant.helpers.issue_registry")
    ha_helpers_issue_registry.async_create_issue = MagicMock(name="async_create_issue")
    ha_helpers_issue_registry.async_delete_issue = MagicMock(name="async_delete_issue")

    class _IssueSeverity:
        ERROR = "error"
        WARNING = "warning"
        CRITICAL = "critical"

    ha_helpers_issue_registry.IssueSeverity = _IssueSeverity

    voluptuous_mod = types.ModuleType("voluptuous")
    voluptuous_mod.Schema = MagicMock(name="Schema")
    # __init__.py builds CONFIG_SCHEMA at import time with these, so the stub
    # must expose them or the module import raises AttributeError.
    voluptuous_mod.Optional = MagicMock(name="Optional")
    voluptuous_mod.Any = MagicMock(name="Any")
    voluptuous_mod.ALLOW_EXTRA = MagicMock(name="ALLOW_EXTRA")
    # __init__.py builds _REFRESH_REPAIRS_SCHEMA at import time with these.
    voluptuous_mod.Required = MagicMock(name="Required")
    voluptuous_mod.In = MagicMock(name="In")

    sys.modules.update(
        {
            "homeassistant": ha,
            "homeassistant.components": ha_components,
            "homeassistant.components.webhook": ha_webhook,
            "homeassistant.components.http": ha_components_http,
            "homeassistant.components.repairs": ha_components_repairs,
            "homeassistant.config_entries": ha_config_entries,
            "homeassistant.core": ha_core,
            "homeassistant.helpers": ha_helpers,
            "homeassistant.helpers.typing": ha_helpers_typing,
            "homeassistant.helpers.issue_registry": ha_helpers_issue_registry,
            "homeassistant.exceptions": ha_exceptions,
            "homeassistant.data_entry_flow": ha_data_entry_flow,
            "aiohttp": aiohttp_mod,
            "aiohttp.web": aiohttp_web,
            "yarl": yarl_mod,
            "voluptuous": voluptuous_mod,
        }
    )


def _import_mcp_proxy(preload_oauth=None):
    """Import the mcp_proxy package's __init__.py with HA imports stubbed.

    `preload_oauth`: pre-register a specific oauth module under the
    relative-import name (`mcp_proxy_init_<variant>.oauth`). Without this,
    the integration's `from .oauth import ...` calls would load a fresh
    oauth module pointing at /config — fine for production, useless for
    tests. The module name is suffixed with the active variant key so
    stable/dev imports never share (or clobber) each other's sys.modules
    entry.
    """
    _install_runtime_stubs()
    component_dir = os.path.join(PROXY_ADDON_DIR, CURRENT["component"])
    init_path = os.path.join(component_dir, "__init__.py")
    mod_name = f"mcp_proxy_init_{CURRENT['key']}"
    sys.modules.pop(mod_name, None)
    sys.modules.pop(f"{mod_name}.oauth", None)
    spec = importlib.util.spec_from_file_location(
        mod_name,
        init_path,
        submodule_search_locations=[component_dir],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    if preload_oauth is not None:
        sys.modules[f"{mod_name}.oauth"] = preload_oauth
    spec.loader.exec_module(mod)
    return mod


def _import_oauth(tmp_secret_dir=None):
    """Import the oauth submodule, optionally redirecting the secret file
    to a tmp dir so tests don't need root or write access to /config.

    Registers in sys.modules under `mcp_proxy_oauth_<variant>` (so test
    patches targeting that name resolve) and the module returned can be
    passed to `_import_mcp_proxy(preload_oauth=...)` so the integration's
    relative import resolves to the same instance.
    """
    _install_runtime_stubs()
    oauth_path = os.path.join(PROXY_ADDON_DIR, CURRENT["component"], "oauth.py")
    mod_name = f"mcp_proxy_oauth_{CURRENT['key']}"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, oauth_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    if tmp_secret_dir is not None:
        mod.SECRET_FILE = Path(tmp_secret_dir) / ".mcp_proxy_oauth_secret"
    return mod


def _bind_repairs(mod, tmp_marker_dir):
    """Load the repairs submodule and bind it as `<mod.__name__>.repairs` so
    the integration's `from .repairs import ...` (evaluated inside
    async_setup_entry at call time) resolves to THIS instance. The marker file
    is redirected into `tmp_marker_dir` so tests never touch /config, and the
    returned module can be patched (e.g. `create_issue`, `_write_marker`)
    before calling async_setup_entry — the `from .repairs import ...` reads the
    module attributes at execution time, so patches take effect.
    """
    repairs_path = os.path.join(PROXY_ADDON_DIR, CURRENT["component"], "repairs.py")
    submod_name = f"{mod.__name__}.repairs"
    sys.modules.pop(submod_name, None)
    spec = importlib.util.spec_from_file_location(submod_name, repairs_path)
    repairs = importlib.util.module_from_spec(spec)
    sys.modules[submod_name] = repairs
    spec.loader.exec_module(repairs)
    repairs.RESTART_MARKER_FILE = (
        Path(tmp_marker_dir) / ".mcp_proxy_oauth_restart_required"
    )
    return repairs


def _ha_auth_supported() -> bool:
    """Feature-detect whether the CURRENT flavor ships ha_auth mode (its
    `auth_native.py` module). The stable flavor skips the ha_auth tests until
    the module is promoted — same lockstep-with-code approach as
    `_wellknown_oauth_urls`."""
    return os.path.exists(
        os.path.join(PROXY_ADDON_DIR, CURRENT["component"], "auth_native.py")
    )


def _none_autoapprove_supported() -> bool:
    """Feature-detect whether the CURRENT flavor ships none-mode auto-approve
    discovery (its `oauth_autoapprove.py` module, issue #1969). The stable
    flavor skips these tests until the module is promoted — same
    lockstep-with-code approach as `_ha_auth_supported`."""
    return os.path.exists(
        os.path.join(PROXY_ADDON_DIR, CURRENT["component"], "oauth_autoapprove.py")
    )


def _import_none_autoapprove_stack():
    """Import the integration wired so none-mode auto-approve resolves.

    Loads __init__, oauth, oauth_autoapprove, and auth_native as submodules of
    ONE package so the relative imports resolve in every direction to the same
    instances: oauth_autoapprove does `from .oauth import ...` at load, the AS
    view does `from .oauth_autoapprove import ...` (none) / `from .auth_native
    import ...` (ha_auth) at request time, and auth_native does `from .oauth
    import ...` at load. Returns (init_mod, oauth_mod, autoapprove_mod,
    auth_native_mod).
    """
    _install_runtime_stubs()
    component_dir = os.path.join(PROXY_ADDON_DIR, CURRENT["component"])
    pkg_name = f"mcp_proxy_init_{CURRENT['key']}"
    for suffix in ("", ".oauth", ".oauth_autoapprove", ".auth_native", ".repairs"):
        sys.modules.pop(f"{pkg_name}{suffix}", None)
    init_path = os.path.join(component_dir, "__init__.py")
    init_spec = importlib.util.spec_from_file_location(
        pkg_name, init_path, submodule_search_locations=[component_dir]
    )
    pkg = importlib.util.module_from_spec(init_spec)
    sys.modules[pkg_name] = pkg
    # oauth first (oauth_autoapprove + auth_native both `from .oauth` at load).
    oauth = _load_pkg_submodule(pkg_name, component_dir, "oauth")
    autoapprove = _load_pkg_submodule(pkg_name, component_dir, "oauth_autoapprove")
    auth_native = _load_pkg_submodule(pkg_name, component_dir, "auth_native")
    init_spec.loader.exec_module(pkg)
    return pkg, oauth, autoapprove, auth_native


def _load_pkg_submodule(pkg_name, component_dir, sub):
    """Load `<pkg_name>.<sub>` from `<component_dir>/<sub>.py` and register it in
    sys.modules under that dotted name so relative imports resolve to it."""
    name = f"{pkg_name}.{sub}"
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(component_dir, f"{sub}.py")
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _import_ha_auth_stack(tmp_secret_dir=None):
    """Import the integration wired for ha_auth mode.

    Loads __init__, oauth, and auth_native all as submodules of ONE package so
    the relative imports resolve in BOTH directions to the same instances:
    auth_native does `from .oauth import ...` at load, and the ha_auth AS view
    does `from .auth_native import ...` at request time. (The legacy helpers load
    oauth top-level, which can't satisfy the view's reverse relative import.)
    Returns (init_mod, oauth_mod, auth_native_mod).
    """
    _install_runtime_stubs()
    component_dir = os.path.join(PROXY_ADDON_DIR, CURRENT["component"])
    pkg_name = f"mcp_proxy_init_{CURRENT['key']}"
    for suffix in ("", ".oauth", ".auth_native", ".repairs"):
        sys.modules.pop(f"{pkg_name}{suffix}", None)
    # Create the package object first so submodules have a parent with __path__.
    init_path = os.path.join(component_dir, "__init__.py")
    init_spec = importlib.util.spec_from_file_location(
        pkg_name, init_path, submodule_search_locations=[component_dir]
    )
    pkg = importlib.util.module_from_spec(init_spec)
    sys.modules[pkg_name] = pkg
    # oauth first (auth_native's `from .oauth` needs it bound), secret redirected.
    oauth = _load_pkg_submodule(pkg_name, component_dir, "oauth")
    if tmp_secret_dir is not None:
        oauth.SECRET_FILE = Path(tmp_secret_dir) / ".mcp_proxy_oauth_secret"
    auth_native = _load_pkg_submodule(pkg_name, component_dir, "auth_native")
    # Execute the package __init__ last; its lazy imports resolve to the above.
    init_spec.loader.exec_module(pkg)
    return pkg, oauth, auth_native


# ---------------------------------------------------------------------------
# Structure tests
# ---------------------------------------------------------------------------


class TestWebhookProxyStructure:
    """Verify webhook proxy addon meets HA addon requirements."""

    def test_required_files_exist(self):
        required = ["config.yaml", "Dockerfile", "start.py", "DOCS.md"]
        for f in required:
            path = os.path.join(PROXY_ADDON_DIR, f)
            assert os.path.exists(path), f"Missing required file: {f}"

    def test_mcp_proxy_integration_exists(self):
        int_dir = os.path.join(PROXY_ADDON_DIR, CURRENT["component"])
        required = ["__init__.py", "config_flow.py", "manifest.json", "strings.json"]
        for f in required:
            path = os.path.join(int_dir, f)
            assert os.path.exists(path), (
                f"Missing integration file: {CURRENT['component']}/{f}"
            )

    def test_config_yaml_valid(self):
        with open(f"{PROXY_ADDON_DIR}/config.yaml") as f:
            config = yaml.safe_load(f)

        required_fields = ["name", "description", "version", "slug", "arch"]
        for field in required_fields:
            assert field in config, f"Missing required field: {field}"

        assert config["slug"] == CURRENT["slug"]
        assert config["hassio_api"] is True
        assert config["homeassistant_api"] is True
        assert config["hassio_role"] == "manager"
        assert "config:rw" in config["map"]

    def test_config_yaml_schema(self):
        with open(f"{PROXY_ADDON_DIR}/config.yaml") as f:
            config = yaml.safe_load(f)

        assert "remote_url" in config["schema"]
        assert "mcp_server_url" in config["schema"]
        assert "mcp_port" in config["schema"]
        assert config["options"]["mcp_port"] == 9583

    def test_config_yaml_oauth_fields(self):
        """OAuth fields are in schema as optional, with no defaults in options.

        Defaultless + optional makes them hidden in the addon UI until the
        "Show unused optional configuration options" toggle is flipped, which
        keeps the basic config tab uncluttered for the no-auth case (where
        the proxy must keep behaving exactly like 1.0.2).
        """
        with open(f"{PROXY_ADDON_DIR}/config.yaml") as f:
            config = yaml.safe_load(f)

        assert config["schema"]["enable_oauth"] == "bool?"
        assert config["schema"]["oauth_client_id"] == "str?"
        assert config["schema"]["oauth_client_secret"] == "password?"
        for key in ("enable_oauth", "oauth_client_id", "oauth_client_secret"):
            assert key not in config["options"], (
                f"{key} should not have a default in options:; that would "
                "make it appear in the basic config tab."
            )

    def test_translations_cover_oauth_fields(self):
        """Each new schema field has a translation entry."""
        with open(f"{PROXY_ADDON_DIR}/translations/en.yaml") as f:
            translations = yaml.safe_load(f)

        cfg = translations["configuration"]
        for key in ("enable_oauth", "oauth_client_id", "oauth_client_secret"):
            assert key in cfg, f"Missing translation for {key}"
            assert cfg[key].get("name"), f"Missing name for {key}"
            assert cfg[key].get("description"), f"Missing description for {key}"
        # Toggle must be flagged as Beta in the user-facing label
        assert "Beta" in cfg["enable_oauth"]["name"]

    def test_config_yaml_debug_logging_field(self):
        """Unlike the OAuth fields, debug_logging is a VISIBLE toggle: it has a
        default in options: (so it shows on the main Configuration page, not
        under "unused optional configuration") and is bool? in schema, with a
        Beta-flagged translation."""
        with open(f"{PROXY_ADDON_DIR}/config.yaml") as f:
            config = yaml.safe_load(f)
        assert config["schema"]["debug_logging"] == "bool?"
        # Present in options with a default → shown on the main config page.
        assert config["options"]["debug_logging"] is False
        with open(f"{PROXY_ADDON_DIR}/translations/en.yaml") as f:
            translations = yaml.safe_load(f)
        cfg = translations["configuration"]["debug_logging"]
        assert cfg.get("name"), "Missing name for debug_logging"
        assert cfg.get("description"), "Missing description for debug_logging"
        assert "Beta" in cfg["name"]

    def test_addon_and_integration_versions_match(self):
        """config.yaml and manifest.json versions track together so that
        `_install_integration` correctly detects updates."""
        with open(f"{PROXY_ADDON_DIR}/config.yaml") as f:
            addon_version = yaml.safe_load(f)["version"]
        with open(f"{PROXY_ADDON_DIR}/{CURRENT['component']}/manifest.json") as f:
            manifest_version = json.load(f)["version"]
        assert addon_version == manifest_version, (
            "Addon config.yaml version and integration manifest.json "
            "version must match; otherwise the integration update "
            "detection in start.py will misfire."
        )

    def test_config_yaml_no_image_field(self):
        """Webhook proxy addon should not have an image field (not published to GHCR yet)."""
        with open(f"{PROXY_ADDON_DIR}/config.yaml") as f:
            config = yaml.safe_load(f)
        assert "image" not in config

    def test_manifest_json_valid(self):
        with open(f"{PROXY_ADDON_DIR}/{CURRENT['component']}/manifest.json") as f:
            manifest = json.load(f)

        assert manifest["domain"] == CURRENT["domain"]
        assert manifest["config_flow"] is True
        assert "webhook" in manifest["dependencies"]

    def test_start_script_syntax(self):
        """Verify start.py is valid Python."""
        import ast

        with open(f"{PROXY_ADDON_DIR}/start.py") as f:
            ast.parse(f.read())


# ---------------------------------------------------------------------------
# Discovery unit tests (mock Supervisor API)
# ---------------------------------------------------------------------------


class TestAddonDiscovery:
    """Test _discover_addon logic with mocked Supervisor API."""

    def test_discovers_stable_addon_first(self):
        start = _import_start()

        def mock_supervisor_get(path):
            if path == "/addons":
                return {
                    "addons": [
                        {"slug": "ha_mcp"},
                        {"slug": "ha_mcp_dev"},
                    ]
                }
            if path == "/addons/ha_mcp/info":
                return {
                    "state": "started",
                    "ip_address": "172.30.33.1",
                    "options": {"backup_hint": "normal"},
                }
            if path == "/addons/ha_mcp_dev/info":
                return {
                    "state": "started",
                    "ip_address": "172.30.33.2",
                    "options": {},
                }
            return None

        with patch.object(start, "_supervisor_get", side_effect=mock_supervisor_get):
            slug, ip, info = start._discover_addon()

        assert slug == "ha_mcp"
        assert ip == "172.30.33.1"

    def test_falls_back_to_dev_addon(self):
        start = _import_start()

        def mock_supervisor_get(path):
            if path == "/addons":
                return {"addons": [{"slug": "ha_mcp_dev"}]}
            if path == "/addons/ha_mcp_dev/info":
                return {
                    "state": "started",
                    "ip_address": "172.30.33.2",
                    "options": {},
                }
            return None

        with patch.object(start, "_supervisor_get", side_effect=mock_supervisor_get):
            slug, ip, info = start._discover_addon()

        assert slug == "ha_mcp_dev"
        assert ip == "172.30.33.2"

    def test_discovers_prefixed_slug(self):
        """Supervisor prefixes third-party addon slugs with a repo hash."""
        start = _import_start()

        def mock_supervisor_get(path):
            if path == "/addons":
                return {"addons": [{"slug": "abc12345_ha_mcp_dev"}]}
            if path == "/addons/abc12345_ha_mcp_dev/info":
                return {
                    "state": "started",
                    "ip_address": "172.30.33.3",
                    "options": {},
                }
            return None

        with patch.object(start, "_supervisor_get", side_effect=mock_supervisor_get):
            slug, ip, info = start._discover_addon()

        assert slug == "abc12345_ha_mcp_dev"
        assert ip == "172.30.33.3"

    def test_prefers_stable_over_dev_with_prefix(self):
        start = _import_start()

        def mock_supervisor_get(path):
            if path == "/addons":
                return {
                    "addons": [
                        {"slug": "xyz999_ha_mcp"},
                        {"slug": "xyz999_ha_mcp_dev"},
                    ]
                }
            if path == "/addons/xyz999_ha_mcp/info":
                return {
                    "state": "started",
                    "ip_address": "172.30.33.1",
                    "options": {},
                }
            if path == "/addons/xyz999_ha_mcp_dev/info":
                return {
                    "state": "started",
                    "ip_address": "172.30.33.2",
                    "options": {},
                }
            return None

        with patch.object(start, "_supervisor_get", side_effect=mock_supervisor_get):
            slug, ip, info = start._discover_addon()

        assert slug == "xyz999_ha_mcp"
        assert ip == "172.30.33.1"

    def test_skips_stopped_addon(self):
        start = _import_start()

        def mock_supervisor_get(path):
            if path == "/addons":
                return {
                    "addons": [
                        {"slug": "ha_mcp"},
                        {"slug": "ha_mcp_dev"},
                    ]
                }
            if path == "/addons/ha_mcp/info":
                return {"state": "stopped", "ip_address": "172.30.33.1", "options": {}}
            if path == "/addons/ha_mcp_dev/info":
                return {"state": "started", "ip_address": "172.30.33.2", "options": {}}
            return None

        with patch.object(start, "_supervisor_get", side_effect=mock_supervisor_get):
            slug, ip, info = start._discover_addon()

        assert slug == "ha_mcp_dev"

    def test_returns_none_when_no_addon_found(self):
        start = _import_start()

        def mock_supervisor_get(path):
            if path == "/addons":
                return {"addons": [{"slug": "some_other_addon"}]}
            return None

        with patch.object(start, "_supervisor_get", side_effect=mock_supervisor_get):
            slug, ip, info = start._discover_addon()

        assert slug is None
        assert ip is None
        assert info is None

    def test_uses_localhost_for_host_network_addon(self):
        """When MCP addon has host_network: true, use 127.0.0.1 not bridge IP."""
        start = _import_start()

        def mock_supervisor_get(path):
            if path == "/addons":
                return {"addons": [{"slug": "ha_mcp"}]}
            if path == "/addons/ha_mcp/info":
                return {
                    "state": "started",
                    "ip_address": "172.30.32.1",
                    "host_network": True,
                    "options": {},
                }
            return None

        with patch.object(start, "_supervisor_get", side_effect=mock_supervisor_get):
            slug, ip, info = start._discover_addon()

        assert slug == "ha_mcp"
        assert ip == "127.0.0.1"

    def test_skips_addon_without_ip(self):
        start = _import_start()

        def mock_supervisor_get(path):
            if path == "/addons":
                return {"addons": [{"slug": "ha_mcp"}]}
            if path == "/addons/ha_mcp/info":
                return {"state": "started", "ip_address": "", "options": {}}
            return None

        with patch.object(start, "_supervisor_get", side_effect=mock_supervisor_get):
            slug, ip, info = start._discover_addon()

        assert slug is None

    def test_falls_back_to_exact_slugs_when_list_fails(self):
        """When /addons endpoint fails, fall back to trying exact slugs."""
        start = _import_start()

        def mock_supervisor_get(path):
            if path == "/addons":
                return None  # Listing fails
            if path == "/addons/ha_mcp/info":
                return {
                    "state": "started",
                    "ip_address": "172.30.33.1",
                    "options": {},
                }
            return None

        with patch.object(start, "_supervisor_get", side_effect=mock_supervisor_get):
            slug, ip, info = start._discover_addon()

        assert slug == "ha_mcp"
        assert ip == "172.30.33.1"


class TestMutexRefusal:
    """_refuse_if_sibling_running is the startup guard that keeps the dev and
    stable Webhook Proxy flavors from both owning HA's root OAuth routes."""

    @pytest.fixture
    def start(self):
        return _import_start()

    def _sibling_addon(self, state):
        # Supervisor hash-prefixes third-party slugs; the guard matches by the
        # "_<base>" suffix, so exercise that shape rather than a bare slug.
        return {"slug": f"abc123_{CURRENT['sibling_base']}", "state": state}

    def test_refuses_and_notifies_when_sibling_started(self, start):
        api_calls = []

        def fake_api(method, path, data=None):
            api_calls.append((method, path, data))
            return {}

        with (
            patch.object(
                start,
                "_supervisor_get",
                return_value={"addons": [self._sibling_addon("started")]},
            ),
            patch.object(start, "_ha_core_api", side_effect=fake_api),
        ):
            assert start._refuse_if_sibling_running() is True

        creates = [
            data
            for method, path, data in api_calls
            if method == "POST" and path == "/services/persistent_notification/create"
        ]
        assert len(creates) == 1
        assert creates[0]["notification_id"] == CURRENT["mutex_id"]

    def test_no_refuse_when_sibling_stopped(self, start):
        api_calls = []
        with (
            patch.object(
                start,
                "_supervisor_get",
                return_value={"addons": [self._sibling_addon("stopped")]},
            ),
            patch.object(
                start, "_ha_core_api", side_effect=lambda *a, **k: api_calls.append(a)
            ),
        ):
            assert start._refuse_if_sibling_running() is False
        assert api_calls == []

    def test_no_refuse_when_addon_list_empty(self, start):
        api_calls = []
        with (
            patch.object(start, "_supervisor_get", return_value={"addons": []}),
            patch.object(
                start, "_ha_core_api", side_effect=lambda *a, **k: api_calls.append(a)
            ),
        ):
            assert start._refuse_if_sibling_running() is False
        assert api_calls == []

    def test_fail_open_loudly_when_supervisor_unreachable(self, start, capsys):
        """When the Supervisor /addons query can't be resolved (always None),
        the guard fails OPEN (returns False → starts anyway) after a bounded
        retry, but NOT silently: it logs a loud error so the bypass is visible.
        time.sleep is neutralized so the retry backoff doesn't actually wait."""
        sup = MagicMock(return_value=None)
        sleep = MagicMock()
        api_calls = []
        with (
            patch.object(start, "_supervisor_get", sup),
            patch.object(start.time, "sleep", sleep),
            patch.object(
                start, "_ha_core_api", side_effect=lambda *a, **k: api_calls.append(a)
            ),
        ):
            assert start._refuse_if_sibling_running() is False

        # Bounded retry: 3 attempts, sleeping only between them (not after the
        # last), so the loop is provably finite.
        assert sup.call_count == 3
        assert sleep.call_count == 2
        # Fail-open is loud, and posts no mutex notification (we didn't refuse).
        assert "Could not query the Supervisor /addons list" in capsys.readouterr().err
        assert api_calls == []


class TestMainDismissesMutexBanners:
    """On a clean start (sibling absent) main() dismisses BOTH its own mutex
    notification and the sibling flavor's, so a stale 'refused to start' banner
    from either flavor clears once the user resolves the conflict here."""

    def _run_main_to_keepalive(self, tmp_path):
        start = _import_start()
        options_dir = tmp_path / "data"
        options_dir.mkdir()
        # Skip discovery via the mcp_server_url override; OAuth + debug off so
        # main() takes the plain success path straight to the dismiss loop.
        (options_dir / "options.json").write_text(
            json.dumps(
                {"mcp_server_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa"}
            )
        )
        config_path = tmp_path / "proxy_config.json"

        def path_factory(arg):
            if arg == "/data/options.json":
                return options_dir / "options.json"
            if arg == "/data":
                return options_dir
            if arg == CURRENT["config_file"]:
                return config_path
            return Path(arg)

        api_calls = []

        def fake_api(method, path, data=None):
            api_calls.append((method, path, data))
            return {}

        with (
            patch.object(start, "Path", side_effect=path_factory),
            patch.object(start, "_supervisor_get", return_value={"addons": []}),
            patch.object(start, "_ha_core_api", side_effect=fake_api),
            patch.object(start, "_install_integration", return_value=(False, False)),
            patch.object(start, "_ensure_config_entry", return_value=True),
            patch.object(
                start, "_install_shutdown_handlers", return_value={"reason": None}
            ),
            patch.object(start, "_health_check", return_value=True),
            patch.object(start, "_shutdown_cleanup"),
            # Break out of the keep-alive loop on its first sleep; main() catches
            # KeyboardInterrupt and shuts down cleanly (returns 0).
            patch.object(start.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            rc = start.main()
        return rc, api_calls, start

    def test_clean_start_dismisses_own_and_sibling_banner(self, tmp_path):
        rc, api_calls, start = self._run_main_to_keepalive(tmp_path)
        assert rc == 0
        dismissed = [
            data["notification_id"]
            for method, path, data in api_calls
            if path == "/services/persistent_notification/dismiss"
        ]
        assert start.MUTEX_NOTIFICATION_ID in dismissed
        assert start.SIBLING_MUTEX_NOTIFICATION_ID in dismissed


class TestSecretPathDiscovery:
    """Test _discover_secret_path with mocked API responses."""

    def test_reads_secret_from_options(self):
        start = _import_start()
        info = {"options": {"secret_path": "/private_abc123"}}

        with patch.object(start, "_supervisor_get_text", return_value=None):
            path = start._discover_secret_path("ha_mcp", info)

        assert path == "/private_abc123"

    def test_adds_leading_slash_to_option(self):
        start = _import_start()
        info = {"options": {"secret_path": "private_abc123"}}

        with patch.object(start, "_supervisor_get_text", return_value=None):
            path = start._discover_secret_path("ha_mcp", info)

        assert path == "/private_abc123"

    def test_parses_secret_from_logs(self):
        start = _import_start()
        info = {"options": {}}  # No secret_path in options

        log_output = (
            "2026-03-05 12:00:00 [INFO] Starting Home Assistant MCP Server...\n"
            "2026-03-05 12:00:01 [INFO] ==============================\n"
            "2026-03-05 12:00:01 [INFO]    Secret Path: /private_zctpwlX7ZkIAr7oqdfLPxw\n"
            "2026-03-05 12:00:01 [INFO] ==============================\n"
        )

        # _discover_secret_path tries multiple log endpoints; return logs for the first
        with patch.object(start, "_supervisor_get_text", return_value=log_output):
            path = start._discover_secret_path("ha_mcp", info)

        assert path == "/private_zctpwlX7ZkIAr7oqdfLPxw"

    def test_parses_secret_from_url_in_logs(self):
        """Match secret path from MCP server URL in logs (real format)."""
        start = _import_start()
        info = {"options": {}}

        log_output = (
            "Starting MCP server 'ha-mcp' with transport 'http' (stateless) on "
            "http://0.0.0.0:9583/private_WBA1dCWENm_4cuFd6l8JUw\n"
        )

        with patch.object(start, "_supervisor_get_text", return_value=log_output):
            path = start._discover_secret_path("ha_mcp", info)

        assert path == "/private_WBA1dCWENm_4cuFd6l8JUw"

    def test_tries_fallback_log_endpoints(self):
        """When first log endpoint fails, tries others."""
        start = _import_start()
        info = {"options": {}}

        def mock_get_text(path):
            if path.endswith("/logs"):
                return None  # First endpoint fails
            if path.endswith("/logs/latest"):
                return "http://0.0.0.0:9583/private_fallback123\n"
            return None

        with patch.object(start, "_supervisor_get_text", side_effect=mock_get_text):
            path = start._discover_secret_path("ha_mcp", info)

        assert path == "/private_fallback123"

    def test_returns_none_when_no_secret_found(self):
        start = _import_start()
        info = {"options": {}}

        log_output = "2026-03-05 12:00:00 [INFO] Starting server...\n"

        with patch.object(start, "_supervisor_get_text", return_value=log_output):
            path = start._discover_secret_path("ha_mcp", info)

        assert path is None

    def test_returns_none_when_logs_unavailable(self):
        start = _import_start()
        info = {"options": {}}

        with patch.object(start, "_supervisor_get_text", return_value=None):
            path = start._discover_secret_path("ha_mcp", info)

        assert path is None

    def test_options_take_priority_over_logs(self):
        start = _import_start()
        info = {"options": {"secret_path": "/private_from_options"}}

        log_output = "http://0.0.0.0:9583/private_from_logs\n"

        with patch.object(start, "_supervisor_get_text", return_value=log_output):
            path = start._discover_secret_path("ha_mcp", info)

        # Options should win
        assert path == "/private_from_options"


class TestWebhookIdPersistence:
    """Test _get_or_create_webhook_id."""

    def test_creates_new_id(self):
        start = _import_start()
        with tempfile.TemporaryDirectory() as tmpdir:
            wid = start._get_or_create_webhook_id(Path(tmpdir))
            assert wid.startswith("mcp_")
            assert len(wid) > 10

            # Verify persisted to file
            stored = (Path(tmpdir) / "webhook_id.txt").read_text()
            assert stored == wid

    def test_reads_existing_id(self):
        start = _import_start()
        with tempfile.TemporaryDirectory() as tmpdir:
            expected = "mcp_existing_test_id_12345"
            (Path(tmpdir) / "webhook_id.txt").write_text(expected)

            wid = start._get_or_create_webhook_id(Path(tmpdir))
            assert wid == expected

    def test_regenerates_if_file_empty(self):
        start = _import_start()
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "webhook_id.txt").write_text("")

            wid = start._get_or_create_webhook_id(Path(tmpdir))
            assert wid.startswith("mcp_")
            assert len(wid) > 10


class TestNabuCasaAutoDetection:
    """Test get_nabu_casa_url."""

    def test_reads_nabu_casa_url(self):
        start = _import_start()

        cloud_data = {
            "data": {
                "remote_enabled": True,
                "remote_domain": "abcdef123.ui.nabu.casa",
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            storage_dir = Path(tmpdir) / ".storage"
            storage_dir.mkdir()
            (storage_dir / "cloud").write_text(json.dumps(cloud_data))

            with patch.object(start, "Path") as mock_path_cls:
                # Make Path("/config/.storage/cloud") return our temp file
                cloud_path = storage_dir / "cloud"
                mock_instance = MagicMock()
                mock_instance.exists.return_value = True
                mock_instance.read_text.return_value = cloud_path.read_text()

                original_path = Path

                def path_side_effect(arg):
                    if arg == "/config/.storage/cloud":
                        return mock_instance
                    return original_path(arg)

                mock_path_cls.side_effect = path_side_effect

                url = start.get_nabu_casa_url()

        assert url == "https://abcdef123.ui.nabu.casa"

    def test_returns_none_when_remote_disabled(self):
        start = _import_start()

        cloud_data = {
            "data": {
                "remote_enabled": False,
                "remote_domain": "abcdef123.ui.nabu.casa",
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            storage_dir = Path(tmpdir) / ".storage"
            storage_dir.mkdir()
            (storage_dir / "cloud").write_text(json.dumps(cloud_data))

            with patch.object(start, "Path") as mock_path_cls:
                cloud_path = storage_dir / "cloud"
                mock_instance = MagicMock()
                mock_instance.exists.return_value = True
                mock_instance.read_text.return_value = cloud_path.read_text()

                original_path = Path

                def path_side_effect(arg):
                    if arg == "/config/.storage/cloud":
                        return mock_instance
                    return original_path(arg)

                mock_path_cls.side_effect = path_side_effect

                url = start.get_nabu_casa_url()

        assert url is None

    def test_returns_none_when_file_missing(self):
        start = _import_start()

        with patch.object(start, "Path") as mock_path_cls:
            mock_instance = MagicMock()
            mock_instance.exists.return_value = False

            original_path = Path

            def path_side_effect(arg):
                if arg == "/config/.storage/cloud":
                    return mock_instance
                return original_path(arg)

            mock_path_cls.side_effect = path_side_effect

            url = start.get_nabu_casa_url()

        assert url is None


class TestTargetUrlConstruction:
    """Test that the full target URL is built correctly from discovered parts."""

    def test_target_url_format(self):
        """Verify target URL is constructed as http://{ip}:{port}{secret_path}."""
        start = _import_start()

        def mock_supervisor_get(path):
            if path == "/addons/ha_mcp/info":
                return {
                    "state": "started",
                    "ip_address": "172.30.33.5",
                    "options": {"secret_path": "/private_testkey123"},
                }
            return None

        with patch.object(start, "_supervisor_get", side_effect=mock_supervisor_get):
            slug, ip, info = start._discover_addon()
            secret = start._discover_secret_path(slug, info)

        target_url = f"http://{ip}:9583{secret}"
        assert target_url == "http://172.30.33.5:9583/private_testkey123"

    def test_custom_port(self):
        """Verify custom mcp_port is used in URL construction."""
        ip = "172.30.33.5"
        secret = "/private_abc"
        port = 8080

        target_url = f"http://{ip}:{port}{secret}"
        assert target_url == "http://172.30.33.5:8080/private_abc"

    def test_mcp_server_url_override_skips_discovery(self):
        """When mcp_server_url is set, discovery should be skipped entirely."""
        start = _import_start()

        # _discover_addon should never be called
        with patch.object(start, "_discover_addon") as mock_discover:
            mock_discover.side_effect = AssertionError("Should not be called")

            # Simulate the main() logic for mcp_server_url override
            mcp_server_url = "http://192.168.1.100:9583/private_custom"
            if mcp_server_url and mcp_server_url.strip():
                target_url = mcp_server_url.strip()
            else:
                start._discover_addon()  # This would fail

            assert target_url == "http://192.168.1.100:9583/private_custom"


# ---------------------------------------------------------------------------
# mcp_proxy/__init__.py — surfacing webhook setup failures
# ---------------------------------------------------------------------------


class TestTargetUrlValidation:
    @pytest.fixture
    def validate(self):
        return _import_mcp_proxy()._validate_target_url

    def test_accepts_real_22char_token(self, validate):
        ok, reason = validate("http://172.30.33.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw")
        assert ok, reason
        assert reason == ""

    def test_accepts_https_scheme(self, validate):
        ok, reason = validate("https://example.com:443/private_aaaaaaaaaaaaaaaa")
        assert ok, reason

    def test_accepts_minimum_16char_token(self, validate):
        ok, reason = validate("http://h:9583/private_aaaaaaaaaaaaaaaa")
        assert ok, reason

    def test_accepts_non_private_path(self, validate):
        """Custom MCP servers may sit at any path; only /private_* triggers length check."""
        ok, reason = validate("http://localhost:8123/api/")
        assert ok, reason

    def test_accepts_arbitrary_other_path(self, validate):
        ok, reason = validate("http://example.com/some/other/mcp")
        assert ok, reason

    def test_rejects_truncated_secret_path(self, validate):
        ok, reason = validate("http://127.0.0.1:9583/private_ZZZZZZZ")
        assert not ok
        assert "secret path" in reason

    def test_rejects_15char_token_at_boundary(self, validate):
        ok, reason = validate("http://h/private_aaaaaaaaaaaaaaa")  # 15 chars
        assert not ok
        assert "secret path" in reason

    def test_rejects_non_http_scheme(self, validate):
        ok, reason = validate("ftp://h/private_aaaaaaaaaaaaaaaa")
        assert not ok
        assert "scheme" in reason

    def test_rejects_missing_host(self, validate):
        ok, reason = validate("http:///private_aaaaaaaaaaaaaaaa")
        assert not ok
        assert "host" in reason

    def test_rejects_empty_string(self, validate):
        ok, reason = validate("")
        assert not ok

    def test_rejects_query_string(self, validate):
        ok, reason = validate("http://h/private_aaaaaaaaaaaaaaaa?foo=bar")
        assert not ok
        assert "query" in reason

    def test_rejects_fragment(self, validate):
        ok, reason = validate("http://h/private_aaaaaaaaaaaaaaaa#frag")
        assert not ok
        assert "fragment" in reason

    def test_rejects_path_params(self, validate):
        ok, reason = validate("http://h/private_aaaaaaaaaaaaaaaa;param")
        assert not ok
        assert "path parameters" in reason

    def test_rejects_invalid_chars_in_private_token(self, validate):
        ok, reason = validate("http://h/private_has%20space_aaaaaaa")
        # urlparse keeps the percent-encoding in path; regex rejects '%' chars.
        assert not ok
        assert "secret path" in reason


class TestSetupEntrySurfaceFailures:
    @pytest.fixture
    def mod(self):
        return _import_mcp_proxy()

    @pytest.fixture
    def hass(self):
        h = MagicMock()
        h.data = {}

        async def run_executor(func, *args):
            return func(*args)

        h.async_add_executor_job = AsyncMock(side_effect=run_executor)
        return h

    async def test_truncated_target_url_raises_config_entry_error(self, mod, hass):
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_ZZZZZZZ",
            "webhook_id": "mcp_test_webhook_id_12345",
        }
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register") as mock_register,
            patch.object(mod.aiohttp, "ClientSession") as mock_session,
            pytest.raises(_FakeConfigEntryError) as exc_info,
        ):
            await mod.async_setup_entry(hass, MagicMock())

        assert "Invalid target_url" in str(exc_info.value)
        mock_register.assert_not_called()
        mock_session.assert_not_called()
        assert mod.DOMAIN not in hass.data

    async def test_truncated_url_does_not_log_full_token(self, mod, hass, caplog):
        """A leaked secret in logs would be a silent regression of the masking."""
        secret_tail = "ZZZZZZZ_real_secret_value"
        proxy_config = {
            "target_url": f"http://h:9583/private_{secret_tail}_but_with_bad_chars!",
            "webhook_id": "mcp_test_webhook_id_12345",
        }
        with (
            caplog.at_level("ERROR"),
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            pytest.raises(_FakeConfigEntryError),
        ):
            await mod.async_setup_entry(hass, MagicMock())

        assert "validation failed" in caplog.text
        assert secret_tail not in caplog.text
        assert "/private_********" in caplog.text

    async def test_missing_target_url_raises_config_entry_error(self, mod, hass):
        proxy_config = {"target_url": "", "webhook_id": "mcp_x"}
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register") as mock_register,
            patch.object(mod.aiohttp, "ClientSession") as mock_session,
            pytest.raises(_FakeConfigEntryError) as exc_info,
        ):
            await mod.async_setup_entry(hass, MagicMock())

        assert "Missing target_url" in str(exc_info.value)
        mock_register.assert_not_called()
        mock_session.assert_not_called()

    async def test_missing_webhook_id_raises_config_entry_error(self, mod, hass):
        proxy_config = {
            "target_url": "http://h:9583/private_aaaaaaaaaaaaaaaa",
            "webhook_id": "",
        }
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register") as mock_register,
            patch.object(mod.aiohttp, "ClientSession") as mock_session,
            pytest.raises(_FakeConfigEntryError),
        ):
            await mod.async_setup_entry(hass, MagicMock())

        mock_register.assert_not_called()
        mock_session.assert_not_called()

    @pytest.mark.parametrize(
        "register_error",
        [RuntimeError("boom"), ValueError("duplicate webhook"), KeyError("not loaded")],
    )
    async def test_register_failure_closes_session_and_raises(
        self, mod, hass, register_error
    ):
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test_webhook_id_12345",
        }
        captured_session = {}

        def make_session(*args, **kwargs):
            session = MagicMock()
            session.close = AsyncMock()
            captured_session["s"] = session
            return session

        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register", side_effect=register_error),
            patch.object(mod.aiohttp, "ClientSession", side_effect=make_session),
            pytest.raises(_FakeConfigEntryError) as exc_info,
        ):
            await mod.async_setup_entry(hass, MagicMock())

        assert "Failed to register webhook endpoint" in str(exc_info.value)
        assert exc_info.value.__cause__ is register_error
        captured_session["s"].close.assert_awaited_once()
        assert mod.DOMAIN not in hass.data

    async def test_corrupted_json_raises_config_entry_error(self, mod, hass):
        async def fake_executor(func, *args):
            raise json.JSONDecodeError("trailing garbage", "{ ", 2)

        hass.async_add_executor_job = AsyncMock(side_effect=fake_executor)
        with (
            patch.object(mod, "async_register") as mock_register,
            pytest.raises(_FakeConfigEntryError) as exc_info,
        ):
            await mod.async_setup_entry(hass, MagicMock())

        assert "Failed to read" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)
        mock_register.assert_not_called()

    async def test_unreadable_config_raises_config_entry_error(self, mod, hass):
        async def fake_executor(func, *args):
            raise OSError("permission denied")

        hass.async_add_executor_job = AsyncMock(side_effect=fake_executor)
        with (
            patch.object(mod, "async_register") as mock_register,
            pytest.raises(_FakeConfigEntryError) as exc_info,
        ):
            await mod.async_setup_entry(hass, MagicMock())

        assert "Failed to read" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, OSError)
        mock_register.assert_not_called()

    async def test_happy_path_registers_and_stores_data(self, mod, hass):
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test_webhook_id_12345",
        }
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register") as mock_register,
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
        ):
            result = await mod.async_setup_entry(hass, MagicMock())

        assert result is True
        mock_register.assert_called_once()
        assert hass.data[mod.DOMAIN]["target_url"] == proxy_config["target_url"]
        assert hass.data[mod.DOMAIN]["webhook_id"] == proxy_config["webhook_id"]

    async def test_no_config_file_returns_true(self, mod, hass):
        """Fresh install: file-not-found is the one valid 'no config' state."""
        with patch.object(mod, "_read_config", return_value=None):
            result = await mod.async_setup_entry(hass, MagicMock())

        assert result is True
        assert mod.DOMAIN not in hass.data


class TestUnloadEntry:
    @pytest.fixture
    def mod(self):
        return _import_mcp_proxy()

    @pytest.fixture
    def hass(self):
        h = MagicMock()
        h.data = {}
        return h

    async def test_unload_after_failed_setup_is_noop(self, mod, hass):
        with patch.object(mod, "async_unregister") as mock_unreg:
            result = await mod.async_unload_entry(hass, MagicMock())

        assert result is True
        mock_unreg.assert_not_called()

    async def test_unload_unregisters_and_closes_session(self, mod, hass):
        session = MagicMock()
        session.close = AsyncMock()
        hass.data[mod.DOMAIN] = {
            "webhook_id": "mcp_test_id",
            "session": session,
            "target_url": "http://h/private_aaaaaaaaaaaaaaaa",
        }
        with patch.object(mod, "async_unregister") as mock_unreg:
            result = await mod.async_unload_entry(hass, MagicMock())

        assert result is True
        mock_unreg.assert_called_once_with(hass, "mcp_test_id")
        session.close.assert_awaited_once()
        assert mod.DOMAIN not in hass.data


# ===========================================================================
# OAuth tests
# ===========================================================================
#
# The "Enable OAuth" toggle is a beta opt-in. The hard requirement is that
# when the toggle is OFF (or the proxy_config has no `oauth` section at all)
# the integration must behave EXACTLY like 1.0.2 — no extra views, no extra
# code paths, no extra HTTP behavior. The first class below proves that
# property, the rest exercise the OAuth code path itself.


class TestOAuthOffPreservesBehavior:
    """Critical regression guard: when oauth is not configured, async_setup_entry
    and _handle_webhook must behave exactly like before."""

    @pytest.fixture
    def mod(self):
        return _import_mcp_proxy()

    @pytest.fixture
    def hass(self):
        h = MagicMock()
        h.data = {}

        async def fake_executor(func, *args):
            return func(*args)

        h.async_add_executor_job = AsyncMock(side_effect=fake_executor)
        return h

    async def test_setup_without_oauth_section_omits_oauth_key(self, mod, hass):
        """The OFF path must never add the "oauth" key (which arms the bearer
        gate). On stable that leaves exactly the legacy three keys; on dev the
        none-mode auto-approve fix (#1969) additionally serves discovery, so it
        adds "autoapprove" + "oauth_mode" — but still NOT "oauth", so the
        forwarder stays unauthenticated."""
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test_webhook_id_12345",
        }
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
        ):
            await mod.async_setup_entry(hass, MagicMock())

        # The bearer-gate key is never set on the OFF path, in EITHER flavor.
        assert "oauth" not in hass.data[mod.DOMAIN]
        expected = {"target_url", "webhook_id", "session"}
        if _none_autoapprove_supported():
            # Dev serves none-mode auto-approve discovery (issue #1969): the
            # provider under "autoapprove" (not "oauth") + the mode marker.
            expected |= {"autoapprove", "oauth_mode"}
            assert (
                hass.data[mod.DOMAIN]["oauth_mode"] == mod.OAUTH_MODE_NONE_AUTOAPPROVE
            )
        assert set(hass.data[mod.DOMAIN].keys()) == expected

    async def test_setup_does_not_import_oauth_module_when_off(self, mod, hass):
        """Confirms the lazy-import: the oauth submodule shouldn't be loaded on
        the OFF path — UNLESS the flavor ships none-mode auto-approve (dev,
        #1969), whose discovery deliberately reuses oauth's metadata views +
        shared PKCE store, so loading it on the OFF path is expected there."""
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test_webhook_id_12345",
        }
        # Wipe any stale import
        oauth_submodule_name = f"mcp_proxy_init_{CURRENT['key']}.oauth"
        sys.modules.pop(oauth_submodule_name, None)
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
        ):
            await mod.async_setup_entry(hass, MagicMock())

        # The submodule name follows from the parent's package name
        # ("mcp_proxy_init_<variant>").
        if _none_autoapprove_supported():
            # Dev's OFF path serves auto-approve discovery, which reuses oauth's
            # metadata views + PKCE store — so oauth IS loaded, by design.
            assert oauth_submodule_name in sys.modules
        else:
            # Stable keeps the original lazy-import guarantee.
            assert oauth_submodule_name not in sys.modules

    async def test_blank_creds_raises_config_entry_error(self, mod, hass):
        """Blank creds in an oauth section signal a config bug — the user
        opted into auth, so silently disabling it would leave them with an
        unprotected endpoint they think is locked. Fail loudly instead."""
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test_webhook_id_12345",
            "oauth": {"client_id": "", "client_secret": ""},
        }
        session = MagicMock()
        session.close = AsyncMock()
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod, "async_unregister") as mock_unreg,
            patch.object(mod.aiohttp, "ClientSession", return_value=session),
            pytest.raises(_FakeConfigEntryError) as exc_info,
        ):
            await mod.async_setup_entry(hass, MagicMock())

        assert "client_id and/or client_secret is blank" in str(exc_info.value)
        assert mod.DOMAIN not in hass.data
        # The webhook we registered above is torn down so we don't leave an
        # unauthenticated endpoint live after OAuth setup bails.
        mock_unreg.assert_called_once_with(hass, "mcp_test_webhook_id_12345")
        # Session was opened then closed cleanly so we don't leak it on the
        # failure path.
        session.close.assert_awaited_once()

    async def test_webhook_handler_no_auth_skips_oauth_check(self, mod):
        """The auth gate must not run when oauth is None — only one extra
        attribute lookup vs the original handler."""
        hass = MagicMock()
        hass.data = {
            mod.DOMAIN: {
                "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "webhook_id": "mcp_test",
                "session": MagicMock(),
                "oauth": None,
            }
        }
        request = MagicMock()
        request.headers = {}
        request.read = AsyncMock(return_value=b"")
        request.method = "POST"
        sentinel = mod.aiohttp.ClientError("stop here")
        hass.data[mod.DOMAIN]["session"].request = MagicMock(side_effect=sentinel)

        await mod._handle_webhook(hass, "mcp_test", request)

        request.read.assert_awaited_once()  # auth gate didn't short-circuit


class TestDebugLogging:
    """The `debug_logging` toggle: OFF keeps the baseline hass.data shape and
    logs nothing per-request; ON stores the flag and logs each inbound request
    (before the OAuth gate, so even a 401'd discovery probe is recorded)."""

    @pytest.fixture
    def mod(self):
        return _import_mcp_proxy()

    @pytest.fixture
    def hass(self):
        h = MagicMock()
        h.data = {}

        async def fake_executor(func, *args):
            return func(*args)

        h.async_add_executor_job = AsyncMock(side_effect=fake_executor)
        return h

    @pytest.fixture(autouse=True)
    def _reset_logger_level(self, mod):
        # _LOGGER (and the level we may set on it) is a process-global singleton,
        # so state set by one test can leak into the next. Reset the level and
        # the "we raised it" flag around every test so the level assertions stay
        # deterministic.
        mod._LOGGER.setLevel(logging.NOTSET)
        mod._LOGGER_LEVEL_RAISED = False
        yield
        mod._LOGGER.setLevel(logging.NOTSET)
        mod._LOGGER_LEVEL_RAISED = False

    async def _run_setup(self, mod, hass, debug):
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test_webhook_id_12345",
            "debug_logging": debug,
        }
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
        ):
            await mod.async_setup_entry(hass, MagicMock())

    async def test_debug_off_omits_key(self, mod, hass):
        """Toggle off (or absent) → no debug_logging key; hass.data shape is
        identical to the baseline, mirroring the oauth-off guarantee."""
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test_webhook_id_12345",
            "debug_logging": False,
        }
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
        ):
            await mod.async_setup_entry(hass, MagicMock())

        assert "debug_logging" not in hass.data[mod.DOMAIN]
        expected = {"target_url", "webhook_id", "session"}
        if _none_autoapprove_supported():
            # Dev's OAuth-off path also serves none-mode auto-approve discovery
            # (issue #1969): provider under "autoapprove" + the mode marker.
            expected |= {"autoapprove", "oauth_mode"}
        assert set(hass.data[mod.DOMAIN].keys()) == expected

    async def test_debug_on_stores_flag(self, mod, hass):
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test_webhook_id_12345",
            "debug_logging": True,
        }
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
        ):
            await mod.async_setup_entry(hass, MagicMock())

        assert hass.data[mod.DOMAIN]["debug_logging"] is True

    async def test_debug_on_logs_inbound_request(self, mod, caplog):
        """When on, _handle_webhook logs the inbound request before any upstream
        work — so a request that fails downstream is still recorded."""
        hass = MagicMock()
        hass.data = {
            mod.DOMAIN: {
                "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "webhook_id": "mcp_test_webhook_id_12345",
                "session": MagicMock(),
                "oauth": None,
                "debug_logging": True,
            }
        }
        request = MagicMock()
        request.headers = {}
        request.read = AsyncMock(return_value=b"")
        request.method = "POST"
        # Source is taken from request.remote (the HA-validated client IP), not
        # the spoofable X-Forwarded-For header.
        request.remote = "203.0.113.4"
        # Fail the upstream call fast; the inbound log already happened by then.
        hass.data[mod.DOMAIN]["session"].request = MagicMock(
            side_effect=mod.aiohttp.ClientError("stop")
        )
        with caplog.at_level(logging.INFO, logger=mod._LOGGER.name):
            await mod._handle_webhook(hass, "mcp_test_webhook_id_12345", request)

        msgs = [r.getMessage() for r in caplog.records]
        assert any("[inbound]" in m for m in msgs)
        assert any("203.0.113.4" in m for m in msgs)

    async def test_debug_off_logs_no_inbound(self, mod, caplog):
        """With the flag absent the handler emits no inbound debug lines."""
        hass = MagicMock()
        hass.data = {
            mod.DOMAIN: {
                "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "webhook_id": "mcp_test_webhook_id_12345",
                "session": MagicMock(),
                "oauth": None,
            }
        }
        request = MagicMock()
        request.headers = {}
        request.read = AsyncMock(return_value=b"")
        request.method = "POST"
        hass.data[mod.DOMAIN]["session"].request = MagicMock(
            side_effect=mod.aiohttp.ClientError("stop")
        )
        with caplog.at_level(logging.INFO, logger=mod._LOGGER.name):
            await mod._handle_webhook(hass, "mcp_test_webhook_id_12345", request)

        assert not any("[inbound]" in r.getMessage() for r in caplog.records)

    async def test_debug_on_logs_401_before_oauth_gate(self, mod, caplog):
        """Headline behavior: the inbound line AND a 401 line are logged BEFORE
        the OAuth gate, so an unauthenticated discovery probe is still recorded
        even though the handler short-circuits without reading the body."""
        provider = MagicMock()
        provider.validate_bearer.return_value = False
        hass = MagicMock()
        hass.data = {
            mod.DOMAIN: {
                "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "webhook_id": "mcp_test_webhook_id_12345",
                "session": MagicMock(),
                "oauth": provider,
                "debug_logging": True,
            }
        }
        request = MagicMock()
        request.headers = {}
        request.read = AsyncMock(return_value=b"")
        request.method = "POST"
        request.remote = "203.0.113.4"
        with caplog.at_level(logging.INFO, logger=mod._LOGGER.name):
            await mod._handle_webhook(hass, "mcp_test_webhook_id_12345", request)

        msgs = [r.getMessage() for r in caplog.records]
        assert any("[inbound]" in m for m in msgs)
        assert any("401 Unauthorized" in m for m in msgs)
        # Logged before the gate short-circuited — the body was never read.
        request.read.assert_not_awaited()

    async def test_debug_on_logs_upstream_response(self, mod, caplog):
        """The upstream-status debug line fires on a successful (non-streaming)
        upstream response — the other half of the per-request logging."""
        hass = MagicMock()
        hass.data = {
            mod.DOMAIN: {
                "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "webhook_id": "mcp_test_webhook_id_12345",
                "session": MagicMock(),
                "oauth": None,
                "debug_logging": True,
            }
        }
        request = MagicMock()
        request.headers = {}
        request.read = AsyncMock(return_value=b"")
        request.method = "POST"
        request.remote = "203.0.113.4"
        # Async-context-manager upstream response (non-streaming JSON).
        upstream = MagicMock()
        upstream.status = 200
        upstream.headers = {"Content-Type": "application/json"}
        upstream.read = AsyncMock(return_value=b"{}")
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=upstream)
        cm.__aexit__ = AsyncMock(return_value=False)
        hass.data[mod.DOMAIN]["session"].request = MagicMock(return_value=cm)
        with caplog.at_level(logging.INFO, logger=mod._LOGGER.name):
            await mod._handle_webhook(hass, "mcp_test_webhook_id_12345", request)

        assert any("upstream responded 200" in r.getMessage() for r in caplog.records)

    async def test_logger_raised_to_info_when_on_and_level_quiet(self, mod, hass):
        """debug on + effective level less verbose than INFO → raise to INFO."""
        mod._LOGGER.setLevel(logging.WARNING)
        await self._run_setup(mod, hass, True)
        assert mod._LOGGER.level == logging.INFO

    async def test_logger_preserves_explicit_debug_when_on(self, mod, hass):
        """debug on must NOT clobber a more-verbose user-set DEBUG."""
        mod._LOGGER.setLevel(logging.DEBUG)
        await self._run_setup(mod, hass, True)
        assert mod._LOGGER.level == logging.DEBUG

    async def test_logger_reset_when_off_after_we_raised(self, mod, hass):
        """A debug on→off cycle undoes the INFO we raised (and only that)."""
        mod._LOGGER.setLevel(logging.WARNING)
        await self._run_setup(mod, hass, True)  # we raise to INFO + flag it
        assert mod._LOGGER.level == logging.INFO
        await self._run_setup(mod, hass, False)  # we undo our own raise
        assert mod._LOGGER.level == logging.NOTSET

    async def test_logger_preserves_explicit_debug_when_off(self, mod, hass):
        """debug off must NOT clobber a user's explicit `logger:` DEBUG — the
        default-config (toggle off) majority case."""
        mod._LOGGER.setLevel(logging.DEBUG)
        await self._run_setup(mod, hass, False)
        assert mod._LOGGER.level == logging.DEBUG

    async def test_logger_preserves_explicit_info_when_off(self, mod, hass):
        """debug off must NOT clobber a user's explicit `logger:` INFO either —
        we only undo an INFO WE raised, never one the user set. No toggle is
        even involved here (the default-config case Patch76 flagged)."""
        mod._LOGGER.setLevel(logging.INFO)
        await self._run_setup(mod, hass, False)
        assert mod._LOGGER.level == logging.INFO

    async def test_debug_logs_auth_presence_never_token(self, mod, caplog):
        """With an Authorization header present, the log records only
        'present' — never the token value (security: no credential leak)."""
        hass = MagicMock()
        hass.data = {
            mod.DOMAIN: {
                "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "webhook_id": "mcp_test_webhook_id_12345",
                "session": MagicMock(),
                "oauth": None,
                "debug_logging": True,
            }
        }
        request = MagicMock()
        request.headers = {"Authorization": "Bearer super-secret-token-value"}
        request.read = AsyncMock(return_value=b"")
        request.method = "POST"
        request.remote = "203.0.113.4"
        hass.data[mod.DOMAIN]["session"].request = MagicMock(
            side_effect=mod.aiohttp.ClientError("stop")
        )
        with caplog.at_level(logging.INFO, logger=mod._LOGGER.name):
            await mod._handle_webhook(hass, "mcp_test_webhook_id_12345", request)

        assert "Authorization header: present" in caplog.text
        assert "super-secret-token-value" not in caplog.text

    async def test_debug_never_logs_request_body(self, mod, caplog):
        """The request body is read after the log line and must never appear
        in the logs."""
        hass = MagicMock()
        hass.data = {
            mod.DOMAIN: {
                "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "webhook_id": "mcp_test_webhook_id_12345",
                "session": MagicMock(),
                "oauth": None,
                "debug_logging": True,
            }
        }
        request = MagicMock()
        request.headers = {}
        request.read = AsyncMock(return_value=b'{"secret":"DO-NOT-LOG-THIS-BODY"}')
        request.method = "POST"
        request.remote = "203.0.113.4"
        hass.data[mod.DOMAIN]["session"].request = MagicMock(
            side_effect=mod.aiohttp.ClientError("stop")
        )
        with caplog.at_level(logging.INFO, logger=mod._LOGGER.name):
            await mod._handle_webhook(hass, "mcp_test_webhook_id_12345", request)

        assert "[inbound]" in caplog.text
        assert "DO-NOT-LOG-THIS-BODY" not in caplog.text

    async def test_debug_never_logs_full_webhook_id(self, mod, caplog):
        """The masked path logs only wh[:6]; the full webhook_id (the shared
        secret in unauthenticated mode) must never appear in the logs."""
        webhook_id = "mcp_super_secret_webhook_id_abcdef123456"
        hass = MagicMock()
        hass.data = {
            mod.DOMAIN: {
                "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "webhook_id": webhook_id,
                "session": MagicMock(),
                "oauth": None,
                "debug_logging": True,
            }
        }
        request = MagicMock()
        request.headers = {}
        request.read = AsyncMock(return_value=b"")
        request.method = "POST"
        request.remote = "203.0.113.4"
        hass.data[mod.DOMAIN]["session"].request = MagicMock(
            side_effect=mod.aiohttp.ClientError("stop")
        )
        with caplog.at_level(logging.INFO, logger=mod._LOGGER.name):
            await mod._handle_webhook(hass, webhook_id, request)

        assert "[inbound]" in caplog.text
        assert webhook_id not in caplog.text
        assert webhook_id[:6] in caplog.text


class TestInboundLogMirror:
    """Issue #1694: inbound debug lines are mirrored into the addon log.

    The integration appends each inbound line to INBOUND_LOG_FILE; the addon's
    keep-alive loop tails that file and echoes new lines to its own stdout so
    they show up in the addon log, not only in Settings -> System -> Logs.
    """

    def test_append_inbound_log_writes_and_caps(self, tmp_path):
        mod = _import_mcp_proxy()
        log_file = tmp_path / "inbound.log"
        with (
            patch.object(mod, "INBOUND_LOG_FILE", log_file),
            patch.object(mod, "_INBOUND_LOG_CAP", 200),
        ):
            for i in range(100):
                mod._append_inbound_log(f"MCP Proxy [inbound]: line number {i}")
            # Capped to bound growth, and the most recent line survives intact.
            assert log_file.stat().st_size <= 200
            assert log_file.read_text().splitlines()[-1].endswith("line number 99")

    def test_append_inbound_log_swallows_oserror(self, tmp_path):
        """A read-only / missing /config must not turn a debug write into a
        propagating error (it runs in the executor, fire-and-forget)."""
        mod = _import_mcp_proxy()
        bad = tmp_path / "missing-dir" / "inbound.log"  # parent doesn't exist
        with patch.object(mod, "INBOUND_LOG_FILE", bad):
            mod._append_inbound_log("x")  # must not raise

    async def test_handle_webhook_mirrors_inbound_line(self, tmp_path):
        """A debug-on request dispatches the inbound line to the executor, which
        lands in INBOUND_LOG_FILE for the addon to tail."""
        mod = _import_mcp_proxy()
        log_file = tmp_path / "inbound.log"

        def fake_executor(func, *args):
            func(*args)  # run the append synchronously for the test
            return MagicMock()

        hass = MagicMock()
        hass.async_add_executor_job = MagicMock(side_effect=fake_executor)
        hass.data = {
            mod.DOMAIN: {
                "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "webhook_id": "mcp_test_webhook_id_12345",
                "session": MagicMock(),
                "oauth": None,
                "debug_logging": True,
            }
        }
        request = MagicMock()
        request.headers = {}
        request.read = AsyncMock(return_value=b"")
        request.method = "POST"
        request.remote = "203.0.113.4"
        hass.data[mod.DOMAIN]["session"].request = MagicMock(
            side_effect=mod.aiohttp.ClientError("stop")
        )
        with patch.object(mod, "INBOUND_LOG_FILE", log_file):
            await mod._handle_webhook(hass, "mcp_test_webhook_id_12345", request)

        text = log_file.read_text()
        assert "[inbound]" in text
        assert "203.0.113.4" in text

    def test_emit_new_inbound_lines_tails_file(self, tmp_path):
        """The addon tail emits only whole lines, holds a partial for the next
        poll, never duplicates, and resets when the file is rotated."""
        start = _import_start()
        log_file = tmp_path / "inbound.log"
        emitted: list[str] = []
        with (
            patch.object(start, "INBOUND_LOG_FILE", log_file),
            patch.object(start, "log_info", side_effect=emitted.append),
        ):
            state = {"offset": 0}
            start._emit_new_inbound_lines(state)  # no file yet
            assert emitted == []

            log_file.write_bytes(b"line A\nline B\npartial")
            start._emit_new_inbound_lines(state)
            assert emitted == ["line A", "line B"]  # partial held back

            with log_file.open("ab") as fh:
                fh.write(b" done\n")
            emitted.clear()
            start._emit_new_inbound_lines(state)
            assert emitted == ["partial done"]  # completed, not re-emitted

            log_file.write_bytes(b"fresh\n")  # truncation/rotation
            emitted.clear()
            start._emit_new_inbound_lines(state)
            assert emitted == ["fresh"]


class TestOAuthProvider:
    """Direct unit tests against the OAuthProvider class."""

    @pytest.fixture
    def provider(self, tmp_path):
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        hass = MagicMock()
        return oauth.OAuthProvider(
            hass=hass,
            client_id="client-id-1234567890",
            client_secret="client-secret-very-secret",
            webhook_id="mcp_webhook_id_xxx",
            signing_key=b"\x00" * 32,
        )

    def test_issues_and_validates_access_token(self, provider):
        token = provider.issue_access_token()
        assert provider.validate_access_token(token) is True

    def test_validates_refresh_token(self, provider):
        token = provider.issue_refresh_token()
        assert provider.validate_refresh_token(token) is True

    def test_access_token_does_not_validate_as_refresh(self, provider):
        access = provider.issue_access_token()
        assert provider.validate_refresh_token(access) is False

    def test_refresh_token_does_not_validate_as_access(self, provider):
        refresh = provider.issue_refresh_token()
        assert provider.validate_access_token(refresh) is False

    def test_garbage_token_rejected(self, provider):
        assert provider.validate_access_token("not.a.real.token") is False
        assert provider.validate_access_token("") is False
        assert provider.validate_access_token("bare") is False

    def test_non_ascii_bearer_rejected_not_raised(self, provider):
        # C1 regression: a bearer whose pre-signature segment carries a
        # non-ASCII char makes body.encode("ascii") raise UnicodeEncodeError.
        # _validate_token must catch it and return False rather than let it
        # escape the webhook gate (HA core's async_handle_webhook would swallow
        # it into a 200 OK and never emit the 401 discovery challenge).
        # The legacy hardening rides the dev flavor until promotion (stable still
        # hmac's body.encode("ascii") outside the decode try), so skip on stable.
        if not _ha_auth_supported():
            pytest.skip("legacy non-ASCII hardening rides the dev flavor until promote")
        assert provider.validate_access_token("é.AA") is False
        assert provider.validate_access_token("\xff.AA") is False

    def test_token_with_tampered_payload_rejected(self, provider):
        token = provider.issue_access_token()
        body, sig = token.rsplit(".", 1)
        # Flip the first base64-url character (6 full data bits, no padding)
        # to guarantee a different decoded byte regardless of key material.
        tampered_body = ("A" if body[0] != "A" else "B") + body[1:]
        assert provider.validate_access_token(f"{tampered_body}.{sig}") is False

    def test_token_with_tampered_signature_rejected(self, provider):
        token = provider.issue_access_token()
        body, sig = token.rsplit(".", 1)
        # Flip the first base64-url character (6 full data bits, no padding)
        # to guarantee a different decoded byte regardless of key material.
        tampered_sig = ("A" if sig[0] != "A" else "B") + sig[1:]
        assert provider.validate_access_token(f"{body}.{tampered_sig}") is False

    def test_expired_token_rejected(self, tmp_path):
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        provider = oauth.OAuthProvider(
            hass=MagicMock(),
            client_id="cid-1234567890ABCDEF",
            client_secret="sec",
            webhook_id="wh",
            signing_key=b"\x00" * 32,
        )
        token = provider.issue_access_token()
        future = int(time.time()) + oauth.ACCESS_TOKEN_TTL + 60
        with patch.object(oauth.time, "time", return_value=future):
            assert provider.validate_access_token(token) is False

    def test_validate_bearer_accepts_valid_token(self, provider):
        token = provider.issue_access_token()
        request = MagicMock()
        request.headers = {"Authorization": f"Bearer {token}"}
        assert provider.validate_bearer(request) is True

    def test_validate_bearer_rejects_basic_scheme(self, provider):
        request = MagicMock()
        request.headers = {"Authorization": "Basic abcd"}
        assert provider.validate_bearer(request) is False

    def test_validate_bearer_rejects_missing_header(self, provider):
        request = MagicMock()
        request.headers = {}
        assert provider.validate_bearer(request) is False

    def test_authenticate_client_accepts_correct_creds(self, provider):
        assert provider.authenticate_client(
            "client-id-1234567890", "client-secret-very-secret"
        )

    def test_authenticate_client_rejects_wrong_id(self, provider):
        assert not provider.authenticate_client("wrong", "client-secret-very-secret")

    def test_authenticate_client_rejects_wrong_secret(self, provider):
        assert not provider.authenticate_client("client-id-1234567890", "wrong")

    def test_authenticate_client_rejects_blanks(self, provider):
        assert not provider.authenticate_client("", "")
        assert not provider.authenticate_client("a", "")
        assert not provider.authenticate_client("", "b")
        assert not provider.authenticate_client(None, None)

    def test_pkce_code_round_trip(self, provider, tmp_path):
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        verifier = "test-verifier-with-enough-length-for-spec-XX"
        challenge = oauth._b64url_encode(
            __import__("hashlib").sha256(verifier.encode()).digest()
        )
        code = provider.issue_code("https://claude.ai/cb", challenge)
        # Wrong verifier rejected
        assert provider.consume_code(code, "https://claude.ai/cb", "wrong") is False
        # Code is wiped on consume attempt — re-issue for the success case
        code2 = provider.issue_code("https://claude.ai/cb", challenge)
        assert provider.consume_code(code2, "https://claude.ai/cb", verifier) is True
        # Code is single-use
        assert provider.consume_code(code2, "https://claude.ai/cb", verifier) is False

    def test_code_rejects_redirect_uri_mismatch(self, provider, tmp_path):
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        verifier = "test-verifier-12345678901234567890-padding-aa"
        challenge = oauth._b64url_encode(
            __import__("hashlib").sha256(verifier.encode()).digest()
        )
        code = provider.issue_code("https://claude.ai/cb", challenge)
        assert provider.consume_code(code, "https://attacker.com/cb", verifier) is False

    def test_rotating_client_id_invalidates_existing_tokens(self, tmp_path):
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        hass = MagicMock()
        provider1 = oauth.OAuthProvider(
            hass=hass,
            client_id="id-aaaaaaaaaaaaaaaaa",
            client_secret="secret",
            webhook_id="wh",
            signing_key=b"\x00" * 32,
        )
        token = provider1.issue_access_token()
        # New provider with a different client_id (admin rotated) — token
        # signed for the old client_id must be rejected.
        provider2 = oauth.OAuthProvider(
            hass=hass,
            client_id="id-bbbbbbbbbbbbbbbbb",
            client_secret="secret",
            webhook_id="wh",
            signing_key=b"\x00" * 32,
        )
        assert provider2.validate_access_token(token) is False

    def test_signing_key_persists_across_provider_instances(self, tmp_path):
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        hass = MagicMock()
        provider1 = oauth.OAuthProvider(
            hass=hass,
            client_id="cid-1234567890ABCDEF",
            client_secret="sec",
            webhook_id="wh",
            signing_key=b"\x00" * 32,
        )
        token = provider1.issue_access_token()
        # New provider on the same disk → same signing key → token still valid
        provider2 = oauth.OAuthProvider(
            hass=hass,
            client_id="cid-1234567890ABCDEF",
            client_secret="sec",
            webhook_id="wh",
            signing_key=b"\x00" * 32,
        )
        assert provider2.validate_access_token(token) is True

    def test_validly_signed_non_dict_payload_rejected(self, tmp_path):
        """A token whose HMAC is valid but whose JSON body is NOT an object
        (e.g. a list or scalar) must be rejected, not crash the `.get(...)`
        access in _validate_token. Build the token exactly like _issue_token
        does but with a list body, signed with the provider's real key."""
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        provider = oauth.OAuthProvider(
            hass=MagicMock(),
            client_id="cid-1234567890ABCDEF",
            client_secret="sec",
            webhook_id="wh",
            signing_key=b"\x00" * 32,
        )
        import hashlib
        import hmac as _hmac

        body = oauth._b64url_encode(json.dumps([1, 2, 3]).encode())
        sig = _hmac.new(
            provider._signing_key, body.encode("ascii"), hashlib.sha256
        ).digest()
        token = f"{body}.{oauth._b64url_encode(sig)}"
        assert provider.validate_access_token(token) is False


def _wellknown_oauth_urls(oauth_mod, webhook_id):
    """Extra well-known metadata URLs a flavor registers (issue #1714), or an
    empty set when the flavor doesn't ship them yet.

    Feature-DETECTED from the oauth module rather than recorded in
    WEBHOOK_PROXY_VARIANTS: the promote workflow mechanically copies dev's
    code onto stable without touching tests, so a static per-variant flag
    would go stale (and fail CI) at the exact moment stable gains the
    feature. Detection keeps the expectation in lockstep with the code under
    test in both flavors.
    """
    if not hasattr(oauth_mod, "WellKnownAuthorizationServerMetadataView"):
        return set()
    base = oauth_mod.OAUTH_BASE
    return {
        f"/.well-known/oauth-protected-resource/api/webhook/{webhook_id}",
        f"/.well-known/oauth-authorization-server{base}",
        f"/.well-known/openid-configuration{base}",
        f"{base}/.well-known/openid-configuration",
        f"{base}/.well-known/oauth-authorization-server",
    }


class TestOAuthSetupEntry:
    """async_setup_entry creates and registers an OAuthProvider when the
    config has an oauth section with non-empty creds."""

    @pytest.fixture
    def mod(self):
        return _import_mcp_proxy()

    @pytest.fixture
    def hass(self, tmp_path):
        h = MagicMock()
        h.data = {}
        h.http = MagicMock()
        h.http.register_view = MagicMock()

        async def fake_executor(func, *args):
            return func(*args)

        h.async_add_executor_job = AsyncMock(side_effect=fake_executor)
        return h

    async def test_oauth_section_creates_provider_and_registers_views(
        self, hass, tmp_path
    ):
        # Pre-load the oauth module with a tmp secret file, then bind it as
        # mcp_proxy_init_<variant>.oauth so the integration's relative import
        # finds it.
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        mod = _import_mcp_proxy(preload_oauth=oauth)
        # Boot-time setup (is_running False): the first registration binds the
        # root views cleanly and takes the marker-clear path, so this test
        # doesn't touch the real /config marker via the mid-session write path.
        hass.is_running = False
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test",
            "oauth": {
                "client_id": "client-1234567890ABCDEF",
                "client_secret": "secret-much-secret",
            },
        }
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
        ):
            await mod.async_setup_entry(hass, MagicMock())

        provider = hass.data[mod.DOMAIN]["oauth"]
        assert provider is not None
        assert provider.client_id == "client-1234567890ABCDEF"
        # 4 core OAuth views, plus the well-known metadata variants on flavors
        # that ship them (feature-detected — see _wellknown_oauth_urls).
        expected_views = 4 + len(_wellknown_oauth_urls(oauth, "mcp_test"))
        assert hass.http.register_view.call_count == expected_views
        # Successful OAuth setup records that THIS flavor owns the root routes,
        # so the sibling flavor refuses loudly instead of shadowing them.
        assert hass.data[mod.OAUTH_ROUTE_OWNER_KEY] == mod.DOMAIN

    async def test_provider_init_failure_unregisters_and_raises(self, hass, tmp_path):
        """When the OAuth provider can't be constructed (e.g. the signing key
        can't be loaded), the user explicitly opted into auth, so we refuse to
        start: raise ConfigEntryError AND tear down the webhook registered
        above so no unauthenticated endpoint is left live, and close the
        session so it isn't leaked."""
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        mod = _import_mcp_proxy(preload_oauth=oauth)
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test_webhook_id_12345",
            "oauth": {
                "client_id": "client-1234567890ABCDEF",
                "client_secret": "secret-much-secret",
            },
        }
        session = MagicMock()
        session.close = AsyncMock()
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod, "async_unregister") as mock_unreg,
            patch.object(mod.aiohttp, "ClientSession", return_value=session),
            patch.object(
                oauth, "load_or_create_secret", side_effect=RuntimeError("boom")
            ),
            pytest.raises(_FakeConfigEntryError) as exc_info,
        ):
            await mod.async_setup_entry(hass, MagicMock())

        assert "Failed to enable OAuth" in str(exc_info.value)
        mock_unreg.assert_called_once_with(hass, "mcp_test_webhook_id_12345")
        session.close.assert_awaited_once()
        assert mod.DOMAIN not in hass.data

    async def test_sibling_flavor_owns_oauth_routes_refuses_loudly(
        self, hass, tmp_path
    ):
        """If the OTHER flavor already registered the root OAuth /authorize +
        /token views in this HA instance (the shared marker names its domain),
        we refuse LOUDLY (ConfigEntryError) instead of silently shadowing its
        routes — HA can't share or release those root views, and our provider
        uses a different signing key. Covers the sibling add-on being stopped
        but its views still bound."""
        mod = _import_mcp_proxy()
        sibling_domain = "mcp_proxy_dev" if mod.DOMAIN == "mcp_proxy" else "mcp_proxy"
        hass.data[mod.OAUTH_ROUTE_OWNER_KEY] = sibling_domain
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test_webhook_id_12345",
            "oauth": {
                "client_id": "client-1234567890ABCDEF",
                "client_secret": "secret-much-secret",
            },
        }
        session = MagicMock()
        session.close = AsyncMock()
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod, "async_unregister") as mock_unreg,
            patch.object(mod.aiohttp, "ClientSession", return_value=session),
            pytest.raises(_FakeConfigEntryError) as exc_info,
        ):
            await mod.async_setup_entry(hass, MagicMock())

        assert "already owns" in str(exc_info.value)
        # Refused before creating our provider: webhook torn down, session
        # closed, our DOMAIN not stored, and the sibling's marker left intact.
        mock_unreg.assert_called_once_with(hass, "mcp_test_webhook_id_12345")
        session.close.assert_awaited_once()
        assert mod.DOMAIN not in hass.data
        assert hass.data[mod.OAUTH_ROUTE_OWNER_KEY] == sibling_domain

    async def test_sibling_claims_routes_during_secret_load_refuses(
        self, hass, tmp_path
    ):
        """TOCTOU guard: the pre-await owner check can pass (no owner yet) and
        the sibling flavor's concurrently-setting-up entry can then register
        and claim the root routes while this entry is suspended in the
        load_or_create_secret executor await. The post-await re-check must see
        the sibling's claim and refuse loudly instead of registering shadowed
        duplicate views."""
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        mod = _import_mcp_proxy(preload_oauth=oauth)
        sibling_domain = "mcp_proxy_dev" if mod.DOMAIN == "mcp_proxy" else "mcp_proxy"
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test_webhook_id_12345",
            "oauth": {
                "client_id": "client-1234567890ABCDEF",
                "client_secret": "secret-much-secret",
            },
        }
        session = MagicMock()
        session.close = AsyncMock()

        def sibling_claims_then_returns_key():
            # Runs inside the executor await — the suspension window in which
            # the sibling's setup can interleave on the event loop.
            hass.data[mod.OAUTH_ROUTE_OWNER_KEY] = sibling_domain
            return b"k" * 32

        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod, "async_unregister") as mock_unreg,
            patch.object(mod.aiohttp, "ClientSession", return_value=session),
            patch.object(
                oauth,
                "load_or_create_secret",
                side_effect=sibling_claims_then_returns_key,
            ),
            pytest.raises(_FakeConfigEntryError) as exc_info,
        ):
            await mod.async_setup_entry(hass, MagicMock())

        assert "claimed" in str(exc_info.value)
        # Same teardown contract as the pre-await guard: webhook torn down,
        # session closed, no views registered, our DOMAIN not stored, and the
        # sibling's claim left intact.
        mock_unreg.assert_called_once_with(hass, "mcp_test_webhook_id_12345")
        session.close.assert_awaited_once()
        hass.http.register_view.assert_not_called()
        assert mod.DOMAIN not in hass.data
        assert hass.data[mod.OAUTH_ROUTE_OWNER_KEY] == sibling_domain


class TestOAuthRestartRepairTrigger:
    """async_setup_entry surfaces the restart Repair when OAuth is enabled
    MID-SESSION (hass.is_running is True), because HA only binds the root
    /authorize + /token views cleanly at startup. On a boot-time setup
    (hass.is_running False), or with OAuth off, it clears any stale
    marker/issue instead — no restart is needed."""

    @pytest.fixture
    def hass(self):
        h = MagicMock()
        h.data = {}
        h.http = MagicMock()
        h.http.register_view = MagicMock()

        async def fake_executor(func, *args):
            return func(*args)

        h.async_add_executor_job = AsyncMock(side_effect=fake_executor)
        return h

    @staticmethod
    def _oauth_config():
        return {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test",
            "oauth": {
                "client_id": "client-1234567890ABCDEF",
                "client_secret": "secret-much-secret",
            },
        }

    async def test_oauth_enabled_mid_session_raises_restart_repair(
        self, hass, tmp_path
    ):
        """Mid-session enable: the marker is written and the Repair issue is
        created; the clear path must NOT run."""
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        mod = _import_mcp_proxy(preload_oauth=oauth)
        repairs = _bind_repairs(mod, tmp_path)
        hass.is_running = True
        with (
            patch.object(mod, "_read_config", return_value=self._oauth_config()),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
            patch.object(repairs, "create_issue") as mock_create_issue,
            patch.object(repairs, "_clear_marker") as mock_clear,
            patch.object(repairs, "_delete_issue_only") as mock_delete,
        ):
            # _write_marker runs for real, writing the redirected tmp marker.
            await mod.async_setup_entry(hass, MagicMock())

        assert repairs.RESTART_MARKER_FILE.exists()
        mock_create_issue.assert_called_once_with(hass, mod.DOMAIN)
        mock_clear.assert_not_called()
        mock_delete.assert_not_called()

    async def test_oauth_enabled_during_boot_clears_marker(self, hass, tmp_path):
        """Boot-time enable (views bind cleanly): the stale marker is cleared,
        the issue is deleted, and NO restart Repair is created."""
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        mod = _import_mcp_proxy(preload_oauth=oauth)
        repairs = _bind_repairs(mod, tmp_path)
        hass.is_running = False
        repairs.RESTART_MARKER_FILE.write_text('{"reason": "stale"}')
        with (
            patch.object(mod, "_read_config", return_value=self._oauth_config()),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
            patch.object(repairs, "create_issue") as mock_create_issue,
            patch.object(repairs, "_write_marker") as mock_write,
            patch.object(repairs, "_delete_issue_only") as mock_delete,
        ):
            # _clear_marker runs for real, deleting the redirected tmp marker.
            await mod.async_setup_entry(hass, MagicMock())

        assert not repairs.RESTART_MARKER_FILE.exists()
        mock_delete.assert_called_once_with(hass, mod.DOMAIN)
        mock_create_issue.assert_not_called()
        mock_write.assert_not_called()

    async def test_oauth_off_clears_marker(self, hass, tmp_path):
        """No oauth section: even mid-session (is_running True) this clears the
        stale marker/issue and never creates a restart Repair."""
        mod = _import_mcp_proxy()
        repairs = _bind_repairs(mod, tmp_path)
        hass.is_running = True
        repairs.RESTART_MARKER_FILE.write_text('{"reason": "stale"}')
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test",
        }
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
            patch.object(repairs, "create_issue") as mock_create_issue,
            patch.object(repairs, "_write_marker") as mock_write,
            patch.object(repairs, "_delete_issue_only") as mock_delete,
        ):
            await mod.async_setup_entry(hass, MagicMock())

        assert not repairs.RESTART_MARKER_FILE.exists()
        mock_delete.assert_called_once_with(hass, mod.DOMAIN)
        mock_create_issue.assert_not_called()
        mock_write.assert_not_called()

    async def test_same_flavor_reload_reuses_views_no_restart(self, hass, tmp_path):
        """Mid-session reload of OUR OWN entry with the SAME OAuth identity
        (route owner already == DOMAIN AND the bound-view fingerprint matches
        the current creds + signing key, is_running True): setup PROCEEDS
        without the sibling 'already owns' raise, does NOT re-register the root
        views (HA can't re-bind them mid-session), and clears any stale
        marker/issue instead of raising a restart Repair — OAuth is already
        live. This is the FIX for the spurious restart Repair on every benign
        mid-session reload."""
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        mod = _import_mcp_proxy(preload_oauth=oauth)
        repairs = _bind_repairs(mod, tmp_path)
        hass.is_running = True
        # Pin the signing key so the fingerprint of the bound views is
        # computable, then seed a MATCHING fingerprint — the "reload with the
        # same identity" case that must reuse the views without a restart.
        fixed_key = b"k" * 32
        creds = self._oauth_config()["oauth"]
        # We already registered the root OAuth views earlier this session, bound
        # to the SAME identity we're now reloading with.
        hass.data[mod.OAUTH_ROUTE_OWNER_KEY] = mod.DOMAIN
        hass.data[mod.OAUTH_ROUTE_KEY_FINGERPRINT] = mod._oauth_route_fingerprint(
            creds["client_id"], creds["client_secret"], fixed_key
        )
        repairs.RESTART_MARKER_FILE.write_text('{"reason": "stale"}')
        with (
            patch.object(mod, "_read_config", return_value=self._oauth_config()),
            patch.object(mod, "async_register"),
            patch.object(mod, "async_unregister") as mock_unreg,
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
            patch.object(oauth, "load_or_create_secret", return_value=fixed_key),
            patch.object(repairs, "create_issue") as mock_create_issue,
            patch.object(repairs, "_write_marker") as mock_write,
            patch.object(repairs, "_delete_issue_only") as mock_delete,
        ):
            # _clear_marker runs for real, deleting the redirected tmp marker.
            result = await mod.async_setup_entry(hass, MagicMock())

        # Setup proceeded (no "already owns" raise); provider still stored and
        # the webhook was NOT torn down.
        assert result is True
        assert hass.data[mod.DOMAIN]["oauth"] is not None
        mock_unreg.assert_not_called()
        # The 4 root views are NOT re-registered on a same-flavor reload.
        assert hass.http.register_view.call_count == 0
        # Ownership marker + bound-view fingerprint stay ours (unchanged).
        assert hass.data[mod.OAUTH_ROUTE_OWNER_KEY] == mod.DOMAIN
        assert hass.data[mod.OAUTH_ROUTE_KEY_FINGERPRINT] == (
            mod._oauth_route_fingerprint(
                creds["client_id"], creds["client_secret"], fixed_key
            )
        )
        # No restart Repair: clear path taken (marker cleared, issue deleted).
        assert not repairs.RESTART_MARKER_FILE.exists()
        mock_delete.assert_called_once_with(hass, mod.DOMAIN)
        mock_create_issue.assert_not_called()
        mock_write.assert_not_called()

    async def test_same_flavor_reload_creds_changed_raises_restart(
        self, hass, tmp_path
    ):
        """Mid-session reload of OUR OWN entry after the OAuth creds/key were
        REGENERATED (route owner == DOMAIN but the bound-view fingerprint no
        longer matches the reloaded identity, is_running True): the live root
        views still serve the OLD identity while the webhook now validates
        against the NEW one, so no client can obtain a token the webhook
        accepts. Setup must PROCEED (no 'already owns' raise), must NOT
        re-register the root views (HA can't re-bind them mid-session), and must
        take the restart path (write marker + create Repair, no clear) so the
        user is prompted to restart HA to activate the new credentials — the
        credential-regeneration bug this fixes."""
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        mod = _import_mcp_proxy(preload_oauth=oauth)
        repairs = _bind_repairs(mod, tmp_path)
        hass.is_running = True
        # We own the routes, but the bound views were registered with a
        # DIFFERENT (now-stale) identity than the creds we're reloading with.
        hass.data[mod.OAUTH_ROUTE_OWNER_KEY] = mod.DOMAIN
        hass.data[mod.OAUTH_ROUTE_KEY_FINGERPRINT] = "stale-fingerprint"
        with (
            patch.object(mod, "_read_config", return_value=self._oauth_config()),
            patch.object(mod, "async_register"),
            patch.object(mod, "async_unregister") as mock_unreg,
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
            patch.object(repairs, "create_issue") as mock_create_issue,
            patch.object(repairs, "_clear_marker") as mock_clear,
            patch.object(repairs, "_delete_issue_only") as mock_delete,
        ):
            # _write_marker runs for real, writing the redirected tmp marker.
            result = await mod.async_setup_entry(hass, MagicMock())

        # Setup proceeded (no "already owns" raise); provider stored, webhook
        # NOT torn down.
        assert result is True
        assert hass.data[mod.DOMAIN]["oauth"] is not None
        mock_unreg.assert_not_called()
        # The root views are NOT re-registered (HA can't re-bind mid-session).
        assert hass.http.register_view.call_count == 0
        # Ownership marker stays ours; the stale fingerprint is left in place so
        # a later boot-time setup re-registers and refreshes it.
        assert hass.data[mod.OAUTH_ROUTE_OWNER_KEY] == mod.DOMAIN
        assert hass.data[mod.OAUTH_ROUTE_KEY_FINGERPRINT] == "stale-fingerprint"
        # Restart Repair path: marker written + issue created, clear NOT taken.
        assert repairs.RESTART_MARKER_FILE.exists()
        mock_create_issue.assert_called_once_with(hass, mod.DOMAIN)
        mock_clear.assert_not_called()
        mock_delete.assert_not_called()


class TestOAuthWebhookHandler:
    """The webhook handler enforces bearer auth when an OAuthProvider is
    configured, and is a no-op gate when not."""

    @pytest.fixture
    def setup(self, tmp_path):
        """Import mcp_proxy with a shared oauth module so the integration's
        `from .oauth import build_unauthorized_response` resolves to the
        same instance used by the test fixture."""
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        mod = _import_mcp_proxy(preload_oauth=oauth)
        return mod, oauth

    def _make_request(self, auth_header=None):
        req = MagicMock()
        req.headers = {"Authorization": auth_header} if auth_header else {}
        req.read = AsyncMock(return_value=b"")
        req.method = "POST"
        req.scheme = "https"
        return req

    def _make_hass(self, mod, oauth_provider):
        h = MagicMock()
        h.data = {
            mod.DOMAIN: {
                "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "webhook_id": "mcp_test",
                "session": MagicMock(),
                "oauth": oauth_provider,
            }
        }
        return h

    async def test_returns_401_on_missing_bearer(self, setup):
        mod, oauth = setup
        provider = oauth.OAuthProvider(
            hass=MagicMock(),
            client_id="cid-1234567890ABCDEF",
            client_secret="sec",
            webhook_id="wh",
            signing_key=b"\x00" * 32,
        )
        hass = self._make_hass(mod, provider)
        request = self._make_request(None)
        request.headers["Host"] = "example.nabu.casa"

        with patch.object(oauth.web, "Response") as response_ctor:
            await mod._handle_webhook(hass, "mcp_test", request)

        request.read.assert_not_awaited()
        response_ctor.assert_called_once()
        kwargs = response_ctor.call_args.kwargs
        assert kwargs.get("status") == 401
        ww = kwargs.get("headers", {}).get("WWW-Authenticate", "")
        assert ww.startswith("Bearer realm=")
        assert "resource_metadata=" in ww

    async def test_returns_401_on_invalid_bearer(self, setup):
        mod, oauth = setup
        provider = oauth.OAuthProvider(
            hass=MagicMock(),
            client_id="cid-1234567890ABCDEF",
            client_secret="sec",
            webhook_id="wh",
            signing_key=b"\x00" * 32,
        )
        hass = self._make_hass(mod, provider)
        request = self._make_request("Bearer not.a.valid.token")
        request.headers["Host"] = "example.nabu.casa"

        with patch.object(oauth.web, "Response") as response_ctor:
            await mod._handle_webhook(hass, "mcp_test", request)

        request.read.assert_not_awaited()
        response_ctor.assert_called_once()
        assert response_ctor.call_args.kwargs.get("status") == 401

    async def test_non_ascii_bearer_gate_returns_401_not_swallowed(self, setup):
        """C1 gate-level regression: a legacy-mode bearer whose pre-signature
        segment has a non-ASCII char must yield the 401 discovery challenge, not
        raise UnicodeEncodeError out of the gate — HA core's async_handle_webhook
        would swallow that into a 200 OK and never challenge the client."""
        mod, oauth = setup
        # The C1 hardening rides the dev flavor until promotion (stable still
        # hmac's body.encode("ascii") outside the decode try), so skip on stable.
        if not _ha_auth_supported():
            pytest.skip("legacy non-ASCII hardening rides the dev flavor until promote")
        provider = oauth.OAuthProvider(
            hass=MagicMock(),
            client_id="cid-1234567890ABCDEF",
            client_secret="sec",
            webhook_id="wh",
            signing_key=b"\x00" * 32,
        )
        hass = self._make_hass(mod, provider)
        request = self._make_request("Bearer é.AA")
        request.headers["Host"] = "example.nabu.casa"

        with patch.object(oauth.web, "Response") as response_ctor:
            await mod._handle_webhook(hass, "mcp_test", request)

        request.read.assert_not_awaited()
        response_ctor.assert_called_once()
        assert response_ctor.call_args.kwargs.get("status") == 401
        ww = response_ctor.call_args.kwargs.get("headers", {}).get(
            "WWW-Authenticate", ""
        )
        assert ww.startswith("Bearer realm=")
        assert "resource_metadata=" in ww

    async def test_passes_through_with_valid_bearer(self, setup):
        mod, oauth = setup
        provider = oauth.OAuthProvider(
            hass=MagicMock(),
            client_id="cid-1234567890ABCDEF",
            client_secret="sec",
            webhook_id="wh",
            signing_key=b"\x00" * 32,
        )
        token = provider.issue_access_token()
        hass = self._make_hass(mod, provider)
        request = self._make_request(f"Bearer {token}")

        sentinel = mod.aiohttp.ClientError("stop here")
        hass.data[mod.DOMAIN]["session"].request = MagicMock(side_effect=sentinel)

        await mod._handle_webhook(hass, "mcp_test", request)

        request.read.assert_awaited_once()  # auth gate passed


class TestResolveOAuthCreds:
    """start.py auto-generates OAuth creds when the user leaves the addon
    fields blank. User-supplied values take precedence; persisted values are
    reused across restarts so a Claude.ai connector keeps working."""

    @pytest.fixture
    def start(self):
        return _import_start()

    def test_user_values_passthrough(self, start, tmp_path):
        cid, sec = start._resolve_oauth_creds(
            tmp_path, "user-supplied-id-1234567890", "user-supplied-secret"
        )
        assert cid == "user-supplied-id-1234567890"
        assert sec == "user-supplied-secret"

    def test_user_values_trimmed(self, start, tmp_path):
        cid, sec = start._resolve_oauth_creds(
            tmp_path, "  user-supplied-id-1234567890  ", "  pw  "
        )
        assert cid == "user-supplied-id-1234567890"
        assert sec == "pw"

    def test_blank_fields_auto_generate(self, start, tmp_path):
        cid, sec = start._resolve_oauth_creds(tmp_path, "", "")
        assert cid.startswith("hamcp-")
        assert len(cid) >= 16
        assert len(sec) >= 32  # token_urlsafe(32) is ~43 chars

    def test_blank_fields_persist_to_disk(self, start, tmp_path):
        cid, sec = start._resolve_oauth_creds(tmp_path, "", "")
        creds_file = tmp_path / "oauth_creds.json"
        assert creds_file.exists()
        stored = json.loads(creds_file.read_text())
        assert stored["client_id"] == cid
        assert stored["client_secret"] == sec

    def test_persisted_values_reused_across_calls(self, start, tmp_path):
        cid1, sec1 = start._resolve_oauth_creds(tmp_path, "", "")
        cid2, sec2 = start._resolve_oauth_creds(tmp_path, "", "")
        assert cid1 == cid2
        assert sec1 == sec2

    def test_user_value_overrides_persisted(self, start, tmp_path):
        # First call generates and persists (return value intentionally unused;
        # this call's side effect is writing oauth_creds.json to disk).
        start._resolve_oauth_creds(tmp_path, "", "")
        # Second call with user-supplied values uses those
        cid, sec = start._resolve_oauth_creds(
            tmp_path, "user-id-1234567890123", "user-secret"
        )
        assert cid == "user-id-1234567890123"
        assert sec == "user-secret"
        # And the persisted file is updated
        stored = json.loads((tmp_path / "oauth_creds.json").read_text())
        assert stored["client_id"] == "user-id-1234567890123"
        assert stored["client_secret"] == "user-secret"

    def test_partial_override_picks_persisted_for_blank_field(self, start, tmp_path):
        # Generate and persist baseline
        gen_cid, gen_sec = start._resolve_oauth_creds(tmp_path, "", "")
        # User rotates only the secret, leaves client_id blank
        cid, sec = start._resolve_oauth_creds(tmp_path, "", "rotated-secret")
        assert cid == gen_cid  # client_id reused from disk
        assert sec == "rotated-secret"

    def test_corrupted_creds_file_falls_back_to_generation(
        self, start, tmp_path, capsys
    ):
        (tmp_path / "oauth_creds.json").write_text("not valid json{{{")
        cid, sec = start._resolve_oauth_creds(tmp_path, "", "")
        assert cid.startswith("hamcp-")
        assert len(sec) >= 32
        # Logged a warning
        assert "Could not read existing OAuth creds" in capsys.readouterr().err


class TestOAuthFilePermissions:
    """The signing-key and creds files must land at 0600 even on an OVERWRITE
    (os.open only applies the mode on CREATE, so a plain overwrite of a
    pre-existing wider file would NOT re-restrict it — the atomic temp+replace
    helper guarantees 0600 on both first-create and regenerate). When the
    filesystem can't honor a restricted create (tmpfs / non-POSIX), both paths
    fall back to a best-effort plain write AND warn rather than failing."""

    def _secret_file(self, tmp_path):
        # Reflect each flavor's REAL default basename (dev's is
        # .mcp_proxy_dev_oauth_secret), not the stable literal.
        return tmp_path / f".{CURRENT['component']}_oauth_secret"

    def test_secret_file_created_0600(self, tmp_path):
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        oauth.SECRET_FILE = self._secret_file(tmp_path)
        oauth.load_or_create_secret()
        assert oauth.SECRET_FILE.exists()
        assert oct(oauth.SECRET_FILE.stat().st_mode & 0o777) == "0o600"

    def test_secret_file_rerestricted_on_overwrite(self, tmp_path):
        """Regression for the core fix: a pre-existing WIDER (0644) short/invalid
        key file gets re-restricted to 0600 when regenerated — the old
        os.open(O_CREAT) reused the wide mode on overwrite and left it readable."""
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        secret_file = self._secret_file(tmp_path)
        oauth.SECRET_FILE = secret_file
        # Short (invalid) existing key at wide perms → forces regeneration.
        secret_file.write_bytes(b"short")
        os.chmod(secret_file, 0o644)
        oauth.load_or_create_secret()
        assert oct(secret_file.stat().st_mode & 0o777) == "0o600"

    def test_secret_falls_back_and_warns_on_oserror(self, tmp_path, caplog):
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        oauth.SECRET_FILE = self._secret_file(tmp_path)
        with (
            patch.object(oauth.os, "open", side_effect=OSError("no mode bits")),
            caplog.at_level(logging.WARNING),
        ):
            secret = oauth.load_or_create_secret()
        # Persisted via the plain-write fallback...
        assert oauth.SECRET_FILE.read_bytes() == secret
        assert len(secret) == 32
        # ...and the degradation was warned about, not swallowed.
        assert any("restricted permissions" in r.getMessage() for r in caplog.records)

    def test_creds_file_created_0600(self, tmp_path):
        start = _import_start()
        start._resolve_oauth_creds(tmp_path, "", "")
        creds_file = tmp_path / "oauth_creds.json"
        assert creds_file.exists()
        assert oct(creds_file.stat().st_mode & 0o777) == "0o600"

    def test_creds_falls_back_and_warns_on_oserror(self, tmp_path, capsys):
        start = _import_start()
        with patch.object(start.os, "open", side_effect=OSError("no mode bits")):
            cid, sec = start._resolve_oauth_creds(tmp_path, "", "")
        creds_file = tmp_path / "oauth_creds.json"
        # Fallback plain-write still persisted VALID creds (not ("", "")).
        assert cid.startswith("hamcp-")
        assert len(sec) >= 32
        stored = json.loads(creds_file.read_text())
        assert stored["client_id"] == cid
        assert stored["client_secret"] == sec
        assert "wider permissions than intended" in capsys.readouterr().err


class TestStartOAuthValidation:
    """start.py-level validation: enable_oauth=true triggers credential
    resolution; only the length check on a user-supplied client_id can
    block startup. Blank fields are valid (auto-generated)."""

    def _patched_path(self, tmp_path, **opts):
        options_dir = tmp_path / "data"
        options_dir.mkdir()
        (options_dir / "options.json").write_text(json.dumps(opts))

        def path_factory(arg):
            if arg == "/data/options.json":
                return options_dir / "options.json"
            if arg == "/data":
                return options_dir
            return Path(arg)

        return path_factory

    def _run_main_with_options(self, tmp_path, **opts):
        start = _import_start()
        path_factory = self._patched_path(tmp_path, **opts)
        # main() calls _refuse_if_sibling_running() first, which now retries the
        # Supervisor /addons query with a time.sleep() backoff on failure. Pin a
        # benign "no siblings" response so the mutex guard passes instantly with
        # no network call, and neutralize the backoff sleep, so this test stays
        # fast and hermetic (no ~4s hang, no real Supervisor HTTP call in CI).
        with (
            patch.object(start, "Path", side_effect=path_factory),
            patch.object(start, "_supervisor_get", return_value={"addons": []}),
            patch.object(start.time, "sleep", return_value=None),
        ):
            return start.main()

    def test_short_user_supplied_client_id_rejected(self, tmp_path, capsys):
        rc = self._run_main_with_options(
            tmp_path,
            enable_oauth=True,
            oauth_client_id="too-short",
            oauth_client_secret="some-secret-here-with-length",
        )
        assert rc == 1
        assert "OAuth Client ID is too short" in capsys.readouterr().err


class TestRegenerateOAuthCreds:
    """The Regenerate OAuth Credentials toggle wipes the stored creds and
    forces fresh generation, then auto-clears itself via the Supervisor API
    so subsequent restarts don't keep regenerating."""

    @pytest.fixture
    def start(self):
        return _import_start()

    def test_regenerate_wipes_existing_creds_file(self, start, tmp_path):
        creds_file = tmp_path / "oauth_creds.json"
        creds_file.write_text(
            '{"client_id": "old-id-1234567890", "client_secret": "old-sec"}'
        )
        start._regenerate_oauth_creds(tmp_path)
        assert not creds_file.exists()

    def test_regenerate_is_idempotent_when_file_missing(self, start, tmp_path):
        # Should not raise even though the file isn't there
        start._regenerate_oauth_creds(tmp_path)
        assert not (tmp_path / "oauth_creds.json").exists()

    def test_regenerate_followed_by_resolve_yields_new_values(self, start, tmp_path):
        creds_file = tmp_path / "oauth_creds.json"
        creds_file.write_text(
            '{"client_id": "old-id-1234567890", "client_secret": "old-sec"}'
        )
        start._regenerate_oauth_creds(tmp_path)
        cid, sec = start._resolve_oauth_creds(tmp_path, "", "")
        assert cid != "old-id-1234567890"
        assert sec != "old-sec"
        assert cid.startswith("hamcp-")
        assert len(sec) >= 32

    def test_clear_regenerate_toggle_posts_options_with_false(self, start):
        captured = {}

        def fake_post(path, data):
            captured["path"] = path
            captured["data"] = data
            return True

        with patch.object(start, "_supervisor_post", side_effect=fake_post):
            ok = start._clear_regenerate_toggle(
                {
                    "remote_url": "https://example.com",
                    "regenerate_oauth_creds": True,
                }
            )

        assert ok is True
        assert captured["path"] == "/addons/self/options"
        # Toggle was flipped, other options preserved
        assert captured["data"] == {
            "options": {
                "remote_url": "https://example.com",
                "regenerate_oauth_creds": False,
            }
        }

    def test_clear_regenerate_toggle_returns_false_on_supervisor_error(self, start):
        with patch.object(start, "_supervisor_post", return_value=False):
            ok = start._clear_regenerate_toggle({"regenerate_oauth_creds": True})
        assert ok is False


class TestStartInstallIntegration:
    """_install_integration returns (first_install, version_changed)."""

    def test_first_install_when_dst_missing(self, tmp_path, monkeypatch):
        start = _import_start()
        src = tmp_path / "src"
        src.mkdir()
        (src / "manifest.json").write_text('{"version": "1.0.3-beta.1"}')
        dst_parent = tmp_path / "dst-parent"

        # Patch the two Path calls inside _install_integration
        def path_factory(arg):
            if arg == f"/opt/{CURRENT['component']}":
                return src
            if arg == f"/config/custom_components/{CURRENT['component']}":
                return dst_parent / CURRENT["component"]
            if arg == "/config/custom_components":
                return dst_parent
            return Path(arg)

        with patch.object(start, "Path", side_effect=path_factory):
            first_install, version_changed = start._install_integration()

        assert first_install is True
        assert version_changed is False
        assert (dst_parent / CURRENT["component"] / "manifest.json").exists()

    def test_version_changed_when_versions_differ(self, tmp_path):
        start = _import_start()
        src = tmp_path / "src"
        src.mkdir()
        (src / "manifest.json").write_text('{"version": "1.0.3-beta.1"}')
        dst_parent = tmp_path / "dst-parent"
        dst_parent.mkdir()
        (dst_parent / CURRENT["component"]).mkdir()
        (dst_parent / CURRENT["component"] / "manifest.json").write_text(
            '{"version": "1.0.2"}'
        )

        def path_factory(arg):
            if arg == f"/opt/{CURRENT['component']}":
                return src
            if arg == f"/config/custom_components/{CURRENT['component']}":
                return dst_parent / CURRENT["component"]
            if arg == "/config/custom_components":
                return dst_parent
            return Path(arg)

        with patch.object(start, "Path", side_effect=path_factory):
            first_install, version_changed = start._install_integration()

        assert first_install is False
        assert version_changed is True

    def test_no_change_when_versions_match(self, tmp_path):
        start = _import_start()
        src = tmp_path / "src"
        src.mkdir()
        (src / "manifest.json").write_text('{"version": "1.0.3-beta.1"}')
        dst_parent = tmp_path / "dst-parent"
        dst_parent.mkdir()
        (dst_parent / CURRENT["component"]).mkdir()
        (dst_parent / CURRENT["component"] / "manifest.json").write_text(
            '{"version": "1.0.3-beta.1"}'
        )

        def path_factory(arg):
            if arg == f"/opt/{CURRENT['component']}":
                return src
            if arg == f"/config/custom_components/{CURRENT['component']}":
                return dst_parent / CURRENT["component"]
            if arg == "/config/custom_components":
                return dst_parent
            return Path(arg)

        with patch.object(start, "Path", side_effect=path_factory):
            first_install, version_changed = start._install_integration()

        assert first_install is False
        assert version_changed is False

    def test_corrupt_dst_manifest_does_not_trigger_version_changed(self, tmp_path):
        """A corrupt destination manifest should NOT report version_changed
        — that's reserved for genuine version differences. The integration
        files are still copied (to repair the install) but the user isn't
        spammed with a 'restart required' notification."""
        start = _import_start()
        src = tmp_path / "src"
        src.mkdir()
        (src / "manifest.json").write_text('{"version": "1.0.3-beta.1"}')
        dst_parent = tmp_path / "dst-parent"
        dst_parent.mkdir()
        (dst_parent / CURRENT["component"]).mkdir()
        # Corrupted JSON
        (dst_parent / CURRENT["component"] / "manifest.json").write_text("not json{{{")

        def path_factory(arg):
            if arg == f"/opt/{CURRENT['component']}":
                return src
            if arg == f"/config/custom_components/{CURRENT['component']}":
                return dst_parent / CURRENT["component"]
            if arg == "/config/custom_components":
                return dst_parent
            return Path(arg)

        with patch.object(start, "Path", side_effect=path_factory):
            first_install, version_changed = start._install_integration()

        assert first_install is False
        assert version_changed is False
        # Repaired install — files are copied
        assert (dst_parent / CURRENT["component"] / "manifest.json").exists()


# ===========================================================================
# OAuth view-level HTTP tests
# ===========================================================================
#
# The TestOAuthProvider class above tests the provider's primitives in
# isolation. The classes below exercise the actual HTTP handlers — the
# wiring between a `web.Request` and the responses MCP clients see. Bugs
# in this layer (a regression that accepts `plain` PKCE or HTTP redirect
# URIs, an XSS in the consent page, missing fields in metadata documents)
# would silently break security or compatibility.


def _provider_for_view_tests(tmp_path, public_base_url=None):
    """Build an OAuthProvider wired up for view-level HTTP tests."""
    oauth = _import_oauth(tmp_secret_dir=tmp_path)
    provider = oauth.OAuthProvider(
        hass=MagicMock(),
        client_id="client-id-1234567890ABCDEF",
        client_secret="client-secret-very-secret",
        webhook_id="mcp_webhook_id_aaaa",
        signing_key=b"\x00" * 32,
        public_base_url=public_base_url,
    )
    return oauth, provider


def _make_view_request(
    *, headers=None, query=None, method="GET", post_data=None, scheme="https"
):
    """Mock a starlette/aiohttp-style web.Request with the bits the views read."""
    req = MagicMock()
    req.headers = headers or {}
    req.query = query or {}
    req.method = method
    req.scheme = scheme
    if post_data is not None:
        req.post = AsyncMock(return_value=post_data)
    return req


class TestBuildBaseUrl:
    """`_build_base_url` is the trust-boundary primitive — it decides
    whether OAuth metadata URLs are pinned to the operator-configured
    public URL or derived from per-request headers."""

    @pytest.fixture
    def oauth(self, tmp_path):
        return _import_oauth(tmp_secret_dir=tmp_path)

    def test_public_base_url_wins_over_headers(self, oauth):
        request = _make_view_request(
            headers={"Host": "evil.example", "X-Forwarded-Proto": "http"}
        )
        result = oauth._build_base_url(request, "https://legit.example")
        assert result == "https://legit.example"

    def test_public_base_url_trailing_slash_stripped(self, oauth):
        request = _make_view_request(headers={"Host": "ignored"})
        result = oauth._build_base_url(request, "https://legit.example/")
        assert result == "https://legit.example"

    def test_falls_back_to_x_forwarded(self, oauth):
        request = _make_view_request(
            headers={
                "Host": "should-be-overridden",
                "X-Forwarded-Host": "real.example",
                "X-Forwarded-Proto": "https",
            }
        )
        result = oauth._build_base_url(request, None)
        assert result == "https://real.example"

    def test_falls_back_to_host_header(self, oauth):
        request = _make_view_request(headers={"Host": "host.example"}, scheme="https")
        result = oauth._build_base_url(request, None)
        assert result == "https://host.example"


class TestProtectedResourceView:
    async def test_returns_resource_metadata_with_pinned_base(self, tmp_path):
        oauth, provider = _provider_for_view_tests(
            tmp_path, public_base_url="https://legit.example"
        )
        view = oauth.ProtectedResourceMetadataView(provider)
        request = _make_view_request(headers={"Host": "evil.example"})

        with patch.object(oauth.web, "json_response") as json_resp:
            await view.get(request)

        body = json_resp.call_args.args[0]
        assert body["resource"] == (
            "https://legit.example/api/webhook/mcp_webhook_id_aaaa"
        )
        assert body["authorization_servers"] == [
            f"https://legit.example{CURRENT['oauth_base']}"
        ]
        assert body["bearer_methods_supported"] == ["header"]


class TestAuthorizationServerView:
    async def test_returns_required_metadata_fields(self, tmp_path):
        oauth, provider = _provider_for_view_tests(
            tmp_path, public_base_url="https://legit.example"
        )
        view = oauth.AuthorizationServerMetadataView(provider)
        request = _make_view_request(headers={"Host": "ignored"})

        with patch.object(oauth.web, "json_response") as json_resp:
            await view.get(request)

        body = json_resp.call_args.args[0]
        assert body["issuer"].endswith(CURRENT["oauth_base"])
        # Authorize/token endpoints live at the root path of the host so
        # that clients constructing them from the resource host (Claude.ai)
        # find them.
        assert body["authorization_endpoint"] == "https://legit.example/authorize"
        assert body["token_endpoint"] == "https://legit.example/token"
        assert "code" in body["response_types_supported"]
        assert "authorization_code" in body["grant_types_supported"]
        assert "refresh_token" in body["grant_types_supported"]
        assert "S256" in body["code_challenge_methods_supported"]
        assert "client_secret_basic" in body["token_endpoint_auth_methods_supported"]
        assert "client_secret_post" in body["token_endpoint_auth_methods_supported"]


class TestWellKnownMetadataViews:
    """The RFC 8414 / RFC 9728 / OIDC well-known variants (issue #1714) must
    serve documents identical to the canonical metadata views, at the exact
    URLs claude.ai was captured probing. Skips on flavors that don't ship the
    views yet."""

    @staticmethod
    def _skip_unless_shipped(oauth):
        if not hasattr(oauth, "WellKnownAuthorizationServerMetadataView"):
            pytest.skip("flavor does not ship the well-known metadata views yet")

    async def test_path_scoped_prm_embeds_webhook_id_and_matches_canonical(
        self, tmp_path
    ):
        oauth, provider = _provider_for_view_tests(
            tmp_path, public_base_url="https://legit.example"
        )
        self._skip_unless_shipped(oauth)
        view = oauth.WellKnownProtectedResourceView(provider)
        # RFC 9728 §3.1: the well-known path is derived from the resource URL,
        # so it must embed this install's actual webhook id.
        assert view.url == (
            "/.well-known/oauth-protected-resource/api/webhook/mcp_webhook_id_aaaa"
        )
        request = _make_view_request(headers={"Host": "ignored"})
        with patch.object(oauth.web, "json_response") as json_resp:
            await view.get(request)
        wellknown_body = json_resp.call_args.args[0]
        with patch.object(oauth.web, "json_response") as json_resp:
            await oauth.ProtectedResourceMetadataView(provider).get(request)
        assert wellknown_body == json_resp.call_args.args[0]

    async def test_wellknown_as_variants_match_canonical_document(self, tmp_path):
        oauth, provider = _provider_for_view_tests(
            tmp_path, public_base_url="https://legit.example"
        )
        self._skip_unless_shipped(oauth)
        request = _make_view_request(headers={"Host": "ignored"})
        with patch.object(oauth.web, "json_response") as json_resp:
            await oauth.AuthorizationServerMetadataView(provider).get(request)
        canonical = json_resp.call_args.args[0]
        base = oauth.OAUTH_BASE
        for url in (
            f"/.well-known/oauth-authorization-server{base}",
            f"/.well-known/openid-configuration{base}",
            f"{base}/.well-known/openid-configuration",
            f"{base}/.well-known/oauth-authorization-server",
        ):
            view = oauth.WellKnownAuthorizationServerMetadataView(
                provider, url=url, name=f"test:{url}"
            )
            assert view.url == url
            with patch.object(oauth.web, "json_response") as json_resp:
                await view.get(request)
            assert json_resp.call_args.args[0] == canonical
        # No DCR: the proxy has fixed credentials, so the document must not
        # advertise a registration endpoint (clients would try it and fail).
        assert "registration_endpoint" not in canonical


class TestAuthorizeViewGet:
    """GET /authorize — consent page rendering and validation rejections."""

    @pytest.fixture
    def setup(self, tmp_path):
        oauth, provider = _provider_for_view_tests(tmp_path)
        view = oauth.AuthorizeView(provider)
        return oauth, provider, view

    @staticmethod
    def _good_query(**overrides):
        # 43-char base64url challenge (the SHA-256(verifier) shape)
        challenge = "X" * 43
        q = {
            "response_type": "code",
            "client_id": "client-id-1234567890ABCDEF",
            "redirect_uri": "https://claude.ai/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "abc123",
        }
        q.update(overrides)
        return q

    async def test_rejects_response_type_other_than_code(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(query=self._good_query(response_type="token"))
        resp = await view.get(request)
        assert resp.status == 400
        assert "unsupported_response_type" in resp.text

    async def test_rejects_plain_code_challenge_method(self, setup):
        """RFC 7636: only S256 is accepted; `plain` is forbidden because it
        downgrades the PKCE protection."""
        oauth, provider, view = setup
        request = _make_view_request(
            query=self._good_query(code_challenge_method="plain")
        )
        resp = await view.get(request)
        assert resp.status == 400
        assert "S256" in resp.text

    async def test_rejects_short_code_challenge(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(query=self._good_query(code_challenge="too-short"))
        resp = await view.get(request)
        assert resp.status == 400
        assert "code_challenge" in resp.text

    async def test_rejects_unknown_client_id(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(
            query=self._good_query(client_id="not-the-configured-id")
        )
        resp = await view.get(request)
        assert resp.status == 400
        assert "client_id" in resp.text

    async def test_rejects_http_redirect_uri(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(
            query=self._good_query(redirect_uri="http://claude.ai/cb")
        )
        resp = await view.get(request)
        assert resp.status == 400
        assert "redirect_uri" in resp.text

    async def test_rejects_redirect_uri_with_fragment(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(
            query=self._good_query(redirect_uri="https://claude.ai/cb#frag")
        )
        resp = await view.get(request)
        assert resp.status == 400
        assert "redirect_uri" in resp.text

    async def test_rejects_redirect_uri_without_host(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(
            query=self._good_query(redirect_uri="https:///nohost")
        )
        resp = await view.get(request)
        assert resp.status == 400

    async def test_renders_consent_page_on_valid_request(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(query=self._good_query())
        with patch.object(oauth.web, "Response") as resp_ctor:
            await view.get(request)

        kwargs = resp_ctor.call_args.kwargs
        assert kwargs.get("content_type") == "text/html"
        html = kwargs.get("text", "")
        assert "Authorize MCP Webhook Proxy" in html
        # Redirect URI is shown to the user so they can verify destination
        assert "https://claude.ai/cb" in html
        # Hidden form fields needed for POST round-trip
        assert 'name="client_id"' in html
        assert 'name="redirect_uri"' in html
        assert 'name="state"' in html
        assert 'name="code_challenge"' in html
        # Form posts back to the same root /authorize URL
        assert 'action="/authorize"' in html

    async def test_escapes_redirect_uri_in_consent_page(self, setup):
        """A malicious actor can put `<script>` in their redirect_uri to
        try to XSS the consent page. The page must HTML-escape it."""
        oauth, provider, view = setup
        evil = "https://evil.example/?<script>alert(1)</script>"
        request = _make_view_request(query=self._good_query(redirect_uri=evil))
        with patch.object(oauth.web, "Response") as resp_ctor:
            await view.get(request)
        html = resp_ctor.call_args.kwargs.get("text", "")
        assert "<script>alert(1)</script>" not in html
        # Escaped form should be present
        assert "&lt;script&gt;" in html


class TestRestartHintOnErrors:
    """Issue #1694: the 'fully restart Home Assistant' hint is appended ONLY to
    the stale-OAuth-registration errors (invalid_client / invalid client_id) a
    restart actually unsticks — it is opt-in per call, not on every error."""

    def test_text_error_hint_is_opt_in(self, tmp_path):
        oauth = _import_oauth(tmp_path)
        # Default: no hint (a client-side request mistake).
        with patch.object(oauth.web, "Response") as resp_ctor:
            oauth._text_error(400, "unsupported_response_type")
        assert "restart Home Assistant" not in resp_ctor.call_args.kwargs.get(
            "text", ""
        )
        # Opt-in: the stale-registration case carries the hint.
        with patch.object(oauth.web, "Response") as resp_ctor:
            oauth._text_error(400, "invalid client_id", restart_hint=True)
        text = resp_ctor.call_args.kwargs.get("text", "")
        assert "invalid client_id" in text
        assert "restart Home Assistant" in text

    def test_json_error_hint_is_opt_in(self, tmp_path):
        oauth = _import_oauth(tmp_path)
        # Default: client-side protocol error, no error_description hint.
        with patch.object(oauth.web, "json_response") as jr:
            oauth._json_error("invalid_grant", 400)
        assert "error_description" not in jr.call_args.args[0]
        # Opt-in: invalid_client carries the hint.
        with patch.object(oauth.web, "json_response") as jr:
            oauth._json_error("invalid_client", 401, restart_hint=True)
        payload = jr.call_args.args[0]
        assert payload["error"] == "invalid_client"
        assert "restart Home Assistant" in payload["error_description"]

    async def test_invalid_client_id_browser_message_has_hint(self, tmp_path):
        """The user's exact case: a wrong client_id at /authorize produces a
        browser 400 whose text tells them to fully restart HA."""
        oauth, provider = _provider_for_view_tests(tmp_path)
        view = oauth.AuthorizeView(provider)
        request = _make_view_request(
            query={
                "response_type": "code",
                "client_id": "not-the-configured-id",
                "redirect_uri": "https://claude.ai/cb",
                "code_challenge": "X" * 43,
                "code_challenge_method": "S256",
                "state": "s",
            }
        )
        with patch.object(oauth.web, "Response") as resp_ctor:
            await view.get(request)
        text = resp_ctor.call_args.kwargs.get("text", "")
        assert "invalid client_id" in text
        assert "restart Home Assistant" in text


class TestShutdownAndWebhookErrors:
    """Issue #1694 follow-ups: the SIGTERM/SIGINT → cleanup contract (the
    headline shutdown fix) and the restart hint on the webhook handler's
    502/500 responses."""

    def test_initial_tail_offset(self, tmp_path):
        start = _import_start()
        log_file = tmp_path / "inbound.log"
        with patch.object(start, "INBOUND_LOG_FILE", log_file):
            assert start._initial_tail_offset() == 0  # no file
            log_file.write_bytes(b"hello\n")
            assert start._initial_tail_offset() == 6  # end of existing file

    def test_install_shutdown_handlers_records_reason_and_raises(self):
        import signal

        start = _import_start()
        old = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
        try:
            reason = start._install_shutdown_handlers()
            assert reason == {"reason": None}
            handler = signal.getsignal(signal.SIGTERM)
            with pytest.raises(KeyboardInterrupt):
                handler(signal.SIGTERM, None)
            assert reason["reason"] == "SIGTERM"
        finally:
            signal.signal(signal.SIGTERM, old[0])
            signal.signal(signal.SIGINT, old[1])

    def test_shutdown_cleanup_resets_handlers_unlinks_and_logs(self, tmp_path):
        import signal

        start = _import_start()
        log_file = tmp_path / "inbound.log"
        log_file.write_text("x\n")
        old = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
        logs: list[str] = []
        try:
            with (
                patch.object(start, "INBOUND_LOG_FILE", log_file),
                patch.object(start, "_remove_config_entry") as rm,
                patch.object(start, "log_info", side_effect=logs.append),
            ):
                start._shutdown_cleanup("SIGTERM")
            rm.assert_called_once()
            assert not log_file.exists()  # mirror file dropped
            assert any("reason: SIGTERM" in m for m in logs)
            # Handlers restored to default so a second signal can't abort cleanup.
            assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
        finally:
            signal.signal(signal.SIGTERM, old[0])
            signal.signal(signal.SIGINT, old[1])

    async def test_webhook_502_has_no_restart_hint(self):
        mod = _import_mcp_proxy()
        hass = MagicMock()
        hass.data = {
            mod.DOMAIN: {
                "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "webhook_id": "mcp_test_webhook_id_12345",
                "session": MagicMock(),
                "oauth": None,
            }
        }
        request = MagicMock()
        request.headers = {}
        request.read = AsyncMock(return_value=b"")
        request.method = "POST"
        hass.data[mod.DOMAIN]["session"].request = MagicMock(
            side_effect=mod.aiohttp.ClientError("down")
        )
        with patch.object(mod.web, "Response") as resp_ctor:
            await mod._handle_webhook(hass, "mcp_test_webhook_id_12345", request)
        text = resp_ctor.call_args.kwargs.get("text", "")
        assert "upstream unavailable" in text
        # Scoped out: a restart won't fix a downed upstream MCP server.
        assert "restart Home Assistant" not in text

    async def test_webhook_500_has_no_restart_hint(self):
        mod = _import_mcp_proxy()
        hass = MagicMock()
        hass.data = {
            mod.DOMAIN: {
                "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "webhook_id": "mcp_test_webhook_id_12345",
                "session": MagicMock(),
                "oauth": None,
            }
        }
        request = MagicMock()
        request.headers = {}
        request.read = AsyncMock(return_value=b"")
        request.method = "POST"
        hass.data[mod.DOMAIN]["session"].request = MagicMock(
            side_effect=RuntimeError("boom")
        )
        with patch.object(mod.web, "Response") as resp_ctor:
            await mod._handle_webhook(hass, "mcp_test_webhook_id_12345", request)
        text = resp_ctor.call_args.kwargs.get("text", "")
        assert "internal error" in text
        # Scoped out: a restart won't fix a proxy bug.
        assert "restart Home Assistant" not in text


class TestAuthorizeViewPost:
    @pytest.fixture
    def setup(self, tmp_path):
        oauth, provider = _provider_for_view_tests(tmp_path)
        view = oauth.AuthorizeView(provider)
        return oauth, provider, view

    @staticmethod
    def _good_form(action="approve", **overrides):
        f = {
            "action": action,
            "client_id": "client-id-1234567890ABCDEF",
            "redirect_uri": "https://claude.ai/cb",
            "code_challenge": "X" * 43,
            "state": "abc123",
        }
        f.update(overrides)
        return f

    async def test_deny_redirects_with_error_and_state(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(
            method="POST", post_data=self._good_form(action="deny")
        )
        resp = await view.post(request)
        assert resp.status == 302
        loc = resp.headers["Location"]
        assert loc.startswith("https://claude.ai/cb")
        assert "error=access_denied" in loc
        assert "state=abc123" in loc

    async def test_approve_issues_code_and_redirects(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(method="POST", post_data=self._good_form())
        resp = await view.post(request)
        assert resp.status == 302
        loc = resp.headers["Location"]
        assert loc.startswith("https://claude.ai/cb")
        assert "code=" in loc
        assert "state=abc123" in loc

    async def test_post_re_validates_hidden_client_id(self, setup):
        """POST must not trust hidden form fields — re-validate them."""
        oauth, provider, view = setup
        request = _make_view_request(
            method="POST",
            post_data=self._good_form(client_id="attacker-substituted-id"),
        )
        resp = await view.post(request)
        assert resp.status == 400
        assert "client_id" in resp.text

    async def test_post_re_validates_hidden_redirect_uri(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(
            method="POST",
            post_data=self._good_form(redirect_uri="http://evil.example/"),
        )
        resp = await view.post(request)
        assert resp.status == 400

    async def test_unknown_action_returns_400(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(
            method="POST", post_data=self._good_form(action="hijack")
        )
        resp = await view.post(request)
        assert resp.status == 400


class TestTokenView:
    @pytest.fixture
    def setup(self, tmp_path):
        oauth, provider = _provider_for_view_tests(tmp_path)
        view = oauth.TokenView(provider)
        return oauth, provider, view

    @staticmethod
    def _basic_header(client_id, client_secret):
        import base64 as _b64

        token = _b64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
        return f"Basic {token}"

    async def test_invalid_client_via_basic_returns_401(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(
            method="POST",
            headers={"Authorization": self._basic_header("wrong", "pw")},
            post_data={"grant_type": "authorization_code"},
        )

        with patch.object(oauth.web, "json_response") as resp_ctor:
            await view.post(request)

        kwargs = resp_ctor.call_args.kwargs
        body = resp_ctor.call_args.args[0]
        assert body["error"] == "invalid_client"
        # Stale-registration case keeps the restart hint.
        assert "restart Home Assistant" in body["error_description"]
        assert kwargs.get("status") == 401
        assert (
            kwargs.get("headers", {}).get("WWW-Authenticate")
            == 'Basic realm="MCP Proxy OAuth"'
        )

    async def test_invalid_client_via_form_returns_401(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(
            method="POST",
            post_data={
                "grant_type": "authorization_code",
                "client_id": "wrong",
                "client_secret": "pw",
            },
        )
        with patch.object(oauth.web, "json_response") as resp_ctor:
            await view.post(request)
        body = resp_ctor.call_args.args[0]
        assert body["error"] == "invalid_client"

    async def test_unsupported_grant_type(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(
            method="POST",
            headers={
                "Authorization": self._basic_header(
                    "client-id-1234567890ABCDEF", "client-secret-very-secret"
                )
            },
            post_data={"grant_type": "client_credentials"},
        )
        with patch.object(oauth.web, "json_response") as resp_ctor:
            await view.post(request)
        body = resp_ctor.call_args.args[0]
        kwargs = resp_ctor.call_args.kwargs
        assert body["error"] == "unsupported_grant_type"
        # Client-side protocol error: no restart hint.
        assert "error_description" not in body
        assert kwargs.get("status") == 400

    async def test_authorization_code_missing_fields(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(
            method="POST",
            headers={
                "Authorization": self._basic_header(
                    "client-id-1234567890ABCDEF", "client-secret-very-secret"
                )
            },
            post_data={"grant_type": "authorization_code"},
        )
        with patch.object(oauth.web, "json_response") as resp_ctor:
            await view.post(request)
        body = resp_ctor.call_args.args[0]
        assert body["error"] == "invalid_request"

    async def test_authorization_code_full_round_trip(self, setup):
        oauth, provider, view = setup
        verifier = "abcdefghij" * 5  # 50 chars, passes RFC 7636 length check
        import hashlib

        challenge = oauth._b64url_encode(hashlib.sha256(verifier.encode()).digest())
        code = provider.issue_code("https://claude.ai/cb", challenge)

        request = _make_view_request(
            method="POST",
            headers={
                "Authorization": self._basic_header(
                    "client-id-1234567890ABCDEF", "client-secret-very-secret"
                )
            },
            post_data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://claude.ai/cb",
                "code_verifier": verifier,
            },
        )
        with patch.object(oauth.web, "json_response") as resp_ctor:
            await view.post(request)

        body = resp_ctor.call_args.args[0]
        assert body["token_type"] == "Bearer"
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["expires_in"] == oauth.ACCESS_TOKEN_TTL
        # Issued tokens validate against the provider
        assert provider.validate_access_token(body["access_token"]) is True
        assert provider.validate_refresh_token(body["refresh_token"]) is True

    async def test_refresh_token_round_trip(self, setup):
        oauth, provider, view = setup
        refresh = provider.issue_refresh_token()

        request = _make_view_request(
            method="POST",
            headers={
                "Authorization": self._basic_header(
                    "client-id-1234567890ABCDEF", "client-secret-very-secret"
                )
            },
            post_data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
            },
        )
        with patch.object(oauth.web, "json_response") as resp_ctor:
            await view.post(request)

        body = resp_ctor.call_args.args[0]
        assert "access_token" in body
        assert "refresh_token" in body
        assert provider.validate_access_token(body["access_token"]) is True

    async def test_refresh_token_invalid_returns_invalid_grant(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(
            method="POST",
            headers={
                "Authorization": self._basic_header(
                    "client-id-1234567890ABCDEF", "client-secret-very-secret"
                )
            },
            post_data={
                "grant_type": "refresh_token",
                "refresh_token": "garbage.token",
            },
        )
        with patch.object(oauth.web, "json_response") as resp_ctor:
            await view.post(request)
        body = resp_ctor.call_args.args[0]
        assert body["error"] == "invalid_grant"


class TestPkceLengthEnforcement:
    """RFC 7636 specifies code_verifier 43-128 chars from a restricted
    charset. The provider must reject deviations rather than silently
    hashing junk."""

    @pytest.fixture
    def provider(self, tmp_path):
        _, p = _provider_for_view_tests(tmp_path)
        return p

    def test_rejects_short_verifier(self, provider):
        # Issue a code, then try to consume with a too-short verifier
        challenge = "X" * 43
        code = provider.issue_code("https://claude.ai/cb", challenge)
        assert provider.consume_code(code, "https://claude.ai/cb", "tooshort") is False

    def test_rejects_long_verifier(self, provider):
        challenge = "X" * 43
        code = provider.issue_code("https://claude.ai/cb", challenge)
        assert provider.consume_code(code, "https://claude.ai/cb", "X" * 129) is False

    def test_rejects_verifier_with_disallowed_chars(self, provider):
        challenge = "X" * 43
        code = provider.issue_code("https://claude.ai/cb", challenge)
        bad_verifier = "a" * 42 + " "  # 43 chars but space is not in unreserved set
        assert (
            provider.consume_code(code, "https://claude.ai/cb", bad_verifier) is False
        )


class TestPendingCodeCap:
    """The pending-code dict must bound under abuse — an attacker spamming
    /authorize without consuming should not exhaust memory."""

    @pytest.fixture
    def provider(self, tmp_path):
        _, p = _provider_for_view_tests(tmp_path)
        return p

    def test_issue_code_returns_none_at_cap(self, provider, tmp_path):
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        challenge = "X" * 43
        # Fill to the cap via the public API — none of these expire within the
        # 5-min TTL, so the prune on each issue keeps them and the cap is hit.
        # (Poking the internal dict is avoided so this stays agnostic to whether
        # the flavor stores codes inline or in the shared PKCECodeStore.)
        for _ in range(oauth.MAX_PENDING_CODES):
            assert provider.issue_code("https://claude.ai/cb", challenge) is not None
        assert provider.issue_code("https://claude.ai/cb", challenge) is None


class TestTokenExpiryBoundary:
    """The expiry check is `>=` — exact-time tokens must still be accepted
    (and one second later must be rejected). A typo to `>` would cut every
    token's life by one second; `<=` would accept expired tokens."""

    @pytest.fixture
    def provider(self, tmp_path):
        _, p = _provider_for_view_tests(tmp_path)
        return p

    def test_token_one_second_before_expiry_valid(self, provider, tmp_path):
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        token = provider.issue_access_token()
        # Decode the exp field
        body, _ = token.rsplit(".", 1)
        payload = json.loads(oauth._b64url_decode(body))
        exp = payload["exp"]
        with patch.object(oauth.time, "time", return_value=exp - 1):
            assert provider.validate_access_token(token) is True

    def test_token_at_exact_expiry_invalid(self, provider, tmp_path):
        """At now == exp, the token has just expired (RFC 7519 convention
        used by mainstream JWT implementations: valid iff now < exp)."""
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        token = provider.issue_access_token()
        body, _ = token.rsplit(".", 1)
        payload = json.loads(oauth._b64url_decode(body))
        exp = payload["exp"]
        with patch.object(oauth.time, "time", return_value=exp):
            assert provider.validate_access_token(token) is False

    def test_token_one_second_past_expiry_invalid(self, provider, tmp_path):
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        token = provider.issue_access_token()
        body, _ = token.rsplit(".", 1)
        payload = json.loads(oauth._b64url_decode(body))
        exp = payload["exp"]
        with patch.object(oauth.time, "time", return_value=exp + 1):
            assert provider.validate_access_token(token) is False


class TestRedirectUriValidation:
    """`_is_valid_redirect_uri` is the spec-floor check on redirect URIs."""

    @pytest.fixture
    def oauth(self, tmp_path):
        return _import_oauth(tmp_secret_dir=tmp_path)

    @pytest.mark.parametrize(
        "uri,expected",
        [
            ("https://claude.ai/callback", True),
            ("https://example.com:8443/cb", True),
            ("http://claude.ai/cb", False),
            ("ftp://claude.ai/cb", False),
            ("javascript:alert(1)", False),
            ("https:///nohost", False),
            ("", False),
            ("https://claude.ai/cb#fragment", False),
            ("not-a-url", False),
        ],
    )
    def test_validates_redirect_uri(self, oauth, uri, expected):
        assert oauth._is_valid_redirect_uri(uri) is expected


class TestOAuthProviderConstructorValidation:
    """OAuthProvider's __init__ enforces invariants (length, non-empty)
    so misuse from a future caller fails fast rather than silently
    breaking auth checks downstream."""

    @pytest.fixture
    def oauth(self, tmp_path):
        return _import_oauth(tmp_secret_dir=tmp_path)

    def test_rejects_blank_client_id(self, oauth):
        with pytest.raises(ValueError, match="client_id"):
            oauth.OAuthProvider(
                hass=MagicMock(),
                client_id="",
                client_secret="secret",
                webhook_id="wh",
                signing_key=b"\x00" * 32,
            )

    def test_rejects_short_client_id(self, oauth):
        with pytest.raises(ValueError, match="client_id"):
            oauth.OAuthProvider(
                hass=MagicMock(),
                client_id="too-short",
                client_secret="secret",
                webhook_id="wh",
                signing_key=b"\x00" * 32,
            )

    def test_rejects_blank_client_secret(self, oauth):
        with pytest.raises(ValueError, match="client_secret"):
            oauth.OAuthProvider(
                hass=MagicMock(),
                client_id="client-id-1234567890ABCDEF",
                client_secret="",
                webhook_id="wh",
                signing_key=b"\x00" * 32,
            )

    def test_rejects_short_signing_key(self, oauth):
        with pytest.raises(ValueError, match="signing_key"):
            oauth.OAuthProvider(
                hass=MagicMock(),
                client_id="client-id-1234567890ABCDEF",
                client_secret="secret",
                webhook_id="wh",
                signing_key=b"too-short",
            )


class TestRepairsModule:
    """Marker-file lifecycle and issue creation/deletion for the
    "Restart Required" Repair card."""

    @pytest.fixture
    def repairs(self, tmp_path):
        _install_runtime_stubs()
        repairs_path = os.path.join(PROXY_ADDON_DIR, CURRENT["component"], "repairs.py")
        mod_name = f"mcp_proxy_repairs_{CURRENT['key']}"
        sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location(mod_name, repairs_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        # Redirect the marker file into the test's tmp dir
        mod.RESTART_MARKER_FILE = tmp_path / ".mcp_proxy_oauth_restart_required"
        return mod

    def test_marker_present_returns_false_when_file_missing(self, repairs):
        assert repairs.marker_present() is False

    def test_marker_present_returns_true_when_file_exists(self, repairs):
        repairs.RESTART_MARKER_FILE.write_text('{"reason": "test"}')
        assert repairs.marker_present() is True

    def test_clear_marker_removes_file(self, repairs):
        repairs.RESTART_MARKER_FILE.write_text("test")
        repairs._clear_marker()
        assert not repairs.RESTART_MARKER_FILE.exists()

    def test_clear_marker_idempotent_when_missing(self, repairs):
        # Should not raise
        repairs._clear_marker()
        assert not repairs.RESTART_MARKER_FILE.exists()

    def test_maybe_create_issue_no_op_when_marker_missing(self, repairs):
        from homeassistant.helpers import issue_registry

        hass = MagicMock()
        repairs.maybe_create_issue(hass, CURRENT["domain"])
        issue_registry.async_create_issue.assert_not_called()

    def test_maybe_create_issue_fires_when_marker_present(self, repairs):
        from homeassistant.helpers import issue_registry

        issue_registry.async_create_issue.reset_mock()
        repairs.RESTART_MARKER_FILE.write_text("test")
        hass = MagicMock()
        repairs.maybe_create_issue(hass, CURRENT["domain"])
        issue_registry.async_create_issue.assert_called_once()
        # Domain + issue_id are positional args after hass
        call_args = issue_registry.async_create_issue.call_args
        assert call_args.args[1] == CURRENT["domain"]
        assert call_args.args[2] == "oauth_restart_required"
        assert call_args.kwargs.get("is_fixable") is True

    def test_clear_issue_deletes_marker_and_calls_delete_issue(self, repairs):
        from homeassistant.helpers import issue_registry

        issue_registry.async_delete_issue.reset_mock()
        repairs.RESTART_MARKER_FILE.write_text("test")
        hass = MagicMock()
        repairs.clear_issue(hass, CURRENT["domain"])
        assert not repairs.RESTART_MARKER_FILE.exists()
        issue_registry.async_delete_issue.assert_called_once_with(
            hass, CURRENT["domain"], "oauth_restart_required"
        )


class TestRepairsFlowSubmit:
    """The repair card's Submit button must call homeassistant.restart with
    blocking=True (so a failed config check surfaces as a flow error instead of
    being swallowed) and must NOT clear the marker — if the restart aborts, the
    marker has to survive so the Repair persists. A wrong service name would
    leave the user stuck on the card; a premature marker-clear would drop the
    Repair on an aborted restart."""

    @pytest.fixture
    def repairs(self, tmp_path):
        _install_runtime_stubs()
        repairs_path = os.path.join(PROXY_ADDON_DIR, CURRENT["component"], "repairs.py")
        mod_name = f"mcp_proxy_repairs_{CURRENT['key']}"
        sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location(mod_name, repairs_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        mod.RESTART_MARKER_FILE = tmp_path / ".mcp_proxy_oauth_restart_required"
        return mod

    async def test_confirm_with_input_calls_restart_and_leaves_marker(self, repairs):
        repairs.RESTART_MARKER_FILE.write_text("test")
        flow = repairs.OAuthRestartRepairFlow()
        hass = MagicMock()
        hass.services.async_call = AsyncMock()
        flow.hass = hass
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})

        await flow.async_step_confirm({})

        # The fix flow must NOT clear the marker: if the restart aborts it has
        # to survive so the Repair persists; a successful restart's boot-time
        # setup clears it once OAuth is actually live.
        assert repairs.RESTART_MARKER_FILE.exists()
        hass.services.async_call.assert_awaited_once_with(
            "homeassistant", "restart", {}, blocking=True
        )

    async def test_confirm_without_input_shows_form(self, repairs):
        flow = repairs.OAuthRestartRepairFlow()
        flow.async_show_form = MagicMock(return_value={"type": "form"})
        await flow.async_step_confirm(None)
        flow.async_show_form.assert_called_once()
        assert flow.async_show_form.call_args.kwargs["step_id"] == "confirm"


class TestStringsJSONIssueKeys:
    """The Repair card's translation keys in mcp_proxy/strings.json must
    match the issue ID and step ID used in repairs.py. A drift between
    the two would render the card with raw key strings instead of
    localized text."""

    def test_issue_translation_keys_match_repair_flow(self):
        with open(f"{PROXY_ADDON_DIR}/{CURRENT['component']}/strings.json") as f:
            strings = json.load(f)

        assert "oauth_restart_required" in strings.get("issues", {})
        issue = strings["issues"]["oauth_restart_required"]
        assert issue.get("title")

        confirm = issue.get("fix_flow", {}).get("step", {}).get("confirm", {})
        assert confirm.get("title")
        assert confirm.get("description")


class TestAsyncSetupBootMarker:
    """async_setup runs on every HA boot. When the restart marker is present
    (left by the addon's fail-closed gate OR a prior mid-session OAuth enable),
    it must surface the Repair via maybe_create_issue; with no marker it must
    create no issue."""

    @pytest.fixture
    def hass(self):
        h = MagicMock()
        h.data = {}

        async def fake_executor(func, *args):
            return func(*args)

        h.async_add_executor_job = AsyncMock(side_effect=fake_executor)
        return h

    async def test_marker_present_creates_issue(self, hass, tmp_path):
        mod = _import_mcp_proxy()
        repairs = _bind_repairs(mod, tmp_path)
        repairs.RESTART_MARKER_FILE.write_text('{"reason": "oauth_enabled"}')
        from homeassistant.helpers import issue_registry

        issue_registry.async_create_issue.reset_mock()
        # No DOMAIN key in config → skip the YAML-migration branch.
        result = await mod.async_setup(hass, {})

        assert result is True
        issue_registry.async_create_issue.assert_called_once()
        call_args = issue_registry.async_create_issue.call_args
        assert call_args.args[1] == mod.DOMAIN
        assert call_args.args[2] == "oauth_restart_required"
        assert call_args.kwargs.get("is_fixable") is True

    async def test_marker_absent_creates_no_issue(self, hass, tmp_path):
        mod = _import_mcp_proxy()
        _bind_repairs(mod, tmp_path)  # marker redirected to tmp, never written
        from homeassistant.helpers import issue_registry

        issue_registry.async_create_issue.reset_mock()
        result = await mod.async_setup(hass, {})

        assert result is True
        issue_registry.async_create_issue.assert_not_called()


class TestRefreshRepairsService:
    """The add-on-invocable `refresh_repairs` service — the only way a Repair
    card can appear at the MOMENT a restart becomes necessary (only in-process
    code can file repair issues; without the service the card appeared only at
    the next boot, i.e. after the restart it was meant to prompt). Skips on
    flavors that don't ship the service yet."""

    @pytest.fixture
    def hass(self):
        h = MagicMock()
        h.data = {}

        async def fake_executor(func, *args):
            return func(*args)

        h.async_add_executor_job = AsyncMock(side_effect=fake_executor)
        return h

    @staticmethod
    def _skip_unless_shipped(mod):
        if not hasattr(mod, "SERVICE_REFRESH_REPAIRS"):
            pytest.skip("flavor does not ship the refresh_repairs service yet")

    async def test_service_registered_on_setup(self, hass, tmp_path):
        mod = _import_mcp_proxy()
        self._skip_unless_shipped(mod)
        _bind_repairs(mod, tmp_path)
        await mod.async_setup(hass, {})
        hass.services.async_register.assert_called_once()
        args = hass.services.async_register.call_args
        assert args.args[0] == mod.DOMAIN
        assert args.args[1] == "refresh_repairs"

    async def test_setup_dismisses_stale_update_notification(self, hass, tmp_path):
        mod = _import_mcp_proxy()
        self._skip_unless_shipped(mod)
        _bind_repairs(mod, tmp_path)
        await mod.async_setup(hass, {})
        hass.services.async_call.assert_called_once_with(
            "persistent_notification",
            "dismiss",
            {"notification_id": mod.UPDATE_NOTIFICATION_ID},
        )

    async def test_update_create_files_issue_immediately(self, hass, tmp_path):
        mod = _import_mcp_proxy()
        self._skip_unless_shipped(mod)
        _bind_repairs(mod, tmp_path)
        from homeassistant.helpers import issue_registry

        issue_registry.async_create_issue.reset_mock()
        handler = mod._make_refresh_repairs_handler(hass)
        call = MagicMock()
        call.data = {"issue_id": "update_restart_required", "action": "create"}
        await handler(call)
        issue_registry.async_create_issue.assert_called_once()
        args = issue_registry.async_create_issue.call_args
        assert args.args[1] == mod.DOMAIN
        assert args.args[2] == "update_restart_required"
        assert args.kwargs.get("is_fixable") is True
        assert args.kwargs.get("translation_key") == "update_restart_required"

    async def test_oauth_create_is_marker_gated(self, hass, tmp_path):
        mod = _import_mcp_proxy()
        self._skip_unless_shipped(mod)
        repairs = _bind_repairs(mod, tmp_path)
        from homeassistant.helpers import issue_registry

        issue_registry.async_create_issue.reset_mock()
        handler = mod._make_refresh_repairs_handler(hass)
        call = MagicMock()
        call.data = {"issue_id": "oauth_restart_required", "action": "create"}
        # No marker on disk → the add-on didn't ask for OAuth enforcement;
        # the service must NOT file the issue.
        await handler(call)
        issue_registry.async_create_issue.assert_not_called()
        # Marker present → file it.
        repairs.RESTART_MARKER_FILE.write_text('{"reason": "stale_integration_code"}')
        await handler(call)
        issue_registry.async_create_issue.assert_called_once()
        assert (
            issue_registry.async_create_issue.call_args.args[2]
            == "oauth_restart_required"
        )

    async def test_clear_action_deletes_issue(self, hass, tmp_path):
        mod = _import_mcp_proxy()
        self._skip_unless_shipped(mod)
        _bind_repairs(mod, tmp_path)
        from homeassistant.helpers import issue_registry

        issue_registry.async_delete_issue.reset_mock()
        handler = mod._make_refresh_repairs_handler(hass)
        call = MagicMock()
        call.data = {"issue_id": "update_restart_required", "action": "clear"}
        await handler(call)
        issue_registry.async_delete_issue.assert_called_once_with(
            hass, mod.DOMAIN, "update_restart_required"
        )

    def test_issue_ids_in_sync_with_repairs_module(self, tmp_path):
        """__init__'s schema and start.py hardcode the issue-id literals;
        repairs.py owns them. Pin the contract."""
        mod = _import_mcp_proxy()
        self._skip_unless_shipped(mod)
        repairs = _bind_repairs(mod, tmp_path)
        assert repairs.ISSUE_ID == "oauth_restart_required"
        assert repairs.UPDATE_ISSUE_ID == "update_restart_required"

    def test_translations_ship_and_match_strings(self):
        """Custom integrations load runtime translations ONLY from
        translations/<lang>.json — strings.json alone renders raw keys in the
        Repairs UI. The two must exist and stay identical."""
        component_dir = Path(PROXY_ADDON_DIR) / CURRENT["component"]
        translations = component_dir / "translations" / "en.json"
        if not translations.exists():
            pytest.skip("flavor does not ship runtime translations yet")
        strings = json.loads((component_dir / "strings.json").read_text())
        en = json.loads(translations.read_text())
        assert en == strings
        assert set(en["issues"]) == {
            "oauth_restart_required",
            "update_restart_required",
        }


class TestRequestRestartRepair:
    """start.py's best-effort bridge to the integration's refresh_repairs
    service. Skips on flavors that don't ship it yet."""

    def test_posts_refresh_repairs_service_call(self):
        start = _import_start()
        if not hasattr(start, "_request_restart_repair"):
            pytest.skip("flavor does not ship _request_restart_repair yet")
        with patch.object(start, "_ha_core_api") as api:
            start._request_restart_repair("update_restart_required")
        api.assert_called_once_with(
            "POST",
            f"/services/{CURRENT['domain']}/refresh_repairs",
            {"issue_id": "update_restart_required", "action": "create"},
        )


class TestProbeOAuthActive:
    """The OAuth probe is the load-bearing detector for the
    "stale-code, fail-closed" path: if the integration code currently
    in HA's Python module cache doesn't enforce OAuth, the probe must
    return False so start.py can disable the webhook before unauth'd
    requests get through."""

    @pytest.fixture
    def start(self):
        return _import_start()

    def test_probe_returns_true_when_metadata_endpoint_returns_json(self, start):
        with (
            patch.object(start, "_read_integration_domain", return_value="mcp_proxy"),
            patch.object(
                start,
                "_ha_core_api",
                return_value={
                    "resource": "https://h/api/webhook/x",
                    "authorization_servers": ["https://h/api/mcp_proxy/oauth"],
                },
            ),
        ):
            assert start._probe_oauth_active() is True

    def test_probe_returns_false_when_endpoint_404s(self, start):
        # _ha_core_api returns None on HTTPError — that's the 404 case. Because
        # this gates a destructive teardown, the probe retries (bounded) before
        # giving up; time.sleep is neutralized so the retry backoff doesn't wait.
        sleep = MagicMock()
        with (
            patch.object(start, "_read_integration_domain", return_value="mcp_proxy"),
            patch.object(start, "_ha_core_api", return_value=None),
            patch.object(start.time, "sleep", sleep),
        ):
            assert start._probe_oauth_active() is False
        # 3 attempts, sleeping only between them (not after the last).
        assert sleep.call_count == 2

    def test_probe_returns_false_when_response_is_not_json_dict(self, start):
        sleep = MagicMock()
        with (
            patch.object(start, "_read_integration_domain", return_value="mcp_proxy"),
            patch.object(start, "_ha_core_api", return_value="not a dict"),
            patch.object(start.time, "sleep", sleep),
        ):
            assert start._probe_oauth_active() is False
        assert sleep.call_count == 2

    def test_probe_returns_false_when_authorization_servers_missing(self, start):
        # A different endpoint accidentally exists at the same URL
        sleep = MagicMock()
        with (
            patch.object(start, "_read_integration_domain", return_value="mcp_proxy"),
            patch.object(start, "_ha_core_api", return_value={"unrelated": "data"}),
            patch.object(start.time, "sleep", sleep),
        ):
            assert start._probe_oauth_active() is False
        assert sleep.call_count == 2

    def test_probe_returns_false_when_manifest_unreadable(self, start):
        sleep = MagicMock()
        with (
            patch.object(start, "_read_integration_domain", return_value=None),
            patch.object(start.time, "sleep", sleep),
        ):
            assert start._probe_oauth_active() is False
        assert sleep.call_count == 2

    def test_probe_returns_true_on_transient_then_active(self, start):
        """A single transient failure (None) must NOT trigger the destructive
        path: the probe retries and returns True once the endpoint reports
        active on a later attempt."""
        sleep = MagicMock()
        results = [None, {"authorization_servers": ["https://h/oauth"]}]
        with (
            patch.object(start, "_read_integration_domain", return_value="mcp_proxy"),
            patch.object(start, "_ha_core_api", side_effect=results),
            patch.object(start.time, "sleep", sleep),
        ):
            assert start._probe_oauth_active() is True
        # Slept once between the transient failure and the successful retry.
        assert sleep.call_count == 1

    def test_probe_uses_manifest_domain_for_url(self, start):
        """The probe URL is derived from the source manifest's domain so
        it works for both the prod variant (mcp_proxy) and the fork-dev
        variant (mcp_proxy_dev) without any code change."""
        captured = {}

        def fake_api(method, path):
            captured["method"] = method
            captured["path"] = path
            return {"authorization_servers": []}

        with (
            patch.object(
                start, "_read_integration_domain", return_value="mcp_proxy_dev"
            ),
            patch.object(start, "_ha_core_api", side_effect=fake_api),
        ):
            start._probe_oauth_active()

        assert captured["path"] == "/mcp_proxy_dev/oauth/protected-resource"


class TestOAuthSetupEntryRegistersExpectedViews:
    """Strengthens the existing register_view test: assert the set of
    URLs actually registered, not just the count. Replacing all four
    views with one shared view should fail the test."""

    @pytest.fixture
    def hass(self):
        h = MagicMock()
        h.data = {}
        h.http = MagicMock()
        h.http.register_view = MagicMock()

        async def fake_executor(func, *args):
            return func(*args)

        h.async_add_executor_job = AsyncMock(side_effect=fake_executor)
        return h

    async def test_registers_expected_oauth_endpoints(self, hass, tmp_path):
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        mod = _import_mcp_proxy(preload_oauth=oauth)
        # Boot-time setup (is_running False) exercises the first-registration
        # clear path rather than the mid-session write path (which would touch
        # the real /config marker).
        hass.is_running = False
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test",
            "oauth": {
                "client_id": "client-id-1234567890ABCDEF",
                "client_secret": "secret-much-secret",
            },
        }
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
        ):
            await mod.async_setup_entry(hass, MagicMock())

        registered_urls = {
            call.args[0].url for call in hass.http.register_view.call_args_list
        }
        # Authorize/token live at the root because Claude.ai constructs
        # those URLs from the resource host without consulting the
        # authorization-server metadata document. Flavors that ship the
        # issue-#1714 well-known metadata variants register those too
        # (feature-detected — see _wellknown_oauth_urls).
        expected = {
            f"{CURRENT['oauth_base']}/protected-resource",
            f"{CURRENT['oauth_base']}/authorization-server",
            "/authorize",
            "/token",
        } | _wellknown_oauth_urls(oauth, "mcp_test")
        assert registered_urls == expected


class TestUnauthorizedResponseShape:
    """The 401 response on the webhook is the OAuth-discovery entry point.
    Its WWW-Authenticate must point at the provider's protected-resource
    metadata URL — not just contain the word 'Bearer'."""

    @pytest.fixture
    def setup(self, tmp_path):
        oauth, provider = _provider_for_view_tests(
            tmp_path, public_base_url="https://legit.example"
        )
        return oauth, provider

    def test_resource_metadata_url_uses_pinned_base(self, setup):
        oauth, provider = setup
        request = _make_view_request(headers={"Host": "evil.example"})
        with patch.object(oauth.web, "Response") as resp_ctor:
            oauth.build_unauthorized_response(request, provider)
        kwargs = resp_ctor.call_args.kwargs
        ww = kwargs["headers"]["WWW-Authenticate"]
        # Pinned base means evil.example is NOT in the metadata URL
        assert "evil.example" not in ww
        assert f"https://legit.example{CURRENT['oauth_base']}/protected-resource" in ww


class TestHaAuthMode:
    """The ha_auth OAuth mode: HA core is the authorization server and the
    add-on is a pure resource server (serves the discovery documents + validates
    bearers via hass.auth). Skips on flavors that don't ship auth_native yet."""

    @pytest.fixture(autouse=True)
    def _skip_on_stable(self, _webhook_proxy_variant):
        if not _ha_auth_supported():
            pytest.skip("flavor does not ship ha_auth (auth_native) yet")

    @pytest.fixture
    def hass(self):
        h = MagicMock()
        h.data = {}
        h.http = MagicMock()
        h.http.register_view = MagicMock()

        async def fake_executor(func, *args):
            return func(*args)

        h.async_add_executor_job = AsyncMock(side_effect=fake_executor)
        return h

    @staticmethod
    def _ha_auth_config():
        return {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test",
            "oauth": {"mode": "ha_auth"},
        }

    # ---- constants agree across the three modules ----

    def test_mode_literals_agree(self, tmp_path):
        mod, oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        assert auth_native.HA_AUTH_MODE == "ha_auth"
        assert auth_native.AUTH_V2 is True
        assert oauth.MODE_HA_AUTH == auth_native.HA_AUTH_MODE
        assert oauth.MODE_LEGACY == "legacy"
        assert mod.OAUTH_MODE_HA_AUTH == auth_native.HA_AUTH_MODE
        assert mod.OAUTH_MODE_LEGACY == "legacy"
        # start.py mirrors the same two literals for the config it writes.
        start = _import_start()
        assert start.HA_AUTH_MODE == auth_native.HA_AUTH_MODE
        assert start.LEGACY_MODE == "legacy"
        # The DOMAIN key the discovery views read via _active_oauth_mode
        # (oauth.DOMAIN) must equal the one the handler writes (mod.DOMAIN);
        # a drift would make every view 404 (no live mode ever resolved).
        assert oauth.DOMAIN == mod.DOMAIN

    # ---- config.yaml + translations coverage for oauth_mode (dev-only) ----

    def test_oauth_mode_schema_and_translation(self):
        with open(f"{PROXY_ADDON_DIR}/config.yaml") as f:
            config = yaml.safe_load(f)
        assert config["schema"]["oauth_mode"] == "list(ha_auth|legacy)?"
        # No default in options: (kept hidden + no inherited value on upgrade).
        assert "oauth_mode" not in config["options"]
        with open(f"{PROXY_ADDON_DIR}/translations/en.yaml") as f:
            tr = yaml.safe_load(f)
        entry = tr["configuration"]["oauth_mode"]
        assert entry.get("name"), "oauth_mode needs a translation name"
        assert entry.get("description"), "oauth_mode needs a translation description"
        assert "Beta" in entry["name"]

    # ---- setup registers exactly the 7 metadata views, no root views ----

    async def test_setup_registers_seven_metadata_views_and_marks_mode(
        self, hass, tmp_path
    ):
        mod, oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        repairs = _bind_repairs(mod, tmp_path)
        repairs.RESTART_MARKER_FILE.write_text('{"reason": "stale"}')
        with (
            patch.object(mod, "_read_config", return_value=self._ha_auth_config()),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
            patch.object(repairs, "create_issue") as mock_create_issue,
            patch.object(repairs, "_write_marker") as mock_write,
            patch.object(repairs, "_delete_issue_only") as mock_delete,
        ):
            result = await mod.async_setup_entry(hass, MagicMock())

        assert result is True
        registered = {
            call.args[0].url for call in hass.http.register_view.call_args_list
        }
        expected = {
            f"{CURRENT['oauth_base']}/protected-resource",
            f"{CURRENT['oauth_base']}/authorization-server",
        } | _wellknown_oauth_urls(oauth, "mcp_test")
        assert registered == expected
        assert len(registered) == 7
        # No root /authorize or /token views in ha_auth mode.
        assert "/authorize" not in registered
        assert "/token" not in registered
        # hass.data carries the ResourceServer + the mode marker.
        assert hass.data[mod.DOMAIN]["oauth_mode"] == mod.OAUTH_MODE_HA_AUTH
        assert isinstance(hass.data[mod.DOMAIN]["oauth"], auth_native.ResourceServer)
        # No owner-key / fingerprint bookkeeping (those guard root views).
        assert mod.OAUTH_ROUTE_OWNER_KEY not in hass.data
        assert mod.OAUTH_ROUTE_KEY_FINGERPRINT not in hass.data
        # Restart-Repair marker CLEARED, never created.
        assert not repairs.RESTART_MARKER_FILE.exists()
        mock_delete.assert_called_once_with(hass, mod.DOMAIN)
        mock_create_issue.assert_not_called()
        mock_write.assert_not_called()

    async def test_ha_auth_init_failure_unregisters_and_raises(self, hass, tmp_path):
        """ha_auth mirror of the legacy provider-init-failure teardown: if
        register_metadata_views raises while enabling OAuth, the user opted into
        auth, so refuse to start — raise ConfigEntryError, tear down the webhook
        registered above (no unauthenticated endpoint left live), close the
        session (no leak), and leave DOMAIN out of hass.data."""
        mod, oauth, _an = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        _bind_repairs(mod, tmp_path)
        session = MagicMock()
        session.close = AsyncMock()
        with (
            patch.object(mod, "_read_config", return_value=self._ha_auth_config()),
            patch.object(mod, "async_register"),
            patch.object(mod, "async_unregister") as mock_unreg,
            patch.object(mod.aiohttp, "ClientSession", return_value=session),
            # The integration lazy-imports register_metadata_views at call time
            # (`from .oauth import register_metadata_views` inside
            # async_setup_entry), so patching the oauth module attribute lands.
            patch.object(
                oauth, "register_metadata_views", side_effect=RuntimeError("boom")
            ),
            pytest.raises(_FakeConfigEntryError) as exc_info,
        ):
            await mod.async_setup_entry(hass, MagicMock())

        assert "Failed to enable OAuth" in str(exc_info.value)
        mock_unreg.assert_called_once_with(hass, "mcp_test")
        session.close.assert_awaited_once()
        assert mod.DOMAIN not in hass.data

    async def test_setup_ignores_stray_public_base_url_host_derived(
        self, hass, tmp_path
    ):
        """Fix 4 through async_setup_entry: a hand-edited ha_auth config that
        ALSO carries a stray top-level `public_base_url` (the key legacy writes
        and __init__ reads at line 507) must NOT pin the ResourceServer — ha_auth
        is always host-derived. __init__ constructs the provider with
        public_base_url=None regardless, so the served discovery base follows the
        request Host, never the stray URL."""
        mod, oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        _bind_repairs(mod, tmp_path)
        config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test",
            # Stray hand-edit: a legacy-style pinned URL left behind after a
            # manual switch to ha_auth. The ambiguity guard only rejects
            # client_id/client_secret keys, so this reaches the provider wiring.
            "public_base_url": "https://stray-pinned.example",
            "oauth": {"mode": "ha_auth"},
        }
        with (
            patch.object(mod, "_read_config", return_value=config),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
        ):
            result = await mod.async_setup_entry(hass, MagicMock())
        assert result is True
        rs = hass.data[mod.DOMAIN]["oauth"]
        assert isinstance(rs, auth_native.ResourceServer)
        # Wiring: the stray URL was dropped — the provider is host-derived.
        assert rs._public_base_url is None
        # Behavior: base_url_for follows the request Host, not the stray URL.
        request = _make_view_request(headers={"Host": "host-abc.example"})
        assert rs.base_url_for(request) == "https://host-abc.example"
        # And the served authorization-server document reflects the request host.
        with patch.object(oauth.web, "json_response") as jr:
            await oauth.AuthorizationServerMetadataView(rs).get(request)
        doc = jr.call_args.args[0]
        assert doc["issuer"] == f"https://host-abc.example{oauth.OAUTH_BASE}"
        assert "stray-pinned.example" not in doc["issuer"]
        assert "stray-pinned.example" not in doc["authorization_endpoint"]
        # It genuinely served the ha_auth document (public client + CIMD).
        assert doc["token_endpoint_auth_methods_supported"] == ["none"]

    async def test_reload_does_not_reregister_views(self, hass, tmp_path):
        mod, oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        _bind_repairs(mod, tmp_path)
        with (
            patch.object(mod, "_read_config", return_value=self._ha_auth_config()),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
        ):
            await mod.async_setup_entry(hass, MagicMock())
            first = hass.http.register_view.call_count
            hass.http.register_view.reset_mock()
            # A second setup (config-entry reload) must NOT re-register the views.
            await mod.async_setup_entry(hass, MagicMock())
        assert first == 7
        assert hass.http.register_view.call_count == 0
        # The register-once flag lives in oauth.py (a top-level hass.data key),
        # not on the integration package.
        assert hass.data[oauth._METADATA_VIEWS_REGISTERED_KEY] is True

    async def test_register_once_flag_survives_unload(self, hass, tmp_path):
        """The register-once flag lives at a TOP-LEVEL hass.data key so it
        outlives async_unload_entry's pop(DOMAIN): HA can't drop the seven bound
        views until it restarts, so a setup -> unload -> setup cycle must NOT
        re-register them (which would trip aiohttp's duplicate-view ValueError)."""
        mod, oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        _bind_repairs(mod, tmp_path)
        session = MagicMock()
        session.close = AsyncMock()
        with (
            patch.object(mod, "_read_config", return_value=self._ha_auth_config()),
            patch.object(mod, "async_register"),
            patch.object(mod, "async_unregister"),
            patch.object(mod.aiohttp, "ClientSession", return_value=session),
        ):
            await mod.async_setup_entry(hass, MagicMock())
            assert hass.http.register_view.call_count == 7
            flag_key = oauth._METADATA_VIEWS_REGISTERED_KEY
            assert hass.data[flag_key] is True
            await mod.async_unload_entry(hass, MagicMock())
            # unload pops DOMAIN...
            assert mod.DOMAIN not in hass.data
            # ...but the top-level register-once flag survives it.
            assert hass.data[flag_key] is True
            hass.http.register_view.reset_mock()
            # A fresh setup after the unload reuses the still-bound views.
            await mod.async_setup_entry(hass, MagicMock())
        assert hass.http.register_view.call_count == 0
        assert hass.data[flag_key] is True

    def _legacy_config(self):
        return {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test",
            "oauth": {
                "mode": "legacy",
                "client_id": "client-1234567890ABCDEF",
                "client_secret": "secret-much-secret",
            },
        }

    async def test_ha_auth_then_legacy_does_not_reregister_metadata(
        self, hass, tmp_path
    ):
        """A live ha_auth -> legacy switch reuses the already-bound seven
        metadata views: legacy's register_views() routes them through the same
        flag-guarded registrar, so the second setup registers ONLY the two root
        views, never a duplicate of the seven."""
        mod, oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        _bind_repairs(mod, tmp_path)
        # Boot-time so legacy binds its root views cleanly (no restart Repair).
        hass.is_running = False
        with (
            patch.object(
                mod,
                "_read_config",
                side_effect=[self._ha_auth_config(), self._legacy_config()],
            ),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
        ):
            await mod.async_setup_entry(hass, MagicMock())  # ha_auth: 7 metadata
            assert hass.http.register_view.call_count == 7
            hass.http.register_view.reset_mock()
            await mod.async_setup_entry(hass, MagicMock())  # legacy: only 2 root
        registered = {
            call.args[0].url for call in hass.http.register_view.call_args_list
        }
        assert registered == {"/authorize", "/token"}
        # None of the seven metadata views were registered a second time.
        assert not (registered & _wellknown_oauth_urls(oauth, "mcp_test"))
        assert hass.data[mod.DOMAIN]["oauth_mode"] == mod.OAUTH_MODE_LEGACY

    async def test_legacy_then_ha_auth_does_not_reregister_metadata(
        self, hass, tmp_path
    ):
        """A live legacy -> ha_auth switch: legacy already bound the seven via
        the shared registrar, so the ha_auth setup's register_metadata_views
        no-ops and registers nothing new."""
        mod, oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        _bind_repairs(mod, tmp_path)
        hass.is_running = False
        with (
            patch.object(
                mod,
                "_read_config",
                side_effect=[self._legacy_config(), self._ha_auth_config()],
            ),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
        ):
            await mod.async_setup_entry(hass, MagicMock())  # legacy: 7 + 2 root
            assert hass.http.register_view.call_count == 9
            hass.http.register_view.reset_mock()
            await mod.async_setup_entry(hass, MagicMock())  # ha_auth: reuse all
        assert hass.http.register_view.call_count == 0
        assert hass.data[mod.DOMAIN]["oauth_mode"] == mod.OAUTH_MODE_HA_AUTH

    async def test_unknown_mode_raises(self, hass, tmp_path):
        mod = _import_mcp_proxy()
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test_webhook_id_12345",
            "oauth": {"mode": "bogus"},
        }
        session = MagicMock()
        session.close = AsyncMock()
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod, "async_unregister") as mock_unreg,
            patch.object(mod.aiohttp, "ClientSession", return_value=session),
            pytest.raises(_FakeConfigEntryError) as exc_info,
        ):
            await mod.async_setup_entry(hass, MagicMock())
        assert "Unknown OAuth mode" in str(exc_info.value)
        mock_unreg.assert_called_once_with(hass, "mcp_test_webhook_id_12345")
        session.close.assert_awaited_once()
        assert mod.DOMAIN not in hass.data

    async def test_ambiguous_ha_auth_with_creds_raises(self, hass, tmp_path):
        """Hard mutual exclusion: mode ha_auth + legacy creds keys is ambiguous
        and must be refused, not guessed."""
        mod, _oauth, _an = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test_webhook_id_12345",
            "oauth": {
                "mode": "ha_auth",
                "client_id": "client-1234567890ABCDEF",
                "client_secret": "secret-much-secret",
            },
        }
        session = MagicMock()
        session.close = AsyncMock()
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod, "async_unregister") as mock_unreg,
            patch.object(mod.aiohttp, "ClientSession", return_value=session),
            pytest.raises(_FakeConfigEntryError) as exc_info,
        ):
            await mod.async_setup_entry(hass, MagicMock())
        assert "Ambiguous" in str(exc_info.value)
        mock_unreg.assert_called_once_with(hass, "mcp_test_webhook_id_12345")
        session.close.assert_awaited_once()
        assert mod.DOMAIN not in hass.data
        # The ResourceServer was never constructed and no view registered.
        hass.http.register_view.assert_not_called()

    async def test_creds_without_mode_takes_legacy_path(self, hass, tmp_path):
        """Back-compat pin: a creds-shaped section without a mode key is legacy,
        and the ha_auth module (auth_native) is NEVER imported for it."""
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        mod = _import_mcp_proxy(preload_oauth=oauth)
        hass.is_running = False
        auth_native_name = f"{mod.__name__}.auth_native"
        sys.modules.pop(auth_native_name, None)
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test",
            "oauth": {
                "client_id": "client-1234567890ABCDEF",
                "client_secret": "secret-much-secret",
            },
        }
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
        ):
            await mod.async_setup_entry(hass, MagicMock())
        assert hass.data[mod.DOMAIN]["oauth_mode"] == mod.OAUTH_MODE_LEGACY
        # Legacy constructed its embedded provider + claimed the root routes.
        assert hass.data[mod.OAUTH_ROUTE_OWNER_KEY] == mod.DOMAIN
        assert hasattr(hass.data[mod.DOMAIN]["oauth"], "validate_bearer")
        # ha_auth's auth_native module must NOT be imported on the legacy path.
        assert auth_native_name not in sys.modules

    async def test_off_path_imports_neither_oauth_nor_auth_native(self, hass, tmp_path):
        """The OAuth-off path must never import auth_native (ha_auth stays lazy).
        oauth stays lazy too UNLESS the flavor ships none-mode auto-approve
        (dev, #1969), whose off-path discovery deliberately reuses oauth's
        metadata views + shared PKCE store."""
        mod = _import_mcp_proxy()
        oauth_name = f"{mod.__name__}.oauth"
        an_name = f"{mod.__name__}.auth_native"
        sys.modules.pop(oauth_name, None)
        sys.modules.pop(an_name, None)
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test",
        }
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
        ):
            await mod.async_setup_entry(hass, MagicMock())
        if _none_autoapprove_supported():
            # None-mode auto-approve reuses oauth (metadata views + PKCE store).
            assert oauth_name in sys.modules
        else:
            assert oauth_name not in sys.modules
        # auth_native (ha_auth) is never pulled in on the OFF path.
        assert an_name not in sys.modules

    # ---- documents ----

    def _resource_server(self, tmp_path, mode="ha_auth"):
        mod, oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        hass = MagicMock()
        hass.data = {mod.DOMAIN: {"oauth_mode": mode}}
        rs = auth_native.ResourceServer(
            hass, "mcp_webhook_id_aaaa", public_base_url="https://legit.example"
        )
        return oauth, rs

    async def test_as_document_ha_auth_shape_canonical_and_wellknown(self, tmp_path):
        oauth, rs = self._resource_server(tmp_path)
        request = _make_view_request(headers={"Host": "ignored"})
        with patch.object(oauth.web, "json_response") as jr:
            await oauth.AuthorizationServerMetadataView(rs).get(request)
        doc = jr.call_args.args[0]
        assert doc["issuer"] == f"https://legit.example{oauth.OAUTH_BASE}"
        assert doc["authorization_endpoint"] == "https://legit.example/auth/authorize"
        assert doc["token_endpoint"] == "https://legit.example/auth/token"
        assert doc["response_types_supported"] == ["code"]
        assert doc["grant_types_supported"] == ["authorization_code", "refresh_token"]
        assert doc["code_challenge_methods_supported"] == ["S256"]
        assert doc["token_endpoint_auth_methods_supported"] == ["none"]
        assert doc["client_id_metadata_document_supported"] is True
        assert "registration_endpoint" not in doc
        # A well-known variant serves the SAME document.
        wk = oauth.WellKnownAuthorizationServerMetadataView(
            rs,
            url=f"{oauth.OAUTH_BASE}/.well-known/oauth-authorization-server",
            name="test:wk",
        )
        with patch.object(oauth.web, "json_response") as jr2:
            await wk.get(request)
        assert jr2.call_args.args[0] == doc

    async def test_prm_document_ha_auth_matches_legacy_shape(self, tmp_path):
        oauth, rs = self._resource_server(tmp_path)
        request = _make_view_request(headers={"Host": "ignored"})
        with patch.object(oauth.web, "json_response") as jr:
            await oauth.ProtectedResourceMetadataView(rs).get(request)
        body = jr.call_args.args[0]
        assert body["resource"] == (
            "https://legit.example/api/webhook/mcp_webhook_id_aaaa"
        )
        assert body["authorization_servers"] == [
            f"https://legit.example{oauth.OAUTH_BASE}"
        ]
        assert body["bearer_methods_supported"] == ["header"]
        assert body["resource_documentation"] == (
            "https://github.com/homeassistant-ai/ha-mcp"
        )

    # ---- runtime mutual-exclusion: root views 404 when ha_auth is active ----

    async def test_authorize_and_token_views_404_when_ha_auth_active(self, tmp_path):
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        hass = MagicMock()
        hass.data = {oauth.DOMAIN: {"oauth_mode": oauth.MODE_HA_AUTH}}
        # A legacy provider still bound from a prior legacy session.
        provider = oauth.OAuthProvider(
            hass=hass,
            client_id="cid-1234567890ABCDEF",
            client_secret="sec",
            webhook_id="wh",
            signing_key=b"\x00" * 32,
        )
        av = oauth.AuthorizeView(provider)
        good_query = {
            "response_type": "code",
            "client_id": "cid-1234567890ABCDEF",
            "redirect_uri": "https://claude.ai/cb",
            "code_challenge": "X" * 43,
            "code_challenge_method": "S256",
            "state": "s",
        }
        with patch.object(oauth.web, "Response") as rc:
            await av.get(_make_view_request(query=good_query, method="GET"))
        assert rc.call_args.kwargs.get("status") == 404
        with patch.object(oauth.web, "Response") as rc2:
            await av.post(
                _make_view_request(method="POST", post_data={"action": "approve"})
            )
        assert rc2.call_args.kwargs.get("status") == 404
        tv = oauth.TokenView(provider)
        with patch.object(oauth.web, "json_response") as jr:
            await tv.post(
                _make_view_request(
                    method="POST", post_data={"grant_type": "authorization_code"}
                )
            )
        assert jr.call_args.kwargs.get("status") == 404

    # ---- webhook gate: bearer validated via hass.auth ----

    def _gate_hass(self, mod, auth_native, validate_return):
        hass = MagicMock()
        hass.auth.async_validate_access_token = MagicMock(return_value=validate_return)
        rs = auth_native.ResourceServer(hass, "wh")
        hass.data = {
            mod.DOMAIN: {
                "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "webhook_id": "mcp_test",
                "session": MagicMock(),
                "oauth": rs,
                "oauth_mode": mod.OAUTH_MODE_HA_AUTH,
            }
        }
        return hass

    async def test_webhook_gate_valid_bearer_proxies(self, tmp_path):
        mod, oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        hass = self._gate_hass(mod, auth_native, object())  # non-None -> authorized
        request = MagicMock()
        request.headers = {"Authorization": "Bearer good-token", "Host": "h"}
        request.read = AsyncMock(return_value=b"")
        request.method = "POST"
        request.scheme = "https"
        sentinel = mod.aiohttp.ClientError("stop here")
        hass.data[mod.DOMAIN]["session"].request = MagicMock(side_effect=sentinel)
        await mod._handle_webhook(hass, "mcp_test", request)
        request.read.assert_awaited_once()  # gate passed -> upstream call reached
        hass.auth.async_validate_access_token.assert_called_once_with("good-token")

    async def test_webhook_gate_invalid_bearer_401(self, tmp_path):
        mod, oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        hass = self._gate_hass(mod, auth_native, None)  # None -> unauthorized
        request = MagicMock()
        request.headers = {"Authorization": "Bearer bad", "Host": "example.nabu.casa"}
        request.read = AsyncMock(return_value=b"")
        request.method = "POST"
        request.scheme = "https"
        with patch.object(oauth.web, "Response") as response_ctor:
            await mod._handle_webhook(hass, "mcp_test", request)
        request.read.assert_not_awaited()
        assert response_ctor.call_args.kwargs.get("status") == 401
        ww = response_ctor.call_args.kwargs.get("headers", {}).get(
            "WWW-Authenticate", ""
        )
        assert ww.startswith("Bearer realm=")
        assert "resource_metadata=" in ww
        hass.auth.async_validate_access_token.assert_called_once_with("bad")

    async def test_webhook_gate_missing_bearer_401_validator_not_called(self, tmp_path):
        mod, oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        hass = self._gate_hass(mod, auth_native, object())
        request = MagicMock()
        request.headers = {"Host": "example.nabu.casa"}  # no Authorization
        request.read = AsyncMock(return_value=b"")
        request.method = "POST"
        request.scheme = "https"
        with patch.object(oauth.web, "Response") as response_ctor:
            await mod._handle_webhook(hass, "mcp_test", request)
        request.read.assert_not_awaited()
        assert response_ctor.call_args.kwargs.get("status") == 401
        # Malformed/missing header rejected WITHOUT invoking the HA validator.
        hass.auth.async_validate_access_token.assert_not_called()

    async def test_validate_request_detailed_reason_matrix(self, tmp_path):
        """The diagnostic reason distinguishes every rejection class — no
        usable bearer, empty token, hass.auth returned None, validator raised
        — plus the accepted case, and never includes the token itself.
        validate_request stays the bool-only wrapper over the same logic."""
        _mod, _oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        # Capability gate, not file-existence: stable ships auth_native since
        # 2.0.0 but gains the detailed validator only at the next promote, so
        # the coarse _ha_auth_supported() gate is not enough here.
        if not hasattr(auth_native.ResourceServer, "validate_request_detailed"):
            pytest.skip("flavor does not ship the detailed bearer validator yet")
        hass = MagicMock()
        rs = auth_native.ResourceServer(hass, "wh")

        def req(headers):
            r = MagicMock()
            r.headers = headers
            return r

        hass.auth.async_validate_access_token = MagicMock(return_value=None)
        assert await rs.validate_request_detailed(req({})) == (
            False,
            "no bearer header",
        )
        assert await rs.validate_request_detailed(
            req({"Authorization": "Basic abc"})
        ) == (False, "no bearer header")
        assert await rs.validate_request_detailed(
            req({"Authorization": "Bearer   "})
        ) == (False, "empty bearer token")
        assert await rs.validate_request_detailed(
            req({"Authorization": "Bearer sekret-tok"})
        ) == (False, "token rejected by hass.auth (returned None)")
        hass.auth.async_validate_access_token = MagicMock(
            side_effect=ValueError("boom")
        )
        ok, reason = await rs.validate_request_detailed(
            req({"Authorization": "Bearer sekret-tok"})
        )
        assert (ok, reason) == (False, "validator raised ValueError")
        assert "sekret-tok" not in reason
        hass.auth.async_validate_access_token = MagicMock(return_value=object())
        assert await rs.validate_request_detailed(
            req({"Authorization": "Bearer sekret-tok"})
        ) == (True, "valid")
        # Wrapper parity: same logic, bool-only contract.
        assert (
            await rs.validate_request(req({"Authorization": "Bearer sekret-tok"}))
            is True
        )

    async def test_gate_debug_log_carries_rejection_reason(self, tmp_path):
        """With debug logging on, the 401 line names WHY ha_auth rejected the
        bearer (the issue #1714 OIDC-leg diagnosis aid) — never the token."""
        mod, oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        # Capability gate (see test_validate_request_detailed_reason_matrix):
        # the gate's reason plumbing ships together with the detailed validator.
        if not hasattr(auth_native.ResourceServer, "validate_request_detailed"):
            pytest.skip("flavor does not ship the detailed bearer validator yet")
        hass = self._gate_hass(mod, auth_native, None)  # validator -> None
        hass.data[mod.DOMAIN]["debug_logging"] = True
        request = MagicMock()
        request.headers = {"Authorization": "Bearer sekret-tok", "Host": "h"}
        request.read = AsyncMock(return_value=b"")
        request.method = "POST"
        request.scheme = "https"
        with (
            patch.object(mod, "_debug_log", new=AsyncMock()) as dbg,
            patch.object(oauth.web, "Response") as response_ctor,
        ):
            await mod._handle_webhook(hass, "mcp_test", request)
        assert response_ctor.call_args.kwargs.get("status") == 401
        logged = " ".join(str(c.args[1]) for c in dbg.call_args_list)
        assert "token rejected by hass.auth (returned None)" in logged
        assert "sekret-tok" not in logged

    async def test_validator_raises_gate_returns_401_not_500(self, tmp_path):
        """A crafted token can make hass.auth.async_validate_access_token raise
        (outside jwt.InvalidTokenError). ResourceServer.validate_request must
        swallow it and return False, and the webhook gate must then emit the
        normal 401 discovery challenge — never a 500 that leaks the exception."""
        mod, oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        hass = self._gate_hass(mod, auth_native, object())
        hass.auth.async_validate_access_token = MagicMock(
            side_effect=RuntimeError("crafted unsigned token")
        )
        rs = hass.data[mod.DOMAIN]["oauth"]
        # Direct: the raise is contained and reported as unauthorized.
        direct = MagicMock()
        direct.headers = {"Authorization": "Bearer crafted"}
        assert await rs.validate_request(direct) is False
        # Gate: 401 challenge, not a 500, and the upstream is never reached.
        request = MagicMock()
        request.headers = {"Authorization": "Bearer crafted", "Host": "h.nabu.casa"}
        request.read = AsyncMock(return_value=b"")
        request.method = "POST"
        request.scheme = "https"
        with patch.object(oauth.web, "Response") as response_ctor:
            await mod._handle_webhook(hass, "mcp_test", request)
        request.read.assert_not_awaited()
        assert response_ctor.call_args.kwargs.get("status") == 401
        ww = response_ctor.call_args.kwargs.get("headers", {}).get(
            "WWW-Authenticate", ""
        )
        assert ww.startswith("Bearer realm=")
        assert "resource_metadata=" in ww

    # ---- validate_request direct unit tests ----

    def _resource_server_for_validate(self, tmp_path, validator):
        mod, oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        hass = MagicMock()
        hass.auth.async_validate_access_token = validator
        return auth_native.ResourceServer(hass, "wh")

    async def test_validate_request_empty_token_not_validated(self, tmp_path):
        """A bare 'Bearer ' with an empty/whitespace token is rejected WITHOUT
        calling the validator (no point hashing junk)."""
        validator = MagicMock(return_value=object())
        rs = self._resource_server_for_validate(tmp_path, validator)
        req = MagicMock()
        req.headers = {"Authorization": "Bearer    "}
        assert await rs.validate_request(req) is False
        validator.assert_not_called()

    async def test_validate_request_wrong_scheme_rejected(self, tmp_path):
        """A non-Bearer scheme (Basic) is rejected without touching the
        validator."""
        validator = MagicMock(return_value=object())
        rs = self._resource_server_for_validate(tmp_path, validator)
        req = MagicMock()
        req.headers = {"Authorization": "Basic dXNlcjpwYXNzd29yZA=="}
        assert await rs.validate_request(req) is False
        validator.assert_not_called()

    async def test_validate_request_mixed_case_bearer_accepted(self, tmp_path):
        """The scheme match is case-insensitive: 'bEaReR' still routes the token
        to the validator (a non-None RefreshToken -> authorized)."""
        validator = MagicMock(return_value=object())
        rs = self._resource_server_for_validate(tmp_path, validator)
        req = MagicMock()
        req.headers = {"Authorization": "bEaReR good-token"}
        assert await rs.validate_request(req) is True
        validator.assert_called_once_with("good-token")

    async def test_validate_request_awaitable_result_branch(self, tmp_path):
        """Defensive future-proofing: if a future HA turns async_validate_access_token
        into a coroutine, validate_request awaits it before the None check."""
        validator = AsyncMock(return_value=object())
        rs = self._resource_server_for_validate(tmp_path, validator)
        req = MagicMock()
        req.headers = {"Authorization": "Bearer good-token"}
        assert await rs.validate_request(req) is True
        validator.assert_awaited_once_with("good-token")

    # ---- unloaded (DOMAIN absent) -> every bound view 404s ----

    async def test_unloaded_views_all_404(self, tmp_path):
        """When the config entry was unloaded (hass.data lacks DOMAIN) but HA
        can't drop the still-bound views until a restart, _active_oauth_mode
        resolves None: both metadata views AND the three root view handlers must
        404 like an unregistered route."""
        _mod, oauth, _an = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        hass = MagicMock()
        hass.data = {}  # DOMAIN absent -> unloaded
        provider = oauth.OAuthProvider(
            hass=hass,
            client_id="cid-1234567890ABCDEF",
            client_secret="sec",
            webhook_id="wh",
            signing_key=b"\x00" * 32,
        )
        req = _make_view_request(headers={"Host": "h"})
        for view in (
            oauth.ProtectedResourceMetadataView(provider),
            oauth.AuthorizationServerMetadataView(provider),
        ):
            with patch.object(oauth.web, "json_response") as jr:
                await view.get(req)
            assert jr.call_args.kwargs.get("status") == 404
        av = oauth.AuthorizeView(provider)
        with patch.object(oauth.web, "Response") as rc:
            await av.get(_make_view_request(query={}, method="GET"))
        assert rc.call_args.kwargs.get("status") == 404
        with patch.object(oauth.web, "Response") as rc:
            await av.post(
                _make_view_request(method="POST", post_data={"action": "approve"})
            )
        assert rc.call_args.kwargs.get("status") == 404
        tv = oauth.TokenView(provider)
        with patch.object(oauth.web, "json_response") as jr:
            await tv.post(
                _make_view_request(
                    method="POST", post_data={"grant_type": "authorization_code"}
                )
            )
        assert jr.call_args.kwargs.get("status") == 404

    async def test_oauth_off_after_on_stale_views_all_404(self, tmp_path):
        """OAuth toggled OFF after a prior ON: the entry reloaded and __init__
        rewrote hass.data[DOMAIN] as {target_url, webhook_id, session} with NO
        'oauth_mode' key, but HA can't drop the still-bound views until a
        restart. _active_oauth_mode resolves domain_data.get('oauth_mode') ->
        None, so every bound view must 404 — the distinct present-dict-without-
        oauth_mode branch (test_unloaded_views_all_404 covers only the
        DOMAIN-absent branch). Regression guard: someone re-adding a MODE_LEGACY
        default on that .get()."""
        _mod, oauth, _an = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        hass = MagicMock()
        # Exact OAuth-off shape __init__ writes (target_url/webhook_id/session),
        # a REAL dict with no "oauth_mode" -> _active_oauth_mode returns None.
        hass.data = {
            oauth.DOMAIN: {
                "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "webhook_id": "wh",
                "session": MagicMock(),
            }
        }
        provider = oauth.OAuthProvider(
            hass=hass,
            client_id="cid-1234567890ABCDEF",
            client_secret="sec",
            webhook_id="wh",
            signing_key=b"\x00" * 32,
        )
        req = _make_view_request(headers={"Host": "h"})
        for view in (
            oauth.ProtectedResourceMetadataView(provider),
            oauth.AuthorizationServerMetadataView(provider),
        ):
            with patch.object(oauth.web, "json_response") as jr:
                await view.get(req)
            assert jr.call_args.kwargs.get("status") == 404
        av = oauth.AuthorizeView(provider)
        with patch.object(oauth.web, "Response") as rc:
            await av.get(_make_view_request(query={}, method="GET"))
        assert rc.call_args.kwargs.get("status") == 404
        with patch.object(oauth.web, "Response") as rc:
            await av.post(
                _make_view_request(method="POST", post_data={"action": "approve"})
            )
        assert rc.call_args.kwargs.get("status") == 404
        tv = oauth.TokenView(provider)
        with patch.object(oauth.web, "json_response") as jr:
            await tv.post(
                _make_view_request(
                    method="POST", post_data={"grant_type": "authorization_code"}
                )
            )
        assert jr.call_args.kwargs.get("status") == 404

    # ---- the 401 challenge is the SAME builder + pointer in both modes ----

    async def test_401_challenge_byte_identical_legacy_vs_ha_auth(self, tmp_path):
        """Both modes emit the 401 through the one shared
        build_unauthorized_response. Legacy pins its base to public_base_url and
        ha_auth derives it from the host; for a request whose host equals the
        pinned URL the two resolve to the same base, so the WWW-Authenticate
        challenge (realm + resource_metadata pointer) is byte-identical."""
        _mod, oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        legacy = oauth.OAuthProvider(
            hass=MagicMock(),
            client_id="cid-1234567890ABCDEF",
            client_secret="sec",
            webhook_id="mcp_webhook_id_aaaa",
            signing_key=b"\x00" * 32,
            public_base_url="https://myhost.example",
        )
        # ha_auth is host-derived (public_base_url None, per PR requirement).
        ha = auth_native.ResourceServer(MagicMock(), "mcp_webhook_id_aaaa", None)
        req = _make_view_request(headers={"Host": "myhost.example"}, scheme="https")
        with patch.object(oauth.web, "Response") as rc:
            oauth.build_unauthorized_response(req, legacy)
        legacy_ww = rc.call_args.kwargs["headers"]["WWW-Authenticate"]
        with patch.object(oauth.web, "Response") as rc:
            oauth.build_unauthorized_response(req, ha)
        ha_ww = rc.call_args.kwargs["headers"]["WWW-Authenticate"]
        assert legacy_ww == ha_ww
        assert legacy_ww == (
            'Bearer realm="MCP Proxy", resource_metadata='
            f'"https://myhost.example{oauth.OAUTH_BASE}/protected-resource"'
        )

    # ---- the ha_auth AS document is served identically at all 4 well-known URLs ----

    async def test_ha_auth_as_doc_identity_across_all_wellknown_variants(
        self, tmp_path
    ):
        """All four well-known authorization-server URLs must serve content
        identical to the canonical ha_auth authorization_server_document (issue
        #1714: claude.ai caches per-URL, so a stale/mismatched doc at any of
        these poisons discovery)."""
        oauth, rs = self._resource_server(tmp_path)  # oauth_mode ha_auth
        request = _make_view_request(headers={"Host": "ignored"})
        with patch.object(oauth.web, "json_response") as jr:
            await oauth.AuthorizationServerMetadataView(rs).get(request)
        canonical = jr.call_args.args[0]
        # It is genuinely the ha_auth document (public client + CIMD), not legacy.
        assert canonical["token_endpoint_auth_methods_supported"] == ["none"]
        assert canonical["client_id_metadata_document_supported"] is True
        assert canonical["authorization_endpoint"] == (
            "https://legit.example/auth/authorize"
        )
        base = oauth.OAUTH_BASE
        variants = (
            f"/.well-known/oauth-authorization-server{base}",
            f"/.well-known/openid-configuration{base}",
            f"{base}/.well-known/openid-configuration",
            f"{base}/.well-known/oauth-authorization-server",
        )
        for url in variants:
            view = oauth.WellKnownAuthorizationServerMetadataView(
                rs, url=url, name=f"test:{url}"
            )
            with patch.object(oauth.web, "json_response") as jr2:
                await view.get(request)
            assert jr2.call_args.args[0] == canonical

    # ---- live mode switch: served doc uses the ACTIVE provider's policy ----

    async def test_live_switch_bound_legacy_view_serves_ha_auth_host_derived(
        self, tmp_path
    ):
        """Regression guard for a no-restart legacy->ha_auth switch. The AS view
        was bound with the LEGACY provider (pinned public_base_url), but
        hass.data now marks ha_auth active with the ResourceServer as the live
        provider. `_active_provider` resolves the live ha_auth provider from
        hass.data at request time, so the served document is the ha_auth shape
        with a HOST-DERIVED base — never the bound legacy provider's pinned URL.
        Every other ha_auth test leaves hass.data[DOMAIN] without an 'oauth' key,
        so this is the only test that exercises the dynamic-provider branch."""
        _mod, oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        shared_hass = MagicMock()
        ha_rs = auth_native.ResourceServer(shared_hass, "wh", None)  # host-derived
        # ha_auth is live; the ResourceServer is the active provider.
        shared_hass.data = {
            oauth.DOMAIN: {"oauth_mode": oauth.MODE_HA_AUTH, "oauth": ha_rs}
        }
        # The view is still bound with the PREVIOUS mode's (legacy) provider,
        # which pins its base URL — HA can't rebind the view mid-session.
        bound_legacy = oauth.OAuthProvider(
            hass=shared_hass,
            client_id="cid-1234567890ABCDEF",
            client_secret="sec",
            webhook_id="wh",
            signing_key=b"\x00" * 32,
            public_base_url="https://pinned-legacy.example",
        )
        request = _make_view_request(headers={"Host": "switch-host.example"})
        with patch.object(oauth.web, "json_response") as jr:
            await oauth.AuthorizationServerMetadataView(bound_legacy).get(request)
        doc = jr.call_args.args[0]
        # ha_auth document shape (public client + CIMD), not legacy.
        assert doc["token_endpoint_auth_methods_supported"] == ["none"]
        assert doc["client_id_metadata_document_supported"] is True
        # Host-derived base from the request, NOT the bound provider's pin.
        assert doc["issuer"] == f"https://switch-host.example{oauth.OAUTH_BASE}"
        assert doc["authorization_endpoint"] == (
            "https://switch-host.example/auth/authorize"
        )
        assert "pinned-legacy.example" not in doc["issuer"]

    async def test_live_switch_bound_ha_auth_view_serves_legacy_pinned(self, tmp_path):
        """The mirror direction of the no-restart switch: the AS view was bound
        with the HA_AUTH provider (host-derived), but hass.data now marks legacy
        active with the pinned OAuthProvider as the live provider.
        `_active_provider` resolves the live legacy provider, so the served
        document is the legacy shape pinned to public_base_url — the request Host
        is ignored (Host-poisoning resistance)."""
        _mod, oauth, auth_native = _import_ha_auth_stack(tmp_secret_dir=tmp_path)
        shared_hass = MagicMock()
        pinned_legacy = oauth.OAuthProvider(
            hass=MagicMock(),
            client_id="cid-1234567890ABCDEF",
            client_secret="sec",
            webhook_id="wh",
            signing_key=b"\x00" * 32,
            public_base_url="https://pinned-legacy.example",
        )
        # legacy is live; the pinned OAuthProvider is the active provider.
        shared_hass.data = {
            oauth.DOMAIN: {"oauth_mode": oauth.MODE_LEGACY, "oauth": pinned_legacy}
        }
        # The view is still bound with the PREVIOUS mode's (ha_auth) provider.
        bound_ha = auth_native.ResourceServer(shared_hass, "wh", None)
        request = _make_view_request(headers={"Host": "other-host.example"})
        with patch.object(oauth.web, "json_response") as jr:
            await oauth.AuthorizationServerMetadataView(bound_ha).get(request)
        doc = jr.call_args.args[0]
        # Legacy document shape (embedded AS, client_secret_* auth methods).
        assert "client_secret_basic" in doc["token_endpoint_auth_methods_supported"]
        assert "client_id_metadata_document_supported" not in doc
        # Pinned base from public_base_url, NOT the request Host.
        assert doc["issuer"] == f"https://pinned-legacy.example{oauth.OAUTH_BASE}"
        assert doc["authorization_endpoint"] == (
            "https://pinned-legacy.example/authorize"
        )
        assert "other-host.example" not in doc["issuer"]


class TestResolveOAuthMode:
    """start.py's upgrade-safe OAuth mode resolution. Skips on flavors that
    don't ship ha_auth (their start.py has no _resolve_oauth_mode)."""

    @pytest.fixture(autouse=True)
    def _skip_on_stable(self, _webhook_proxy_variant):
        if not _ha_auth_supported():
            pytest.skip("flavor does not ship ha_auth (_resolve_oauth_mode) yet")

    @pytest.fixture
    def start(self):
        return _import_start()

    def test_explicit_mode_wins(self, start, tmp_path):
        assert start._resolve_oauth_mode("ha_auth", "", "", tmp_path) == "ha_auth"
        assert start._resolve_oauth_mode("legacy", "", "", tmp_path) == "legacy"

    def test_explicit_ha_auth_wins_even_with_legacy_creds_file(self, start, tmp_path):
        (tmp_path / "oauth_creds.json").write_text(
            json.dumps({"client_id": "x", "client_secret": "y"})
        )
        assert start._resolve_oauth_mode("ha_auth", "cid", "sec", tmp_path) == "ha_auth"

    def test_unset_with_configured_creds_is_legacy(self, start, tmp_path):
        assert (
            start._resolve_oauth_mode("", "cid-1234567890ABCDEF", "sec", tmp_path)
            == "legacy"
        )

    def test_unset_with_persisted_creds_file_is_legacy(self, start, tmp_path):
        (tmp_path / "oauth_creds.json").write_text(
            json.dumps({"client_id": "x", "client_secret": "y"})
        )
        assert start._resolve_oauth_mode("", "", "", tmp_path) == "legacy"

    def test_unset_no_legacy_traces_defaults_ha_auth(self, start, tmp_path):
        assert start._resolve_oauth_mode("", "", "", tmp_path) == "ha_auth"

    def test_unknown_mode_falls_through_to_detection(self, start, tmp_path):
        """Supervisor's enum validation normally blocks an unknown oauth_mode,
        but if one reaches here it must fall through to auto-detection rather
        than crash: no legacy trace -> ha_auth, a legacy trace -> legacy."""
        assert start._resolve_oauth_mode("bogus", "", "", tmp_path) == "ha_auth"
        assert (
            start._resolve_oauth_mode("bogus", "cid-1234567890ABCDEF", "sec", tmp_path)
            == "legacy"
        )

    def test_id_only_without_secret_or_file_is_legacy(self, start, tmp_path):
        """A configured client id alone (no secret, no persisted creds file) is
        still a legacy trace -> stay on legacy."""
        assert (
            start._resolve_oauth_mode("", "cid-1234567890ABCDEF", "", tmp_path)
            == "legacy"
        )

    def test_secret_only_without_id_or_file_is_legacy(self, start, tmp_path):
        """A configured client secret alone (no id, no persisted creds file) is
        still a legacy trace -> stay on legacy."""
        assert start._resolve_oauth_mode("", "", "sec", tmp_path) == "legacy"

    def test_creds_file_stat_oserror_preserves_legacy(self, start, capsys):
        """start.py ~654: Path.exists() re-raises a stat error other than 'not
        there' (e.g. EACCES). A stat failure makes legacy detection
        INDETERMINATE — it is no proof the file is absent — and flipping an
        existing persisted-creds legacy user to ha_auth would break their
        connector. So _resolve_oauth_mode must catch the OSError, log that the
        legacy-install check could not be completed, and preserve the previous
        flow by returning 'legacy' instead of defaulting to ha_auth."""
        creds_file = MagicMock()
        creds_file.exists.side_effect = OSError("EACCES: permission denied")
        data_dir = MagicMock()
        data_dir.__truediv__.return_value = creds_file
        # No configured id/secret, but the creds-file stat fails -> preserve the
        # previous (legacy) flow rather than silently switching to ha_auth.
        assert start._resolve_oauth_mode("", "", "", data_dir) == "legacy"
        # log_error goes to stderr; the indeterminate-preserve lines must appear.
        err = capsys.readouterr().err
        assert "indeterminate" in err
        assert "Preserving the previous (legacy) OAuth flow" in err


class TestStartHaAuthMode:
    """start.py main(): ha_auth writes the mode-only oauth section and skips the
    legacy credential machinery; legacy writes the mode + creds. Skips on
    flavors without ha_auth."""

    @pytest.fixture(autouse=True)
    def _skip_on_stable(self, _webhook_proxy_variant):
        if not _ha_auth_supported():
            pytest.skip("flavor does not ship ha_auth start.py support yet")

    def _run_main(self, tmp_path, options, *, probe_active=True):
        start = _import_start()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "options.json").write_text(json.dumps(options))
        config_path = tmp_path / "proxy_config.json"

        def path_factory(arg):
            if arg == "/data/options.json":
                return data_dir / "options.json"
            if arg == "/data":
                return data_dir
            if arg == CURRENT["config_file"]:
                return config_path
            if arg == CURRENT["oauth_marker"]:
                return tmp_path / "oauth_marker"
            return Path(arg)

        resolve_creds = MagicMock(
            wraps=lambda d, cid, sec: (cid or "hamcp-generated1234567890", sec or "sec")
        )
        # A bool probe_active is a constant result; a list/tuple is a per-call
        # sequence (side_effect) so a caller can model an initial probe failing
        # and the re-probe succeeding after the HA restart.
        probe_kwargs = (
            {"side_effect": list(probe_active)}
            if isinstance(probe_active, (list, tuple))
            else {"return_value": probe_active}
        )
        with (
            patch.object(start, "Path", side_effect=path_factory),
            patch.object(start, "_supervisor_get", return_value={"addons": []}),
            patch.object(start, "_ha_core_api", return_value={}) as ha_core_api,
            patch.object(start, "_install_integration", return_value=(False, False)),
            patch.object(start, "_ensure_config_entry", return_value=True),
            patch.object(start, "_reload_config_entry"),
            patch.object(start, "_resolve_oauth_creds", resolve_creds),
            patch.object(start, "_probe_oauth_active", **probe_kwargs),
            # Destructive fail-closed helpers: patched so the stale-code path is
            # observable without tearing down / blocking on a real HA restart.
            patch.object(start, "_remove_config_entry") as remove_entry,
            patch.object(start, "_wait_for_ha_restart") as wait_restart,
            patch.object(start, "_request_restart_repair") as request_repair,
            patch.object(
                start, "_install_shutdown_handlers", return_value={"reason": None}
            ),
            patch.object(start, "_health_check", return_value=True),
            patch.object(start, "_shutdown_cleanup"),
            patch.object(start.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            rc = start.main()
        written = json.loads(config_path.read_text())
        mocks = {
            "resolve_creds": resolve_creds,
            "remove_entry": remove_entry,
            "wait_restart": wait_restart,
            "request_repair": request_repair,
            "ha_core_api": ha_core_api,
        }
        return rc, written, mocks

    def test_ha_auth_writes_mode_only_section_and_skips_creds(self, tmp_path, capsys):
        rc, written, mocks = self._run_main(
            tmp_path,
            {
                "mcp_server_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "enable_oauth": True,
                # no oauth_mode, no creds, no /data/oauth_creds.json -> ha_auth
            },
        )
        assert rc == 0
        # Exactly the mode marker; no credential keys.
        assert written["oauth"] == {"mode": "ha_auth"}
        # The legacy credential machinery is never invoked in ha_auth mode.
        mocks["resolve_creds"].assert_not_called()
        # Probe active -> no fail-closed teardown.
        mocks["remove_entry"].assert_not_called()
        out = capsys.readouterr().out
        assert "Home Assistant login" in out
        assert "BLANK" in out
        # Pin the user-critical guidance lines, not just the headline: tokens
        # are revoked from the HA profile, and a UI-required Client Secret can
        # be any value (HA ignores it) — users act on these exact lines.
        assert "Revoke access anytime from your Home Assistant profile's" in out
        assert "any value works — Home Assistant ignores it." in out

    def test_legacy_writes_mode_marker_and_creds(self, tmp_path, capsys):
        rc, written, mocks = self._run_main(
            tmp_path,
            {
                "mcp_server_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "enable_oauth": True,
                "oauth_client_id": "client-1234567890ABCDEF",
                "oauth_client_secret": "secret-much-secret",
            },
        )
        assert rc == 0
        assert written["oauth"]["mode"] == "legacy"
        assert written["oauth"]["client_id"] == "client-1234567890ABCDEF"
        assert written["oauth"]["client_secret"] == "secret-much-secret"
        mocks["resolve_creds"].assert_called_once()
        # Pin the copy-paste creds lines the legacy banner exists for.
        out = capsys.readouterr().out
        assert "OAuth Client ID:     client-1234567890ABCDEF" in out
        assert "OAuth Client Secret: secret-much-secret" in out

    def test_ha_auth_stale_code_probe_fails_closed(self, tmp_path):
        """The fail-closed stale-code probe runs in ha_auth too (it closes the
        code-upgrade fail-open window, not the no-restart-to-toggle promise).
        When the OAuth metadata endpoint isn't served yet — old cached module
        after an upgrade — the webhook is torn down and a click-to-restart
        Repair is requested, even in ha_auth mode."""
        rc, written, mocks = self._run_main(
            tmp_path,
            {
                "mcp_server_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "enable_oauth": True,
            },
            probe_active=False,
        )
        assert rc == 0
        # The ha_auth section is written BEFORE the probe, so it still lands.
        assert written["oauth"] == {"mode": "ha_auth"}
        # Fail-closed teardown fired: webhook removed, restart-Repair requested,
        # and the HA-restart wait entered.
        assert mocks["remove_entry"].called
        assert mocks["request_repair"].called
        mocks["wait_restart"].assert_called_once()
        # The marker the integration's Repair flow watches was written.
        assert (tmp_path / "oauth_marker").exists()

    def test_ha_auth_probe_recovers_after_restart(self, tmp_path, capsys):
        """Auto-recovery branch (start.py ~1482): the initial stale-code probe
        fails, so main() tears the entry down, writes the restart marker, and
        waits for the HA restart; the re-probe then succeeds, so the stale
        notification is dismissed, the marker is unlinked, and the webhook is
        reported re-enabled — no manual intervention needed."""
        rc, written, mocks = self._run_main(
            tmp_path,
            {
                "mcp_server_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "enable_oauth": True,
            },
            probe_active=[False, True],  # initial probe fails, re-probe succeeds
        )
        assert rc == 0
        # The ha_auth section is written BEFORE the probe, so it still lands.
        assert written["oauth"] == {"mode": "ha_auth"}
        # Initial probe failed -> fail-closed teardown ran and the restart was
        # awaited.
        assert mocks["remove_entry"].called
        assert mocks["request_repair"].called
        mocks["wait_restart"].assert_called_once()
        # Re-probe succeeded -> the OAuth-stale notification was dismissed (the
        # recovery branch's dismiss, not the several dismissed at startup).
        dismiss_calls = [
            c
            for c in mocks["ha_core_api"].call_args_list
            if c.args[:2] == ("POST", "/services/persistent_notification/dismiss")
            and c.args[2].get("notification_id", "").endswith("oauth_stale")
        ]
        assert len(dismiss_calls) == 1
        # ...the marker was unlinked, and the webhook reported re-enabled.
        assert not (tmp_path / "oauth_marker").exists()
        assert "webhook re-enabled" in capsys.readouterr().out

    def test_ha_auth_with_remote_url_logs_ignored_notice_and_no_pin(
        self, tmp_path, capsys
    ):
        """Fix 6: in ha_auth mode a configured External URL (remote_url) is
        deliberately ignored — discovery is host-derived. main() logs the
        info notice AND writes the mode-only oauth section with NO
        public_base_url anywhere (reinforcing Fix 4: the External URL never
        leaks into the ha_auth config)."""
        rc, written, _mocks = self._run_main(
            tmp_path,
            {
                "mcp_server_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "enable_oauth": True,
                "remote_url": "https://example.nabu.casa",
                # no oauth_mode, no creds, no /data/oauth_creds.json -> ha_auth
            },
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "External URL is ignored in ha_auth mode" in out
        # Mode-only section; the External URL did not leak into the config.
        assert written["oauth"] == {"mode": "ha_auth"}
        assert "public_base_url" not in written
        assert "public_base_url" not in written["oauth"]

    def test_config_read_failure_refuses_to_start(self, tmp_path, capsys):
        """FIX 1 (fail closed): if /data/options.json is present but
        unreadable/corrupt, main() cannot tell whether the user enabled OAuth,
        so it must refuse to start rather than fall back to the unauthenticated
        defaults (enable_oauth=False). It logs a loud refusing-to-start block,
        returns 1, and writes NO proxy config file.

        Gated on _ha_auth_supported() like the rest of this class: the
        fail-closed config read rides the dev flavor's start.py until it is
        promoted to stable. Uses its own harness (not _run_main) because
        _run_main reads the proxy config after main(), which is never written
        on this early return."""
        start = _import_start()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # Corrupt options.json: present but not valid JSON.
        (data_dir / "options.json").write_text("{ this is not valid json ")
        config_path = tmp_path / "proxy_config.json"

        def path_factory(arg):
            if arg == "/data/options.json":
                return data_dir / "options.json"
            if arg == "/data":
                return data_dir
            if arg == CURRENT["config_file"]:
                return config_path
            if arg == CURRENT["oauth_marker"]:
                return tmp_path / "oauth_marker"
            return Path(arg)

        # Only the sibling-mutex probe runs before the config read; the mocks
        # for webhook/config work are unnecessary because main() returns first.
        with (
            patch.object(start, "Path", side_effect=path_factory),
            patch.object(start, "_supervisor_get", return_value={"addons": []}),
        ):
            rc = start.main()

        assert rc == 1
        # main() returned before any config work -> no proxy config written.
        assert not config_path.exists()
        # log_error goes to stderr; the loud refusing-to-start block must appear.
        err = capsys.readouterr().err
        assert "refusing to start" in err
        assert "Could not read the add-on configuration" in err


class TestStartOAuthOffConfigShape:
    """enable_oauth stays OFF by default and this PR must not change that: an
    absent/false enable_oauth writes the byte-identical no-oauth config file
    shape (no `oauth` section, no `public_base_url`). Runs on BOTH flavors."""

    def _run_main_off(self, tmp_path, options):
        start = _import_start()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "options.json").write_text(json.dumps(options))
        config_path = tmp_path / "proxy_config.json"

        def path_factory(arg):
            if arg == "/data/options.json":
                return data_dir / "options.json"
            if arg == "/data":
                return data_dir
            if arg == CURRENT["config_file"]:
                return config_path
            return Path(arg)

        with (
            patch.object(start, "Path", side_effect=path_factory),
            patch.object(start, "_supervisor_get", return_value={"addons": []}),
            patch.object(start, "_ha_core_api", return_value={}),
            patch.object(start, "_install_integration", return_value=(False, False)),
            patch.object(start, "_ensure_config_entry", return_value=True),
            patch.object(start, "_reload_config_entry"),
            patch.object(
                start, "_install_shutdown_handlers", return_value={"reason": None}
            ),
            patch.object(start, "_health_check", return_value=True),
            patch.object(start, "_shutdown_cleanup"),
            patch.object(start.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            rc = start.main()
        return rc, json.loads(config_path.read_text())

    def test_absent_enable_oauth_writes_no_oauth_section(self, tmp_path):
        rc, written = self._run_main_off(
            tmp_path,
            {"mcp_server_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa"},
        )
        assert rc == 0
        # Byte-identical no-oauth shape: exactly the two legacy keys.
        assert set(written.keys()) == {"target_url", "webhook_id"}
        assert "oauth" not in written
        assert "public_base_url" not in written

    def test_false_enable_oauth_writes_no_oauth_section(self, tmp_path):
        rc, written = self._run_main_off(
            tmp_path,
            {
                "mcp_server_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "enable_oauth": False,
            },
        )
        assert rc == 0
        assert set(written.keys()) == {"target_url", "webhook_id"}
        assert "oauth" not in written
        assert "public_base_url" not in written


class TestProxyConfigFilePersistence:
    """The proxy-config handoff file carries the OAuth keys when auth is
    enabled, so it must actually land on disk with the flavor's expected
    permissions: the dev flavor writes it via _atomic_write_0600 (with a
    warned plain-write fallback); stable still plain-writes until the next
    promote carries the dev change over."""

    def _run(self, tmp_path):
        return TestMainDismissesMutexBanners()._run_main_to_keepalive(tmp_path)

    def test_config_file_written_and_dev_gets_0600(self, tmp_path):
        rc, _api_calls, _start = self._run(tmp_path)
        assert rc == 0
        config_path = tmp_path / "proxy_config.json"
        assert config_path.exists()
        assert isinstance(json.loads(config_path.read_text()), dict)
        if CURRENT["key"] == "dev":
            assert oct(config_path.stat().st_mode & 0o777) == "0o600"

    def test_dev_falls_back_and_warns_when_0600_unavailable(self, tmp_path, capsys):
        if CURRENT["key"] != "dev":
            pytest.skip("stable plain-writes until the dev 0600 change promotes")
        # os.open is only reached via _atomic_write_0600 on this path (OAuth
        # off, creds resolution skipped), so failing it exercises exactly the
        # proxy-config fallback branch.
        with patch.object(os, "open", side_effect=OSError("no mode bits")):
            rc, _api_calls, _start = self._run(tmp_path)
        assert rc == 0
        config_path = tmp_path / "proxy_config.json"
        assert config_path.exists()
        assert isinstance(json.loads(config_path.read_text()), dict)
        err = capsys.readouterr().err
        assert "Could not create the proxy config file" in err


class TestNoneAutoApproveMode:
    """None-mode auto-approve discovery (OAuth off — issue #1969, dev-only).

    When OAuth is off the add-on serves its own corrected RFC 8414/9728 discovery
    documents plus an invisible auto-approve authorization server, so claude.ai's
    intermittent discovery resolves against the add-on instead of HA core's
    broken origin-root doc — and completes with no HA login. The webhook stays
    unauthenticated (the forwarder never 401s). Skips on flavors that don't ship
    oauth_autoapprove yet."""

    @pytest.fixture(autouse=True)
    def _skip_on_stable(self, _webhook_proxy_variant):
        if not _none_autoapprove_supported():
            pytest.skip("flavor does not ship none-mode auto-approve yet")

    @pytest.fixture
    def hass(self):
        h = MagicMock()
        h.data = {}
        h.http = MagicMock()
        h.http.register_view = MagicMock()

        async def fake_executor(func, *args):
            return func(*args)

        h.async_add_executor_job = AsyncMock(side_effect=fake_executor)
        return h

    @staticmethod
    def _pkce_pair():
        """A valid (code_verifier, code_challenge) pair per RFC 7636 S256."""
        import base64
        import hashlib
        import secrets

        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        return verifier, challenge

    @staticmethod
    def _none_live_hass(oauth, autoapprove, webhook_id="mcp_test"):
        """A hass whose live cfg is none-mode auto-approve (provider under the
        AUTOAPPROVE_PROVIDER_KEY, mode marker set, NO "oauth" key)."""
        hass = MagicMock()
        hass.data = {}
        hass.http = MagicMock()
        provider = autoapprove.AutoApproveProvider(hass, webhook_id, None)
        hass.data[oauth.DOMAIN] = {
            "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
            "webhook_id": webhook_id,
            "session": MagicMock(),
            "oauth_mode": oauth.MODE_NONE_AUTOAPPROVE,
            oauth.AUTOAPPROVE_PROVIDER_KEY: provider,
        }
        return hass, provider

    CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"

    # ---- literals agree across the modules ----

    def test_mode_and_key_literals_agree(self):
        mod, oauth, autoapprove, _an = _import_none_autoapprove_stack()
        assert oauth.MODE_NONE_AUTOAPPROVE == "none_autoapprove"
        assert mod.OAUTH_MODE_NONE_AUTOAPPROVE == oauth.MODE_NONE_AUTOAPPROVE
        # The provider key the discovery views read (oauth._active_provider) must
        # equal the one the handler writes (mod._setup_none_autoapprove).
        assert oauth.AUTOAPPROVE_PROVIDER_KEY == mod.AUTOAPPROVE_PROVIDER_KEY
        assert oauth.AUTOAPPROVE_PROVIDER_KEY == "autoapprove"

    # ---- none-mode authorization-server document ----

    def test_as_document_shape(self):
        _mod, _oauth, autoapprove, _an = _import_none_autoapprove_stack()
        doc = autoapprove.authorization_server_document("https://x.example")
        base = CURRENT["oauth_base"]
        assert doc["issuer"] == f"https://x.example{base}"
        # Points at OUR auto-approve endpoints, NOT HA core's /auth/*.
        assert doc["authorization_endpoint"] == f"https://x.example{base}/authorize"
        assert doc["token_endpoint"] == f"https://x.example{base}/token"
        assert doc["response_types_supported"] == ["code"]
        assert doc["grant_types_supported"] == ["authorization_code"]
        assert doc["code_challenge_methods_supported"] == ["S256"]
        # The two fields HA core's root doc lacks — the whole reason for #1969.
        assert doc["token_endpoint_auth_methods_supported"] == ["none"]
        assert doc["client_id_metadata_document_supported"] is True
        # No DCR: fixed/auto flow, so no registration endpoint advertised.
        assert "registration_endpoint" not in doc

    async def test_as_view_serves_none_document_when_live(self):
        _mod, oauth, autoapprove, _an = _import_none_autoapprove_stack()
        hass, provider = self._none_live_hass(oauth, autoapprove)
        view = oauth.AuthorizationServerMetadataView(provider)
        request = _make_view_request(headers={"Host": "legit.example"})
        with patch.object(oauth.web, "json_response") as jr:
            await view.get(request)
        doc = jr.call_args.args[0]
        base = CURRENT["oauth_base"]
        assert doc["authorization_endpoint"] == f"https://legit.example{base}/authorize"
        assert doc["token_endpoint_auth_methods_supported"] == ["none"]

    async def test_fixed_path_protected_resource_404s_in_none_mode(self):
        # SECURITY (#1976): the fixed, guessable /protected-resource path must
        # NOT leak the webhook id (the sole credential in none mode) to an
        # anonymous GET — it 404s in none-autoapprove mode.
        _mod, oauth, autoapprove, _an = _import_none_autoapprove_stack()
        hass, provider = self._none_live_hass(oauth, autoapprove)
        request = _make_view_request(headers={"Host": "legit.example"})
        with patch.object(oauth.web, "json_response") as jr:
            await oauth.ProtectedResourceMetadataView(provider).get(request)
        assert jr.call_args.kwargs["status"] == 404

    async def test_path_scoped_protected_resource_still_serves_in_none_mode(self):
        # The path-scoped view (URL embeds the id) KEEPS serving in none mode —
        # its caller already knows the id, so nothing leaks (#1976). This is the
        # doc claude.ai's none-mode discovery actually uses.
        _mod, oauth, autoapprove, _an = _import_none_autoapprove_stack()
        hass, provider = self._none_live_hass(oauth, autoapprove)
        request = _make_view_request(headers={"Host": "legit.example"})
        wk = oauth.WellKnownProtectedResourceView(provider)
        assert wk.url == "/.well-known/oauth-protected-resource/api/webhook/mcp_test"
        with patch.object(oauth.web, "json_response") as jr:
            await wk.get(request)
        body = jr.call_args.args[0]
        base = CURRENT["oauth_base"]
        assert body["resource"] == "https://legit.example/api/webhook/mcp_test"
        assert body["authorization_servers"] == [f"https://legit.example{base}"]
        # It served (200) rather than 404ing.
        assert jr.call_args.kwargs.get("status") in (None, 200)

    # ---- AutoApproveProvider (PKCE store + cosmetic token) ----

    def test_provider_pkce_roundtrip_and_one_shot(self):
        _mod, _oauth, autoapprove, _an = _import_none_autoapprove_stack()
        provider = autoapprove.AutoApproveProvider(MagicMock(), "mcp_test", None)
        verifier, challenge = self._pkce_pair()
        code = provider.issue_code(self.CLAUDE_REDIRECT, challenge)
        assert code
        assert provider.consume_code(code, self.CLAUDE_REDIRECT, verifier) is True
        # One-shot: a second consume of the same code fails.
        assert provider.consume_code(code, self.CLAUDE_REDIRECT, verifier) is False

    def test_provider_rejects_wrong_verifier(self):
        _mod, _oauth, autoapprove, _an = _import_none_autoapprove_stack()
        provider = autoapprove.AutoApproveProvider(MagicMock(), "mcp_test", None)
        _v, challenge = self._pkce_pair()
        other_verifier, _c = self._pkce_pair()
        code = provider.issue_code(self.CLAUDE_REDIRECT, challenge)
        assert (
            provider.consume_code(code, self.CLAUDE_REDIRECT, other_verifier) is False
        )

    def test_access_token_is_opaque_and_unique(self):
        _mod, _oauth, autoapprove, _an = _import_none_autoapprove_stack()
        t1 = autoapprove.AutoApproveProvider.issue_access_token()
        t2 = autoapprove.AutoApproveProvider.issue_access_token()
        assert isinstance(t1, str) and len(t1) >= 20
        assert t1 != t2

    # ---- redirect open-redirect gate (allowlist-only) ----

    def test_redirect_gate_allows_claude_callback_only(self):
        _mod, _oauth, autoapprove, _an = _import_none_autoapprove_stack()
        assert autoapprove._is_valid_autoapprove_redirect(self.CLAUDE_REDIRECT) is True
        # A same-host-different-path or any other https URL is NOT allowlisted.
        assert (
            autoapprove._is_valid_autoapprove_redirect("https://claude.ai/evil")
            is False
        )
        assert (
            autoapprove._is_valid_autoapprove_redirect("https://evil.example/cb")
            is False
        )
        # http (non-https) fails the floor before the allowlist even matters.
        assert (
            autoapprove._is_valid_autoapprove_redirect(
                "http://claude.ai/api/mcp/auth_callback"
            )
            is False
        )

    # ---- AutoApproveAuthorizeView (GET: issue code, 302, no UI) ----

    async def test_authorize_404_when_not_live(self):
        _mod, _oauth, autoapprove, _an = _import_none_autoapprove_stack()
        hass = MagicMock()
        hass.data = {}
        view = autoapprove.AutoApproveAuthorizeView(hass)
        request = _make_view_request(query={"response_type": "code"})
        with patch.object(autoapprove.web, "json_response") as jr:
            await view.get(request)
        assert jr.call_args.kwargs["status"] == 404

    async def test_authorize_happy_path_issues_code_and_redirects(self):
        _mod, oauth, autoapprove, _an = _import_none_autoapprove_stack()
        hass, provider = self._none_live_hass(oauth, autoapprove)
        view = autoapprove.AutoApproveAuthorizeView(hass)
        verifier, challenge = self._pkce_pair()
        request = _make_view_request(
            query={
                "response_type": "code",
                "client_id": "https://claude.ai/whatever",
                "redirect_uri": self.CLAUDE_REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "st-1",
            }
        )
        with patch.object(autoapprove.web, "Response") as resp:
            await view.get(request)
        # 302 straight back — no consent page rendered.
        assert resp.call_args.kwargs["status"] == 302
        location = resp.call_args.kwargs["headers"]["Location"]
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(location).query)
        assert q["state"][0] == "st-1"
        # The issued code is real: it consumes with the matching verifier.
        assert provider.consume_code(q["code"][0], self.CLAUDE_REDIRECT, verifier)

    async def test_authorize_rejects_non_allowlisted_redirect_with_400(self):
        _mod, oauth, autoapprove, _an = _import_none_autoapprove_stack()
        hass, _provider = self._none_live_hass(oauth, autoapprove)
        view = autoapprove.AutoApproveAuthorizeView(hass)
        _v, challenge = self._pkce_pair()
        request = _make_view_request(
            query={
                "response_type": "code",
                "client_id": "https://evil.example/cimd",
                "redirect_uri": "https://evil.example/steal",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "st",
            }
        )
        with (
            patch.object(autoapprove.web, "json_response") as jr,
            patch.object(autoapprove.web, "Response") as resp,
        ):
            await view.get(request)
        # 400 in place — NEVER a 302 to an unvalidated target.
        assert jr.call_args.kwargs["status"] == 400
        resp.assert_not_called()

    async def test_authorize_rejects_non_s256(self):
        _mod, oauth, autoapprove, _an = _import_none_autoapprove_stack()
        hass, _provider = self._none_live_hass(oauth, autoapprove)
        view = autoapprove.AutoApproveAuthorizeView(hass)
        _v, challenge = self._pkce_pair()
        request = _make_view_request(
            query={
                "response_type": "code",
                "redirect_uri": self.CLAUDE_REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "plain",
                "state": "st",
            }
        )
        with patch.object(autoapprove.web, "json_response") as jr:
            await view.get(request)
        assert jr.call_args.kwargs["status"] == 400

    # ---- AutoApproveTokenView (POST: PKCE exchange, public client) ----

    async def test_token_404_when_not_live(self):
        _mod, _oauth, autoapprove, _an = _import_none_autoapprove_stack()
        hass = MagicMock()
        hass.data = {}
        view = autoapprove.AutoApproveTokenView(hass)
        request = _make_view_request(post_data={}, method="POST")
        with patch.object(autoapprove.web, "json_response") as jr:
            await view.post(request)
        assert jr.call_args.kwargs["status"] == 404

    async def test_token_valid_pkce_no_client_secret(self):
        _mod, oauth, autoapprove, _an = _import_none_autoapprove_stack()
        hass, provider = self._none_live_hass(oauth, autoapprove)
        verifier, challenge = self._pkce_pair()
        code = provider.issue_code(self.CLAUDE_REDIRECT, challenge)
        view = autoapprove.AutoApproveTokenView(hass)
        # NOTE: no client_secret in the form — public client.
        request = _make_view_request(
            post_data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.CLAUDE_REDIRECT,
                "code_verifier": verifier,
            },
            method="POST",
        )
        with patch.object(autoapprove.web, "json_response") as jr:
            await view.post(request)
        # Success is a bare json_response (default 200, no status kwarg).
        assert jr.call_args.kwargs.get("status") in (None, 200)
        body = jr.call_args.args[0]
        assert body["token_type"] == "Bearer"
        assert body["access_token"]
        assert isinstance(body["expires_in"], int)
        # None mode issues no refresh token.
        assert "refresh_token" not in body
        # RFC 6749 §5.1: the token body carries credentials and must not be
        # cached — parity with the component's auto-approve token view (#1976).
        assert jr.call_args.kwargs["headers"] == {
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        }
        assert jr.call_args.kwargs["headers"]["Cache-Control"] == "no-store"

    async def test_token_wrong_verifier_is_invalid_grant(self):
        _mod, oauth, autoapprove, _an = _import_none_autoapprove_stack()
        hass, provider = self._none_live_hass(oauth, autoapprove)
        _v, challenge = self._pkce_pair()
        code = provider.issue_code(self.CLAUDE_REDIRECT, challenge)
        wrong_verifier, _c = self._pkce_pair()
        view = autoapprove.AutoApproveTokenView(hass)
        request = _make_view_request(
            post_data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.CLAUDE_REDIRECT,
                "code_verifier": wrong_verifier,
            },
            method="POST",
        )
        with patch.object(autoapprove.web, "json_response") as jr:
            await view.post(request)
        assert jr.call_args.kwargs["status"] == 400
        assert jr.call_args.args[0]["error"] == "invalid_grant"

    async def test_token_code_is_one_time(self):
        _mod, oauth, autoapprove, _an = _import_none_autoapprove_stack()
        hass, provider = self._none_live_hass(oauth, autoapprove)
        verifier, challenge = self._pkce_pair()
        code = provider.issue_code(self.CLAUDE_REDIRECT, challenge)
        view = autoapprove.AutoApproveTokenView(hass)
        post_data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.CLAUDE_REDIRECT,
            "code_verifier": verifier,
        }
        with patch.object(autoapprove.web, "json_response") as jr:
            await view.post(
                _make_view_request(post_data=dict(post_data), method="POST")
            )
        assert jr.call_args.kwargs.get("status") in (None, 200)
        with patch.object(autoapprove.web, "json_response") as jr:
            await view.post(
                _make_view_request(post_data=dict(post_data), method="POST")
            )
        assert jr.call_args.kwargs["status"] == 400
        assert jr.call_args.args[0]["error"] == "invalid_grant"

    # ---- full setup registers the views + marks the mode ----

    async def test_setup_registers_metadata_and_autoapprove_views(self, hass, tmp_path):
        mod, oauth, autoapprove, _an = _import_none_autoapprove_stack()
        repairs = _bind_repairs(mod, tmp_path)
        repairs.RESTART_MARKER_FILE.write_text('{"reason": "stale"}')
        # No "oauth" section = OAuth off = none-mode auto-approve.
        config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test",
        }
        with (
            patch.object(mod, "_read_config", return_value=config),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
            patch.object(repairs, "create_issue") as mock_create,
            patch.object(repairs, "_write_marker") as mock_write,
            patch.object(repairs, "_delete_issue_only") as mock_delete,
        ):
            result = await mod.async_setup_entry(hass, MagicMock())

        assert result is True
        registered = {
            call.args[0].url for call in hass.http.register_view.call_args_list
        }
        metadata = {
            f"{CURRENT['oauth_base']}/protected-resource",
            f"{CURRENT['oauth_base']}/authorization-server",
        } | _wellknown_oauth_urls(oauth, "mcp_test")
        autoapprove_urls = {
            f"{CURRENT['oauth_base']}/authorize",
            f"{CURRENT['oauth_base']}/token",
        }
        assert registered == metadata | autoapprove_urls
        assert len(registered) == 9  # 7 discovery + 2 auto-approve
        # Mode marker + provider under the non-"oauth" key.
        assert hass.data[mod.DOMAIN]["oauth_mode"] == mod.OAUTH_MODE_NONE_AUTOAPPROVE
        assert isinstance(
            hass.data[mod.DOMAIN]["autoapprove"], autoapprove.AutoApproveProvider
        )
        # The bearer-gate key stays off — the webhook is unauthenticated.
        assert "oauth" not in hass.data[mod.DOMAIN]
        # No root /authorize or /token (those are legacy-only).
        assert "/authorize" not in registered
        assert "/token" not in registered
        # No restart repair — none-mode binds path-scoped views only.
        assert not repairs.RESTART_MARKER_FILE.exists()
        mock_delete.assert_called_once_with(hass, mod.DOMAIN)
        mock_create.assert_not_called()
        mock_write.assert_not_called()

    async def test_setup_failure_fails_open_keeps_plain_proxy(self, hass, tmp_path):
        """Unlike ha_auth/legacy, a none-mode auto-approve setup failure must NOT
        tear down the (intentionally unauthenticated) webhook — it just skips the
        discovery enhancement and keeps forwarding."""
        mod, oauth, _aa, _an = _import_none_autoapprove_stack()
        _bind_repairs(mod, tmp_path)
        config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test",
        }
        with (
            patch.object(mod, "_read_config", return_value=config),
            patch.object(mod, "async_register"),
            patch.object(mod, "async_unregister") as mock_unreg,
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock()),
            patch.object(
                oauth, "register_metadata_views", side_effect=RuntimeError("boom")
            ),
        ):
            result = await mod.async_setup_entry(hass, MagicMock())

        assert result is True  # setup still succeeds (fail-open)
        # Webhook was NOT torn down.
        mock_unreg.assert_not_called()
        # Discovery not marked live, but the plain proxy config is present.
        data = hass.data[mod.DOMAIN]
        assert "autoapprove" not in data
        assert "oauth_mode" not in data
        assert "oauth" not in data
        assert data["target_url"].endswith("private_zctpwlX7ZkIAr7oqdfLPxw")

    # ---- mode switch none <-> ha_auth with no rebind (hard requirement) ----

    async def test_as_document_switches_none_ha_auth_without_rebind(self):
        _mod, oauth, autoapprove, auth_native = _import_none_autoapprove_stack()
        hass = MagicMock()
        hass.data = {}
        aa_provider = autoapprove.AutoApproveProvider(hass, "mcp_test", None)
        # none-autoapprove live
        hass.data[oauth.DOMAIN] = {
            "oauth_mode": oauth.MODE_NONE_AUTOAPPROVE,
            oauth.AUTOAPPROVE_PROVIDER_KEY: aa_provider,
        }
        view = oauth.AuthorizationServerMetadataView(aa_provider)  # bound once
        request = _make_view_request(headers={"Host": "legit.example"})
        base = CURRENT["oauth_base"]

        with patch.object(oauth.web, "json_response") as jr:
            await view.get(request)
        assert jr.call_args.args[0]["authorization_endpoint"] == (
            f"https://legit.example{base}/authorize"
        )

        # Flip to ha_auth by mutating hass.data — no rebind, no restart.
        rs = auth_native.ResourceServer(hass, "mcp_test", None)
        hass.data[oauth.DOMAIN] = {"oauth_mode": oauth.MODE_HA_AUTH, "oauth": rs}
        with patch.object(oauth.web, "json_response") as jr:
            await view.get(request)
        assert jr.call_args.args[0]["authorization_endpoint"] == (
            "https://legit.example/auth/authorize"
        )

        # Flip back to none — the same bound view serves the auto-approve doc.
        hass.data[oauth.DOMAIN] = {
            "oauth_mode": oauth.MODE_NONE_AUTOAPPROVE,
            oauth.AUTOAPPROVE_PROVIDER_KEY: aa_provider,
        }
        with patch.object(oauth.web, "json_response") as jr:
            await view.get(request)
        assert jr.call_args.args[0]["authorization_endpoint"] == (
            f"https://legit.example{base}/authorize"
        )

    # ---- forwarder stays unauthenticated (no 401) with auto-approve live ----

    async def test_webhook_forwarder_ignores_bearer_in_none_mode(self):
        mod, oauth, autoapprove, _an = _import_none_autoapprove_stack()
        hass = MagicMock()
        provider = autoapprove.AutoApproveProvider(hass, "mcp_test", None)
        hass.data = {
            mod.DOMAIN: {
                "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "webhook_id": "mcp_test",
                "session": MagicMock(),
                "oauth_mode": mod.OAUTH_MODE_NONE_AUTOAPPROVE,
                "autoapprove": provider,
            }
        }
        request = MagicMock()
        request.headers = {"Authorization": "Bearer whatever-cosmetic-or-forged"}
        request.read = AsyncMock(return_value=b"")
        request.method = "POST"
        sentinel = mod.aiohttp.ClientError("stop here")
        hass.data[mod.DOMAIN]["session"].request = MagicMock(side_effect=sentinel)

        await mod._handle_webhook(hass, "mcp_test", request)

        # The gate keys off "oauth" (absent here), so it never 401s — the body
        # is read and forwarded upstream (which raises the sentinel -> 502).
        request.read.assert_awaited_once()
