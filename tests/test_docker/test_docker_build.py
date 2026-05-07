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

    def test_uv_not_in_runtime(self):
        """Verify uv is excluded from runtime image (multi-stage build)."""
        result = subprocess.run(
            ["docker", "run", "--rm", "ha-mcp-test", "which", "uv"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "uv should not be in the runtime image"

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

    def test_home_env_set_to_mcpuser(self):
        """Verify ``ENV HOME=/home/mcpuser`` is honored at runtime.

        Issue #1125 regression: without this, Docker leaves ``HOME=/`` under
        a ``USER`` directive (moby/moby#2968), so ``Path.home()`` resolves
        to ``/`` and ha-mcp tries to mkdir ``/.ha-mcp`` — fatal under
        ``read_only: true``. This test catches a future PR that
        accidentally removes the ``ENV HOME`` line.
        """
        result = subprocess.run(
            ["docker", "run", "--rm", "ha-mcp-test", "sh", "-c", "echo $HOME"],
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "/home/mcpuser"

    def test_home_dir_is_world_traversable(self):
        """Verify ``/home/mcpuser`` is mode 0755 (not the default 0700).

        Hardened-Docker users frequently set ``--user UID:GID`` overrides;
        if ``$HOME`` isn't world-traversable they get ``PermissionError``
        when ha-mcp stats a path under it. The chmod is the second half of
        the issue #1125 fix.
        """
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "ha-mcp-test",
                "stat",
                "-c",
                "%a",
                "/home/mcpuser",
            ],
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "755"
