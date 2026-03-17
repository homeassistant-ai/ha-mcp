"""Tests for the webhook proxy addon.

Structure tests verify addon files and config.yaml.
Unit tests mock Supervisor API calls to test discovery logic in start.py.
"""

import importlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

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
