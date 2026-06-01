"""Unit tests for ha_mcp_tools custom component file operations.

These tests focus on the pure Python utility functions that don't require
Home Assistant dependencies.
"""

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Mock the Home Assistant imports before importing the module
sys.modules["voluptuous"] = MagicMock()
sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.components"] = MagicMock()
sys.modules["homeassistant.components.persistent_notification"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.config_validation"] = MagicMock()
sys.modules["homeassistant.helpers.storage"] = MagicMock()
sys.modules["homeassistant.loader"] = MagicMock()


# Now we can import the functions
from custom_components.ha_mcp_tools import (  # noqa: E402
    _delete_file_sync,
    _is_path_allowed_for_dir,
    _is_path_allowed_for_read,
    _list_files_sync,
    _mask_secrets_content,
    _migrate_legacy_backup_dir,
    _read_file_sync,
    _write_file_sync,
)
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    ALLOWED_READ_DIRS,
    ALLOWED_WRITE_DIRS,
)


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

    def test_drops_comments_and_blank_lines(self):
        """The structural mask emits only ``key: [MASKED]`` lines. Comments and
        blank lines are intentionally not reproduced — they are not needed to
        show which keys exist, and dropping them avoids leaking a secret that a
        user pasted into a comment."""
        content = (
            "\n# comment about the API key\napi_key: secret123\n\npassword: pass456\n"
        )
        result = _mask_secrets_content(content)

        assert "secret123" not in result
        assert "pass456" not in result
        assert result == "api_key: [MASKED]\npassword: [MASKED]"

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
        assert result == "outer: [MASKED]"

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
        assert result == "backup_ssh_key: [MASKED]\napi_password: [MASKED]"

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
        assert _mask_secrets_content("foo: !secret bar\n") == "foo: [MASKED]"

    def test_yaml_anchors_do_not_leak_dereferenced_secrets(self):
        """A secret defined once via an anchor and reused via aliases is
        dereferenced into every key by the loader; each must still be masked and
        the secret must not survive anywhere in the output."""
        content = "base_token: &tok 'secret123'\nprod: *tok\ndev: *tok\n"
        result = _mask_secrets_content(content)
        assert "secret123" not in result
        assert result == "base_token: [MASKED]\nprod: [MASKED]\ndev: [MASKED]"


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
        (legacy / "link.bak").symlink_to(target)
        (legacy / "real.bak").write_text("real content")

        moved, failed = _migrate_legacy_backup_dir(tmp_path)

        assert (moved, failed) == (1, 0)
        new_dir = tmp_path / ".ha_mcp_tools_backups"
        assert (new_dir / "real.bak").read_text() == "real content"
        # Symlink left in place; legacy dir not removed because non-empty.
        assert (legacy / "link.bak").is_symlink()
        assert legacy.exists()

    def test_async_setup_entry_wires_migration_and_notification(self):
        """Source-level guard: async_setup_entry must call the migration helper
        and create a persistent notification referencing the GHSA. Brittle on
        purpose — this is a security regression guard, not a behavioral test.
        """
        import inspect

        from custom_components.ha_mcp_tools import async_setup_entry

        src = inspect.getsource(async_setup_entry)
        assert "_migrate_legacy_backup_dir" in src, (
            "async_setup_entry must invoke the legacy-backup migration"
        )
        assert "persistent_notification.async_create" in src, (
            "async_setup_entry must surface migration via persistent_notification"
        )
        assert "GHSA-g39v-cvjh-8fpf" in src, (
            "persistent notification must reference the security advisory"
        )
