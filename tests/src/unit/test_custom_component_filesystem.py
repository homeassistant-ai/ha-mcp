"""Unit tests for ha_mcp_tools custom component file operations.

These tests focus on the pure Python utility functions that don't require
Home Assistant dependencies.
"""

import os
import sys
import pytest
from pathlib import Path
from unittest.mock import MagicMock
import tempfile
import shutil


# Mock the Home Assistant imports before importing the module
sys.modules['voluptuous'] = MagicMock()
sys.modules['homeassistant'] = MagicMock()
sys.modules['homeassistant.config_entries'] = MagicMock()
sys.modules['homeassistant.core'] = MagicMock()
sys.modules['homeassistant.helpers'] = MagicMock()
sys.modules['homeassistant.helpers.config_validation'] = MagicMock()


# Now we can import the functions
from custom_components.ha_mcp_tools import (
    _is_path_allowed_for_dir,
    _is_path_allowed_for_read,
    _mask_secrets_content,
)
from custom_components.ha_mcp_tools.const import ALLOWED_READ_DIRS, ALLOWED_WRITE_DIRS


class TestIsPathAllowedForDir:
    """Test _is_path_allowed_for_dir function."""

    def test_allows_www_directory(self, tmp_path):
        """Should allow paths in www/ directory."""
        assert _is_path_allowed_for_dir(tmp_path, "www/", ALLOWED_READ_DIRS) is True
        assert _is_path_allowed_for_dir(tmp_path, "www/test.css", ALLOWED_READ_DIRS) is True
        assert _is_path_allowed_for_dir(tmp_path, "www/subdir/test.js", ALLOWED_READ_DIRS) is True

    def test_allows_themes_directory(self, tmp_path):
        """Should allow paths in themes/ directory."""
        assert _is_path_allowed_for_dir(tmp_path, "themes/", ALLOWED_READ_DIRS) is True
        assert _is_path_allowed_for_dir(tmp_path, "themes/dark.yaml", ALLOWED_READ_DIRS) is True

    def test_allows_custom_templates_directory(self, tmp_path):
        """Should allow paths in custom_templates/ directory."""
        assert _is_path_allowed_for_dir(tmp_path, "custom_templates/", ALLOWED_READ_DIRS) is True
        assert _is_path_allowed_for_dir(tmp_path, "custom_templates/test.jinja2", ALLOWED_READ_DIRS) is True

    def test_blocks_config_root_files(self, tmp_path):
        """Should block access to files in config root (not in allowed dirs)."""
        assert _is_path_allowed_for_dir(tmp_path, "configuration.yaml", ALLOWED_READ_DIRS) is False
        assert _is_path_allowed_for_dir(tmp_path, "secrets.yaml", ALLOWED_READ_DIRS) is False

    def test_blocks_path_traversal_with_dotdot(self, tmp_path):
        """Should block path traversal with '..'."""
        assert _is_path_allowed_for_dir(tmp_path, "../etc/passwd", ALLOWED_READ_DIRS) is False
        assert _is_path_allowed_for_dir(tmp_path, "www/../secrets.yaml", ALLOWED_READ_DIRS) is False
        assert _is_path_allowed_for_dir(tmp_path, "www/../../etc/passwd", ALLOWED_READ_DIRS) is False

    def test_blocks_absolute_paths(self, tmp_path):
        """Should block absolute paths."""
        assert _is_path_allowed_for_dir(tmp_path, "/etc/passwd", ALLOWED_READ_DIRS) is False
        assert _is_path_allowed_for_dir(tmp_path, "/www/test.css", ALLOWED_READ_DIRS) is False

    def test_blocks_storage_directory(self, tmp_path):
        """Should block .storage directory."""
        assert _is_path_allowed_for_dir(tmp_path, ".storage/", ALLOWED_READ_DIRS) is False
        assert _is_path_allowed_for_dir(tmp_path, ".storage/auth", ALLOWED_READ_DIRS) is False

    def test_blocks_custom_components_directory(self, tmp_path):
        """Should block custom_components directory for writes."""
        assert _is_path_allowed_for_dir(tmp_path, "custom_components/", ALLOWED_WRITE_DIRS) is False


