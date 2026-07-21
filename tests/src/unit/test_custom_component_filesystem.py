"""Unit tests for ha_mcp_tools custom component file operations.

These tests focus on the pure Python utility functions that don't require
Home Assistant dependencies.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Mock the Home Assistant imports before importing the module
sys.modules["voluptuous"] = MagicMock()
sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.components"] = MagicMock()
sys.modules["homeassistant.components.persistent_notification"] = MagicMock()
sys.modules["homeassistant.config"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
# setdefault (not =): other test modules stub homeassistant.core with a real
# `callback` identity function (production code decorates real closures with
# it - a clobbered auto-mock attribute would silently replace them). This
# file doesn't care what's already there, only that something importable is.
sys.modules.setdefault("homeassistant.core", MagicMock())
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.config_validation"] = MagicMock()
sys.modules["homeassistant.helpers.storage"] = MagicMock()
sys.modules["homeassistant.loader"] = MagicMock()


# Now we can import the functions
from custom_components.ha_mcp_tools import (  # noqa: E402
    _PACKAGE_DIR_CACHE,
    _decode_legacy_backup_name,
    _delete_file_sync,
    _detect_package_dirs,
    _dir_in_package_dir,
    _extract_yaml_subtree,
    _extract_yaml_views,
    _is_path_allowed_for_dir,
    _is_path_allowed_for_read,
    _is_within_config_dir,
    _list_files_sync,
    _list_legacy_backups_sync,
    _load_package_dir_markers,
    _mask_secrets_content,
    _migrate_legacy_backup_dir,
    _normalize_extra_dir,
    _package_dir_markers_cached,
    _package_folder_relative_to_config,
    _parse_and_validate_yaml_path,
    _path_in_package_dir,
    _read_file_sync,
    _read_legacy_backup_sync,
    _resolves_within,
    _violates_deny_floor,
    _volume_root_for,
    _write_file_sync,
)
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    ALLOWED_READ_DIRS,
    ALLOWED_VOLUME_ROOTS,
    ALLOWED_WRITE_DIRS,
    ALLOWED_YAML_KEYS,
)

from ._symlink_support import symlink_or_skip  # noqa: E402


class TestIsPathAllowedForDir:
    """Test _is_path_allowed_for_dir function."""

    def test_allows_www_directory(self, tmp_path):
        """Should allow paths in www/ directory."""
        assert _is_path_allowed_for_dir(tmp_path, "www/", ALLOWED_READ_DIRS) is True
        assert (
            _is_path_allowed_for_dir(tmp_path, "www/test.css", ALLOWED_READ_DIRS)
            is True
        )
        assert (
            _is_path_allowed_for_dir(tmp_path, "www/subdir/test.js", ALLOWED_READ_DIRS)
            is True
        )

    def test_allows_themes_directory(self, tmp_path):
        """Should allow paths in themes/ directory."""
        assert _is_path_allowed_for_dir(tmp_path, "themes/", ALLOWED_READ_DIRS) is True
        assert (
            _is_path_allowed_for_dir(tmp_path, "themes/dark.yaml", ALLOWED_READ_DIRS)
            is True
        )

    def test_allows_custom_templates_directory(self, tmp_path):
        """Should allow paths in custom_templates/ directory."""
        assert (
            _is_path_allowed_for_dir(tmp_path, "custom_templates/", ALLOWED_READ_DIRS)
            is True
        )
        assert (
            _is_path_allowed_for_dir(
                tmp_path, "custom_templates/test.jinja2", ALLOWED_READ_DIRS
            )
            is True
        )

    def test_blocks_config_root_files(self, tmp_path):
        """Should block access to files in config root (not in allowed dirs)."""
        assert (
            _is_path_allowed_for_dir(tmp_path, "configuration.yaml", ALLOWED_READ_DIRS)
            is False
        )
        assert (
            _is_path_allowed_for_dir(tmp_path, "secrets.yaml", ALLOWED_READ_DIRS)
            is False
        )

    def test_blocks_path_traversal_with_dotdot(self, tmp_path):
        """Should block path traversal with '..'."""
        assert (
            _is_path_allowed_for_dir(tmp_path, "../etc/passwd", ALLOWED_READ_DIRS)
            is False
        )
        assert (
            _is_path_allowed_for_dir(tmp_path, "www/../secrets.yaml", ALLOWED_READ_DIRS)
            is False
        )
        assert (
            _is_path_allowed_for_dir(
                tmp_path, "www/../../etc/passwd", ALLOWED_READ_DIRS
            )
            is False
        )

    def test_blocks_absolute_paths(self, tmp_path):
        """Should block absolute paths."""
        assert (
            _is_path_allowed_for_dir(tmp_path, "/etc/passwd", ALLOWED_READ_DIRS)
            is False
        )
        assert (
            _is_path_allowed_for_dir(tmp_path, "/www/test.css", ALLOWED_READ_DIRS)
            is False
        )

    def test_blocks_storage_directory(self, tmp_path):
        """Should block .storage directory."""
        assert (
            _is_path_allowed_for_dir(tmp_path, ".storage/", ALLOWED_READ_DIRS) is False
        )
        assert (
            _is_path_allowed_for_dir(tmp_path, ".storage/auth", ALLOWED_READ_DIRS)
            is False
        )

    def test_blocks_custom_components_directory(self, tmp_path):
        """Should block custom_components directory for writes."""
        assert (
            _is_path_allowed_for_dir(tmp_path, "custom_components/", ALLOWED_WRITE_DIRS)
            is False
        )

    def test_allows_dashboards_directory(self, tmp_path):
        """Should allow paths in dashboards/ directory (YAML-mode dashboards)."""
        assert (
            _is_path_allowed_for_dir(tmp_path, "dashboards/", ALLOWED_READ_DIRS) is True
        )
        assert (
            _is_path_allowed_for_dir(
                tmp_path, "dashboards/main.yaml", ALLOWED_READ_DIRS
            )
            is True
        )
        assert (
            _is_path_allowed_for_dir(tmp_path, "dashboards/", ALLOWED_WRITE_DIRS)
            is True
        )
        assert (
            _is_path_allowed_for_dir(
                tmp_path, "dashboards/main.yaml", ALLOWED_WRITE_DIRS
            )
            is True
        )

    def test_allows_blueprints_directory_read_only(self, tmp_path):
        """blueprints/ is listable/readable but NOT writable (issue #1965)."""
        # Read/list allowed
        assert (
            _is_path_allowed_for_dir(tmp_path, "blueprints/", ALLOWED_READ_DIRS) is True
        )
        assert (
            _is_path_allowed_for_dir(
                tmp_path, "blueprints/automation/author/thing.yaml", ALLOWED_READ_DIRS
            )
            is True
        )
        # Writes deliberately denied — blueprints is absent from ALLOWED_WRITE_DIRS
        assert (
            _is_path_allowed_for_dir(tmp_path, "blueprints/", ALLOWED_WRITE_DIRS)
            is False
        )
        assert (
            _is_path_allowed_for_dir(
                tmp_path, "blueprints/automation/author/thing.yaml", ALLOWED_WRITE_DIRS
            )
            is False
        )


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

    def test_allows_blueprints_files(self, tmp_path):
        """Should allow reading files under blueprints/ (issue #1965)."""
        assert (
            _is_path_allowed_for_read(
                tmp_path, "blueprints/automation/homeassistant/motion_light.yaml"
            )
            is True
        )
        assert (
            _is_path_allowed_for_read(tmp_path, "blueprints/script/author/thing.yaml")
            is True
        )

    def test_allows_custom_components_py_files(self, tmp_path):
        """Should allow reading custom_components/**/*.py files."""
        assert (
            _is_path_allowed_for_read(
                tmp_path, "custom_components/my_integration/init.py"
            )
            is True
        )
        assert (
            _is_path_allowed_for_read(
                tmp_path, "custom_components/my_integration/__init__.py"
            )
            is True
        )

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

    def test_allows_dashboards_yaml_files(self, tmp_path):
        """Should allow reading files under dashboards/ directory."""
        assert _is_path_allowed_for_read(tmp_path, "dashboards/main.yaml") is True
        assert _is_path_allowed_for_read(tmp_path, "dashboards/sub/nested.yaml") is True


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

    def test_mask_marker_reparses_as_a_scalar(self):
        """The masked text is itself re-parsed by read_file's yaml_path views,
        so the marker must be quoted: an unquoted ``[MASKED]`` is flow-sequence
        syntax and would come back as the list ``["MASKED"]``."""
        import io

        from custom_components.ha_mcp_tools.yaml_rt import make_yaml, yaml_jsonify

        result = _mask_secrets_content("api_key: secret123\n")

        reparsed = make_yaml().load(io.StringIO(result))
        assert yaml_jsonify(reparsed) == {"api_key": "[MASKED]"}

    def test_drops_comments_and_blank_lines(self):
        """The structural mask emits only ``key: "[MASKED]"`` lines. Comments and
        blank lines are intentionally not reproduced — they are not needed to
        show which keys exist, and dropping them avoids leaking a secret that a
        user pasted into a comment."""
        content = (
            "\n# comment about the API key\napi_key: secret123\n\npassword: pass456\n"
        )
        result = _mask_secrets_content(content)

        assert "secret123" not in result
        assert "pass456" not in result
        assert result == 'api_key: "[MASKED]"\npassword: "[MASKED]"'

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

    def test_nested_mapping_fully_masked(self):
        """A nested mapping is masked at its top-level key, hiding the whole
        subtree rather than exposing nested values."""
        result = _mask_secrets_content("outer:\n  inner_secret: value\n")

        assert "value" not in result
        assert result == 'outer: "[MASKED]"'

    def test_block_scalar_leaves_no_secret_bytes(self):
        """Core advisory PoC (GHSA-mc92-ww4q-6fg4): a block scalar's continuation
        lines have no colon and leaked verbatim under the old line-by-line regex."""
        content = (
            "backup_ssh_key: |\n"
            "  -----BEGIN OPENSSH PRIVATE KEY-----\n"
            "  b3BlbnNzaC1rZXktdjEAAAAA\n"
            "  -----END OPENSSH PRIVATE KEY-----\n"
            "api_password: hunter2\n"
        )
        result = _mask_secrets_content(content)

        assert "BEGIN OPENSSH" not in result
        assert "b3BlbnNzaC1rZXktdjEAAAAA" not in result
        assert "hunter2" not in result
        assert result == 'backup_ssh_key: "[MASKED]"\napi_password: "[MASKED]"'

    def test_empty_or_non_mapping_withheld(self):
        """Empty file (None) or a top-level list/scalar: nothing to mask
        key-wise, so the content is withheld rather than returned raw."""
        assert "[MASKED]" not in _mask_secrets_content("")
        assert "withheld" in _mask_secrets_content("").lower()
        assert "withheld" in _mask_secrets_content("- a\n- b\n").lower()

    def test_duplicate_keys_withheld(self):
        """ruamel raises DuplicateKeyError (a YAMLError subclass); the fix fails
        closed rather than leaking the raw text."""
        assert "withheld" in _mask_secrets_content("dup: 1\ndup: 2\n").lower()

    def test_custom_tag_does_not_crash(self):
        """The round-trip loader resolves HA-style custom tags instead of
        raising, so masking still produces a redacted key line."""
        assert _mask_secrets_content("foo: !secret bar\n") == 'foo: "[MASKED]"'

    def test_yaml_anchors_do_not_leak_dereferenced_secrets(self):
        """A secret defined once via an anchor and reused via aliases is
        dereferenced into every key by the loader; each must still be masked and
        the secret must not survive anywhere in the output."""
        content = "base_token: &tok 'secret123'\nprod: *tok\ndev: *tok\n"
        result = _mask_secrets_content(content)
        assert "secret123" not in result
        assert result == 'base_token: "[MASKED]"\nprod: "[MASKED]"\ndev: "[MASKED]"'


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
        (config_path / "secrets.yaml").write_text(
            "api_key: secret123\npassword: pass456"
        )
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
        assert _is_path_allowed_for_dir(
            config_dir, "www/new_file.css", ALLOWED_WRITE_DIRS
        )

    def test_write_to_config_root_blocked(self, config_dir):
        """Should block writing to config root."""
        assert not _is_path_allowed_for_dir(
            config_dir, "configuration.yaml", ALLOWED_WRITE_DIRS
        )
        assert not _is_path_allowed_for_dir(
            config_dir, "new_file.yaml", ALLOWED_WRITE_DIRS
        )


# ---------------------------------------------------------------------------
# Sync helpers — bundle blocking I/O for hass.async_add_executor_job offload.
# These run in the executor thread; the async handler formats the structured
# response from the returned dict (success keys or {"_error": <kind>}).
# ---------------------------------------------------------------------------


class TestListFilesSync:
    """Test _list_files_sync helper."""

    def test_returns_files_for_existing_directory(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "b.txt").write_text("world")
        sub = tmp_path / "sub"
        sub.mkdir()

        result = _list_files_sync(tmp_path, tmp_path, None)

        assert "_error" not in result
        names = [f["name"] for f in result["files"]]
        assert names == ["sub", "a.txt", "b.txt"]  # dirs first, then alpha
        a_entry = next(f for f in result["files"] if f["name"] == "a.txt")
        assert a_entry["size"] == 5
        assert a_entry["is_dir"] is False
        sub_entry = next(f for f in result["files"] if f["name"] == "sub")
        assert sub_entry["is_dir"] is True
        assert sub_entry["size"] == 0

    def test_returns_not_found_for_missing_directory(self, tmp_path):
        result = _list_files_sync(tmp_path / "missing", tmp_path, None)
        assert result == {"_error": "not_found"}

    def test_returns_not_a_dir_for_file_path(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("hello")
        result = _list_files_sync(f, tmp_path, None)
        assert result == {"_error": "not_a_dir"}

    def test_pattern_filters_files(self, tmp_path):
        (tmp_path / "a.yaml").write_text("a")
        (tmp_path / "b.yaml").write_text("b")
        (tmp_path / "c.txt").write_text("c")

        result = _list_files_sync(tmp_path, tmp_path, "*.yaml")

        names = sorted(f["name"] for f in result["files"])
        assert names == ["a.yaml", "b.yaml"]


class TestReadFileSync:
    """Test _read_file_sync helper."""

    def test_returns_content_for_existing_file(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_text("hello world")

        result = _read_file_sync(f)

        assert result["content"] == "hello world"
        assert result["size"] == 11
        assert "mtime" in result

    def test_returns_not_found_for_missing_file(self, tmp_path):
        result = _read_file_sync(tmp_path / "missing.txt")
        assert result == {"_error": "not_found"}

    def test_returns_not_a_file_for_directory(self, tmp_path):
        result = _read_file_sync(tmp_path)
        assert result == {"_error": "not_a_file"}

    def test_propagates_unicode_decode_error(self, tmp_path):
        f = tmp_path / "binary.bin"
        f.write_bytes(b"\xff\xfe\xfd")
        with pytest.raises(UnicodeDecodeError):
            _read_file_sync(f)


class TestWriteFileSync:
    """Test _write_file_sync helper."""

    def test_creates_new_file(self, tmp_path):
        target = tmp_path / "sub" / "x.txt"

        result = _write_file_sync(
            target, "hello", overwrite=False, create_dirs=True, config_dir=tmp_path
        )

        assert "_error" not in result
        assert result["is_new"] is True
        assert result["size"] == 5
        assert target.read_text() == "hello"

    def test_blocks_overwrite_when_disabled(self, tmp_path):
        target = tmp_path / "x.txt"
        target.write_text("original")

        result = _write_file_sync(
            target, "new", overwrite=False, create_dirs=False, config_dir=tmp_path
        )

        assert result == {"_error": "exists_no_overwrite"}
        assert target.read_text() == "original"

    def test_overwrites_when_allowed(self, tmp_path):
        target = tmp_path / "x.txt"
        target.write_text("original")

        result = _write_file_sync(
            target, "new", overwrite=True, create_dirs=False, config_dir=tmp_path
        )

        assert result["is_new"] is False
        assert target.read_text() == "new"

    def test_returns_no_parent_when_create_dirs_false(self, tmp_path):
        target = tmp_path / "missing_dir" / "x.txt"

        result = _write_file_sync(
            target, "hi", overwrite=False, create_dirs=False, config_dir=tmp_path
        )

        assert result["_error"] == "no_parent"
        assert result["parent"] == "missing_dir"

    def test_no_parent_reports_absolute_for_out_of_config_dir(self, tmp_path):
        # #1586: a missing parent on a HAOS sibling volume is NOT under the
        # config dir, so relative_to would raise — the parent is reported
        # absolute instead (mirrors _list_files_sync's volume handling).
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        volume = tmp_path / "share"
        volume.mkdir()
        target = volume / "missing" / "x.txt"

        result = _write_file_sync(
            target, "data", overwrite=False, create_dirs=False, config_dir=config_dir
        )

        assert result["_error"] == "no_parent"
        assert result["parent"] == str(volume / "missing")


class TestDeleteFileSync:
    """Test _delete_file_sync helper."""

    def test_deletes_existing_file(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_text("hello")

        result = _delete_file_sync(f)

        assert result == {"size": 5}
        assert not f.exists()

    def test_returns_not_found_for_missing_file(self, tmp_path):
        result = _delete_file_sync(tmp_path / "missing.txt")
        assert result == {"_error": "not_found"}

    def test_returns_not_a_file_for_directory(self, tmp_path):
        result = _delete_file_sync(tmp_path)
        assert result == {"_error": "not_a_file"}
        assert tmp_path.exists()


class TestMigrateLegacyBackupDir:
    """Test _migrate_legacy_backup_dir helper (GHSA-g39v-cvjh-8fpf)."""

    def test_no_legacy_dir_is_noop(self, tmp_path):
        """Returns (0, 0) and creates nothing when legacy dir is absent."""
        moved, failed = _migrate_legacy_backup_dir(tmp_path)
        assert (moved, failed) == (0, 0)
        assert not (tmp_path / ".ha_mcp_tools_backups").exists()
        assert not (tmp_path / "www" / "yaml_backups").exists()

    def test_moves_files_and_removes_legacy_dir(self, tmp_path):
        """Moves .bak files out of www/yaml_backups/ and removes the empty dir."""
        legacy = tmp_path / "www" / "yaml_backups"
        legacy.mkdir(parents=True)
        (legacy / "configuration.yaml.20260101_120000.bak").write_text("a: 1")
        (legacy / "packages_test.yaml.20260102_120000.bak").write_text("b: 2")

        moved, failed = _migrate_legacy_backup_dir(tmp_path)

        assert (moved, failed) == (2, 0)
        new_dir = tmp_path / ".ha_mcp_tools_backups"
        assert new_dir.is_dir()
        moved_names = sorted(p.name for p in new_dir.iterdir())
        assert moved_names == [
            "configuration.yaml.20260101_120000.bak",
            "packages_test.yaml.20260102_120000.bak",
        ]
        # Legacy dir should be removed once empty.
        assert not legacy.exists()

    def test_does_not_clobber_existing_backups(self, tmp_path):
        """Preserves an existing same-named file in the new dir."""
        legacy = tmp_path / "www" / "yaml_backups"
        legacy.mkdir(parents=True)
        new_dir = tmp_path / ".ha_mcp_tools_backups"
        new_dir.mkdir()

        (legacy / "x.bak").write_text("from legacy")
        (new_dir / "x.bak").write_text("already here")

        moved, failed = _migrate_legacy_backup_dir(tmp_path)

        assert (moved, failed) == (1, 0)
        assert (new_dir / "x.bak").read_text() == "already here"
        # Legacy file is renamed during migration so it isn't lost.
        assert (new_dir / "x.legacy.bak").read_text() == "from legacy"

    def test_collision_counter_when_legacy_suffix_taken(self, tmp_path):
        """If <name>.bak AND <name>.legacy.bak both exist, use .legacy1.bak."""
        legacy = tmp_path / "www" / "yaml_backups"
        legacy.mkdir(parents=True)
        new_dir = tmp_path / ".ha_mcp_tools_backups"
        new_dir.mkdir()

        (legacy / "x.bak").write_text("incoming")
        (new_dir / "x.bak").write_text("already here")
        (new_dir / "x.legacy.bak").write_text("older legacy")

        moved, failed = _migrate_legacy_backup_dir(tmp_path)

        assert (moved, failed) == (1, 0)
        # Both pre-existing files preserved.
        assert (new_dir / "x.bak").read_text() == "already here"
        assert (new_dir / "x.legacy.bak").read_text() == "older legacy"
        # New file lands at next free .legacyN suffix.
        assert (new_dir / "x.legacy1.bak").read_text() == "incoming"

    def test_leaves_legacy_dir_when_other_files_present(self, tmp_path):
        """Doesn't remove legacy dir if user dropped non-.bak files in it."""
        legacy = tmp_path / "www" / "yaml_backups"
        legacy.mkdir(parents=True)
        (legacy / "stray_subdir").mkdir()
        (legacy / "config.bak").write_text("data")
        (legacy / "notes.txt").write_text("user dropped this")

        moved, failed = _migrate_legacy_backup_dir(tmp_path)

        assert (moved, failed) == (1, 0)
        # Subdirectory and stray non-.bak file left in place.
        assert legacy.exists()
        assert (legacy / "stray_subdir").is_dir()
        assert (legacy / "notes.txt").read_text() == "user dropped this"

    def test_skips_symlinks(self, tmp_path):
        """Symlinks in legacy dir are not migrated (avoids surprise dereferencing)."""
        legacy = tmp_path / "www" / "yaml_backups"
        legacy.mkdir(parents=True)
        target = tmp_path / "elsewhere.bak"
        target.write_text("target content")
        symlink_or_skip(legacy / "link.bak", target)
        (legacy / "real.bak").write_text("real content")

        moved, failed = _migrate_legacy_backup_dir(tmp_path)

        assert (moved, failed) == (1, 0)
        new_dir = tmp_path / ".ha_mcp_tools_backups"
        assert (new_dir / "real.bak").read_text() == "real content"
        # Symlink left in place; legacy dir not removed because non-empty.
        assert (legacy / "link.bak").is_symlink()
        assert legacy.exists()

    def test_async_setup_entry_wires_migration_and_notification(self):
        """Source-level guard: the tools setup must call the migration helper
        and create a persistent notification referencing the GHSA. Brittle on
        purpose — this is a security regression guard, not a behavioral test.

        The tools setup body lives in ``_async_setup_tools_entry`` (the public
        ``async_setup_entry`` now dispatches on entry type to it).
        """
        import inspect

        from custom_components.ha_mcp_tools import _async_setup_tools_entry

        src = inspect.getsource(_async_setup_tools_entry)
        assert "_migrate_legacy_backup_dir" in src, (
            "tools setup must invoke the legacy-backup migration"
        )
        assert "persistent_notification.async_create" in src, (
            "tools setup must surface migration via persistent_notification"
        )
        assert "GHSA-g39v-cvjh-8fpf" in src, (
            "persistent notification must reference the security advisory"
        )


class TestDecodeLegacyBackupName:
    """_decode_legacy_backup_name is the best-effort inverse of the lossy
    pre-#1579 .bak naming (<safe_name>.<YYYYMMDD>_<HHMMSS>.bak)."""

    def test_configuration_yaml_unambiguous(self):
        out = _decode_legacy_backup_name("configuration.yaml.20260101_120000.bak")
        assert out == {
            "file_path": "configuration.yaml",
            "timestamp": "20260101_120000",
            "path_ambiguous": False,
        }

    def test_flat_package_unambiguous(self):
        out = _decode_legacy_backup_name("packages_lights.yaml.20260102_010203.bak")
        assert out["file_path"] == "packages/lights.yaml"
        assert out["timestamp"] == "20260102_010203"
        assert out["path_ambiguous"] is False

    def test_flat_theme_unambiguous(self):
        out = _decode_legacy_backup_name("themes_dark.yaml.20260102_010203.bak")
        assert out["file_path"] == "themes/dark.yaml"
        assert out["path_ambiguous"] is False

    def test_underscore_in_rest_is_flagged_ambiguous(self):
        # "packages_my_lights.yaml" could be packages/my_lights.yaml (literal _)
        # OR packages/my/lights.yaml (collapsed nested sep) — indistinguishable.
        out = _decode_legacy_backup_name("packages_my_lights.yaml.20260102_010203.bak")
        assert out["file_path"] == "packages/my_lights.yaml"
        assert out["path_ambiguous"] is True

    def test_unknown_flat_name_has_no_path(self):
        # A safe_name that is neither configuration.yaml nor a packages_/themes_
        # prefix can't be mapped back to an allowed write target.
        out = _decode_legacy_backup_name("automations.yaml.20260101_120000.bak")
        assert out["file_path"] is None
        assert out["timestamp"] == "20260101_120000"
        assert out["path_ambiguous"] is True

    def test_non_timestamped_name_does_not_decode(self):
        # Pre-fix www/yaml_backups names or migration-renamed .legacy.bak files
        # don't carry the timestamp suffix → undecodable, never auto-restored.
        for name in (
            "x.bak",
            "configuration.yaml.20260101_120000.legacy.bak",
            "random-file.bak",
        ):
            out = _decode_legacy_backup_name(name)
            assert out["file_path"] is None
            assert out["timestamp"] is None
            assert out["path_ambiguous"] is True


class TestListLegacyBackupsSync:
    """_list_legacy_backups_sync enumerates regular .bak files only."""

    def test_missing_dir_returns_empty(self, tmp_path):
        assert _list_legacy_backups_sync(tmp_path / ".ha_mcp_tools_backups") == []

    def test_lists_only_bak_files_skips_strays(self, tmp_path):
        d = tmp_path / ".ha_mcp_tools_backups"
        d.mkdir()
        (d / "configuration.yaml.20260101_120000.bak").write_text("a: 1")
        (d / "notes.txt").write_text("not a backup")
        (d / "subdir").mkdir()
        target = tmp_path / "outside.bak"
        target.write_text("x: 9")
        symlink_or_skip(d / "link.bak", target)

        out = _list_legacy_backups_sync(d)

        names = [b["filename"] for b in out]
        assert names == ["configuration.yaml.20260101_120000.bak"]
        entry = out[0]
        assert entry["file_path"] == "configuration.yaml"
        assert entry["path_ambiguous"] is False
        assert entry["timestamp"] == "20260101_120000"
        assert entry["size"] == len("a: 1")

    def test_sorted_newest_first(self, tmp_path):
        d = tmp_path / ".ha_mcp_tools_backups"
        d.mkdir()
        old = d / "configuration.yaml.20260101_120000.bak"
        new = d / "themes_dark.yaml.20260201_120000.bak"
        old.write_text("a: 1")
        new.write_text("b: 2")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))

        out = _list_legacy_backups_sync(d)

        assert [b["filename"] for b in out] == [new.name, old.name]


class TestReadLegacyBackupSync:
    """_read_legacy_backup_sync reads a single .bak file's text content."""

    def test_reads_content(self, tmp_path):
        f = tmp_path / "configuration.yaml.20260101_120000.bak"
        # newline pinned so the on-disk byte count matches len() on Windows
        # too (text mode would write CRLF and break the size assertion).
        f.write_text("a: 1\nb: 2\n", newline="\n")
        out = _read_legacy_backup_sync(f)
        assert out["content"] == "a: 1\nb: 2\n"
        assert out["size"] == len("a: 1\nb: 2\n")
        assert "mtime" in out

    def test_missing_file(self, tmp_path):
        out = _read_legacy_backup_sync(tmp_path / "nope.bak")
        assert out == {"_error": "not_found"}

    def test_directory_is_not_a_file(self, tmp_path):
        d = tmp_path / "adir.bak"
        d.mkdir()
        assert _read_legacy_backup_sync(d) == {"_error": "not_a_file"}

    def test_symlink_is_not_a_file(self, tmp_path):
        target = tmp_path / "real.bak"
        target.write_text("a: 1")
        link = tmp_path / "link.bak"
        symlink_or_skip(link, target)
        assert _read_legacy_backup_sync(link) == {"_error": "not_a_file"}


class TestLegacyBackupServiceWiring:
    """Source-level guards: the read-only legacy-backup services must stay
    registered, token-gated, and confined to .ha_mcp_tools_backups/."""

    def test_services_registered_and_unregistered(self):
        import inspect

        # The tools setup/unload bodies live in the ``_async_*_tools_entry``
        # helpers; the public entry points now dispatch on entry type to them.
        # The legacy-backup handlers are built by module-level builders.
        from custom_components.ha_mcp_tools import (
            _async_setup_tools_entry,
            _async_unload_tools_entry,
            _build_list_legacy_backups_handler,
            _build_read_legacy_backup_handler,
        )

        setup_src = inspect.getsource(_async_setup_tools_entry)
        assert "SERVICE_LIST_LEGACY_BACKUPS" in setup_src
        assert "SERVICE_READ_LEGACY_BACKUP" in setup_src
        # Both handlers must token-gate before any FS access.
        assert "handle_list_legacy_backups" in setup_src
        assert "handle_read_legacy_backup" in setup_src
        handler_src = inspect.getsource(
            _build_list_legacy_backups_handler
        ) + inspect.getsource(_build_read_legacy_backup_handler)
        assert handler_src.count("_caller_token_ok") >= 2

        unload_src = inspect.getsource(_async_unload_tools_entry)
        assert "SERVICE_LIST_LEGACY_BACKUPS" in unload_src
        assert "SERVICE_READ_LEGACY_BACKUP" in unload_src


class TestReadFileSecretsMaskingOrder:
    """secrets.yaml masking must survive the yaml_path/include_parsed views.

    The masking holds only because ``_shape_read_file_response`` captures
    ``full_content`` AFTER ``_mask_secrets_content`` and extracts the views
    from that. Nothing else pins the ordering, so a future reorder that
    captured ``full_content`` from the raw text would leak plaintext secrets
    through ``subtree``/``parsed`` — the two views that did not exist when the
    masking was written. Source-level guard, per the file's existing wiring
    guards. The behavioural half runs e2e
    (test_yaml_read.py::TestSecretsMasking).
    """

    def _handler_source(self) -> str:
        import inspect

        from custom_components.ha_mcp_tools import _shape_read_file_response

        return inspect.getsource(_shape_read_file_response)

    def test_full_content_is_captured_after_masking(self):
        src = self._handler_source()
        mask = src.index("_mask_secrets_content(content)")
        capture = src.index("full_content = content")
        assert mask < capture, (
            "full_content must be captured AFTER _mask_secrets_content, or "
            "yaml_path/include_parsed would extract from unmasked text"
        )

    def test_views_are_extracted_from_full_content(self):
        """And the extraction must read that captured text, not raw content.

        Handed to the executor as bare args, so the anchor carries no "(".
        """
        src = self._handler_source()
        assert "_extract_yaml_views, full_content, yaml_path" in src


class TestEditYamlConfigBackCompat:
    """Source guard: the strict (PREVENT_EXTRA) edit_yaml_config schema must
    keep accepting the legacy ``backup`` key. A pre-7.9.0 server still sends
    it, and the component reaches users via HACS ahead of the server, so
    dropping the key would reject every ha_config_set_yaml call from an old
    server ("extra keys not allowed"). Voluptuous is mocked in this suite, so
    this asserts the shim at the source level rather than by validating the
    schema object."""

    def test_schema_tolerates_backup_key(self):
        import inspect

        import custom_components.ha_mcp_tools as comp

        src = inspect.getsource(comp)
        start = src.index("SERVICE_EDIT_YAML_CONFIG_SCHEMA = vol.Schema(")
        block = src[start : src.index("\n)\n", start)]
        assert 'vol.Optional("backup")' in block, (
            "edit_yaml_config dropped the back-compat 'backup' shim; "
            "pre-7.9.0 servers still send it and would be rejected"
        )


class TestEditYamlConfigExtraAllowedKeys:
    """Source guards for the operator extra-key wiring (#1887).

    Two links in the chain are invisible to the parse-level tests: the schema
    entry that lets the field through at all, and the handler passing it into
    the validator. Dropping either leaves every parse test green while the
    feature is dead - the first rejects the whole call with the opaque
    "extra keys not allowed", the second silently ignores the operator's keys.
    Voluptuous is mocked in this suite, so these assert at the source level.
    """

    def test_schema_accepts_extra_allowed_keys(self):
        import inspect

        import custom_components.ha_mcp_tools as comp

        src = inspect.getsource(comp)
        start = src.index("SERVICE_EDIT_YAML_CONFIG_SCHEMA = vol.Schema(")
        block = src[start : src.index("\n)\n", start)]
        assert 'vol.Optional("extra_allowed_keys"' in block, (
            "edit_yaml_config dropped the extra_allowed_keys field; the strict "
            "schema would reject every call from a server that sends it"
        )

    def test_handler_forwards_caller_extra_keys_to_validator(self):
        import inspect

        import custom_components.ha_mcp_tools as comp

        src = inspect.getsource(comp)
        start = src.index("_parse_and_validate_yaml_path(\n")
        block = src[start : start + 400]
        assert "extra_allowed_keys=_caller_extra_allowed_keys(call)" in block, (
            "the edit handler stopped forwarding the caller's extra keys; the "
            "operator setting would be silently inert"
        )


class TestDenyFloor:
    """The non-overridable deny floor (issue #1567): a user-configured extra
    directory can NEVER reach .storage or an unmasked secrets file, even when
    the directory is explicitly present in the extra-dirs list."""

    def test_storage_blocked_for_read_even_as_extra_dir(self, tmp_path):
        assert (
            _is_path_allowed_for_read(tmp_path, ".storage/auth", [".storage"]) is False
        )
        assert _is_path_allowed_for_read(tmp_path, ".storage", [".storage"]) is False

    def test_storage_blocked_for_dir_even_as_extra_dir(self, tmp_path):
        assert (
            _is_path_allowed_for_dir(
                tmp_path, ".storage/auth", ALLOWED_WRITE_DIRS, [".storage"]
            )
            is False
        )
        assert (
            _is_path_allowed_for_dir(
                tmp_path, ".storage", ALLOWED_READ_DIRS, [".storage"]
            )
            is False
        )

    def test_violates_deny_floor_storage(self, tmp_path):
        assert _violates_deny_floor(tmp_path, ".storage") is True
        assert _violates_deny_floor(tmp_path, ".storage/auth") is True

    def test_root_secrets_yaml_not_a_violation(self, tmp_path):
        # The canonical config-root secrets.yaml passes the floor; the read
        # handler masks it. Only OTHER secrets.yaml files are blocked.
        assert _violates_deny_floor(tmp_path, "secrets.yaml") is False
        assert _is_path_allowed_for_read(tmp_path, "secrets.yaml") is True

    def test_nested_secrets_yaml_blocked(self, tmp_path):
        # A secrets.yaml surfaced via a custom dir would be returned UNMASKED.
        assert _violates_deny_floor(tmp_path, "pyscript/secrets.yaml") is True
        assert (
            _is_path_allowed_for_read(tmp_path, "pyscript/secrets.yaml", ["pyscript"])
            is False
        )

    def test_symlink_into_storage_blocked(self, tmp_path):
        (tmp_path / ".storage").mkdir()
        symlink_or_skip(tmp_path / "evil", tmp_path / ".storage")
        assert _violates_deny_floor(tmp_path, "evil/auth") is True
        assert _is_path_allowed_for_read(tmp_path, "evil/auth", ["evil"]) is False
        assert (
            _is_path_allowed_for_dir(
                tmp_path, "evil/auth", ALLOWED_WRITE_DIRS, ["evil"]
            )
            is False
        )

    def test_renamed_symlink_to_secrets_blocked(self, tmp_path):
        # A symlink with an innocuous name pointing at secrets.yaml must not
        # leak it UNMASKED — masking keys off the literal 'secrets.yaml' path,
        # so the floor must catch the resolved target's basename.
        (tmp_path / "secrets.yaml").write_text("api_key: SECRET\n")
        (tmp_path / "www").mkdir()
        symlink_or_skip(tmp_path / "www" / "notes.txt", tmp_path / "secrets.yaml")
        assert _violates_deny_floor(tmp_path, "www/notes.txt") is True
        assert _is_path_allowed_for_read(tmp_path, "www/notes.txt") is False

    def test_case_insensitive_storage_blocked(self, tmp_path):
        # On a case-insensitive FS '.STORAGE' opens the real '.storage'; the
        # floor must match case-insensitively so it can't be bypassed.
        assert _violates_deny_floor(tmp_path, ".STORAGE") is True
        assert _violates_deny_floor(tmp_path, ".Storage/auth") is True

    def test_case_insensitive_secrets_blocked(self, tmp_path):
        assert _violates_deny_floor(tmp_path, "pyscript/SECRETS.YAML") is True
        # The canonical lowercase root file still passes (it is masked).
        assert _violates_deny_floor(tmp_path, "secrets.yaml") is False

    def test_config_dir_under_storage_is_not_blanket_banned(self, tmp_path):
        # Regression (patch76 nit): if the HA config dir itself lives below a
        # ".storage" component (e.g. /var/.storage/config), normal access must
        # NOT be denied — the floor only scans the config-RELATIVE portion of
        # the resolved path, mirroring the pre-PR allowlist model.
        config_dir = tmp_path / ".storage" / "config"
        config_dir.mkdir(parents=True)
        assert _violates_deny_floor(config_dir, "www/x.css") is False
        assert _is_path_allowed_for_read(config_dir, "www/x.css") is True
        assert (
            _is_path_allowed_for_dir(config_dir, "www/x.css", ALLOWED_WRITE_DIRS)
            is True
        )
        # A real .storage UNDER this config is still denied (relative-input scan).
        assert _violates_deny_floor(config_dir, ".storage/auth") is True

    def test_symlink_dotdot_lexical_erase_escape_blocked(self, tmp_path):
        # #1586 review (HIGH): a symlink under an allowed dir + ``..`` escapes
        # the config dir even though os.path.normpath lexically collapses
        # ``link/..`` to a safe-looking in-config path. open(2) resolves the
        # symlink THEN applies ``..``, landing outside — _resolves_within
        # resolves the RAW path so enforcement matches the real open target.
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "www").mkdir()
        # www/link -> config_dir; "www/link/../escaped" -> config_dir/../escaped
        # = tmp_path/escaped, OUTSIDE the config dir.
        symlink_or_skip(config_dir / "www" / "link", config_dir)
        escape = os.path.join("www", "link", "..", "escaped.txt")
        assert _is_path_allowed_for_read(config_dir, escape) is False
        assert _is_path_allowed_for_dir(config_dir, escape, ALLOWED_WRITE_DIRS) is False
        assert _is_path_allowed_for_read(config_dir, ".storage/auth") is False


class TestLoadAllowedPaths:
    """_load_allowed_paths re-validates the persisted store and fails safe.

    Store is mocked (HA is stubbed at import), so async_load is an AsyncMock.
    Runs under asyncio_mode=auto (no explicit marker needed).
    """

    async def _load(self, monkeypatch, tmp_path, *, load_return=None, load_exc=None):
        import custom_components.ha_mcp_tools as comp

        hass = MagicMock()
        hass.config.config_dir = str(tmp_path)
        store = MagicMock()
        if load_exc is not None:
            store.async_load = AsyncMock(side_effect=load_exc)
        else:
            store.async_load = AsyncMock(return_value=load_return)
        monkeypatch.setattr(comp, "Store", lambda *a, **k: store)
        return await comp._load_allowed_paths(hass)

    async def test_revalidates_and_drops_invalid_stored_entries(
        self, monkeypatch, tmp_path
    ):
        # Traversal, deny-floor, non-string, and a duplicate are all dropped;
        # the one valid entry survives (deduped).
        result = await self._load(
            monkeypatch,
            tmp_path,
            load_return={"paths": ["pyscript", "../etc", ".storage", "pyscript", 123]},
        )
        assert result == ["pyscript"]

    async def test_corrupt_store_load_fails_safe(self, monkeypatch, tmp_path):
        # A raising async_load (corrupt blob) must not propagate — fall back to [].
        result = await self._load(
            monkeypatch, tmp_path, load_exc=ValueError("corrupt json")
        )
        assert result == []

    async def test_non_list_paths_ignored(self, monkeypatch, tmp_path):
        result = await self._load(
            monkeypatch, tmp_path, load_return={"paths": "not-a-list"}
        )
        assert result == []

    async def test_first_run_returns_empty(self, monkeypatch, tmp_path):
        result = await self._load(monkeypatch, tmp_path, load_return=None)
        assert result == []


class TestLoadCallerToken:
    """_load_or_create_caller_token must fail safe on a corrupt store rather
    than propagating out of async_setup_entry and bricking the integration."""

    async def test_corrupt_load_regenerates_token(self, monkeypatch):
        import custom_components.ha_mcp_tools as comp

        hass = MagicMock()
        store = MagicMock()
        store.async_load = AsyncMock(side_effect=ValueError("corrupt blob"))
        store.async_save = AsyncMock()
        monkeypatch.setattr(comp, "Store", lambda *a, **k: store)
        token = await comp._load_or_create_caller_token(hass)
        # A fresh token is generated and persisted (overwriting the bad blob).
        assert isinstance(token, str) and token
        store.async_save.assert_awaited_once()

    async def test_existing_token_returned(self, monkeypatch):
        import custom_components.ha_mcp_tools as comp

        hass = MagicMock()
        store = MagicMock()
        store.async_load = AsyncMock(return_value={"token": "existing-tok"})
        store.async_save = AsyncMock()
        monkeypatch.setattr(comp, "Store", lambda *a, **k: store)
        token = await comp._load_or_create_caller_token(hass)
        assert token == "existing-tok"
        store.async_save.assert_not_awaited()


class TestExtraDirsReadWrite:
    """A user-configured extra directory grants BOTH read and write."""

    def test_extra_dir_allows_read(self, tmp_path):
        assert (
            _is_path_allowed_for_read(tmp_path, "pyscript/foo.py", ["pyscript"]) is True
        )
        assert (
            _is_path_allowed_for_read(tmp_path, "pyscript/sub/bar.py", ["pyscript"])
            is True
        )

    def test_extra_dir_allows_write_and_list(self, tmp_path):
        assert (
            _is_path_allowed_for_dir(
                tmp_path, "pyscript/foo.py", ALLOWED_WRITE_DIRS, ["pyscript"]
            )
            is True
        )
        assert (
            _is_path_allowed_for_dir(
                tmp_path, "pyscript", ALLOWED_READ_DIRS, ["pyscript"]
            )
            is True
        )

    def test_dir_not_in_extra_still_blocked(self, tmp_path):
        assert (
            _is_path_allowed_for_read(tmp_path, "esphome/x.yaml", ["pyscript"]) is False
        )
        assert (
            _is_path_allowed_for_dir(
                tmp_path, "esphome/x.yaml", ALLOWED_WRITE_DIRS, ["pyscript"]
            )
            is False
        )

    def test_no_extra_dirs_preserves_builtin_behavior(self, tmp_path):
        # Default (no extra dirs) — built-in allowlist behavior unchanged.
        assert _is_path_allowed_for_read(tmp_path, "www/x.css") is True
        assert _is_path_allowed_for_read(tmp_path, "pyscript/foo.py") is False
        assert (
            _is_path_allowed_for_dir(tmp_path, "www/x.css", ALLOWED_WRITE_DIRS) is True
        )

    def test_nested_extra_dir_grants_read_write(self, tmp_path):
        # A multi-segment entry grants the dir itself and paths under it
        # (the normalizer accepts nested entries, so enforcement must honor
        # them — not silently store a dead entry).
        if sys.platform == "win32":
            pytest.skip("Linux path semantics (normpath flips / to \\ on Windows)")
        extra = ["foo/bar"]
        assert _is_path_allowed_for_read(tmp_path, "foo/bar", extra) is True
        assert _is_path_allowed_for_read(tmp_path, "foo/bar/x.py", extra) is True
        assert (
            _is_path_allowed_for_dir(
                tmp_path, "foo/bar/x.py", ALLOWED_WRITE_DIRS, extra
            )
            is True
        )

    def test_nested_extra_dir_respects_path_boundary(self, tmp_path):
        extra = ["foo/bar"]
        # A sibling and the parent itself are NOT granted by 'foo/bar'.
        assert _is_path_allowed_for_read(tmp_path, "foo/other/x.py", extra) is False
        assert _is_path_allowed_for_read(tmp_path, "foo/x.py", extra) is False
        # Prefix must respect the separator boundary: 'foo/barbaz' is not under
        # 'foo/bar'.
        assert _is_path_allowed_for_read(tmp_path, "foo/barbaz/x.py", extra) is False


class TestNormalizeExtraDir:
    """Validation/normalization applied by set_allowed_paths before persisting."""

    def test_accepts_simple_dir(self, tmp_path):
        assert _normalize_extra_dir("pyscript", tmp_path) == "pyscript"
        assert _normalize_extra_dir("python_scripts", tmp_path) == "python_scripts"

    def test_strips_whitespace_and_trailing_slash(self, tmp_path):
        assert _normalize_extra_dir("  pyscript/  ", tmp_path) == "pyscript"

    def test_accepts_nested_dir(self, tmp_path):
        if sys.platform == "win32":
            pytest.skip("Linux path semantics (normpath flips / to \\ on Windows)")
        assert _normalize_extra_dir("foo/bar", tmp_path) == "foo/bar"

    def test_rejects_empty_and_root(self, tmp_path):
        assert _normalize_extra_dir("", tmp_path) is None
        assert _normalize_extra_dir("   ", tmp_path) is None
        assert _normalize_extra_dir(".", tmp_path) is None

    def test_rejects_traversal(self, tmp_path):
        assert _normalize_extra_dir("..", tmp_path) is None
        assert _normalize_extra_dir("../etc", tmp_path) is None
        assert _normalize_extra_dir("www/../../etc", tmp_path) is None

    def test_rejects_absolute(self, tmp_path):
        assert _normalize_extra_dir("/etc/passwd", tmp_path) is None
        assert _normalize_extra_dir("/pyscript", tmp_path) is None

    def test_rejects_deny_floor(self, tmp_path):
        assert _normalize_extra_dir(".storage", tmp_path) is None
        assert _normalize_extra_dir(".storage/auth", tmp_path) is None
        # Collapses to .storage after normpath — still rejected.
        assert _normalize_extra_dir("www/../.storage", tmp_path) is None

    def test_rejects_non_string(self, tmp_path):
        assert _normalize_extra_dir(123, tmp_path) is None  # type: ignore[arg-type]


class TestIsWithinConfigDir:
    """The symlink-aware containment check (fixes the prior str-prefix bug)."""

    def test_sibling_prefix_not_treated_as_inside(self, tmp_path):
        # A sibling dir sharing a name prefix must NOT count as inside config.
        config = tmp_path / "config"
        config.mkdir()
        (tmp_path / "config-evil").mkdir()
        # '../config-evil' normalizes outside config and must be rejected.
        assert (
            _is_within_config_dir(config, os.path.normpath("../config-evil")) is False
        )

    def test_plain_subpath_is_inside(self, tmp_path):
        assert _is_within_config_dir(tmp_path, "www/x.css") is True


class TestVolumeRootFor:
    """_volume_root_for: which HAOS sibling volume an absolute path falls in (#1586)."""

    def test_matches_each_volume_root_exactly(self):
        for root in ALLOWED_VOLUME_ROOTS:
            assert _volume_root_for(root) == root

    def test_matches_path_under_root(self):
        assert _volume_root_for("/share/llm/notes.md") == "/share"
        assert _volume_root_for("/media/tts/out.mp3") == "/media"
        assert _volume_root_for("/ssl/fullchain.pem") == "/ssl"
        assert _volume_root_for("/backup/abc.tar") == "/backup"

    def test_rejects_non_volume_absolute(self):
        assert _volume_root_for("/etc/passwd") is None
        assert _volume_root_for("/config/configuration.yaml") is None
        assert _volume_root_for("/") is None

    def test_respects_separator_boundary(self):
        # A sibling sharing a name prefix must NOT match (would be a string-
        # prefix bug): '/shared' is not '/share', '/backups' is not '/backup'.
        assert _volume_root_for("/shared") is None
        assert _volume_root_for("/shared/x") is None
        assert _volume_root_for("/backups") is None
        assert _volume_root_for("/ssl_extra/x") is None


class TestNormalizeVolumeDir:
    """_normalize_extra_dir accepts the fixed HAOS sibling-volume roots (#1586)."""

    def test_accepts_each_volume_root(self, tmp_path):
        for root in ("/share", "/media", "/ssl", "/backup"):
            assert _normalize_extra_dir(root, tmp_path) == root

    def test_accepts_subdir_of_volume(self, tmp_path):
        assert _normalize_extra_dir("/share/llm", tmp_path) == "/share/llm"

    def test_strips_whitespace_and_trailing_slash(self, tmp_path):
        assert _normalize_extra_dir("  /media/tts/  ", tmp_path) == "/media/tts"

    def test_still_rejects_non_volume_absolute(self, tmp_path):
        assert _normalize_extra_dir("/etc/passwd", tmp_path) is None
        assert _normalize_extra_dir("/pyscript", tmp_path) is None
        assert _normalize_extra_dir("/config/secrets.yaml", tmp_path) is None

    def test_respects_volume_boundary(self, tmp_path):
        assert _normalize_extra_dir("/shared", tmp_path) is None

    def test_rejects_deny_floor_under_volume(self, tmp_path):
        assert _normalize_extra_dir("/share/.storage", tmp_path) is None
        assert _normalize_extra_dir("/share/.storage/auth", tmp_path) is None
        # secrets.yaml is always denied on a volume (volume reads are unmasked).
        assert _normalize_extra_dir("/backup/secrets.yaml", tmp_path) is None

    def test_traversal_collapsing_outside_volume_rejected(self, tmp_path):
        # normpath collapses '/share/../etc' -> '/etc', no longer a volume root.
        assert _normalize_extra_dir("/share/../etc", tmp_path) is None


class TestVolumePathEnforcement:
    """Read/write/list enforcement for configured HAOS volume paths (#1586).

    A configured volume grants BOTH read and write, matching the config-relative
    extra-dir model — and the deny floor + path boundaries still hold.
    """

    def test_configured_volume_grants_read(self, tmp_path):
        extra = ["/share"]
        assert _is_path_allowed_for_read(tmp_path, "/share/llm/n.md", extra) is True
        assert _is_path_allowed_for_read(tmp_path, "/share", extra) is True

    def test_configured_volume_grants_write_and_list(self, tmp_path):
        extra = ["/share"]
        assert (
            _is_path_allowed_for_dir(
                tmp_path, "/share/x.txt", ALLOWED_WRITE_DIRS, extra
            )
            is True
        )
        assert (
            _is_path_allowed_for_dir(tmp_path, "/share", ALLOWED_READ_DIRS, extra)
            is True
        )

    def test_unconfigured_volume_blocked(self, tmp_path):
        # /media is a known root but was never added to the allowlist.
        assert _is_path_allowed_for_read(tmp_path, "/media/x", ["/share"]) is False
        assert (
            _is_path_allowed_for_dir(
                tmp_path, "/media/x", ALLOWED_WRITE_DIRS, ["/share"]
            )
            is False
        )

    def test_no_extra_dirs_blocks_all_volumes(self, tmp_path):
        assert _is_path_allowed_for_read(tmp_path, "/share/x", None) is False
        assert _is_path_allowed_for_read(tmp_path, "/backup/x", []) is False

    def test_subdir_entry_respects_boundary(self, tmp_path):
        extra = ["/share/llm"]
        assert _is_path_allowed_for_read(tmp_path, "/share/llm/a", extra) is True
        assert _is_path_allowed_for_read(tmp_path, "/share/other/a", extra) is False
        # '/share/llmx' is not under '/share/llm' (separator boundary).
        assert _is_path_allowed_for_read(tmp_path, "/share/llmx/a", extra) is False

    def test_deny_floor_enforced_on_volume(self, tmp_path):
        extra = ["/share"]
        assert (
            _is_path_allowed_for_read(tmp_path, "/share/.storage/auth", extra) is False
        )
        assert (
            _is_path_allowed_for_read(tmp_path, "/share/secrets.yaml", extra) is False
        )
        assert (
            _is_path_allowed_for_dir(
                tmp_path, "/share/.storage/x", ALLOWED_WRITE_DIRS, extra
            )
            is False
        )

    def test_non_volume_absolute_blocked_even_if_listed(self, tmp_path):
        # Defense in depth: even if a non-volume absolute path reached the live
        # list (the normalizer drops it), enforcement still refuses it.
        assert _is_path_allowed_for_read(tmp_path, "/etc/passwd", ["/etc"]) is False

    def test_config_relative_unaffected_by_volume_entries(self, tmp_path):
        # A mixed allowlist (config-relative + volume) keeps both behaviors.
        extra = ["pyscript", "/share"]
        assert _is_path_allowed_for_read(tmp_path, "pyscript/foo.py", extra) is True
        assert _is_path_allowed_for_read(tmp_path, "/share/foo", extra) is True
        assert _is_path_allowed_for_read(tmp_path, "esphome/x", extra) is False


class TestListFilesSyncVolumePaths:
    """_list_files_sync reports absolute paths for dirs outside the config dir (#1586)."""

    def test_reports_absolute_path_for_out_of_config_dir(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        volume = tmp_path / "share"
        volume.mkdir()
        (volume / "note.txt").write_text("hi")
        result = _list_files_sync(volume, config_dir, None)
        assert "_error" not in result
        paths = {f["name"]: f["path"] for f in result["files"]}
        # Not relative_to(config_dir) (would raise) — reported absolute instead.
        assert paths["note.txt"] == str(volume / "note.txt")

    def test_config_relative_listing_still_relative(self, tmp_path):
        (tmp_path / "www").mkdir()
        (tmp_path / "www" / "a.css").write_text("x")
        result = _list_files_sync(tmp_path / "www", tmp_path, None)
        paths = {f["name"]: f["path"] for f in result["files"]}
        assert paths["a.css"] == os.path.join("www", "a.css")


class TestResolvesWithin:
    """The symlink-safe containment primitive that backs both the config and
    volume gates (#1586 review). Resolves the RAW path (symlinks + ``..`` as
    open(2) does), not the lexically-normalized form."""

    def test_plain_subpath_within(self, tmp_path):
        (tmp_path / "sub").mkdir()
        assert _resolves_within(tmp_path, "sub/file.txt") is True

    def test_nonexistent_subpath_within(self, tmp_path):
        # resolve() tolerates non-existent leaves; a path that lexically stays
        # under base is allowed (creation paths must work).
        assert _resolves_within(tmp_path, "does/not/exist.txt") is True

    def test_absolute_raw_path_handled(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        assert _resolves_within(tmp_path, str(sub / "f")) is True
        assert _resolves_within(sub, str(tmp_path / "other")) is False

    def test_symlink_escaping_out_rejected(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        symlink_or_skip(base / "link", outside)
        # base/link/x resolves to outside/x — escapes base.
        assert _resolves_within(base, "link/x") is False

    def test_symlink_dotdot_lexical_erase_escape_rejected(self, tmp_path):
        # The HIGH finding's core: `<base>/<symlink>/..`. normpath lexically
        # collapses `link/..` back to base, but open(2) resolves link first,
        # then `..`, landing in link's REAL parent — outside base.
        base = tmp_path / "base"
        base.mkdir()
        target = tmp_path / "target"
        target.mkdir()
        symlink_or_skip(base / "link", target)
        # base/link/../escape -> target/../escape -> tmp_path/escape (outside base)
        assert _resolves_within(base, "link/../escape") is False


class TestVolumeSymlinkEscape:
    """End-to-end #1586 review HIGH regression: a symlink under a configured
    volume that escapes the volume root is denied for read AND write/delete."""

    def test_volume_symlink_dotdot_escape_blocked(self, tmp_path, monkeypatch):
        import custom_components.ha_mcp_tools as comp

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        volume = tmp_path / "vol"
        volume.mkdir()
        # Treat the tmp volume as a recognized HAOS sibling-volume root.
        monkeypatch.setattr(comp, "ALLOWED_VOLUME_ROOTS", (str(volume),))
        # vol/link -> volume's parent; vol/link/../escaped escapes the volume.
        symlink_or_skip(volume / "link", tmp_path)
        extra = [str(volume)]
        escape = str(volume / "link" / ".." / "escaped.txt")
        assert _is_path_allowed_for_read(config_dir, escape, extra) is False
        assert (
            _is_path_allowed_for_dir(config_dir, escape, ALLOWED_WRITE_DIRS, extra)
            is False
        )

    def test_volume_symlink_within_root_allowed(self, tmp_path, monkeypatch):
        import custom_components.ha_mcp_tools as comp

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        volume = tmp_path / "vol"
        volume.mkdir()
        (volume / "real").mkdir()
        monkeypatch.setattr(comp, "ALLOWED_VOLUME_ROOTS", (str(volume),))
        # A symlink that stays inside the volume is fine.
        symlink_or_skip(volume / "link", volume / "real")
        extra = [str(volume)]
        ok = str(volume / "link" / "f.txt")
        assert _is_path_allowed_for_read(config_dir, ok, extra) is True

    def test_volume_renamed_symlink_to_secrets_within_root_blocked(
        self, tmp_path, monkeypatch
    ):
        # The dangerous case the volume deny-floor's RESOLVED-target branch
        # exists for: an innocuously-named symlink that stays INSIDE the volume
        # root (so _resolves_within allows it) but points at secrets.yaml. Only
        # the resolved-basename floor check catches it (volume reads are never
        # masked, so this must be denied).
        import custom_components.ha_mcp_tools as comp

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        volume = tmp_path / "vol"
        volume.mkdir()
        monkeypatch.setattr(comp, "ALLOWED_VOLUME_ROOTS", (str(volume),))
        (volume / "secrets.yaml").write_text("api_key: SECRET\n")
        symlink_or_skip(volume / "notes.txt", volume / "secrets.yaml")
        extra = [str(volume)]
        assert (
            _is_path_allowed_for_read(config_dir, str(volume / "notes.txt"), extra)
            is False
        )

    def test_volume_symlink_into_storage_within_root_blocked(
        self, tmp_path, monkeypatch
    ):
        # A symlink that resolves to a .storage dir WITHIN the volume root stays
        # inside the root (passes _resolves_within) — only the resolved-segment
        # deny floor blocks it. Read AND write/delete must be denied.
        import custom_components.ha_mcp_tools as comp

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        volume = tmp_path / "vol"
        volume.mkdir()
        monkeypatch.setattr(comp, "ALLOWED_VOLUME_ROOTS", (str(volume),))
        (volume / ".storage").mkdir()
        symlink_or_skip(volume / "link", volume / ".storage")
        extra = [str(volume)]
        target = str(volume / "link" / "auth")
        assert _is_path_allowed_for_read(config_dir, target, extra) is False
        assert (
            _is_path_allowed_for_dir(config_dir, target, ALLOWED_WRITE_DIRS, extra)
            is False
        )

    def test_normalize_volume_dir_rejects_symlink_to_secrets(
        self, tmp_path, monkeypatch
    ):
        # Validation-time (store) deny floor: adding an innocuously-named entry
        # that resolves to secrets.yaml must be rejected before persisting.
        import custom_components.ha_mcp_tools as comp

        volume = tmp_path / "vol"
        volume.mkdir()
        monkeypatch.setattr(comp, "ALLOWED_VOLUME_ROOTS", (str(volume),))
        (volume / "secrets.yaml").write_text("x: y\n")
        symlink_or_skip(volume / "innocent", volume / "secrets.yaml")
        assert _normalize_extra_dir(str(volume / "innocent"), tmp_path) is None


class TestExtractYamlSubtree:
    """``_extract_yaml_subtree`` — the round-trip subtree extraction the
    read_file service exposes via ``yaml_path`` for ha-mcp's per-edit
    auto-backup (#1579). Runs component-side because ruamel lives here."""

    def test_extracts_top_level_key(self):
        src = "rest:\n  resource: http://a\n  method: GET\ntimer: {}\n"
        out = _extract_yaml_subtree(src, "rest")
        assert out is not None
        assert "resource: http://a" in out
        assert "timer" not in out

    def test_preserves_comments_and_ha_tags(self):
        src = "command_line:\n  - command: !secret cmd  # inline note\n"
        out = _extract_yaml_subtree(src, "command_line")
        assert out is not None
        assert "!secret cmd" in out
        assert "# inline note" in out

    def test_walks_dotted_path(self):
        src = "lovelace:\n  dashboards:\n    my-d:\n      mode: yaml\n"
        out = _extract_yaml_subtree(src, "lovelace.dashboards.my-d")
        assert out is not None
        assert out.strip() == "mode: yaml"

    def test_missing_key_returns_none(self):
        assert _extract_yaml_subtree("a: 1\n", "nope") is None

    def test_non_mapping_root_returns_none(self):
        assert _extract_yaml_subtree("- a\n- b\n", "anything") is None

    def test_malformed_yaml_returns_none(self):
        # Malformed YAML yields None (the edit itself would then fail and
        # report the parse error); capture just skips.
        assert _extract_yaml_subtree("key: [1, 2\n", "key") is None


class TestExtractYamlViews:
    """#1788: ``_extract_yaml_views`` backs read_file's yaml_path/include_parsed.
    ``_extract_yaml_subtree`` is now a thin wrapper over it, so the text-view
    contract above still pins that half.
    """

    def test_parsed_omitted_unless_requested(self):
        views = _extract_yaml_views("rest:\n  method: GET\n", "rest")
        assert views["subtree"] is not None
        assert "parsed" not in views

    def test_parsed_returns_structured_data(self):
        views = _extract_yaml_views(
            "rest:\n  method: GET\n  timeout: 5\n", "rest", True
        )
        assert views["parsed"] == {"method": "GET", "timeout": 5}

    def test_parsed_keeps_secret_unresolved(self):
        """The security property: a parsed view renders !secret in SOURCE form.

        Resolving would require reading secrets.yaml, which this path never
        does — so no plaintext secret can reach the response.
        """
        views = _extract_yaml_views(
            "rest:\n  api_key: !secret alert2_api_key\n", "rest", True
        )
        assert views["parsed"] == {"api_key": "!secret alert2_api_key"}
        # And the text view keeps the tag too.
        assert "!secret alert2_api_key" in views["subtree"]

    def test_parsed_keeps_include_unresolved(self):
        views = _extract_yaml_views("group: !include groups.yaml\n", "group", True)
        assert views["parsed"] == "!include groups.yaml"

    def test_parsed_is_json_serializable(self):
        # ruamel hands back CommentedMap/ScalarInt subclasses; the response is
        # JSON-encoded on the way out, so plain types are the contract.
        views = _extract_yaml_views(
            "a:\n  n: 1\n  f: 1.5\n  b: true\n  s: x\n  z:\n  l: [1, 2]\n",
            "a",
            True,
        )
        assert json.dumps(views["parsed"])
        assert views["parsed"] == {
            "n": 1,
            "f": 1.5,
            "b": True,
            "s": "x",
            "z": None,
            "l": [1, 2],
        }

    def test_parsed_anchored_bool_stays_a_bool(self):
        """A bool carrying an anchor loads as ruamel's ScalarBoolean, which
        subclasses int but NOT bool — so an isinstance(x, bool) check alone
        lets it fall through to the int branch and serialize as 1/0."""
        views = _extract_yaml_views("a:\n  on: &flag true\n  off: *flag\n", "a", True)
        assert views["parsed"] == {"on": True, "off": True}

    def test_parsed_non_finite_floats_render_to_source_form(self):
        """.inf/.nan are valid YAML but have no JSON encoding, so they come
        back as their source form rather than breaking the response."""
        views = _extract_yaml_views(
            "a:\n  hi: .inf\n  lo: -.inf\n  n: .nan\n", "a", True
        )
        assert views["parsed"] == {"hi": ".inf", "lo": "-.inf", "n": ".nan"}
        assert json.dumps(views["parsed"], allow_nan=False)

    def test_parsed_timestamps_render_as_iso_strings(self):
        """!!timestamp comes back as date/datetime, which json cannot encode."""
        views = _extract_yaml_views(
            "a:\n  d: 2024-01-02\n  dt: 2024-01-02 03:04:05\n", "a", True
        )
        assert views["parsed"] == {"d": "2024-01-02", "dt": "2024-01-02T03:04:05"}
        assert json.dumps(views["parsed"])

    def test_present_but_null_key_is_a_match_not_an_absence(self):
        """`default_config:` is a real key with no value — the text view must
        come back so it does not read as "not defined"."""
        views = _extract_yaml_views(
            "default_config:\nhttp:\n  x: 1\n", "default_config", True
        )
        assert views["subtree"] is not None
        assert views["parsed"] is None
        assert "parse_error" not in views

    def test_missing_key_yields_no_parsed(self):
        views = _extract_yaml_views("a: 1\n", "nope", True)
        assert views["subtree"] is None
        assert views.get("parsed") is None

    def test_malformed_yaml_yields_no_parsed(self):
        views = _extract_yaml_views("key: [1, 2\n", "key", True)
        assert views["subtree"] is None
        assert "parsed" not in views

    def test_malformed_yaml_reports_parse_error(self):
        """A broken file must be distinguishable from one lacking the key.

        Without this, one syntactically broken package in a glob reads as a
        clean "the key is not defined anywhere".
        """
        views = _extract_yaml_views("key: [1, 2\n", "key")
        assert views["subtree"] is None
        assert "not valid YAML" in views["parse_error"]

    def test_absent_key_reports_no_parse_error(self):
        views = _extract_yaml_views("a: 1\n", "nope")
        assert views["subtree"] is None
        assert "parse_error" not in views

    def test_parse_error_carries_position_but_no_file_content(self):
        """ruamel embeds the offending source line in its message; that line
        can hold an inline credential, so only the position is reported."""
        secret = "hunter2-should-never-surface"
        views = _extract_yaml_views(
            f"rest:\n  api_key: {secret}\n  bad: [1, 2\n", "rest"
        )
        assert views["subtree"] is None
        assert secret not in views["parse_error"]
        assert "line" in views["parse_error"]


class TestRecorderYamlKey:
    """#1852: recorder is editable via edit_yaml_config / ha_config_set_yaml.

    recorder is YAML-only (no UI or storage-mode helper) and has no
    code-execution surface, so it belongs in the plain ALLOWED_YAML_KEYS set
    (editable in configuration.yaml and package files alike).
    """

    def test_recorder_in_allowed_keys(self):
        assert "recorder" in ALLOWED_YAML_KEYS

    def test_recorder_parses_as_single_key(self):
        kind, parts, err = _parse_and_validate_yaml_path("recorder")
        assert err is None
        assert kind == "single"
        assert parts == ("recorder",)

    def test_recorder_accepted_in_configuration_yaml(self):
        # Not a packages-only key, so it validates even outside a package file.
        _, _, err = _parse_and_validate_yaml_path("recorder", is_package=False)
        assert err is None


def _fake_hass_fs(config_dir):
    """A hass whose executor offload actually runs the given function."""
    hass = MagicMock()
    hass.config.config_dir = str(config_dir)
    hass.config.path = lambda name, cd=str(config_dir): os.path.join(cd, name)

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = _run
    return hass


def _write_config(tmp_path, text, name="configuration.yaml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


class TestLoadPackageDirMarkers:
    """#1854: the packages folder argument is read from the include directive."""

    def test_no_packages(self, tmp_path):
        p = _write_config(tmp_path, "default_config:\n")
        assert _load_package_dir_markers(p) == set()

    def test_single_include_dir_named(self, tmp_path):
        p = _write_config(
            tmp_path, "homeassistant:\n  packages: !include_dir_named custom_packages\n"
        )
        assert _load_package_dir_markers(p) == {"custom_packages"}

    def test_include_dir_merge_named(self, tmp_path):
        p = _write_config(
            tmp_path,
            "homeassistant:\n  packages: !include_dir_merge_named integrations\n",
        )
        assert _load_package_dir_markers(p) == {"integrations"}

    def test_named_group_mapping(self, tmp_path):
        p = _write_config(
            tmp_path, "homeassistant:\n  packages:\n    grp: !include_dir_named pkgs\n"
        )
        assert _load_package_dir_markers(p) == {"pkgs"}

    def test_inline_packages_declare_no_folder(self, tmp_path):
        p = _write_config(
            tmp_path,
            "homeassistant:\n  packages:\n    inline_pkg:\n      light: []\n",
        )
        assert _load_package_dir_markers(p) == set()

    def test_other_ha_tags_ignored(self, tmp_path):
        p = _write_config(
            tmp_path,
            "homeassistant:\n  packages: !include_dir_named custom_packages\n"
            "recorder:\n  db_url: !secret db_url\n",
        )
        assert _load_package_dir_markers(p) == {"custom_packages"}

    def test_absolute_include_returned_raw(self, tmp_path):
        # Absolute args are returned as-is here; relativization happens in
        # _package_folder_relative_to_config.
        p = _write_config(
            tmp_path,
            "homeassistant:\n  packages: !include_dir_named /config/integrations\n",
        )
        assert _load_package_dir_markers(p) == {"/config/integrations"}

    def test_follows_split_homeassistant_include(self, tmp_path):
        # #1854 review: the homeassistant section split into another file. The
        # included file holds the body of the section (no homeassistant: wrapper).
        _write_config(
            tmp_path, "packages: !include_dir_named integrations\n", name="ha.yaml"
        )
        p = _write_config(
            tmp_path, "default_config:\nhomeassistant: !include ha.yaml\n"
        )
        assert _load_package_dir_markers(p) == {"integrations"}

    def test_missing_file_returns_empty(self, tmp_path):
        assert _load_package_dir_markers(str(tmp_path / "nope.yaml")) == set()

    def test_malformed_yaml_returns_empty(self, tmp_path):
        assert (
            _load_package_dir_markers(_write_config(tmp_path, "key: [1, 2\n")) == set()
        )


class TestPackageFolderRelativeToConfig:
    """#1854 review: normalize/relativize captured folder arguments."""

    def test_plain_relative(self):
        assert (
            _package_folder_relative_to_config("integrations", "/config")
            == "integrations"
        )

    def test_absolute_under_config_relativized(self):
        assert (
            _package_folder_relative_to_config("/config/integrations", "/config")
            == "integrations"
        )

    def test_absolute_outside_config_dropped(self):
        assert _package_folder_relative_to_config("/etc/pkgs", "/config") is None

    def test_parent_escape_dropped(self):
        assert _package_folder_relative_to_config("../evil", "/config") is None

    def test_config_root_dropped(self):
        assert _package_folder_relative_to_config("/config", "/config") is None


class TestPathInPackageDir:
    """#1854 review: literal folder matching (glob-metachar safe)."""

    def test_flat_and_nested(self):
        dirs = {"packages", "custom_packages"}
        assert _path_in_package_dir("packages/foo.yaml", dirs)
        assert _path_in_package_dir("custom_packages/sub/foo.yaml", dirs)

    def test_non_yaml_rejected(self):
        assert not _path_in_package_dir("packages/foo.txt", {"packages"})

    def test_sibling_prefix_not_matched(self):
        assert not _path_in_package_dir("packagesX/foo.yaml", {"packages"})

    def test_glob_metachar_folder_literal(self):
        # A folder named 'pkg[1]' must match its own path literally, not 'pkg1'.
        assert _path_in_package_dir("pkg[1]/foo.yaml", {"pkg[1]"})
        assert not _path_in_package_dir("pkg1/foo.yaml", {"pkg[1]"})

    def test_default_packages_when_none(self):
        assert _path_in_package_dir("packages/foo.yaml", None)


class TestPackageDirMarkersCached:
    """#1788: folder detection runs on every file op, and a glob fires one per
    matched file, so the parse is cached behind an mtime signature covering
    every file the loader read.
    """

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        _PACKAGE_DIR_CACHE.clear()
        yield
        _PACKAGE_DIR_CACHE.clear()

    def test_second_call_does_not_reparse(self, tmp_path, monkeypatch):
        p = _write_config(
            tmp_path, "homeassistant:\n  packages: !include_dir_named pkgs\n"
        )
        assert _package_dir_markers_cached(p) == {"pkgs"}

        def _boom(*_a, **_k):
            raise AssertionError("cache miss: re-parsed an unchanged config")

        monkeypatch.setattr(
            "custom_components.ha_mcp_tools._load_package_dir_markers_tracked", _boom
        )
        assert _package_dir_markers_cached(p) == {"pkgs"}

    def test_edit_invalidates(self, tmp_path):
        p = _write_config(
            tmp_path, "homeassistant:\n  packages: !include_dir_named pkgs\n"
        )
        assert _package_dir_markers_cached(p) == {"pkgs"}
        _write_config(
            tmp_path, "homeassistant:\n  packages: !include_dir_named other\n"
        )
        os.utime(p, ns=(0, 0))  # force a distinct mtime, not clock granularity
        assert _package_dir_markers_cached(p) == {"other"}

    def test_edit_to_included_file_invalidates(self, tmp_path):
        """The reason the signature spans every file read, not just the root.

        With the homeassistant: section split into an !include, keying on
        configuration.yaml alone would serve a stale allowlist forever.
        """
        _write_config(tmp_path, "packages: !include_dir_named pkgs\n", name="ha.yaml")
        p = _write_config(tmp_path, "homeassistant: !include ha.yaml\n")
        assert _package_dir_markers_cached(p) == {"pkgs"}

        (tmp_path / "ha.yaml").write_text(
            "packages: !include_dir_named moved\n", encoding="utf-8"
        )
        os.utime(tmp_path / "ha.yaml", ns=(0, 0))
        assert _package_dir_markers_cached(p) == {"moved"}

    def test_creating_a_missing_include_invalidates(self, tmp_path):
        """A missing include is stamped -1, so creating it later invalidates."""
        p = _write_config(tmp_path, "homeassistant: !include later.yaml\n")
        assert _package_dir_markers_cached(p) == set()

        (tmp_path / "later.yaml").write_text(
            "packages: !include_dir_named pkgs\n", encoding="utf-8"
        )
        assert _package_dir_markers_cached(p) == {"pkgs"}

    def test_cached_set_is_not_mutable_through_caller(self, tmp_path):
        p = _write_config(
            tmp_path, "homeassistant:\n  packages: !include_dir_named pkgs\n"
        )
        _package_dir_markers_cached(p).add("injected")
        assert _package_dir_markers_cached(p) == {"pkgs"}


class TestDetectPackageDirs:
    """#1854: detection reads configuration.yaml and adds the packages fallback."""

    async def test_default_when_no_packages(self, tmp_path):
        _write_config(tmp_path, "default_config:\n")
        assert await _detect_package_dirs(_fake_hass_fs(tmp_path)) == {"packages"}

    async def test_detects_custom_folder(self, tmp_path):
        _write_config(
            tmp_path, "homeassistant:\n  packages: !include_dir_named integrations\n"
        )
        assert await _detect_package_dirs(_fake_hass_fs(tmp_path)) == {
            "packages",
            "integrations",
        }

    async def test_relativizes_absolute_include(self, tmp_path):
        cfg = str(tmp_path)
        _write_config(
            tmp_path,
            f"homeassistant:\n  packages: !include_dir_named {cfg}/integrations\n",
        )
        assert await _detect_package_dirs(_fake_hass_fs(tmp_path)) == {
            "packages",
            "integrations",
        }

    async def test_missing_config_falls_back(self, tmp_path):
        # No configuration.yaml written -> unreadable -> default only.
        assert await _detect_package_dirs(_fake_hass_fs(tmp_path)) == {"packages"}


class TestReadAllowlistPackageFolder:
    """#1854: the read allowlist honours the configured packages folder.

    The pre-write backup snapshots a file by reading it, so the read path must
    accept the same non-default packages folder the YAML editor writes to.
    """

    def test_custom_folder_allowed_when_detected(self, tmp_path):
        assert _is_path_allowed_for_read(
            tmp_path, "integrations/lights.yaml", None, {"packages", "integrations"}
        )

    def test_custom_folder_rejected_when_not_detected(self, tmp_path):
        # Default set is {"packages"} only — an unconfigured folder is not read.
        assert not _is_path_allowed_for_read(tmp_path, "integrations/lights.yaml")

    def test_default_packages_folder_still_allowed(self, tmp_path):
        assert _is_path_allowed_for_read(tmp_path, "packages/foo.yaml")


class TestDirInPackageDir:
    """#1788: folder-level twin of _path_in_package_dir, used by the lister."""

    def test_matches_folder_itself_and_nested(self):
        dirs = {"packages", "custom_packages"}
        assert _dir_in_package_dir("packages", dirs)
        assert _dir_in_package_dir("custom_packages/sub", dirs)

    def test_sibling_prefix_not_matched(self):
        assert not _dir_in_package_dir("packagesX", {"packages"})

    def test_glob_metachar_folder_literal(self):
        # Mirrors _path_in_package_dir: a folder named 'pkg[1]' matches itself,
        # not the fnmatch expansion 'pkg1'.
        assert _dir_in_package_dir("pkg[1]", {"pkg[1]"})
        assert not _dir_in_package_dir("pkg1", {"pkg[1]"})

    def test_no_default_packages_when_none(self):
        # Deliberately UNLIKE _path_in_package_dir, which defaults to
        # {"packages"}: this helper backs a check shared with write/delete, so
        # omitting package_dirs must grant nothing.
        assert not _dir_in_package_dir("packages", None)
        assert not _dir_in_package_dir("packages", set())


class TestListAllowlistPackageFolder:
    """#1788: the lister honours the configured packages folder the way the
    read allowlist already does (#1854) — a packages folder that can be read
    file-by-file must also be enumerable, which is what cross-file key
    discovery needs.
    """

    def test_packages_folder_listable_when_passed(self, tmp_path):
        assert _is_path_allowed_for_dir(
            tmp_path, "packages", ALLOWED_READ_DIRS, None, {"packages"}
        )

    def test_custom_folder_listable_when_detected(self, tmp_path):
        assert _is_path_allowed_for_dir(
            tmp_path,
            "integrations",
            ALLOWED_READ_DIRS,
            None,
            {"packages", "integrations"},
        )

    def test_packages_not_listable_without_package_dirs(self, tmp_path):
        # Back-compat: callers that don't resolve the packages folder keep the
        # historical (deny) behaviour.
        assert not _is_path_allowed_for_dir(tmp_path, "packages", ALLOWED_READ_DIRS)

    def test_write_dirs_never_gain_package_access(self, tmp_path):
        """Security invariant: _is_path_allowed_for_dir is shared with
        write_file and delete_file, which pass no package_dirs. A packages
        folder must stay unwritable there — edit_yaml_config is the only write
        path that may reach config YAML.
        """
        assert not _is_path_allowed_for_dir(tmp_path, "packages", ALLOWED_WRITE_DIRS)
        assert not _is_path_allowed_for_dir(
            tmp_path, "packages/lights.yaml", ALLOWED_WRITE_DIRS
        )

    def test_deny_floor_still_applies_to_package_dirs(self, tmp_path):
        # The widened allow decision must not outrank the deny floor: a
        # maliciously configured '.storage' packages folder stays blocked.
        assert not _is_path_allowed_for_dir(
            tmp_path, ".storage", ALLOWED_READ_DIRS, None, {".storage"}
        )
