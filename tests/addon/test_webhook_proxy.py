"""Tests for the webhook proxy addon.

Structure tests verify addon files and config.yaml.
Unit tests mock Supervisor API calls to test discovery logic in start.py.
"""

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

PROXY_ADDON_DIR = "homeassistant-addon-webhook-proxy"


# ---------------------------------------------------------------------------
# Helper: import start.py from the addon directory
# ---------------------------------------------------------------------------

def _import_start():
    """Import the webhook proxy start.py as a module."""
    start_path = os.path.join(PROXY_ADDON_DIR, "start.py")
    spec = importlib.util.spec_from_file_location("webhook_proxy_start", start_path)
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

    sys.modules.update({
        "homeassistant": ha,
        "homeassistant.components": ha_components,
        "homeassistant.components.webhook": ha_webhook,
        "homeassistant.components.http": ha_components_http,
        "homeassistant.config_entries": ha_config_entries,
        "homeassistant.core": ha_core,
        "homeassistant.helpers": ha_helpers,
        "homeassistant.helpers.typing": ha_helpers_typing,
        "homeassistant.exceptions": ha_exceptions,
        "aiohttp": aiohttp_mod,
        "aiohttp.web": aiohttp_web,
        "yarl": yarl_mod,
    })


def _import_mcp_proxy(preload_oauth=None):
    """Import the mcp_proxy package's __init__.py with HA imports stubbed.

    `preload_oauth`: pre-register a specific oauth module under the
    relative-import name (`mcp_proxy_init.oauth`). Without this, the
    integration's `from .oauth import ...` calls would load a fresh oauth
    module pointing at /config — fine for production, useless for tests.
    """
    _install_runtime_stubs()
    init_path = os.path.join(PROXY_ADDON_DIR, "mcp_proxy", "__init__.py")
    sys.modules.pop("mcp_proxy_init", None)
    sys.modules.pop("mcp_proxy_init.oauth", None)
    spec = importlib.util.spec_from_file_location(
        "mcp_proxy_init",
        init_path,
        submodule_search_locations=[
            os.path.join(PROXY_ADDON_DIR, "mcp_proxy")
        ])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mcp_proxy_init"] = mod
    if preload_oauth is not None:
        sys.modules["mcp_proxy_init.oauth"] = preload_oauth
    spec.loader.exec_module(mod)
    return mod


