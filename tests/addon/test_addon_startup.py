"""Test Home Assistant add-on startup and logging."""

import functools
import importlib.util
import json
import subprocess
import time
from pathlib import Path

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy


@functools.cache
def _load_addon_start():
    """Import homeassistant-addon/start.py as a module."""
    spec = importlib.util.spec_from_file_location(
        "addon_start",
        Path(__file__).parents[2] / "homeassistant-addon" / "start.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSecretPathValidation:
    """Unit tests for secret path validation logic."""

    @pytest.fixture(autouse=True)
    def addon(self):
        self.addon = _load_addon_start()

    def test_valid_path_accepted(self, tmp_path):
        secret_file = tmp_path / "secret_path.txt"
        secret_file.write_text("/private_abc123")
        result = self.addon.get_or_create_secret_path(tmp_path)
        assert result == "/private_abc123"

    def test_url_in_file_triggers_regeneration(self, tmp_path):
        secret_file = tmp_path / "secret_path.txt"
        secret_file.write_text("https://192.168.1.18:9583/private_abc123")
        result = self.addon.get_or_create_secret_path(tmp_path)
        assert result.startswith("/private_")
        assert "://" not in result
        assert secret_file.read_text() == result

    def test_empty_file_triggers_regeneration(self, tmp_path):
        secret_file = tmp_path / "secret_path.txt"
        secret_file.write_text("")
        result = self.addon.get_or_create_secret_path(tmp_path)
        assert result.startswith("/private_")

    def test_url_custom_path_triggers_regeneration(self, tmp_path):
        result = self.addon.get_or_create_secret_path(
            tmp_path, custom_path="http://attacker.example.com/x"
        )
        assert result.startswith("/private_")
        assert "://" not in result

    def test_valid_custom_path_used(self, tmp_path):
        result = self.addon.get_or_create_secret_path(
            tmp_path, custom_path="my_custom_secret"
        )
        assert result == "/my_custom_secret"

    def test_no_secret_file_generates_new_path(self, tmp_path):
        result = self.addon.get_or_create_secret_path(tmp_path)
        assert result.startswith("/private_")
        assert (tmp_path / "secret_path.txt").read_text() == result

    def test_whitespace_custom_path_falls_through_to_stored(self, tmp_path):
        (tmp_path / "secret_path.txt").write_text("/private_stored")
        result = self.addon.get_or_create_secret_path(tmp_path, custom_path="   ")
        assert result == "/private_stored"

    def test_is_valid_secret_path(self):
        assert self.addon._is_valid_secret_path("/private_abc") is True
        assert self.addon._is_valid_secret_path("/mysecrt") is True  # exactly 8 chars
        assert (
            self.addon._is_valid_secret_path("/custom") is False
        )  # 7 chars — too short
        assert self.addon._is_valid_secret_path("/short") is False  # too short
        assert self.addon._is_valid_secret_path("https://example.com/x") is False
        assert self.addon._is_valid_secret_path("/https://evil.com") is False
        assert self.addon._is_valid_secret_path("no-leading-slash") is False
        assert self.addon._is_valid_secret_path("") is False


class TestSkillsAsToolsMigration:
    """Unit tests for one-time enable_skills_as_tools default migration.

    Background: commit 7e3f5c1 set enable_skills_as_tools to True by default,
    but a later config refactor accidentally reverted it to False. This
    migration flips it back on once for existing users who have False stored,
    then respects their choice on subsequent boots.
    """

    MARKER_NAME = ".skills_as_tools_default_migration_v1"

    @pytest.fixture(autouse=True)
    def addon(self):
        self.addon = _load_addon_start()

    def _make_options(self, tmp_path, value):
        """Write an options.json with enable_skills_as_tools=value."""
        config_file = tmp_path / "options.json"
        with open(config_file, "w") as f:
            json.dump({"enable_skills_as_tools": value}, f)
        return config_file

    def test_migration_flips_stored_false_and_persists(self, tmp_path):
        """First boot after update: stored False gets forced to True and
        persisted to options.json so the UI reflects the new value."""
        config_file = self._make_options(tmp_path, False)

        result = self.addon.migrate_skills_as_tools_default(
            data_dir=tmp_path,
            config_file=config_file,
            stored_value=False,
        )

        assert result is True
        assert (tmp_path / self.MARKER_NAME).exists()
        with open(config_file) as f:
            assert json.load(f)["enable_skills_as_tools"] is True

    def test_migration_respects_marker_when_exists(self, tmp_path):
        """After migration has run, respect the user's stored value even if
        it is False (user deliberately toggled it off)."""
        config_file = self._make_options(tmp_path, False)
        (tmp_path / self.MARKER_NAME).touch()

        result = self.addon.migrate_skills_as_tools_default(
            data_dir=tmp_path,
            config_file=config_file,
            stored_value=False,
        )

        assert result is False
        # Marker should still exist; options.json untouched.
        assert (tmp_path / self.MARKER_NAME).exists()
        with open(config_file) as f:
            assert json.load(f)["enable_skills_as_tools"] is False

    def test_migration_creates_marker_when_stored_true(self, tmp_path):
        """First boot, stored already True: no persistence needed, but the
        marker must still be created so a future user-initiated False is
        respected."""
        config_file = self._make_options(tmp_path, True)

        result = self.addon.migrate_skills_as_tools_default(
            data_dir=tmp_path,
            config_file=config_file,
            stored_value=True,
        )

        assert result is True
        assert (tmp_path / self.MARKER_NAME).exists()

    def test_migration_survives_missing_options_json(self, tmp_path):
        """If options.json does not exist, the migration still applies the
        runtime override and creates the marker — no crash."""
        config_file = tmp_path / "options.json"  # Does not exist

        result = self.addon.migrate_skills_as_tools_default(
            data_dir=tmp_path,
            config_file=config_file,
            stored_value=False,
        )

        assert result is True
        assert (tmp_path / self.MARKER_NAME).exists()

    def test_migration_survives_options_json_write_failure(self, tmp_path, monkeypatch):
        """If persisting to options.json fails (e.g., read-only filesystem),
        the runtime override is still applied and the marker is still
        created so the migration does not loop forever."""
        config_file = self._make_options(tmp_path, False)

        real_open = open

        def failing_open(path, mode="r", *args, **kwargs):
            if str(path).endswith("options.json") and "w" in mode:
                raise OSError("Simulated read-only filesystem")
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", failing_open)

        result = self.addon.migrate_skills_as_tools_default(
            data_dir=tmp_path,
            config_file=config_file,
            stored_value=False,
        )

        assert result is True
        assert (tmp_path / self.MARKER_NAME).exists()

    def test_migration_respects_marker_with_stored_true(self, tmp_path):
        """Marker exists, stored True: respect stored, no rewrite."""
        config_file = self._make_options(tmp_path, True)
        (tmp_path / self.MARKER_NAME).touch()

        result = self.addon.migrate_skills_as_tools_default(
            data_dir=tmp_path,
            config_file=config_file,
            stored_value=True,
        )

        assert result is True


IMAGE_TAG = "ha-mcp-addon-test"
DOCKERFILE = "homeassistant-addon/Dockerfile"


def _build_addon_image():
    """Build the addon test image via docker CLI (supports BuildKit)."""
    result = subprocess.run(
        [
            "docker",
            "build",
            "-t",
            IMAGE_TAG,
            "-f",
            DOCKERFILE,
            "--build-arg",
            "BUILD_VERSION=1.0.0-test",
            "--build-arg",
            "BUILD_ARCH=amd64",
            ".",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"Failed to build {IMAGE_TAG}:\n{result.stderr}")


@pytest.mark.slow
class TestAddonStartup:
    """Test add-on container startup behavior."""

    @pytest.fixture(autouse=True, scope="class")
    def build_image(self):
        """Build the addon image once before all tests in this class."""
        _build_addon_image()

    @pytest.fixture
    def addon_config(self, tmp_path):
        """Create a test add-on configuration file."""
        config = {
            "backup_hint": "normal",
            "secret_path": "",  # Auto-generate
        }
        config_file = tmp_path / "options.json"
        with open(config_file, "w") as f:
            json.dump(config, f)
        return config_file

    @pytest.fixture
    def container(self, addon_config):
        """Create the add-on container for testing (image built by build_image fixture)."""
        return (
            DockerContainer(image=IMAGE_TAG)
            .with_bind_ports(9583, 9583)
            .with_env("SUPERVISOR_TOKEN", "test-supervisor-token")
            .with_env("HOMEASSISTANT_URL", "http://supervisor/core")
            .with_volume_mapping(str(addon_config.parent), "/data", mode="rw")
        )

    def test_addon_startup_logs(self, container):
        """Test that add-on produces expected startup logs."""
        # Configure wait strategy for server actually starting
        container.waiting_for(
            LogMessageWaitStrategy("Uvicorn running on").with_startup_timeout(30)
        )

        # Start container
        container.start()

        try:
            # Get logs (both stdout and stderr)
            stdout, stderr = container.get_logs()
            logs = stdout.decode("utf-8") + "\n" + stderr.decode("utf-8")

            # Verify expected log messages
            assert "[INFO] Starting Home Assistant MCP Server..." in logs
            assert "[INFO] Backup hint mode: normal" in logs
            assert "[INFO] Generated new secret path with 128-bit entropy" in logs
            assert "[INFO] Home Assistant URL: http://supervisor/core" in logs
            assert "🔐 MCP Server URL: http://<home-assistant-ip>:9583/private_" in logs
            assert "Secret Path: /private_" in logs
            assert (
                "⚠️  IMPORTANT: Copy this exact URL - the secret path is required!"
                in logs
            )

            # Verify debug messages
            assert "[INFO] Importing ha_mcp module..." in logs
            assert "[INFO] Starting MCP server..." in logs

            # Verify FastMCP started successfully
            assert "Starting MCP server 'ha-mcp'" in logs
            assert "Uvicorn running on http://0.0.0.0:9583" in logs

            # Should not have errors
            assert "[ERROR] Failed to start MCP server:" not in logs

        finally:
            container.stop()

    def test_addon_startup_custom_secret_path(self, tmp_path):
        """Test that add-on uses custom secret path when configured."""
        # Create config with custom secret path
        config = {
            "backup_hint": "strong",
            "secret_path": "/my_custom_secret",
        }
        config_file = tmp_path / "options.json"
        with open(config_file, "w") as f:
            json.dump(config, f)

        container = (
            DockerContainer(image=IMAGE_TAG)
            .with_bind_ports(9583, 9583)
            .with_env("SUPERVISOR_TOKEN", "test-supervisor-token")
            .with_env("HOMEASSISTANT_URL", "http://supervisor/core")
            .with_volume_mapping(str(config_file.parent), "/data", mode="rw")
        )

        # Configure wait strategy
        container.waiting_for(
            LogMessageWaitStrategy("MCP Server URL:").with_startup_timeout(30)
        )

        container.start()

        try:
            # Get logs
            logs = container.get_logs()[0].decode("utf-8")

            # Verify custom config is used
            assert "[INFO] Backup hint mode: strong" in logs
            assert "[INFO] Using custom secret path from configuration" in logs
            assert (
                "🔐 MCP Server URL: http://<home-assistant-ip>:9583/my_custom_secret"
                in logs
            )
            assert "Secret Path: /my_custom_secret" in logs

        finally:
            container.stop()

    def test_addon_startup_missing_supervisor_token(self, addon_config):
        """Test that add-on exits with error when SUPERVISOR_TOKEN is missing."""
        container = (
            DockerContainer(image=IMAGE_TAG)
            .with_bind_ports(9583, 9583)
            .with_volume_mapping(str(addon_config.parent), "/data", mode="ro")
        )

        container.start()

        try:
            # Wait a bit for container to start and error
            time.sleep(3)

            # Get logs (both stdout and stderr)
            stdout, stderr = container.get_logs()
            logs = stdout.decode("utf-8") + "\n" + stderr.decode("utf-8")

            # Verify error message
            assert "[ERROR] SUPERVISOR_TOKEN not found! Cannot authenticate." in logs

        finally:
            container.stop()
