"""Unit tests for theme file editing in ha_mcp_tools.

The service runs on a real ``hass`` whose config directory is the test's
``tmp_path``. Core loads no ``frontend`` here, so ``frontend.reload_themes``
exists only where a test registers it with ``async_mock_service``.
"""

import asyncio
import threading
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceNotFound
from pytest_homeassistant.common import async_mock_service

from custom_components.ha_mcp_tools.file_ops import file_lock, write_text_atomic

from ._edit_yaml import EditYaml, make_edit_yaml


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Core's config directory is this test's ``tmp_path``."""
    return hass_tmp_config_dir


@pytest.fixture
async def edit_yaml(hass: HomeAssistant) -> EditYaml:
    """Call ``ha_mcp_tools.edit_yaml_config`` the way ha-mcp does."""
    return await make_edit_yaml(hass)


class TestThemeEditHandler:
    """Component handler tests for themes/*.yaml editing."""

    async def test_theme_add_creates_file_with_theme_key(
        self, hass: HomeAssistant, edit_yaml: EditYaml, tmp_path: Path
    ) -> None:
        reload_calls = async_mock_service(hass, "frontend", "reload_themes")
        theme_file = tmp_path / "themes" / "dark-theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)

        result = await edit_yaml(
            {
                "file": "themes/dark-theme.yaml",
                "action": "add",
                "yaml_path": "dark-theme",
                "content": "primary-color: '#1976D2'\naccent-color: '#FFC107'",
                "backup": False,
            }
        )

        assert result["success"] is True
        assert result["action"] == "add"
        assert result["yaml_path"] == "dark-theme"
        assert result["post_action"] == "reload_performed"
        assert result["reload_service"] == "frontend.reload_themes"

        written = theme_file.read_text()
        assert "dark-theme:" in written
        assert "primary-color: '#1976D2'" in written

        assert len(reload_calls) == 1
        assert reload_calls[0].data == {}

    async def test_theme_edit_waits_for_the_file_lock(
        self,
        hass: HomeAssistant,
        edit_yaml: EditYaml,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """edit_file can write the same theme; an overlapping read-modify-write
        would undo its change while both report success."""
        (tmp_path / "themes").mkdir()
        lock_asked = asyncio.Event()

        def _asked_lock(hass: HomeAssistant) -> asyncio.Lock:
            lock_asked.set()
            return file_lock(hass)

        monkeypatch.setattr(
            "custom_components.ha_mcp_tools.file_ops.file_lock", _asked_lock
        )
        async with file_lock(hass):
            edit = asyncio.ensure_future(
                edit_yaml(
                    {
                        "file": "themes/dark.yaml",
                        "action": "add",
                        "yaml_path": "dark",
                        "content": "primary-color: red",
                    }
                )
            )
            # The edit either finishes, or asks for the lock and waits on it.
            asked = asyncio.ensure_future(lock_asked.wait())
            await asyncio.wait({edit, asked}, return_when=asyncio.FIRST_COMPLETED)
            asked.cancel()
            assert not edit.done()
            assert not (tmp_path / "themes" / "dark.yaml").exists()
        assert (await edit)["success"] is True
        assert (tmp_path / "themes" / "dark.yaml").exists()

    async def test_cancelled_theme_edit_keeps_the_lock_until_its_write_ends(
        self,
        hass: HomeAssistant,
        edit_yaml: EditYaml,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Its thread keeps writing; a later edit_file must not report success
        before that write overwrites it."""
        (tmp_path / "themes").mkdir()
        writing = asyncio.Event()
        release = threading.Event()

        def _blocked_write(path: Path, text: str) -> None:
            hass.loop.call_soon_threadsafe(writing.set)
            release.wait(timeout=30)
            write_text_atomic(path, text)

        monkeypatch.setattr(
            "custom_components.ha_mcp_tools.write_text_atomic", _blocked_write
        )
        edit = asyncio.ensure_future(
            edit_yaml(
                {
                    "file": "themes/dark.yaml",
                    "action": "add",
                    "yaml_path": "dark",
                    "content": "primary-color: red",
                }
            )
        )
        try:
            async with asyncio.timeout(10):
                await writing.wait()
            edit.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            assert file_lock(hass).locked()
        finally:
            release.set()

        (outcome,) = await asyncio.gather(edit, return_exceptions=True)
        assert isinstance(outcome, asyncio.CancelledError)
        assert not file_lock(hass).locked()
        assert (tmp_path / "themes" / "dark.yaml").exists()

    async def test_theme_add_merges_into_existing_theme(
        self, edit_yaml: EditYaml, tmp_path: Path
    ) -> None:
        theme_file = tmp_path / "themes" / "dark-theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)
        theme_file.write_text(
            "dark-theme:\n  primary-color: '#1976D2'\n  text-color: '#FFFFFF'"
        )

        result = await edit_yaml(
            {
                "file": "themes/dark-theme.yaml",
                "action": "add",
                "yaml_path": "dark-theme",
                "content": "accent-color: '#FFC107'",
                "backup": False,
            }
        )

        assert result["success"] is True

        written = theme_file.read_text()
        assert "primary-color: '#1976D2'" in written
        assert "text-color: '#FFFFFF'" in written
        assert "accent-color: '#FFC107'" in written

    async def test_theme_add_type_mismatch_error(
        self, edit_yaml: EditYaml, tmp_path: Path
    ) -> None:
        theme_file = tmp_path / "themes" / "bad-theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)
        theme_file.write_text("bad-theme: not-a-dict")

        result = await edit_yaml(
            {
                "file": "themes/bad-theme.yaml",
                "action": "add",
                "yaml_path": "bad-theme",
                "content": "primary-color: '#1976D2'",
                "backup": False,
            }
        )

        assert result["success"] is False
        assert "Type mismatch" in result["error"]

    async def test_theme_replace_overwrites_content(
        self, edit_yaml: EditYaml, tmp_path: Path
    ) -> None:
        theme_file = tmp_path / "themes" / "light-theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)
        theme_file.write_text(
            "light-theme:\n  primary-color: '#000000'\n  old-key: old-value"
        )

        result = await edit_yaml(
            {
                "file": "themes/light-theme.yaml",
                "action": "replace",
                "yaml_path": "light-theme",
                "content": "primary-color: '#FFFFFF'\nnew-key: new-value",
                "backup": False,
            }
        )

        assert result["success"] is True

        written = theme_file.read_text()
        assert "primary-color: '#FFFFFF'" in written
        assert "new-key: new-value" in written
        assert "old-key" not in written

    async def test_theme_remove_deletes_theme_key(
        self, edit_yaml: EditYaml, tmp_path: Path
    ) -> None:
        theme_file = tmp_path / "themes" / "multi-theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)
        theme_file.write_text(
            "theme1:\n  primary-color: '#111111'\ntheme2:\n  primary-color: '#222222'"
        )

        result = await edit_yaml(
            {
                "file": "themes/multi-theme.yaml",
                "action": "remove",
                "yaml_path": "theme1",
                "backup": False,
            }
        )

        assert result["success"] is True

        written = theme_file.read_text()
        assert "theme1" not in written
        assert "theme2" in written

    async def test_theme_remove_last_theme_leaves_empty_mapping(
        self, edit_yaml: EditYaml, tmp_path: Path
    ) -> None:
        theme_file = tmp_path / "themes" / "single-theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)
        theme_file.write_text("only-theme:\n  primary-color: '#123456'")

        result = await edit_yaml(
            {
                "file": "themes/single-theme.yaml",
                "action": "remove",
                "yaml_path": "only-theme",
                "backup": False,
            }
        )

        assert result["success"] is True
        # ruamel serializes an empty root mapping as "{}"; both that and a
        # truly empty file parse cleanly under !include_dir_merge_named.
        assert theme_file.read_text().strip() in ("", "{}")

    async def test_theme_add_second_theme_to_existing_file(
        self, edit_yaml: EditYaml, tmp_path: Path
    ) -> None:
        theme_file = tmp_path / "themes" / "pair.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)
        theme_file.write_text("theme-a:\n  primary-color: '#111111'")

        result = await edit_yaml(
            {
                "file": "themes/pair.yaml",
                "action": "add",
                "yaml_path": "theme-b",
                "content": "primary-color: '#222222'",
                "backup": False,
            }
        )

        assert result["success"] is True
        written = theme_file.read_text()
        assert "theme-a:" in written
        assert "primary-color: '#111111'" in written
        assert "theme-b:" in written
        assert "primary-color: '#222222'" in written

    async def test_theme_remove_not_found_error(
        self, edit_yaml: EditYaml, tmp_path: Path
    ) -> None:
        theme_file = tmp_path / "themes" / "empty-theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)
        theme_file.write_text("other-theme:\n  primary-color: '#000000'")

        result = await edit_yaml(
            {
                "file": "themes/empty-theme.yaml",
                "action": "remove",
                "yaml_path": "nonexistent",
                "backup": False,
            }
        )

        assert result["success"] is False
        assert "not found" in result["error"]

    async def test_theme_content_must_be_mapping(self, edit_yaml: EditYaml) -> None:
        result = await edit_yaml(
            {
                "file": "themes/new-theme.yaml",
                "action": "add",
                "yaml_path": "new-theme",
                "content": "- not\n- a\n- mapping",
                "backup": False,
            }
        )

        assert result["success"] is False
        assert "must be a YAML mapping" in result["error"]

    async def test_theme_dotted_name_rejected(self, edit_yaml: EditYaml) -> None:
        result = await edit_yaml(
            {
                "file": "themes/bad-name.yaml",
                "action": "add",
                "yaml_path": "my.dotted.theme",
                "content": "primary-color: '#000000'",
                "backup": False,
            }
        )

        assert result["success"] is False
        assert "cannot contain dots" in result["error"]

    async def test_theme_empty_name_rejected(self, edit_yaml: EditYaml) -> None:
        result = await edit_yaml(
            {
                "file": "themes/no-name.yaml",
                "action": "add",
                "yaml_path": "",
                "content": "primary-color: '#000000'",
                "backup": False,
            }
        )

        assert result["success"] is False
        assert "non-empty" in result["error"]

    async def test_theme_path_traversal_blocked(self, edit_yaml: EditYaml) -> None:
        result = await edit_yaml(
            {
                "file": "themes/../secrets.yaml",
                "action": "add",
                "yaml_path": "theme",
                "content": "primary-color: '#000000'",
                "backup": False,
            }
        )

        assert result["success"] is False
        assert "not allowed" in result["error"]

    async def test_theme_dotfile_basename_rejected(self, edit_yaml: EditYaml) -> None:
        """A dotfile theme basename is rejected before write.

        ``themes/.hidden.yaml`` matches the ``themes/*.yaml`` allowlist, but
        HA's ``!include_dir_merge_named`` skips dot-prefixed files, so writing
        it would report a phantom ``reload_performed`` for a theme that never
        loads.
        """
        result = await edit_yaml(
            {
                "file": "themes/.hidden.yaml",
                "action": "add",
                "yaml_path": "Hidden",
                "content": "primary-color: '#000000'",
                "backup": False,
            }
        )

        assert result["success"] is False
        assert "dot-prefixed" in result["error"]

    async def test_theme_dot_prefixed_directory_rejected(
        self, edit_yaml: EditYaml
    ) -> None:
        """A theme file under a dot-prefixed subdirectory is rejected.

        ``themes/.foo/bar.yaml`` matches ``themes/*.yaml`` (fnmatch ``*`` spans
        ``/``) and its basename ``bar.yaml`` is not a dotfile, but HA's
        ``_find_files`` prunes dot-prefixed directories from the walk, so the
        theme never loads — the same phantom ``reload_performed`` the basename
        guard catches, one directory level up.
        """
        result = await edit_yaml(
            {
                "file": "themes/.foo/bar.yaml",
                "action": "add",
                "yaml_path": "Bar",
                "content": "primary-color: '#000000'",
                "backup": False,
            }
        )

        assert result["success"] is False
        assert "dot-prefixed" in result["error"]

    async def test_theme_mid_name_dot_allowed(
        self, edit_yaml: EditYaml, tmp_path: Path
    ) -> None:
        """A dot mid-name (not segment-prefix) is allowed.

        ``themes/my.theme.yaml`` has a dot in the basename, but the segment
        ``my.theme.yaml`` does not START with '.', so HA's loader keeps it and
        the guard must not reject it. Pins the predicate against a regression
        to a naive ``"." in seg`` check.
        """
        theme_file = tmp_path / "themes" / "my.theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)

        result = await edit_yaml(
            {
                "file": "themes/my.theme.yaml",
                "action": "add",
                "yaml_path": "MyTheme",
                "content": "primary-color: '#abcdef'",
                "backup": False,
            }
        )

        assert result["success"] is True
        assert theme_file.exists()

    async def test_theme_reload_failure_degrades_gracefully(
        self, edit_yaml: EditYaml, tmp_path: Path
    ) -> None:
        """Without the frontend loaded, Core has no frontend.reload_themes."""
        theme_file = tmp_path / "themes" / "test-theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)

        result = await edit_yaml(
            {
                "file": "themes/test-theme.yaml",
                "action": "add",
                "yaml_path": "test-theme",
                "content": "primary-color: '#000000'",
                "backup": False,
            }
        )

        assert result["success"] is True
        assert result["post_action"] == "reload_available"
        assert result["reload_service"] == "frontend.reload_themes"
        assert result["reload_error"] == str(
            ServiceNotFound("frontend", "reload_themes")
        )

        # The write itself must have landed despite the failed reload.
        assert "primary-color: '#000000'" in theme_file.read_text()

    async def test_theme_nested_directory_supported(
        self, edit_yaml: EditYaml, tmp_path: Path
    ) -> None:
        theme_file = tmp_path / "themes" / "custom" / "nested-theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)

        result = await edit_yaml(
            {
                "file": "themes/custom/nested-theme.yaml",
                "action": "add",
                "yaml_path": "nested-theme",
                "content": "primary-color: '#123456'",
                "backup": False,
            }
        )

        assert result["success"] is True
        assert theme_file.exists()

    async def test_configuration_yaml_still_works(
        self, edit_yaml: EditYaml, tmp_path: Path
    ) -> None:
        config_file = tmp_path / "configuration.yaml"
        config_file.write_text("homeassistant:\n  name: Home")

        result = await edit_yaml(
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": "template",
                "content": "- sensor:\n    - name: Test\n      state: 'on'",
                "backup": False,
            }
        )

        assert result["success"] is True

    async def test_packages_yaml_still_works(
        self, edit_yaml: EditYaml, tmp_path: Path
    ) -> None:
        package_file = tmp_path / "packages" / "test.yaml"
        package_file.parent.mkdir(parents=True, exist_ok=True)

        result = await edit_yaml(
            {
                "file": "packages/test.yaml",
                "action": "add",
                "yaml_path": "automation",
                "content": "- id: test\n  alias: Test\n  trigger: []",
                "backup": False,
            }
        )

        assert result["success"] is True
