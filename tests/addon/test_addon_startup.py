"""Test Home Assistant add-on startup and logging."""

import json
import subprocess
import time

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

IMAGE_TAG = "ha-mcp-addon-test"
DOCKERFILE = "homeassistant-addon/Dockerfile"


def _build_addon_image():
    """Build the addon test image via docker CLI (supports BuildKit)."""
    result = subprocess.run(
        [
            "docker", "build",
            "-t", IMAGE_TAG,
            "-f", DOCKERFILE,
            "--build-arg", "BUILD_VERSION=1.0.0-test",
            "--build-arg", "BUILD_ARCH=amd64",
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
            assert "⚠️  IMPORTANT: Copy this exact URL - the secret path is required!" in logs

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
            assert "🔐 MCP Server URL: http://<home-assistant-ip>:9583/my_custom_secret" in logs
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
