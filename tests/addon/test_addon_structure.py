"""Test Home Assistant add-on structure and configuration."""

import os
import stat
import sys

import pytest
import yaml

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # Fallback for older Python


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

        # Verify add-on version matches package version (synced by semantic-release)
        with open("pyproject.toml", "rb") as f:
            pyproject = tomllib.load(f)
        expected_version = pyproject["project"]["version"]
        assert config["version"] == expected_version, \
            f"Add-on version {config['version']} should match package version {expected_version}"

        # Verify essential configurations
        assert config["hassio_api"] is True, "hassio_api required for Supervisor"
        assert config["homeassistant_api"] is True, "homeassistant_api required"

        # Verify image field uses per-architecture naming
        assert config["image"] == "ghcr.io/homeassistant-ai/ha-mcp-addon-{arch}", \
            "image field must use per-architecture naming with {arch} placeholder"

        # Verify port configuration (fixed internal port)
        assert "ports" in config, "ports section required for HTTP transport"
        assert "9583/tcp" in config["ports"], "port 9583/tcp must be exposed"

        # Verify secret_path configuration (optional advanced override)
        assert "secret_path" not in config["options"], \
            "secret_path should be optional and omitted so Supervisor treats it as advanced"
        assert "secret_path" in config["schema"], "schema must include secret_path field"
        assert config["schema"]["secret_path"] == "str?", \
            "secret_path schema should be optional string (str?)"

        # Verify backup_hint configuration
        assert "backup_hint" in config["options"], "options must include backup_hint field"
        assert config["options"]["backup_hint"] == "normal", "default backup_hint should be normal"
        assert config["schema"]["backup_hint"] == "list(strong|normal|weak|auto)", \
            "backup_hint schema must enumerate allowed values"

        # Verify architectures (only 64-bit platforms supported by uv image)
        expected_archs = ["amd64", "aarch64"]
        assert all(arch in config["arch"] for arch in expected_archs)

        # Verify 32-bit platforms are not included
        unsupported_archs = ["armhf", "armv7", "i386"]
        assert not any(arch in config["arch"] for arch in unsupported_archs), \
            "32-bit platforms not supported by uv base image"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix permissions not applicable on Windows")
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
