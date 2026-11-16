"""Pre-#1579 YAML backups: the migration out of ``www/`` and the read-only
services over ``.ha_mcp_tools_backups/``.

Older versions wrote backups under ``www/yaml_backups/``, which Home Assistant
serves unauthenticated at ``/local/`` (GHSA-g39v-cvjh-8fpf). Setting the File &
YAML Tools entry up moves them and raises a persistent notification. Core sets
the entry up, and the tests call its services the way ha-mcp does.
"""

import os
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import WebSocketGenerator

from custom_components.ha_mcp_tools import (
    SERVICE_LIST_LEGACY_BACKUPS,
    SERVICE_READ_LEGACY_BACKUP,
)
from custom_components.ha_mcp_tools.const import DOMAIN

from ._component_core import ws_result
from ._component_filesystem import LEGACY_BACKUP_DIR, config_dir, put
from ._file_services import FileService, make_file_service
from ._symlink_support import symlink_or_skip

NOTIFICATION = "ha_mcp_tools_ghsa_g39v_cvjh_8fpf"
LEGACY = "www/yaml_backups"


def _names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


async def _notifications(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> dict[str, Any]:
    """The persistent notifications the user sees, as the frontend asks for them."""
    assert await async_setup_component(hass, "persistent_notification", {})
    shown = await ws_result(await hass_ws_client(hass), "persistent_notification/get")
    return {note["notification_id"]: note for note in shown}


class TestMigration:
    async def test_without_a_legacy_folder_nothing_is_created_or_announced(
        self, hass: HomeAssistant, hass_ws_client: WebSocketGenerator
    ) -> None:
        await make_file_service(hass)

        assert not (config_dir(hass) / LEGACY_BACKUP_DIR).exists()
        assert not (config_dir(hass) / LEGACY).exists()
        assert NOTIFICATION not in await _notifications(hass, hass_ws_client)

    async def test_backups_are_moved_out_of_www_and_the_user_is_told(
        self, hass: HomeAssistant, hass_ws_client: WebSocketGenerator
    ) -> None:
        await put(
            hass,
            {
                f"{LEGACY}/configuration.yaml.20260101_120000.bak": "a: 1",
                f"{LEGACY}/packages_test.yaml.20260102_120000.bak": "b: 2",
            },
        )

        await make_file_service(hass)

        root = config_dir(hass)
        assert _names(root / LEGACY_BACKUP_DIR) == [
            "configuration.yaml.20260101_120000.bak",
            "packages_test.yaml.20260102_120000.bak",
        ]
        assert not (root / LEGACY).exists()
        message = (await _notifications(hass, hass_ws_client))[NOTIFICATION]["message"]
        assert "Moved 2 YAML backup file(s)" in message
        assert "GHSA-g39v-cvjh-8fpf" in message

    async def test_a_backup_of_the_same_name_is_not_overwritten(
        self, hass: HomeAssistant
    ) -> None:
        await put(
            hass,
            {f"{LEGACY}/x.bak": "from legacy", f"{LEGACY_BACKUP_DIR}/x.bak": "kept"},
        )

        await make_file_service(hass)

        moved = config_dir(hass) / LEGACY_BACKUP_DIR
        assert (moved / "x.bak").read_text() == "kept"
        assert (moved / "x.legacy.bak").read_text() == "from legacy"

    async def test_a_taken_legacy_name_gets_the_next_number(
        self, hass: HomeAssistant
    ) -> None:
        await put(
            hass,
            {
                f"{LEGACY}/x.bak": "incoming",
                f"{LEGACY_BACKUP_DIR}/x.bak": "already here",
                f"{LEGACY_BACKUP_DIR}/x.legacy.bak": "older legacy",
            },
        )

        await make_file_service(hass)

        moved = config_dir(hass) / LEGACY_BACKUP_DIR
        assert (moved / "x.bak").read_text() == "already here"
        assert (moved / "x.legacy.bak").read_text() == "older legacy"
        assert (moved / "x.legacy1.bak").read_text() == "incoming"

    async def test_files_that_are_not_backups_keep_the_folder_in_place(
        self, hass: HomeAssistant
    ) -> None:
        await put(
            hass,
            {
                f"{LEGACY}/stray_subdir/inner.txt": "x",
                f"{LEGACY}/config.bak": "data",
                f"{LEGACY}/notes.txt": "user dropped this",
            },
        )

        await make_file_service(hass)

        legacy = config_dir(hass) / LEGACY
        assert _names(legacy) == ["notes.txt", "stray_subdir"]
        assert (legacy / "notes.txt").read_text() == "user dropped this"
        assert _names(config_dir(hass) / LEGACY_BACKUP_DIR) == ["config.bak"]

    async def test_a_link_is_left_where_it_is(self, hass: HomeAssistant) -> None:
        """Moving it would dereference it."""
        await put(hass, {f"{LEGACY}/real.bak": "real content", "elsewhere.bak": "t"})
        legacy = config_dir(hass) / LEGACY
        symlink_or_skip(legacy / "link.bak", config_dir(hass) / "elsewhere.bak")

        await make_file_service(hass)

        assert _names(config_dir(hass) / LEGACY_BACKUP_DIR) == ["real.bak"]
        assert (legacy / "link.bak").is_symlink()


async def _list(files: FileService) -> dict[str, Any]:
    return await files(SERVICE_LIST_LEGACY_BACKUPS, {})


async def _read(files: FileService, filename: str) -> dict[str, Any]:
    return await files(SERVICE_READ_LEGACY_BACKUP, {"filename": filename})


@pytest.fixture
async def files(hass: HomeAssistant) -> FileService:
    return await make_file_service(hass)


class TestListLegacyBackups:
    @pytest.mark.parametrize(
        ("name", "file_path", "timestamp", "ambiguous"),
        [
            pytest.param(
                "configuration.yaml.20260101_120000.bak",
                "configuration.yaml",
                "20260101_120000",
                False,
                id="configuration.yaml",
            ),
            pytest.param(
                "packages_lights.yaml.20260102_010203.bak",
                "packages/lights.yaml",
                "20260102_010203",
                False,
                id="a flat package",
            ),
            pytest.param(
                "themes_dark.yaml.20260102_010203.bak",
                "themes/dark.yaml",
                "20260102_010203",
                False,
                id="a flat theme",
            ),
            pytest.param(
                "packages_my_lights.yaml.20260102_010203.bak",
                "packages/my_lights.yaml",
                "20260102_010203",
                True,
                id="an underscore could be a literal one or a nested separator",
            ),
            pytest.param(
                "automations.yaml.20260101_120000.bak",
                None,
                "20260101_120000",
                True,
                id="a name that maps to no allowed write target",
            ),
            pytest.param("x.bak", None, None, True, id="no timestamp"),
            pytest.param(
                "configuration.yaml.20260101_120000.legacy.bak",
                None,
                None,
                True,
                id="a migration-renamed backup",
            ),
            pytest.param("random-file.bak", None, None, True, id="any other name"),
        ],
    )
    async def test_the_original_path_is_decoded_only_when_it_is_unambiguous(
        self,
        hass: HomeAssistant,
        files: FileService,
        name: str,
        file_path: str | None,
        timestamp: str | None,
        ambiguous: bool,
    ) -> None:
        """The old naming replaced every separator with ``_``, so a guess at the
        original path must never be restored to."""
        await put(hass, {f"{LEGACY_BACKUP_DIR}/{name}": "a: 1"})

        (backup,) = (await _list(files))["backups"]

        assert backup["filename"] == name
        assert backup["file_path"] == file_path
        assert backup["timestamp"] == timestamp
        assert backup["path_ambiguous"] is ambiguous

    async def test_without_a_backup_folder_the_list_is_empty(
        self, files: FileService
    ) -> None:
        assert await _list(files) == {"success": True, "backups": [], "count": 0}

    async def test_only_regular_bak_files_are_listed(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(
            hass,
            {
                f"{LEGACY_BACKUP_DIR}/configuration.yaml.20260101_120000.bak": "a: 1",
                f"{LEGACY_BACKUP_DIR}/notes.txt": "not a backup",
                f"{LEGACY_BACKUP_DIR}/subdir/inner.bak": "x",
                "outside.bak": "x: 9",
            },
        )
        root = config_dir(hass)
        symlink_or_skip(root / LEGACY_BACKUP_DIR / "link.bak", root / "outside.bak")

        result = await _list(files)

        assert [b["filename"] for b in result["backups"]] == [
            "configuration.yaml.20260101_120000.bak"
        ]
        assert result["count"] == 1
        assert result["backups"][0]["size"] == len("a: 1")

    async def test_the_newest_backup_comes_first(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        old = "configuration.yaml.20260101_120000.bak"
        new = "themes_dark.yaml.20260201_120000.bak"
        await put(
            hass,
            {
                f"{LEGACY_BACKUP_DIR}/{old}": "a: 1",
                f"{LEGACY_BACKUP_DIR}/{new}": "b: 2",
            },
        )
        folder = config_dir(hass) / LEGACY_BACKUP_DIR
        os.utime(folder / old, (1000, 1000))
        os.utime(folder / new, (2000, 2000))

        result = await _list(files)

        assert [b["filename"] for b in result["backups"]] == [new, old]


class TestReadLegacyBackup:
    async def test_returns_the_content_with_what_the_name_decodes_to(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        name = "configuration.yaml.20260101_120000.bak"
        await put(hass, {f"{LEGACY_BACKUP_DIR}/{name}": "a: 1\nb: 2\n"})

        result = await _read(files, name)

        assert result["success"] is True
        assert result["content"] == "a: 1\nb: 2\n"
        assert result["size"] == len("a: 1\nb: 2\n")
        assert result["file_path"] == "configuration.yaml"
        assert result["path_ambiguous"] is False
        assert result["timestamp"] == "20260101_120000"

    async def test_a_missing_backup_is_reported(self, files: FileService) -> None:
        assert await _read(files, "nope.bak") == {
            "success": False,
            "error": "Backup does not exist: nope.bak",
        }

    async def test_a_folder_named_like_a_backup_is_not_a_file(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {f"{LEGACY_BACKUP_DIR}/adir.bak/inner.txt": "x"})

        assert await _read(files, "adir.bak") == {
            "success": False,
            "error": "Path is not a file: adir.bak",
        }

    async def test_a_link_is_not_followed(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {f"{LEGACY_BACKUP_DIR}/real.bak": "a: 1"})
        folder = config_dir(hass) / LEGACY_BACKUP_DIR
        symlink_or_skip(folder / "link.bak", folder / "real.bak")

        assert await _read(files, "link.bak") == {
            "success": False,
            "error": "Path is not a file: link.bak",
        }

    @pytest.mark.parametrize(
        "filename", ["../configuration.yaml.bak", "sub/x.bak", "x.txt", "..", ""]
    )
    async def test_only_a_bare_bak_name_from_the_backup_folder_is_read(
        self, hass: HomeAssistant, files: FileService, filename: str
    ) -> None:
        await put(hass, {"configuration.yaml.bak": "a: 1", "sub/x.bak": "x"})

        result = await _read(files, filename)

        assert result["success"] is False
        assert "Invalid backup filename" in result["error"]


class TestServiceWiring:
    """The read-only legacy-backup services stay registered, token-gated, and
    confined to ``.ha_mcp_tools_backups/``."""

    @pytest.mark.parametrize(
        ("service", "data"),
        [
            (SERVICE_LIST_LEGACY_BACKUPS, {}),
            (SERVICE_READ_LEGACY_BACKUP, {"filename": "x.bak"}),
        ],
    )
    @pytest.mark.parametrize("token", [None, "not-the-token"])
    async def test_a_caller_without_the_token_is_refused_before_any_file_access(
        self,
        hass: HomeAssistant,
        files: FileService,
        service: str,
        data: dict[str, Any],
        token: str | None,
    ) -> None:
        await put(hass, {f"{LEGACY_BACKUP_DIR}/x.bak": "a: 1"})
        presented = {} if token is None else {"_ha_mcp_token": token}

        result = await hass.services.async_call(
            DOMAIN, service, {**data, **presented}, blocking=True, return_response=True
        )

        assert result is not None
        assert result["error_code"] == "unauthorized"
        assert result["success"] is False
        assert "content" not in result
        assert not result.get("backups")

    async def test_the_services_go_with_the_entry(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        names = (SERVICE_LIST_LEGACY_BACKUPS, SERVICE_READ_LEGACY_BACKUP)
        assert all(hass.services.has_service(DOMAIN, name) for name in names)
        (entry,) = hass.config_entries.async_entries(DOMAIN)

        assert await hass.config_entries.async_unload(entry.entry_id)

        assert not any(hass.services.has_service(DOMAIN, name) for name in names)
