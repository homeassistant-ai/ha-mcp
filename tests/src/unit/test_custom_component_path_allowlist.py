"""Which paths ``read_file``, ``list_files``, ``write_file`` and ``delete_file``
accept: the built-in folders, the extra directories an admin adds with
``set_allowed_paths``, and the HAOS sibling volumes (#1567, #1586).

The paths that stay refused whatever is configured are in
``test_custom_component_deny_floor.py``. Home Assistant sets the File & YAML
Tools entry up, and the tests call its services the way ha-mcp does.
"""

from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import (
    SERVICE_DELETE_FILE,
    SERVICE_LIST_FILES,
    SERVICE_READ_FILE,
    SERVICE_SET_ALLOWED_PATHS,
    SERVICE_WRITE_FILE,
)
from custom_components.ha_mcp_tools.const import ALLOWED_VOLUME_ROOTS

from ._component_filesystem import put, refused, use_volume_roots
from ._file_services import FileService, make_file_service


@pytest.fixture
async def files(hass: HomeAssistant) -> FileService:
    return await make_file_service(hass)


async def _read(files: FileService, path: str) -> dict[str, Any]:
    return await files(SERVICE_READ_FILE, {"path": path})


async def _list(files: FileService, path: str) -> dict[str, Any]:
    return await files(SERVICE_LIST_FILES, {"path": path})


async def _write(files: FileService, path: str) -> dict[str, Any]:
    return await files(SERVICE_WRITE_FILE, {"path": path, "content": "x"})


async def _allow(files: FileService, *paths: str) -> dict[str, Any]:
    return await files(SERVICE_SET_ALLOWED_PATHS, {"paths": list(paths)})


async def _dropped(files: FileService, entry: str) -> None:
    """The entry is refused and nothing is granted."""
    result = await _allow(files, entry)

    assert result["paths"] == []
    assert result["rejected"] == [entry]


class TestBuiltInFolders:
    @pytest.mark.parametrize(
        "folder",
        [
            "www",
            "themes",
            "custom_templates",
            "dashboards",
            "blueprints",
            "blueprints/automation/author",
        ],
    )
    async def test_these_folders_can_be_listed(
        self, hass: HomeAssistant, files: FileService, folder: str
    ) -> None:
        await put(hass, {f"{folder}/x.txt": "x"})

        result = await _list(files, folder)

        assert result["success"] is True
        assert [f["name"] for f in result["files"]] == ["x.txt"]

    @pytest.mark.parametrize(
        ("path", "stored_as"),
        [
            ("configuration.yaml", None),
            ("automations.yaml", None),
            ("scripts.yaml", None),
            ("scenes.yaml", None),
            ("secrets.yaml", None),
            ("home-assistant.log.fault", None),
            # home-assistant.log is read where Core writes it:
            # test_custom_component_read_file.TestCoreLogFile.
            ("www/test.css", None),
            ("www/subdir/test.js", None),
            ("themes/dark.yaml", None),
            ("packages/lights.yaml", None),
            ("dashboards/sub/nested.yaml", None),
            ("blueprints/automation/homeassistant/motion_light.yaml", None),
            ("blueprints/script/author/thing.yaml", None),
            ("custom_components/my_integration/init.py", None),
            ("custom_components/my_integration/__init__.py", None),
            ("www/../secrets.yaml", "secrets.yaml"),
        ],
    )
    async def test_these_files_can_be_read(
        self,
        hass: HomeAssistant,
        files: FileService,
        path: str,
        stored_as: str | None,
    ) -> None:
        """A path that climbs out of a folder is fine while it stays in the
        config directory: ``www/../secrets.yaml`` is ``secrets.yaml``, which is
        readable (masked)."""
        await put(hass, {stored_as or path: "a: 1\n", "www/x": "x"})

        result = await _read(files, path)

        assert result["success"] is True, result

    @pytest.mark.parametrize(
        ("path", "stored_as"),
        [
            ("random_file.txt", "random_file.txt"),
            ("deps/some_file", "deps/some_file"),
            (".storage/auth", ".storage/auth"),
            ("../etc/passwd", None),
            ("../../etc/passwd", None),
            ("/etc/passwd", None),
        ],
    )
    async def test_these_files_cannot_be_read(
        self,
        hass: HomeAssistant,
        files: FileService,
        path: str,
        stored_as: str | None,
    ) -> None:
        if stored_as:
            await put(hass, {stored_as: "x"})

        assert refused(await _read(files, path))

    @pytest.mark.parametrize(
        "path",
        [
            "www/test.css",
            "themes/dark.yaml",
            "custom_templates/test.jinja2",
            "dashboards/main.yaml",
        ],
    )
    async def test_these_folders_can_be_written(
        self, hass: HomeAssistant, files: FileService, path: str
    ) -> None:
        result = await _write(files, path)

        assert result["success"] is True
        assert (Path(hass.config.config_dir) / path).read_text() == "x"

    @pytest.mark.parametrize(
        "path",
        [
            "configuration.yaml",
            "new_file.yaml",
            "blueprints/automation/author/thing.yaml",
            "custom_components/my_integration/__init__.py",
            "packages/lights.yaml",
            "../outside.txt",
            "/etc/outside.txt",
        ],
    )
    async def test_these_paths_cannot_be_written(
        self, hass: HomeAssistant, files: FileService, path: str
    ) -> None:
        """Blueprints are readable but not writable (#1965), and a packages
        folder is only reachable through ``edit_yaml_config``."""
        result = await _write(files, path)

        assert refused(result), result
        assert not (Path(hass.config.config_dir) / path).exists()

    @pytest.mark.parametrize(
        "path",
        [
            "configuration.yaml",
            "secrets.yaml",
            "../etc/passwd",
            "www/../secrets.yaml",
            "www/../../etc/passwd",
            "/etc/passwd",
            "/www/test.css",
            ".storage/",
            ".storage/auth",
            "custom_components/",
        ],
    )
    async def test_these_paths_cannot_be_listed(
        self, hass: HomeAssistant, files: FileService, path: str
    ) -> None:
        await put(hass, {"configuration.yaml": "a: 1\n", ".storage/auth": "x"})

        assert refused(await _list(files, path))

    async def test_a_packages_file_cannot_be_deleted(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"packages/lights.yaml": "a: 1\n"})

        result = await files(SERVICE_DELETE_FILE, {"path": "packages/lights.yaml"})

        assert refused(result)
        assert (Path(hass.config.config_dir) / "packages" / "lights.yaml").exists()


