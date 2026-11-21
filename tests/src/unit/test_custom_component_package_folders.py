"""The packages folder is whichever one ``configuration.yaml`` binds (#1854).

``homeassistant: packages: !include_dir_named <folder>`` names any folder, not
only ``packages``. ``read_file`` and ``list_files`` honour it, so the pre-write
backup can read what the YAML editor writes and ``ha_config_get_yaml`` can
enumerate it (#1788); ``write_file`` and ``delete_file`` never gain package
access. Home Assistant sets the File & YAML Tools entry up, and the tests call
its services the way ha-mcp does.
"""

import os
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import (
    SERVICE_DELETE_FILE,
    SERVICE_LIST_FILES,
    SERVICE_READ_FILE,
    SERVICE_WRITE_FILE,
)

from ._component_filesystem import config_dir, put, refused
from ._file_services import FileService, make_file_service

FOLDERS = ("packages", "custom_packages", "integrations", "pkgs", "other")


@pytest.fixture
async def files(hass: HomeAssistant) -> FileService:
    return await make_file_service(hass)


async def _read(files: FileService, path: str) -> dict[str, Any]:
    return await files(SERVICE_READ_FILE, {"path": path})


async def _list(files: FileService, path: str) -> dict[str, Any]:
    return await files(SERVICE_LIST_FILES, {"path": path})


async def _readable_folders(
    hass: HomeAssistant, files: FileService, config: str | None
) -> set[str]:
    """The folders whose ``x.yaml`` can be read, and ``loose.yaml`` for the
    config root, with ``config`` as ``configuration.yaml``."""
    await put(hass, {f"{folder}/x.yaml": "a: 1\n" for folder in FOLDERS})
    await put(hass, {"loose.yaml": "a: 1\n"})
    if config is not None:
        await put(hass, {"configuration.yaml": config})
    readable = set()
    for name in (*FOLDERS, "loose"):
        path = f"{name}/x.yaml" if name != "loose" else "loose.yaml"
        if (await _read(files, path))["success"]:
            readable.add(name)
    return readable