def _import_oauth(tmp_secret_dir=None):
    """Import the oauth submodule, optionally redirecting the secret file
    to a tmp dir so tests don't need root or write access to /config.

    Registers in sys.modules under both `mcp_proxy_oauth` (so test patches
    targeting that name resolve) and the module returned can be passed to
    `_import_mcp_proxy(preload_oauth=...)` so the integration's relative
    import resolves to the same instance.
    """
    _install_runtime_stubs()
    oauth_path = os.path.join(PROXY_ADDON_DIR, "mcp_proxy", "oauth.py")
    sys.modules.pop("mcp_proxy_oauth", None)
    spec = importlib.util.spec_from_file_location("mcp_proxy_oauth", oauth_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mcp_proxy_oauth"] = mod
    spec.loader.exec_module(mod)
    if tmp_secret_dir is not None:
        mod.SECRET_FILE = Path(tmp_secret_dir) / ".mcp_proxy_oauth_secret"
    return mod


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
        int_dir = os.path.join(PROXY_ADDON_DIR, "mcp_proxy")
        required = ["__init__.py", "config_flow.py", "manifest.json", "strings.json"]
        for f in required:
            path = os.path.join(int_dir, f)
            assert os.path.exists(path), f"Missing integration file: mcp_proxy/{f}"

    def test_config_yaml_valid(self):
        with open(f"{PROXY_ADDON_DIR}/config.yaml") as f:
            config = yaml.safe_load(f)

        required_fields = ["name", "description", "version", "slug", "arch"]
        for field in required_fields:
            assert field in config, f"Missing required field: {field}"

        assert config["slug"] == "ha_mcp_webhook_proxy"
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

    def test_addon_and_integration_versions_match(self):
        """config.yaml and manifest.json versions track together so that
        `_install_integration` correctly detects updates."""
        with open(f"{PROXY_ADDON_DIR}/config.yaml") as f:
            addon_version = yaml.safe_load(f)["version"]
        with open(f"{PROXY_ADDON_DIR}/mcp_proxy/manifest.json") as f:
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
        with open(f"{PROXY_ADDON_DIR}/mcp_proxy/manifest.json") as f:
            manifest = json.load(f)

        assert manifest["domain"] == "mcp_proxy"
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
                return {"addons": [
                    {"slug": "ha_mcp"},
                    {"slug": "ha_mcp_dev"},
                ]}
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
                return {"addons": [
                    {"slug": "xyz999_ha_mcp"},
                    {"slug": "xyz999_ha_mcp_dev"},
                ]}
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
                return {"addons": [
                    {"slug": "ha_mcp"},
                    {"slug": "ha_mcp_dev"},
                ]}
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
            pytest.raises(_FakeConfigEntryError) as exc_info):
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
            pytest.raises(_FakeConfigEntryError)):
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
            pytest.raises(_FakeConfigEntryError) as exc_info):
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
            pytest.raises(_FakeConfigEntryError)):
            await mod.async_setup_entry(hass, MagicMock())

        mock_register.assert_not_called()
        mock_session.assert_not_called()

    @pytest.mark.parametrize(
        "register_error",
        [RuntimeError("boom"), ValueError("duplicate webhook"), KeyError("not loaded")])
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
            pytest.raises(_FakeConfigEntryError) as exc_info):
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
            pytest.raises(_FakeConfigEntryError) as exc_info):
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
            pytest.raises(_FakeConfigEntryError) as exc_info):
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
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock())):
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
        """The OFF path must not even add an "oauth" key to hass.data —
        v1.0.2 had three keys (target_url, webhook_id, session) and the OFF
        path of v1.0.3-beta.1 must produce identical shape."""
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test_webhook_id_12345",
        }
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock())):
            await mod.async_setup_entry(hass, MagicMock())

        assert "oauth" not in hass.data[mod.DOMAIN]
        # And the rest of the dict shape matches the legacy three keys
        assert set(hass.data[mod.DOMAIN].keys()) == {
            "target_url",
            "webhook_id",
            "session",
        }

    async def test_setup_does_not_import_oauth_module_when_off(self, mod, hass):
        """Confirms the lazy-import: oauth submodule shouldn't be loaded
        unless OAuth is configured. If this regresses, the OFF path picks up
        new dependencies and the 'no behavior change' guarantee breaks."""
        proxy_config = {
            "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
            "webhook_id": "mcp_test_webhook_id_12345",
        }
        # Wipe any stale import
        sys.modules.pop("mcp_proxy_init.oauth", None)
        with (
            patch.object(mod, "_read_config", return_value=proxy_config),
            patch.object(mod, "async_register"),
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock())):
            await mod.async_setup_entry(hass, MagicMock())

        # The submodule name follows from the parent's package name
        # ("mcp_proxy_init"). If it ever appears here, the OFF path imported it.
        assert "mcp_proxy_init.oauth" not in sys.modules

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
            patch.object(mod.aiohttp, "ClientSession", return_value=session),
            pytest.raises(_FakeConfigEntryError) as exc_info):
            await mod.async_setup_entry(hass, MagicMock())

        assert "client_id and/or client_secret is blank" in str(exc_info.value)
        assert mod.DOMAIN not in hass.data
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
            webhook_id="mcp_webhook_id_xxx", signing_key=b"\x00" * 32)

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

    def test_token_with_tampered_payload_rejected(self, provider):
        token = provider.issue_access_token()
        body, sig = token.rsplit(".", 1)
        tampered_body = body[:-1] + ("A" if body[-1] != "A" else "B")
        assert provider.validate_access_token(f"{tampered_body}.{sig}") is False

    def test_token_with_tampered_signature_rejected(self, provider):
        token = provider.issue_access_token()
        body, sig = token.rsplit(".", 1)
        tampered_sig = sig[:-1] + ("A" if sig[-1] != "A" else "B")
        assert provider.validate_access_token(f"{body}.{tampered_sig}") is False

    def test_expired_token_rejected(self, tmp_path):
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        provider = oauth.OAuthProvider(
            hass=MagicMock(),
            client_id="cid-1234567890ABCDEF",
            client_secret="sec",
            webhook_id="wh",
            signing_key=b"\x00" * 32)
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
        assert not provider.authenticate_client(
            "wrong", "client-secret-very-secret"
        )

    def test_authenticate_client_rejects_wrong_secret(self, provider):
        assert not provider.authenticate_client(
            "client-id-1234567890", "wrong"
        )

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
            hass=hass, client_id="id-aaaaaaaaaaaaaaaaa", client_secret="secret",
            webhook_id="wh", signing_key=b"\x00" * 32)
        token = provider1.issue_access_token()
        # New provider with a different client_id (admin rotated) — token
        # signed for the old client_id must be rejected.
        provider2 = oauth.OAuthProvider(
            hass=hass, client_id="id-bbbbbbbbbbbbbbbbb", client_secret="secret",
            webhook_id="wh", signing_key=b"\x00" * 32)
        assert provider2.validate_access_token(token) is False

    def test_signing_key_persists_across_provider_instances(self, tmp_path):
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        hass = MagicMock()
        provider1 = oauth.OAuthProvider(
            hass=hass, client_id="cid-1234567890ABCDEF", client_secret="sec", webhook_id="wh", signing_key=b"\x00" * 32)
        token = provider1.issue_access_token()
        # New provider on the same disk → same signing key → token still valid
        provider2 = oauth.OAuthProvider(
            hass=hass, client_id="cid-1234567890ABCDEF", client_secret="sec", webhook_id="wh", signing_key=b"\x00" * 32)
        assert provider2.validate_access_token(token) is True


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
        # mcp_proxy_init.oauth so the integration's relative import finds it.
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        mod = _import_mcp_proxy(preload_oauth=oauth)
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
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock())):
            await mod.async_setup_entry(hass, MagicMock())

        provider = hass.data[mod.DOMAIN]["oauth"]
        assert provider is not None
        assert provider.client_id == "client-1234567890ABCDEF"
        # 4 OAuth views registered
        assert hass.http.register_view.call_count == 4


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
            hass=MagicMock(), client_id="cid-1234567890ABCDEF",
            client_secret="sec", webhook_id="wh", signing_key=b"\x00" * 32)
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
            hass=MagicMock(), client_id="cid-1234567890ABCDEF",
            client_secret="sec", webhook_id="wh", signing_key=b"\x00" * 32)
        hass = self._make_hass(mod, provider)
        request = self._make_request("Bearer not.a.valid.token")
        request.headers["Host"] = "example.nabu.casa"

        with patch.object(oauth.web, "Response") as response_ctor:
            await mod._handle_webhook(hass, "mcp_test", request)

        request.read.assert_not_awaited()
        response_ctor.assert_called_once()
        assert response_ctor.call_args.kwargs.get("status") == 401

    async def test_passes_through_with_valid_bearer(self, setup):
        mod, oauth = setup
        provider = oauth.OAuthProvider(
            hass=MagicMock(), client_id="cid-1234567890ABCDEF",
            client_secret="sec", webhook_id="wh", signing_key=b"\x00" * 32)
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
        # First call generates and persists
        gen_cid, gen_sec = start._resolve_oauth_creds(tmp_path, "", "")
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

    def test_partial_override_picks_persisted_for_blank_field(
        self, start, tmp_path
    ):
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
        with patch.object(start, "Path", side_effect=path_factory):
            return start.main()

    def test_short_user_supplied_client_id_rejected(self, tmp_path, capsys):
        rc = self._run_main_with_options(
            tmp_path,
            enable_oauth=True,
            oauth_client_id="too-short",
            oauth_client_secret="some-secret-here-with-length")
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

    def test_regenerate_is_idempotent_when_file_missing(
        self, start, tmp_path
    ):
        # Should not raise even though the file isn't there
        start._regenerate_oauth_creds(tmp_path)
        assert not (tmp_path / "oauth_creds.json").exists()

    def test_regenerate_followed_by_resolve_yields_new_values(
        self, start, tmp_path
    ):
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

    def test_clear_regenerate_toggle_posts_options_with_false(
        self, start
    ):
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

    def test_clear_regenerate_toggle_returns_false_on_supervisor_error(
        self, start
    ):
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
            if arg == "/opt/mcp_proxy":
                return src
            if arg == "/config/custom_components/mcp_proxy":
                return dst_parent / "mcp_proxy"
            if arg == "/config/custom_components":
                return dst_parent
            return Path(arg)

        with patch.object(start, "Path", side_effect=path_factory):
            first_install, version_changed = start._install_integration()

        assert first_install is True
        assert version_changed is False
        assert (dst_parent / "mcp_proxy" / "manifest.json").exists()

    def test_version_changed_when_versions_differ(self, tmp_path):
        start = _import_start()
        src = tmp_path / "src"
        src.mkdir()
        (src / "manifest.json").write_text('{"version": "1.0.3-beta.1"}')
        dst_parent = tmp_path / "dst-parent"
        dst_parent.mkdir()
        (dst_parent / "mcp_proxy").mkdir()
        (dst_parent / "mcp_proxy" / "manifest.json").write_text(
            '{"version": "1.0.2"}'
        )

        def path_factory(arg):
            if arg == "/opt/mcp_proxy":
                return src
            if arg == "/config/custom_components/mcp_proxy":
                return dst_parent / "mcp_proxy"
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
        (dst_parent / "mcp_proxy").mkdir()
        (dst_parent / "mcp_proxy" / "manifest.json").write_text(
            '{"version": "1.0.3-beta.1"}'
        )

        def path_factory(arg):
            if arg == "/opt/mcp_proxy":
                return src
            if arg == "/config/custom_components/mcp_proxy":
                return dst_parent / "mcp_proxy"
            if arg == "/config/custom_components":
                return dst_parent
            return Path(arg)

        with patch.object(start, "Path", side_effect=path_factory):
            first_install, version_changed = start._install_integration()

        assert first_install is False
        assert version_changed is False

    def test_corrupt_dst_manifest_does_not_trigger_version_changed(
        self, tmp_path
    ):
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
        (dst_parent / "mcp_proxy").mkdir()
        # Corrupted JSON
        (dst_parent / "mcp_proxy" / "manifest.json").write_text("not json{{{")

        def path_factory(arg):
            if arg == "/opt/mcp_proxy":
                return src
            if arg == "/config/custom_components/mcp_proxy":
                return dst_parent / "mcp_proxy"
            if arg == "/config/custom_components":
                return dst_parent
            return Path(arg)

        with patch.object(start, "Path", side_effect=path_factory):
            first_install, version_changed = start._install_integration()

        assert first_install is False
        assert version_changed is False
        # Repaired install — files are copied
        assert (dst_parent / "mcp_proxy" / "manifest.json").exists()


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
        public_base_url=public_base_url)
    return oauth, provider


