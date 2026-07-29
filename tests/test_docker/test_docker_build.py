"""Test Docker image builds successfully and contains expected components."""

import subprocess

DATA_DIR = "/home/mcpuser/.ha-mcp"


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

    def test_mcpuser_ids_are_pinned(self):
        """Verify the built image really hands ``mcpuser`` UID/GID 999.

        A volume records numeric ownership, not names, so the IDs have to stay
        put: an existing ha-mcp-data volume owned by the old UID would become
        unwritable after an image update, resurrecting the tmpdir
        fallback of issue #2078. The Dockerfile pins the IDs to stop them
        drifting; this test guards the pin itself — it fails if the explicit
        ``-u``/``-g`` flags are dropped and allocation goes back to whatever
        ``-r`` picks, or if a future base image shifts them some other way.
        Changing the pinned value is a deliberate act that must update both
        sides, and doing so breaks existing volumes — see the Dockerfile.
        """
        result = subprocess.run(
            ["docker", "run", "--rm", "ha-mcp-test", "id", "-u", "mcpuser"],
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "999", f"unexpected UID: {result.stdout!r}"

        result = subprocess.run(
            ["docker", "run", "--rm", "ha-mcp-test", "id", "-g", "mcpuser"],
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "999", f"unexpected GID: {result.stdout!r}"

    def test_data_dir_exists_and_is_owned_by_mcpuser(self):
        """Verify ``~/.ha-mcp`` ships in the image owned by ``mcpuser``.

        This is the mount point the docs tell Docker users to bind a volume
        to. It has to exist in the image: Docker seeds a fresh named volume
        from the image directory at the mount point (ownership included), but
        creates the directory root-owned when the image has nothing there.
        Root-owned means ``mcpuser`` can't write, so ``get_data_dir()`` warns
        and falls back to a tmpdir, and settings vanish on restart —
        issue #2078.
        """
        result = subprocess.run(
            ["docker", "run", "--rm", "ha-mcp-test", "stat", "-c", "%U", DATA_DIR],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{DATA_DIR} missing from image: {result.stderr}"
        assert result.stdout.strip() == "mcpuser"

    def test_settings_survive_container_re_creation(self):
        """Verify data on a named volume outlives the container that wrote it.

        This is issue #2078 verbatim: write settings, throw the container
        away, start a new one, and the settings should still be there. Both
        containers use ``--rm``, so the second one only sees the file if the
        volume — not the discarded writable layer — is holding it. Uses a
        throwaway volume name so it never collides with a real deployment.
        """
        volume = "ha-mcp-test-persistence"
        marker = f"{DATA_DIR}/tool_config.json"
        # Start clean: a volume left over from an earlier run would keep its
        # old ownership and mask a regression in the image's mount point.
        subprocess.run(
            ["docker", "volume", "rm", "-f", volume], capture_output=True, text=True
        )
        try:
            write = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{volume}:{DATA_DIR}",
                    "ha-mcp-test",
                    "sh",
                    "-c",
                    f'echo persisted > "{marker}"',
                ],
                capture_output=True,
                text=True,
            )
            assert write.returncode == 0, (
                f"mcpuser cannot write to a named volume at {DATA_DIR}: {write.stderr}"
            )

            # Fresh container, same volume — the first one no longer exists.
            read = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{volume}:{DATA_DIR}",
                    "ha-mcp-test",
                    "cat",
                    marker,
                ],
                capture_output=True,
                text=True,
            )
            assert read.returncode == 0, (
                f"{marker} did not survive container re-creation: {read.stderr}"
            )
            assert read.stdout.strip() == "persisted"
        finally:
            subprocess.run(
                ["docker", "volume", "rm", "-f", volume],
                capture_output=True,
                text=True,
            )
