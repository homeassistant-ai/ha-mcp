"""Test Home Assistant add-on structure and configuration."""

import os
import stat
import yaml
import pytest


ADDON_DIR = "homeassistant-addon"


class TestAddonStructure:
    """Verify add-on meets Home Assistant requirements."""

    def test_required_files_exist(self):
        """Check all required add-on files are present."""
        required_files = [
            "config.yaml",
            "Dockerfile",
            "build.yaml",
            "run.sh",
            "README.md",
            "DOCS.md",
        ]
        for file in required_files:
            path = os.path.join(ADDON_DIR, file)
            assert os.path.exists(path), f"Missing required file: {file}"

    def test_config_yaml_valid(self):
        """Verify config.yaml is valid YAML with required fields."""
        with open(f"{ADDON_DIR}/config.yaml") as f:
            config = yaml.safe_load(f)

        required_fields = ["name", "description", "version", "slug", "arch"]
        for field in required_fields:
            assert field in config, f"Missing required field: {field}"

        # Verify essential configurations
        assert config["stdin"] is True, "stdin must be true for MCP"
        assert config["hassio_api"] is True, "hassio_api required for Supervisor"
        assert config["homeassistant_api"] is True, "homeassistant_api required"

        # Verify architectures
        expected_archs = ["amd64", "aarch64", "armhf", "armv7", "i386"]
        assert all(arch in config["arch"] for arch in expected_archs)

    def test_build_yaml_valid(self):
        """Verify build.yaml contains proper base images."""
        with open(f"{ADDON_DIR}/build.yaml") as f:
            build = yaml.safe_load(f)

        assert "build_from" in build
        for arch in ["amd64", "aarch64", "armhf", "armv7", "i386"]:
            assert arch in build["build_from"]
            assert "base-python:3.11" in build["build_from"][arch]

    def test_run_script_executable(self):
        """Verify run.sh has executable permissions."""
        run_sh = f"{ADDON_DIR}/run.sh"
        st = os.stat(run_sh)
        assert st.st_mode & stat.S_IXUSR, "run.sh must be executable"

    def test_run_script_has_shebang(self):
        """Verify run.sh has proper shebang."""
        with open(f"{ADDON_DIR}/run.sh") as f:
            first_line = f.readline()
        assert first_line.startswith("#!"), "run.sh missing shebang"