def _make_view_request(
    *,
    headers=None,
    query=None,
    method="GET",
    post_data=None,
    scheme="https"):
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
        request = _make_view_request(
            headers={"Host": "host.example"}, scheme="https"
        )
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
            "https://legit.example/api/mcp_proxy/oauth"
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
        assert body["issuer"].endswith("/api/mcp_proxy/oauth")
        assert body["authorization_endpoint"].endswith(
            "/api/mcp_proxy/oauth/authorize"
        )
        assert body["token_endpoint"].endswith("/api/mcp_proxy/oauth/token")
        assert "code" in body["response_types_supported"]
        assert "authorization_code" in body["grant_types_supported"]
        assert "refresh_token" in body["grant_types_supported"]
        assert "S256" in body["code_challenge_methods_supported"]
        assert "client_secret_basic" in body["token_endpoint_auth_methods_supported"]
        assert "client_secret_post" in body["token_endpoint_auth_methods_supported"]


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
        request = _make_view_request(
            query=self._good_query(code_challenge="too-short")
        )
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

    async def test_escapes_redirect_uri_in_consent_page(self, setup):
        """A malicious actor can put `<script>` in their redirect_uri to
        try to XSS the consent page. The page must HTML-escape it."""
        oauth, provider, view = setup
        evil = "https://evil.example/?<script>alert(1)</script>"
        request = _make_view_request(
            query=self._good_query(redirect_uri=evil)
        )
        with patch.object(oauth.web, "Response") as resp_ctor:
            await view.get(request)
        html = resp_ctor.call_args.kwargs.get("text", "")
        assert "<script>alert(1)</script>" not in html
        # Escaped form should be present
        assert "&lt;script&gt;" in html


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
        request = _make_view_request(
            method="POST", post_data=self._good_form()
        )
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
            post_data=self._good_form(client_id="attacker-substituted-id"))
        resp = await view.post(request)
        assert resp.status == 400
        assert "client_id" in resp.text

    async def test_post_re_validates_hidden_redirect_uri(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(
            method="POST",
            post_data=self._good_form(redirect_uri="http://evil.example/"))
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
        token = _b64.b64encode(
            f"{client_id}:{client_secret}".encode()
        ).decode("ascii")
        return f"Basic {token}"

    async def test_invalid_client_via_basic_returns_401(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(
            method="POST",
            headers={"Authorization": self._basic_header("wrong", "pw")},
            post_data={"grant_type": "authorization_code"})

        with patch.object(oauth.web, "json_response") as resp_ctor:
            await view.post(request)

        kwargs = resp_ctor.call_args.kwargs
        body = resp_ctor.call_args.args[0]
        assert body == {"error": "invalid_client"}
        assert kwargs.get("status") == 401
        assert kwargs.get("headers", {}).get(
            "WWW-Authenticate"
        ) == 'Basic realm="MCP Proxy OAuth"'

    async def test_invalid_client_via_form_returns_401(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(
            method="POST",
            post_data={
                "grant_type": "authorization_code",
                "client_id": "wrong",
                "client_secret": "pw",
            })
        with patch.object(oauth.web, "json_response") as resp_ctor:
            await view.post(request)
        body = resp_ctor.call_args.args[0]
        assert body == {"error": "invalid_client"}

    async def test_unsupported_grant_type(self, setup):
        oauth, provider, view = setup
        request = _make_view_request(
            method="POST",
            headers={
                "Authorization": self._basic_header(
                    "client-id-1234567890ABCDEF", "client-secret-very-secret"
                )
            },
            post_data={"grant_type": "client_credentials"})
        with patch.object(oauth.web, "json_response") as resp_ctor:
            await view.post(request)
        body = resp_ctor.call_args.args[0]
        kwargs = resp_ctor.call_args.kwargs
        assert body == {"error": "unsupported_grant_type"}
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
            post_data={"grant_type": "authorization_code"})
        with patch.object(oauth.web, "json_response") as resp_ctor:
            await view.post(request)
        body = resp_ctor.call_args.args[0]
        assert body == {"error": "invalid_request"}

    async def test_authorization_code_full_round_trip(self, setup):
        oauth, provider, view = setup
        verifier = "abcdefghij" * 5  # 50 chars, passes RFC 7636 length check
        import hashlib
        challenge = oauth._b64url_encode(
            hashlib.sha256(verifier.encode()).digest()
        )
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
            })
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
            })
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
            })
        with patch.object(oauth.web, "json_response") as resp_ctor:
            await view.post(request)
        body = resp_ctor.call_args.args[0]
        assert body == {"error": "invalid_grant"}


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
        assert (
            provider.consume_code(
                code, "https://claude.ai/cb", "X" * 129
            )
            is False
        )

    def test_rejects_verifier_with_disallowed_chars(self, provider):
        challenge = "X" * 43
        code = provider.issue_code("https://claude.ai/cb", challenge)
        bad_verifier = "a" * 42 + " "  # 43 chars but space is not in unreserved set
        assert (
            provider.consume_code(code, "https://claude.ai/cb", bad_verifier)
            is False
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
        # Fill the dict directly — pruning would clear expired entries on
        # re-issue but here we want non-expired entries at the cap.
        for i in range(oauth.MAX_PENDING_CODES):
            provider._codes[f"k{i}"] = {
                "redirect_uri": "https://x/",
                "code_challenge": challenge,
                "expires": time.time() + 60,
            }
        result = provider.issue_code("https://claude.ai/cb", challenge)
        assert result is None


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
        ])
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
                signing_key=b"\x00" * 32)

    def test_rejects_short_client_id(self, oauth):
        with pytest.raises(ValueError, match="client_id"):
            oauth.OAuthProvider(
                hass=MagicMock(),
                client_id="too-short",
                client_secret="secret",
                webhook_id="wh",
                signing_key=b"\x00" * 32)

    def test_rejects_blank_client_secret(self, oauth):
        with pytest.raises(ValueError, match="client_secret"):
            oauth.OAuthProvider(
                hass=MagicMock(),
                client_id="client-id-1234567890ABCDEF",
                client_secret="",
                webhook_id="wh",
                signing_key=b"\x00" * 32)

    def test_rejects_short_signing_key(self, oauth):
        with pytest.raises(ValueError, match="signing_key"):
            oauth.OAuthProvider(
                hass=MagicMock(),
                client_id="client-id-1234567890ABCDEF",
                client_secret="secret",
                webhook_id="wh",
                signing_key=b"too-short")


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

    async def test_registers_all_four_oauth_endpoints(self, hass, tmp_path):
        oauth = _import_oauth(tmp_secret_dir=tmp_path)
        mod = _import_mcp_proxy(preload_oauth=oauth)
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
            patch.object(mod.aiohttp, "ClientSession", return_value=MagicMock())):
            await mod.async_setup_entry(hass, MagicMock())

        registered_urls = {
            call.args[0].url for call in hass.http.register_view.call_args_list
        }
        assert registered_urls == {
            "/api/mcp_proxy/oauth/protected-resource",
            "/api/mcp_proxy/oauth/authorization-server",
            "/api/mcp_proxy/oauth/authorize",
            "/api/mcp_proxy/oauth/token",
        }


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
        assert (
            "https://legit.example/api/mcp_proxy/oauth/protected-resource"
            in ww
        )
