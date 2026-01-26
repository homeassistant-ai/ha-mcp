"""Test Home Assistant add-on startup and logging."""

import json
import time
from pathlib import Path

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy


@pytest.mark.slow
class TestAddonStartup:
    """Test add-on container startup behavior."""

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
        """Build and start the add-on container for testing."""
        # Build the Docker image from the Dockerfile
        dockerfile_path = Path("homeassistant-addon/Dockerfile")
        context_path = Path(".")

        container = (
            DockerContainer(image="ha-mcp-addon-test")
            .with_bind_ports(9583, 9583)
            .with_env("SUPERVISOR_TOKEN", "test-supervisor-token")
            .with_env("HOMEASSISTANT_URL", "http://supervisor/core")
            .with_volume_mapping(str(addon_config.parent), "/data", mode="ro")
        )

        # Build the image first (use as_posix() for Windows compatibility)
        container.get_docker_client().client.images.build(
            path=str(context_path),
            dockerfile=dockerfile_path.as_posix(),
            tag="ha-mcp-addon-test",
            rm=True,
            buildargs={
                "BUILD_VERSION": "1.0.0-test",
                "BUILD_ARCH": "amd64",
            },
        )

        return container

    def test_addon_startup_logs(self, container):
        """Test that add-on produces expected startup logs."""
        # Configure wait strategy for server actually starting
        container.waiting_for(
            LogMessageWaitStrategy("Starting MCP server").with_startup_timeout(30)
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

        # Build and start container
        dockerfile_path = Path("homeassistant-addon/Dockerfile")
        context_path = Path(".")

        container = (
            DockerContainer(image="ha-mcp-addon-test")
            .with_bind_ports(9583, 9583)
            .with_env("SUPERVISOR_TOKEN", "test-supervisor-token")
            .with_env("HOMEASSISTANT_URL", "http://supervisor/core")
            .with_volume_mapping(str(config_file.parent), "/data", mode="rw")
        )

        # Build if not already built (use as_posix() for Windows compatibility)
        try:
            container.get_docker_client().client.images.get("ha-mcp-addon-test")
        except Exception:
            container.get_docker_client().client.images.build(
                path=str(context_path),
                dockerfile=dockerfile_path.as_posix(),
                tag="ha-mcp-addon-test",
                rm=True,
                buildargs={
                    "BUILD_VERSION": "1.0.0-test",
                    "BUILD_ARCH": "amd64",
                },
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
        # Build and start container without SUPERVISOR_TOKEN
        dockerfile_path = Path("homeassistant-addon/Dockerfile")
        context_path = Path(".")

        container = (
            DockerContainer(image="ha-mcp-addon-test")
            .with_bind_ports(9583, 9583)
            .with_volume_mapping(str(addon_config.parent), "/data", mode="ro")
        )

        # Build if not already built (use as_posix() for Windows compatibility)
        try:
            container.get_docker_client().client.images.get("ha-mcp-addon-test")
        except Exception:
            container.get_docker_client().client.images.build(
                path=str(context_path),
                dockerfile=dockerfile_path.as_posix(),
                tag="ha-mcp-addon-test",
                rm=True,
                buildargs={
                    "BUILD_VERSION": "1.0.0-test",
                    "BUILD_ARCH": "amd64",
                },
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