class TestIsPathAllowedForRead:
    """Test _is_path_allowed_for_read function."""

    def test_allows_configuration_yaml(self, tmp_path):
        """Should allow reading configuration.yaml."""
        assert _is_path_allowed_for_read(tmp_path, "configuration.yaml") is True

    def test_allows_automations_yaml(self, tmp_path):
        """Should allow reading automations.yaml."""
        assert _is_path_allowed_for_read(tmp_path, "automations.yaml") is True

    def test_allows_scripts_yaml(self, tmp_path):
        """Should allow reading scripts.yaml."""
        assert _is_path_allowed_for_read(tmp_path, "scripts.yaml") is True

    def test_allows_scenes_yaml(self, tmp_path):
        """Should allow reading scenes.yaml."""
        assert _is_path_allowed_for_read(tmp_path, "scenes.yaml") is True

    def test_allows_secrets_yaml(self, tmp_path):
        """Should allow reading secrets.yaml (content will be masked)."""
        assert _is_path_allowed_for_read(tmp_path, "secrets.yaml") is True

    def test_allows_home_assistant_log(self, tmp_path):
        """Should allow reading home-assistant.log."""
        assert _is_path_allowed_for_read(tmp_path, "home-assistant.log") is True

    def test_allows_www_files(self, tmp_path):
        """Should allow reading files in www/ directory."""
        assert _is_path_allowed_for_read(tmp_path, "www/test.css") is True
        assert _is_path_allowed_for_read(tmp_path, "www/subdir/test.js") is True

    def test_allows_themes_files(self, tmp_path):
        """Should allow reading files in themes/ directory."""
        assert _is_path_allowed_for_read(tmp_path, "themes/dark.yaml") is True

    def test_allows_packages_yaml(self, tmp_path):
        """Should allow reading packages/*.yaml files."""
        assert _is_path_allowed_for_read(tmp_path, "packages/lights.yaml") is True

    def test_allows_custom_components_py_files(self, tmp_path):
        """Should allow reading custom_components/**/*.py files."""
        assert _is_path_allowed_for_read(tmp_path, "custom_components/my_integration/init.py") is True
        assert _is_path_allowed_for_read(tmp_path, "custom_components/my_integration/__init__.py") is True

    def test_blocks_path_traversal(self, tmp_path):
        """Should block path traversal attempts outside config dir."""
        assert _is_path_allowed_for_read(tmp_path, "../etc/passwd") is False
        # Note: www/../secrets.yaml normalizes to secrets.yaml which IS allowed
        # (secrets.yaml reading is permitted with content masking)
        # This is intentional - we block escaping the config dir, not internal traversal
        assert _is_path_allowed_for_read(tmp_path, "../../etc/passwd") is False

    def test_blocks_absolute_paths(self, tmp_path):
        """Should block absolute paths."""
        assert _is_path_allowed_for_read(tmp_path, "/etc/passwd") is False

    def test_blocks_storage_directory(self, tmp_path):
        """Should block .storage directory."""
        assert _is_path_allowed_for_read(tmp_path, ".storage/auth") is False

    def test_blocks_random_files(self, tmp_path):
        """Should block arbitrary files not in allowed list."""
        assert _is_path_allowed_for_read(tmp_path, "random_file.txt") is False
        assert _is_path_allowed_for_read(tmp_path, "deps/some_file") is False


