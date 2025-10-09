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
            "start.py",
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

    def test_start_script_executable(self):
        """Verify start.py has executable permissions."""
        start_py = f"{ADDON_DIR}/start.py"
        st = os.stat(start_py)
        assert st.st_mode & stat.S_IXUSR, "start.py must be executable"

    def test_start_script_has_shebang(self):
        """Verify start.py has proper shebang."""
        with open(f"{ADDON_DIR}/start.py") as f:
            first_line = f.readline()
        assert first_line.startswith("#!"), "start.py missing shebang"
        assert "python" in first_line.lower(), "start.py shebang must reference python"