class TestDetection:
    @pytest.mark.parametrize(
        ("config", "split", "folders"),
        [
            pytest.param("default_config:\n", {}, set(), id="no packages"),
            pytest.param(None, {}, set(), id="no configuration.yaml"),
            pytest.param(
                "homeassistant:\n  packages: !include_dir_named custom_packages\n",
                {},
                {"custom_packages"},
                id="include_dir_named",
            ),
            pytest.param(
                "homeassistant:\n  packages: !include_dir_merge_named integrations\n",
                {},
                {"integrations"},
                id="include_dir_merge_named",
            ),
            pytest.param(
                "homeassistant:\n  packages:\n    grp: !include_dir_named pkgs\n",
                {},
                {"pkgs"},
                id="a named group",
            ),
            pytest.param(
                "homeassistant:\n  packages:\n    inline_pkg:\n      light: []\n",
                {},
                set(),
                id="inline packages declare no folder",
            ),
            pytest.param(
                "homeassistant:\n  packages: !include_dir_named custom_packages\n"
                "recorder:\n  db_url: !secret db_url\n",
                {},
                {"custom_packages"},
                id="other HA tags are ignored",
            ),
            pytest.param(
                "homeassistant:\n  packages: !include_dir_named <config>/integrations\n",
                {},
                {"integrations"},
                id="an absolute include under the config dir is made relative",
            ),
            pytest.param(
                "homeassistant:\n  packages: !include_dir_named /etc/pkgs\n",
                {},
                set(),
                id="an absolute include elsewhere is dropped",
            ),
            pytest.param(
                "homeassistant:\n  packages: !include_dir_named ../evil\n",
                {},
                set(),
                id="a parent escape is dropped",
            ),
            pytest.param(
                "homeassistant:\n  packages: !include_dir_named <config>\n",
                {},
                set(),
                id="the config root is dropped",
            ),
            pytest.param(
                "default_config:\nhomeassistant: !include ha.yaml\n",
                {"ha.yaml": "packages: !include_dir_named integrations\n"},
                {"integrations"},
                id="a homeassistant section split into another file",
            ),
            pytest.param("key: [1, 2\n", {}, set(), id="malformed YAML"),
        ],
    )
    async def test_the_folder_the_config_binds_is_readable(
        self,
        hass: HomeAssistant,
        files: FileService,
        config: str | None,
        split: dict[str, str],
        folders: set[str],
    ) -> None:
        await put(hass, split)
        text = config.replace("<config>", str(config_dir(hass))) if config else None

        readable = await _readable_folders(hass, files, text)

        assert readable == {"packages"} | folders

    async def test_only_yaml_files_in_the_folder_are_readable(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"packages/foo.txt": "a", "packages/foo.yaml": "a: 1\n"})

        assert refused(await _read(files, "packages/foo.txt"))
        assert (await _read(files, "packages/foo.yaml"))["success"] is True

    async def test_a_folder_that_only_shares_the_prefix_is_not_a_packages_folder(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"packagesX/foo.yaml": "a: 1\n"})

        assert refused(await _read(files, "packagesX/foo.yaml"))
        assert refused(await _list(files, "packagesX"))

    async def test_glob_characters_in_a_folder_name_are_literal(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        """A folder named ``pkg[1]`` is its own path, not the pattern ``pkg1``."""
        await put(
            hass,
            {
                "configuration.yaml": "homeassistant:\n  packages: !include_dir_named pkg[1]\n",
                "pkg[1]/foo.yaml": "a: 1\n",
                "pkg1/foo.yaml": "a: 1\n",
            },
        )

        assert (await _read(files, "pkg[1]/foo.yaml"))["success"] is True
        assert refused(await _read(files, "pkg1/foo.yaml"))
        assert not refused(await _list(files, "pkg[1]"))
        assert refused(await _list(files, "pkg1"))


class TestListingAndWriting:
    async def test_the_default_folder_is_listable_with_no_configuration(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"packages/lights.yaml": "a: 1\n"})

        result = await _list(files, "packages")

        assert [f["name"] for f in result["files"]] == ["lights.yaml"]

    async def test_a_configured_folder_and_its_subfolders_are_listable(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(
            hass,
            {
                "configuration.yaml": "homeassistant:\n  packages: !include_dir_named integrations\n",
                "integrations/sub/a.yaml": "a: 1\n",
            },
        )

        assert not refused(await _list(files, "integrations"))
        assert [
            f["name"] for f in (await _list(files, "integrations/sub"))["files"]
        ] == ["a.yaml"]

    async def test_a_folder_that_is_not_bound_is_not_listable(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"integrations/a.yaml": "a: 1\n"})

        assert refused(await _list(files, "integrations"))

    async def test_a_configured_folder_can_neither_be_written_nor_deleted(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        """``edit_yaml_config`` is the only write path to config YAML."""
        await put(
            hass,
            {
                "configuration.yaml": "homeassistant:\n  packages: !include_dir_named integrations\n",
                "integrations/a.yaml": "a: 1\n",
            },
        )

        written = await files(
            SERVICE_WRITE_FILE,
            {"path": "integrations/a.yaml", "content": "b: 2\n", "overwrite": True},
        )
        deleted = await files(SERVICE_DELETE_FILE, {"path": "integrations/a.yaml"})

        assert refused(written)
        assert refused(deleted)
        assert (config_dir(hass) / "integrations" / "a.yaml").read_text() == "a: 1\n"

    async def test_a_folder_bound_to_storage_stays_closed(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        """A widened allow decision does not outrank the deny floor."""
        await put(
            hass,
            {
                "configuration.yaml": "homeassistant:\n  packages: !include_dir_named .storage\n",
                ".storage/a.yaml": "a: 1\n",
            },
        )

        assert refused(await _list(files, ".storage"))
        assert refused(await _read(files, ".storage/a.yaml"))


class TestDetectionFollowsEdits:
    """Detection runs on every file operation, and a glob fires one per matched
    file, so it is cached against the modified time of every file it read."""

    @staticmethod
    def _bind(folder: str) -> str:
        return f"homeassistant:\n  packages: !include_dir_named {folder}\n"

    async def test_editing_configuration_yaml_moves_the_folder(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"pkgs/x.yaml": "a: 1\n", "other/x.yaml": "a: 1\n"})
        await put(hass, {"configuration.yaml": self._bind("pkgs")})
        assert (await _read(files, "pkgs/x.yaml"))["success"] is True

        await put(hass, {"configuration.yaml": self._bind("other")})
        os.utime(config_dir(hass) / "configuration.yaml", ns=(0, 0))

        assert refused(await _read(files, "pkgs/x.yaml"))
        assert (await _read(files, "other/x.yaml"))["success"] is True

    async def test_editing_an_included_file_moves_the_folder(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        """Keyed on ``configuration.yaml`` alone, a split ``homeassistant:``
        section would serve a stale folder forever."""
        await put(hass, {"pkgs/x.yaml": "a: 1\n", "moved/x.yaml": "a: 1\n"})
        await put(
            hass,
            {
                "configuration.yaml": "homeassistant: !include ha.yaml\n",
                "ha.yaml": "packages: !include_dir_named pkgs\n",
            },
        )
        assert (await _read(files, "pkgs/x.yaml"))["success"] is True

        await put(hass, {"ha.yaml": "packages: !include_dir_named moved\n"})
        os.utime(config_dir(hass) / "ha.yaml", ns=(0, 0))

        assert refused(await _read(files, "pkgs/x.yaml"))
        assert (await _read(files, "moved/x.yaml"))["success"] is True

    async def test_creating_an_include_that_was_missing_moves_the_folder(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(
            hass,
            {
                "pkgs/x.yaml": "a: 1\n",
                "configuration.yaml": "homeassistant: !include later.yaml\n",
            },
        )
        assert refused(await _read(files, "pkgs/x.yaml"))

        await put(hass, {"later.yaml": "packages: !include_dir_named pkgs\n"})

        assert (await _read(files, "pkgs/x.yaml"))["success"] is True

    async def test_a_file_whose_modified_time_is_unchanged_is_not_read_again(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        """What keeps a glob over many files from parsing ``configuration.yaml``
        once per match."""
        await put(hass, {"pkgs/x.yaml": "a: 1\n", "other/x.yaml": "a: 1\n"})
        await put(hass, {"configuration.yaml": self._bind("pkgs")})
        path = config_dir(hass) / "configuration.yaml"
        stamp = path.stat().st_mtime_ns
        assert (await _read(files, "pkgs/x.yaml"))["success"] is True

        await put(hass, {"configuration.yaml": self._bind("other")})
        os.utime(path, ns=(stamp, stamp))

        assert (await _read(files, "pkgs/x.yaml"))["success"] is True
        assert refused(await _read(files, "other/x.yaml"))
