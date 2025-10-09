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

        required_fields = ["name", "description", "version", "slug", "arch", "image"]
        for field in required_fields:
            assert field in config, f"Missing required field: {field}"

        # Verify add-on uses independent versioning (not tied to MCP server version)
        assert config["version"] == "1.0.0", "Add-on should use version 1.0.0"

        # Verify essential configurations
        assert config["hassio_api"] is True, "hassio_api required for Supervisor"
        assert config["homeassistant_api"] is True, "homeassistant_api required"

        # Verify image field uses per-architecture naming
        assert config["image"] == "ghcr.io/homeassistant-ai/ha-mcp-addon-{arch}", \
            "image field must use per-architecture naming with {arch} placeholder"

        # Verify port configuration
        assert "ports" in config, "ports section required for HTTP transport"
        assert "9583/tcp" in config["ports"], "port 9583/tcp must be exposed"
        assert config["options"]["port"] == 9583, "default port should be 9583"

        # Verify architectures (only 64-bit platforms supported by uv image)
        expected_archs = ["amd64", "aarch64"]
        assert all(arch in config["arch"] for arch in expected_archs)

        # Verify 32-bit platforms are not included
        unsupported_archs = ["armhf", "armv7", "i386"]
        assert not any(arch in config["arch"] for arch in unsupported_archs), \
            "32-bit platforms not supported by uv base image"

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
