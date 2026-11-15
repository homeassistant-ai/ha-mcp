"""``list_files``, ``write_file`` and ``delete_file`` against the files on disk.

Home Assistant sets the File & YAML Tools entry up, and the tests call its
services the way ha-mcp does. Which paths a service accepts is covered in
``test_custom_component_path_allowlist.py`` and
``test_custom_component_deny_floor.py``.
"""

import glob
import os
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import (
    SERVICE_DELETE_FILE,
    SERVICE_LIST_FILES,
    SERVICE_WRITE_FILE,
)

from ._component_filesystem import config_dir, put, use_volume_root
from ._file_services import FileService, make_file_service


@pytest.fixture
async def files(hass: HomeAssistant) -> FileService:
    return await make_file_service(hass)


@pytest.fixture
def volume(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A volume outside the config directory, as /share is on HAOS."""
    return use_volume_root(monkeypatch, tmp_path_factory.mktemp("share"))


@pytest.fixture
async def volume_files(hass: HomeAssistant, volume: Path) -> FileService:
    return await make_file_service(hass, str(volume))


class TestListFiles:
    async def test_folders_come_first_then_files_by_name(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(
            hass, {"www/b.txt": "world", "www/a.txt": "hello", "www/sub/inner.txt": "x"}
        )

        result = await files(SERVICE_LIST_FILES, {"path": "www"})

        assert result["success"] is True
        assert [f["name"] for f in result["files"]] == ["sub", "a.txt", "b.txt"]
        assert result["count"] == 3
        entries = {f["name"]: f for f in result["files"]}
        assert (entries["a.txt"]["size"], entries["a.txt"]["is_dir"]) == (5, False)
        assert (entries["sub"]["size"], entries["sub"]["is_dir"]) == (0, True)
        assert entries["a.txt"]["path"] == os.path.join("www", "a.txt")

    @pytest.mark.parametrize(
        ("path", "error"),
        [
            ("www/missing", "Directory does not exist: www/missing"),
            ("www/a.txt", "Path is not a directory: www/a.txt"),
        ],
    )
    async def test_a_path_that_is_not_a_folder_is_reported(
        self, hass: HomeAssistant, files: FileService, path: str, error: str
    ) -> None:
        await put(hass, {"www/a.txt": "hello"})

        result = await files(SERVICE_LIST_FILES, {"path": path})

        assert result == {"success": False, "error": error, "files": []}

    async def test_a_pattern_keeps_only_matching_names(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"www/a.yaml": "a", "www/b.yaml": "b", "www/c.txt": "c"})

        result = await files(SERVICE_LIST_FILES, {"path": "www", "pattern": "*.yaml"})

        assert sorted(f["name"] for f in result["files"]) == ["a.yaml", "b.yaml"]

    async def test_a_bracketed_name_is_absent_from_its_own_expansion(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        """The premise ha_config_get_yaml's double lookup rests on.

        A bracket class never matches the literal name it is written as, so a
        file called ``svc[a].yaml`` is invisible to its own name used as a
        pattern; the escaped form finds it.
        """
        await put(hass, {"www/svc[a].yaml": "a", "www/svca.yaml": "b"})

        as_pattern = await files(
            SERVICE_LIST_FILES, {"path": "www", "pattern": "svc[a].yaml"}
        )
        as_literal = await files(
            SERVICE_LIST_FILES,
            {"path": "www", "pattern": glob.escape("svc[a].yaml")},
        )

        assert [f["name"] for f in as_pattern["files"]] == ["svca.yaml"]
        assert [f["name"] for f in as_literal["files"]] == ["svc[a].yaml"]

    async def test_a_volume_listing_reports_absolute_paths(
        self, hass: HomeAssistant, volume: Path, volume_files: FileService
    ) -> None:
        """A volume is not under the config directory, so its entries cannot be
        reported relative to it."""
        await hass.async_add_executor_job((volume / "note.txt").write_text, "hi")

        result = await volume_files(SERVICE_LIST_FILES, {"path": str(volume)})

        assert result["success"] is True, result
        assert [f["path"] for f in result["files"]] == [str(volume / "note.txt")]


class TestWriteFile:
    async def test_creates_the_file_and_its_folders(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        result = await files(
            SERVICE_WRITE_FILE, {"path": "www/sub/x.txt", "content": "hello"}
        )

        assert result["success"] is True
        assert result["created"] is True
        assert result["size"] == 5
        assert (config_dir(hass) / "www" / "sub" / "x.txt").read_text() == "hello"

    async def test_an_existing_file_is_kept_unless_overwrite_is_set(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"www/x.txt": "original"})
        target = config_dir(hass) / "www" / "x.txt"

        refused_write = await files(
            SERVICE_WRITE_FILE, {"path": "www/x.txt", "content": "new"}
        )
        assert refused_write == {
            "success": False,
            "error": "File already exists: www/x.txt. Set overwrite=true to replace.",
        }
        assert target.read_text() == "original"

        replaced = await files(
            SERVICE_WRITE_FILE,
            {"path": "www/x.txt", "content": "new", "overwrite": True},
        )
        assert replaced["success"] is True
        assert replaced["created"] is False
        assert target.read_text() == "new"

    async def test_a_missing_folder_is_reported_when_not_to_be_created(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        result = await files(
            SERVICE_WRITE_FILE,
            {"path": "www/missing_dir/x.txt", "content": "hi", "create_dirs": False},
        )

        assert result == {
            "success": False,
            "error": "Parent directory does not exist: www/missing_dir",
        }
        assert not (config_dir(hass) / "www" / "missing_dir").exists()

    async def test_a_missing_volume_folder_is_reported_absolute(
        self, volume: Path, volume_files: FileService
    ) -> None:
        """A volume folder is not under the config directory (#1586)."""
        result = await volume_files(
            SERVICE_WRITE_FILE,
            {
                "path": str(volume / "missing" / "x.txt"),
                "content": "data",
                "create_dirs": False,
            },
        )

        assert result == {
            "success": False,
            "error": f"Parent directory does not exist: {volume / 'missing'}",
        }


class TestDeleteFile:
    async def test_removes_the_file_and_reports_its_size(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"www/x.txt": "hello"})

        result = await files(SERVICE_DELETE_FILE, {"path": "www/x.txt"})

        assert result["success"] is True
        assert result["deleted_size"] == 5
        assert not (config_dir(hass) / "www" / "x.txt").exists()

    async def test_a_missing_file_is_reported(self, files: FileService) -> None:
        result = await files(SERVICE_DELETE_FILE, {"path": "www/missing.txt"})

        assert result == {
            "success": False,
            "error": "File does not exist: www/missing.txt",
        }

    async def test_a_folder_is_not_deleted(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"www/sub/inner.txt": "x"})

        result = await files(SERVICE_DELETE_FILE, {"path": "www/sub"})

        assert result == {
            "success": False,
            "error": "Path is not a file (cannot delete directories): www/sub",
        }
        assert (config_dir(hass) / "www" / "sub" / "inner.txt").exists()