class TestMaskSecretsContent:
    """Test _mask_secrets_content function."""

    def test_masks_simple_values(self):
        """Should mask simple key-value pairs."""
        content = """
api_key: supersecretapikey123
password: mypassword
token: abc123xyz
"""
        result = _mask_secrets_content(content)

        assert "supersecretapikey123" not in result
        assert "mypassword" not in result
        assert "abc123xyz" not in result
        assert "[MASKED]" in result

    def test_masks_quoted_values(self):
        """Should mask quoted values."""
        content = """
api_key: "supersecretapikey123"
password: 'mypassword'
"""
        result = _mask_secrets_content(content)

        assert "supersecretapikey123" not in result
        assert "mypassword" not in result
        assert "[MASKED]" in result

    def test_preserves_comments(self):
        """Should preserve comment lines."""
        content = """
# This is a comment about the API key
api_key: secret123

# Another comment
password: pass456
"""
        result = _mask_secrets_content(content)

        assert "# This is a comment about the API key" in result
        assert "# Another comment" in result
        assert "secret123" not in result
        assert "pass456" not in result

    def test_preserves_empty_lines(self):
        """Should preserve empty lines."""
        content = """
key1: value1

key2: value2
"""
        result = _mask_secrets_content(content)

        lines = result.split("\n")
        # Check that empty line is preserved
        assert "" in lines

    def test_preserves_key_names(self):
        """Should preserve key names but mask values."""
        content = """
api_key: secret123
password: pass456
token: tok789
"""
        result = _mask_secrets_content(content)

        assert "api_key:" in result
        assert "password:" in result
        assert "token:" in result
        assert "secret123" not in result
        assert "pass456" not in result
        assert "tok789" not in result

    def test_handles_indented_content(self):
        """Should handle indented content correctly."""
        content = """
  indented_key: indented_value
    more_indented: more_value
"""
        result = _mask_secrets_content(content)

        assert "indented_key:" in result
        assert "more_indented:" in result
        assert "indented_value" not in result
        assert "more_value" not in result


class TestFileOperationsIntegration:
    """Integration tests for file operations using a temp directory."""

    @pytest.fixture
    def config_dir(self):
        """Create a temporary config directory with test files."""
        temp_dir = tempfile.mkdtemp()
        config_path = Path(temp_dir)

        # Create www directory with files
        www_dir = config_path / "www"
        www_dir.mkdir()
        (www_dir / "test.css").write_text(".test { color: red; }")
        (www_dir / "test.js").write_text("console.log('test');")

        # Create themes directory
        themes_dir = config_path / "themes"
        themes_dir.mkdir()
        (themes_dir / "dark.yaml").write_text("dark:\n  primary-color: '#000'")

        # Create custom_templates directory
        templates_dir = config_path / "custom_templates"
        templates_dir.mkdir()
        (templates_dir / "test.jinja2").write_text("{{ value }}")

        # Create config files
        (config_path / "configuration.yaml").write_text("homeassistant:\n  name: Test")
        (config_path / "secrets.yaml").write_text("api_key: secret123\npassword: pass456")
        (config_path / "automations.yaml").write_text("- alias: Test\n  trigger: []")

        yield config_path

        # Cleanup
        shutil.rmtree(temp_dir)

    def test_list_www_directory(self, config_dir):
        """Should list files in www directory."""
        assert _is_path_allowed_for_dir(config_dir, "www/", ALLOWED_READ_DIRS)

        www_dir = config_dir / "www"
        files = list(www_dir.iterdir())
        file_names = [f.name for f in files]

        assert "test.css" in file_names
        assert "test.js" in file_names

    def test_read_allowed_file(self, config_dir):
        """Should read allowed files."""
        # www files are allowed
        assert _is_path_allowed_for_read(config_dir, "www/test.css")
        content = (config_dir / "www" / "test.css").read_text()
        assert ".test { color: red; }" in content

    def test_write_to_www_allowed(self, config_dir):
        """Should allow writing to www directory."""
        assert _is_path_allowed_for_dir(config_dir, "www/new_file.css", ALLOWED_WRITE_DIRS)

    def test_write_to_config_root_blocked(self, config_dir):
        """Should block writing to config root."""
        assert not _is_path_allowed_for_dir(config_dir, "configuration.yaml", ALLOWED_WRITE_DIRS)
        assert not _is_path_allowed_for_dir(config_dir, "new_file.yaml", ALLOWED_WRITE_DIRS)
