"""Test Docker image builds successfully and contains expected components."""

import subprocess


class TestDockerBuild:
    """Test standalone Docker deployment."""

    def test_dockerfile_builds_successfully(self):
        """Verify Dockerfile builds without errors."""
        result = subprocess.run(
            ["docker", "build", "-t", "ha-mcp-test", "."],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Build failed: {result.stderr}"

    def test_uv_is_installed(self):
        """Verify uv package manager is available in image."""
        result = subprocess.run(
            ["docker", "run", "--rm", "ha-mcp-test", "uv", "--version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "uv" in result.stdout.lower()

    def test_ha_mcp_command_exists(self):
        """Verify ha-mcp command is installed."""
        result = subprocess.run(
            ["docker", "run", "--rm", "ha-mcp-test", "which", "ha-mcp"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_runs_as_non_root_user(self):
        """Verify container runs as non-root user for security."""
        result = subprocess.run(
            ["docker", "run", "--rm", "ha-mcp-test", "whoami"],
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "mcpuser"

    def test_python_version(self):
        """Verify Python 3.11+ is installed."""
        result = subprocess.run(
            ["docker", "run", "--rm", "ha-mcp-test", "python", "--version"],
            capture_output=True,
            text=True,
        )
        assert "Python 3.1" in result.stdout