class TestExtraDirectories:
    """A configured extra directory grants read, write and list."""

    @pytest.mark.parametrize(
        ("entry", "stored"),
        [
            ("pyscript", "pyscript"),
            ("python_scripts", "python_scripts"),
            ("  pyscript/  ", "pyscript"),
            ("foo/bar", "foo/bar"),
        ],
    )
    async def test_a_folder_is_stored_cleaned(
        self, files: FileService, entry: str, stored: str
    ) -> None:
        result = await _allow(files, entry)

        assert result["paths"] == [stored]
        assert result["rejected"] == []

    @pytest.mark.parametrize(
        "entry",
        [
            "",
            "   ",
            ".",
            "..",
            "../etc",
            "www/../../etc",
            "/etc/passwd",
            "/pyscript",
            ".storage",
            ".storage/auth",
            "www/../.storage",
        ],
    )
    async def test_a_folder_that_leaves_the_config_directory_or_hits_the_floor_is_dropped(
        self, files: FileService, entry: str
    ) -> None:
        await _dropped(files, entry)

    async def test_a_granted_folder_can_be_read_written_and_listed(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"pyscript/foo.py": "a", "pyscript/sub/bar.py": "b"})
        await _allow(files, "pyscript")

        assert (await _read(files, "pyscript/foo.py"))["content"] == "a"
        assert (await _read(files, "pyscript/sub/bar.py"))["content"] == "b"
        assert (await _write(files, "pyscript/new.py"))["success"] is True
        listed = await _list(files, "pyscript")
        assert [f["name"] for f in listed["files"]] == ["sub", "foo.py", "new.py"]

    async def test_other_folders_stay_closed_and_the_built_ins_stay_open(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"esphome/x.yaml": "a", "pyscript/foo.py": "b"})
        await _allow(files, "pyscript")

        assert refused(await _read(files, "esphome/x.yaml"))
        assert refused(await _write(files, "esphome/new.yaml"))
        assert (await _write(files, "www/x.css"))["success"] is True

    async def test_a_folder_is_closed_until_it_is_configured(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"pyscript/foo.py": "a"})

        assert refused(await _read(files, "pyscript/foo.py"))

        await _allow(files, "pyscript")

        assert (await _read(files, "pyscript/foo.py"))["success"] is True

    async def test_removing_a_folder_closes_it_again(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"pyscript/foo.py": "a"})
        await _allow(files, "pyscript")
        await _allow(files)

        assert refused(await _read(files, "pyscript/foo.py"))

    async def test_a_nested_entry_grants_only_that_folder_and_what_is_below_it(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        """The entry ``foo/bar`` is a path boundary: not its parent, a sibling,
        or a folder that only shares its prefix."""
        await put(
            hass,
            {
                "foo/bar/x.py": "a",
                "foo/other/x.py": "b",
                "foo/x.py": "c",
                "foo/barbaz/x.py": "d",
            },
        )
        await _allow(files, "foo/bar")

        assert (await _read(files, "foo/bar/x.py"))["success"] is True
        assert (await _write(files, "foo/bar/new.py"))["success"] is True
        for outside in ("foo/other/x.py", "foo/x.py", "foo/barbaz/x.py"):
            assert refused(await _read(files, outside)), outside


class TestVolumes:
    """A volume is an absolute path, enforced against its own root rather than
    the config directory; a configured one grants read, write and list."""

    @pytest.fixture
    def volumes(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[Path, Path]:
        share = tmp_path_factory.mktemp("share")
        media = tmp_path_factory.mktemp("media")
        use_volume_roots(monkeypatch, share, media)
        return share, media

    @pytest.mark.parametrize("root", ALLOWED_VOLUME_ROOTS)
    async def test_each_volume_root_can_be_added(
        self, files: FileService, root: str
    ) -> None:
        assert (await _allow(files, root))["paths"] == [root]

    @pytest.mark.parametrize(
        ("entry", "stored"),
        [
            ("/share/llm", "/share/llm"),
            ("  /media/tts/  ", "/media/tts"),
        ],
    )
    async def test_a_folder_of_a_volume_is_stored_cleaned(
        self, files: FileService, entry: str, stored: str
    ) -> None:
        assert (await _allow(files, entry))["paths"] == [stored]

    @pytest.mark.parametrize(
        "entry",
        [
            "/etc/passwd",
            "/",
            "/backup/abc.tar",
            "/config/secrets.yaml",
            "/shared",
            "/ssl_extra/x",
            "/share/.storage",
            "/share/.storage/auth",
            "/media/secrets.yaml",
            "/share/../etc",
        ],
    )
    async def test_a_path_that_is_not_in_a_volume_or_hits_the_floor_is_dropped(
        self, files: FileService, entry: str
    ) -> None:
        """``/shared`` is not ``/share``, and a ``..`` that leaves the volume
        leaves it."""
        await _dropped(files, entry)

    async def test_a_configured_volume_can_be_read_written_and_listed(
        self,
        hass: HomeAssistant,
        files: FileService,
        volumes: tuple[Path, Path],
    ) -> None:
        share, _ = volumes
        await hass.async_add_executor_job((share / "n.md").write_text, "note")
        await _allow(files, str(share))

        assert (await _read(files, str(share / "n.md")))["content"] == "note"
        assert not refused(await _read(files, str(share)))
        assert (await _write(files, str(share / "x.txt")))["success"] is True
        listed = await _list(files, str(share))
        assert [f["name"] for f in listed["files"]] == ["n.md", "x.txt"]

    async def test_a_volume_is_closed_until_it_is_configured(
        self,
        hass: HomeAssistant,
        files: FileService,
        volumes: tuple[Path, Path],
    ) -> None:
        share, media = volumes
        await hass.async_add_executor_job((media / "x").write_text, "x")
        await hass.async_add_executor_job((share / "x").write_text, "x")

        assert refused(await _read(files, str(share / "x")))

        await _allow(files, str(share))

        assert (await _read(files, str(share / "x")))["success"] is True
        assert refused(await _read(files, str(media / "x")))
        assert refused(await _write(files, str(media / "new")))

    async def test_a_volume_folder_entry_keeps_to_its_folder(
        self,
        hass: HomeAssistant,
        files: FileService,
        volumes: tuple[Path, Path],
    ) -> None:
        share, _ = volumes
        for folder in ("llm", "other", "llmx"):
            await hass.async_add_executor_job((share / folder).mkdir)
            await hass.async_add_executor_job((share / folder / "a").write_text, "a")
        await _allow(files, str(share / "llm"))

        assert (await _read(files, str(share / "llm" / "a")))["success"] is True
        assert refused(await _read(files, str(share / "other" / "a")))
        assert refused(await _read(files, str(share / "llmx" / "a")))

    async def test_a_volume_and_a_config_folder_can_be_configured_together(
        self,
        hass: HomeAssistant,
        files: FileService,
        volumes: tuple[Path, Path],
    ) -> None:
        share, _ = volumes
        await put(hass, {"pyscript/foo.py": "a", "esphome/x": "b"})
        await hass.async_add_executor_job((share / "foo").write_text, "c")
        await _allow(files, "pyscript", str(share))

        assert (await _read(files, "pyscript/foo.py"))["success"] is True
        assert (await _read(files, str(share / "foo")))["success"] is True
        assert refused(await _read(files, "esphome/x"))
