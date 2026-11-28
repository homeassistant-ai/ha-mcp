"""The File & YAML Tools entry's file services and what it persists.

``list_files``, ``write_file`` and ``delete_file`` against the files on disk,
and the caller token, extra directories (#1567) and extra YAML write keys
(#1887) kept in ``.storage``. A store that is corrupt, hand-edited or from
another version must never keep the entry from loading, and never widen what it
may touch. Core sets the entry up and the tests call its services the way
ha-mcp does. Which paths a service accepts is covered in
``test_custom_component_path_allowlist.py`` and
``test_custom_component_deny_floor.py``.
"""

import glob
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import (
    SERVICE_DELETE_FILE,
    SERVICE_EDIT_YAML_CONFIG,
    SERVICE_GET_ALLOWED_PATHS,
    SERVICE_GET_CALLER_TOKEN,
    SERVICE_GET_EXTRA_YAML_KEYS,
    SERVICE_LIST_FILES,
    SERVICE_READ_FILE,
    SERVICE_SET_EXTRA_YAML_KEYS,
    SERVICE_WRITE_FILE,
)
from custom_components.ha_mcp_tools.const import DOMAIN

from ._component_filesystem import (
    ALLOWED_PATHS_STORE,
    CALLER_TOKEN_STORE,
    EXTRA_YAML_KEYS_STORE,
    config_dir,
    put,
    use_volume_root,
)
from ._file_services import FileService, make_file_service
from ._symlink_support import symlink_or_skip


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


# -- file services ---


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

    async def test_a_broken_symlink_does_not_fail_the_listing(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        """The link itself is listed; a working link keeps its target's size."""
        await put(hass, {"www/a.txt": "hello world"})
        www = config_dir(hass) / "www"
        symlink_or_skip(www / "broken.txt", www / "missing.txt")
        symlink_or_skip(www / "link.txt", www / "a.txt")

        result = await files(SERVICE_LIST_FILES, {"path": "www"})

        assert result["success"] is True, result
        entries = {f["name"]: f for f in result["files"]}
        assert entries["link.txt"]["size"] == len("hello world")
        assert entries["broken.txt"]["size"] == 0
        assert entries["broken.txt"]["is_dir"] is False

    async def test_modified_is_the_same_iso_time_the_read_reply_gives(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"www/a.txt": "hello"})
        mtime = 1_700_000_000.5
        await hass.async_add_executor_job(
            os.utime, config_dir(hass) / "www" / "a.txt", (mtime, mtime)
        )

        result = await files(SERVICE_LIST_FILES, {"path": "www"})
        read = await files(SERVICE_READ_FILE, {"path": "www/a.txt"})

        (entry,) = result["files"]
        assert entry["modified"] == read["modified"]

    @pytest.mark.parametrize(
        ("error", "message"),
        [
            (PermissionError("denied"), "Permission denied: www"),
            (OSError("disk gone"), "disk gone"),
        ],
    )
    async def test_a_folder_that_cannot_be_read_fails_the_listing(
        self,
        hass: HomeAssistant,
        files: FileService,
        monkeypatch: pytest.MonkeyPatch,
        error: OSError,
        message: str,
    ) -> None:
        await put(hass, {"www/a.txt": "hello"})
        www = config_dir(hass) / "www"
        original = Path.iterdir

        def refuse(path: Path) -> Iterator[Path]:
            if path == www:
                raise error
            return original(path)

        monkeypatch.setattr(Path, "iterdir", refuse)

        result = await files(SERVICE_LIST_FILES, {"path": "www"})

        assert result == {"success": False, "error": message, "files": []}


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


# -- stored settings ---


def _stored(key: str, data: Any, *, version: int = 1) -> dict[str, Any]:
    return {"version": version, "minor_version": 1, "key": key, "data": data}


def _a_store_from_a_newer_version(key: str, data: Any) -> dict[str, Any]:
    """A store Core cannot migrate: loading it raises."""
    return _stored(key, data, version=99)


async def _token(hass: HomeAssistant) -> str:
    response = await hass.services.async_call(
        DOMAIN, SERVICE_GET_CALLER_TOKEN, {}, blocking=True, return_response=True
    )
    assert response is not None
    return str(response["token"])


class TestCallerToken:
    async def test_a_stored_token_is_kept(
        self, hass: HomeAssistant, hass_storage: dict[str, Any]
    ) -> None:
        hass_storage[CALLER_TOKEN_STORE] = _stored(
            CALLER_TOKEN_STORE, {"token": "existing-tok"}
        )

        await make_file_service(hass)

        assert await _token(hass) == "existing-tok"
        assert hass_storage[CALLER_TOKEN_STORE]["data"] == {"token": "existing-tok"}

    async def test_a_first_run_makes_and_stores_a_token(
        self, hass: HomeAssistant, hass_storage: dict[str, Any]
    ) -> None:
        await make_file_service(hass)

        token = await _token(hass)

        assert token
        assert hass_storage[CALLER_TOKEN_STORE]["data"] == {"token": token}

    async def test_a_store_that_cannot_be_loaded_is_replaced_not_fatal(
        self, hass: HomeAssistant, hass_storage: dict[str, Any]
    ) -> None:
        """It must not stop the integration from loading; ha-mcp fetches the
        new token on its next call."""
        hass_storage[CALLER_TOKEN_STORE] = _a_store_from_a_newer_version(
            CALLER_TOKEN_STORE, {"token": "existing-tok"}
        )

        await make_file_service(hass)

        token = await _token(hass)
        assert token
        assert token != "existing-tok"
        assert hass_storage[CALLER_TOKEN_STORE]["data"] == {"token": token}


class TestAllowedPathsStore:
    async def test_stored_entries_are_checked_again_and_the_bad_ones_logged(
        self,
        hass: HomeAssistant,
        hass_storage: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Invalid, duplicate and pre-#2875 ``/backup`` entries are dropped, so
        a hand-edited store cannot load a traversal or floor entry."""
        paths = ["pyscript", "../etc", ".storage", "pyscript", 123, "/backup/x"]
        hass_storage[ALLOWED_PATHS_STORE] = _stored(
            ALLOWED_PATHS_STORE, {"paths": paths}
        )

        with caplog.at_level(logging.WARNING):
            files = await make_file_service(hass)

        assert (await files(SERVICE_GET_ALLOWED_PATHS, {}))["paths"] == ["pyscript"]
        assert "'/backup/x'" in caplog.text

    @pytest.mark.parametrize(
        "stored",
        [
            pytest.param(None, id="a first run"),
            pytest.param({"paths": "not-a-list"}, id="paths that are not a list"),
            pytest.param("not-a-mapping", id="data that is not a mapping"),
        ],
    )
    async def test_a_store_that_holds_nothing_usable_grants_nothing(
        self, hass: HomeAssistant, hass_storage: dict[str, Any], stored: Any
    ) -> None:
        if stored is not None:
            hass_storage[ALLOWED_PATHS_STORE] = _stored(ALLOWED_PATHS_STORE, stored)

        files = await make_file_service(hass)

        assert (await files(SERVICE_GET_ALLOWED_PATHS, {}))["paths"] == []

    async def test_a_store_that_cannot_be_loaded_grants_nothing(
        self, hass: HomeAssistant, hass_storage: dict[str, Any]
    ) -> None:
        hass_storage[ALLOWED_PATHS_STORE] = _a_store_from_a_newer_version(
            ALLOWED_PATHS_STORE, {"paths": ["pyscript"]}
        )

        files = await make_file_service(hass)

        assert (await files(SERVICE_GET_ALLOWED_PATHS, {}))["paths"] == []


class TestExtraYamlKeysStore:
    async def test_stored_keys_are_checked_again(
        self, hass: HomeAssistant, hass_storage: dict[str, Any]
    ) -> None:
        """Whitespace, a denied key, a duplicate and a non-string are dropped;
        the one valid key survives."""
        keys = [" alert2 ", "homeassistant", "alert2", 123]
        hass_storage[EXTRA_YAML_KEYS_STORE] = _stored(
            EXTRA_YAML_KEYS_STORE, {"keys": keys}
        )

        files = await make_file_service(hass)

        assert (await files(SERVICE_GET_EXTRA_YAML_KEYS, {}))["keys"] == ["alert2"]

    @pytest.mark.parametrize(
        "stored",
        [
            pytest.param(None, id="a first run"),
            pytest.param({"keys": "nope"}, id="keys that are not a list"),
        ],
    )
    async def test_a_store_that_holds_nothing_usable_grants_nothing(
        self, hass: HomeAssistant, hass_storage: dict[str, Any], stored: Any
    ) -> None:
        if stored is not None:
            hass_storage[EXTRA_YAML_KEYS_STORE] = _stored(EXTRA_YAML_KEYS_STORE, stored)

        files = await make_file_service(hass)

        assert (await files(SERVICE_GET_EXTRA_YAML_KEYS, {}))["keys"] == []

    async def test_a_store_that_cannot_be_loaded_grants_nothing(
        self, hass: HomeAssistant, hass_storage: dict[str, Any]
    ) -> None:
        hass_storage[EXTRA_YAML_KEYS_STORE] = _a_store_from_a_newer_version(
            EXTRA_YAML_KEYS_STORE, {"keys": ["alert2"]}
        )

        files = await make_file_service(hass)

        assert (await files(SERVICE_GET_EXTRA_YAML_KEYS, {}))["keys"] == []


class TestSetExtraYamlKeys:
    @pytest.mark.parametrize(
        ("sent", "kept", "rejected"),
        [
            pytest.param(
                [" beta ", "alpha", "alpha", "beta"],
                ["alpha", "beta"],
                [],
                id="stripped, deduplicated and sorted",
            ),
            pytest.param(
                ["alert2", "homeassistant", "frontend"],
                ["alert2"],
                ["homeassistant", "frontend"],
                id="a key that redefines a trust boundary is dropped",
            ),
            pytest.param(
                ["ok", "   ", ""], ["ok"], ["   ", ""], id="blank entries are dropped"
            ),
            pytest.param(
                ["Alert2", "MQTT"],
                ["Alert2", "MQTT"],
                [],
                id="case is kept, as Home Assistant's own lookup is exact",
            ),
        ],
    )
    async def test_the_keys_are_cleaned_and_the_rest_reported(
        self,
        files: FileService,
        sent: list[str],
        kept: list[str],
        rejected: list[str],
    ) -> None:
        result = await files(SERVICE_SET_EXTRA_YAML_KEYS, {"keys": sent})

        assert result["success"] is True
        assert result["keys"] == kept
        assert sorted(result["rejected"]) == sorted(rejected)

    async def test_the_keys_are_stored_and_apply_at_once(
        self, files: FileService, hass_storage: dict[str, Any]
    ) -> None:
        await files(SERVICE_SET_EXTRA_YAML_KEYS, {"keys": [" alert2 ", "alert2"]})

        assert hass_storage[EXTRA_YAML_KEYS_STORE]["data"] == {"keys": ["alert2"]}
        current = await files(SERVICE_GET_EXTRA_YAML_KEYS, {})
        assert current["keys"] == ["alert2"]
        assert "homeassistant" in current["deny_floor"]


class TestEditYamlConfig:
    """``edit_yaml_config`` takes the operator's extra keys from the call and
    from the component's own store (#1887)."""

    @staticmethod
    async def _add(files: FileService, key: str, **fields: Any) -> dict[str, Any]:
        return await files(
            SERVICE_EDIT_YAML_CONFIG,
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": key,
                "content": "a: 1\n",
                **fields,
            },
        )

    async def test_recorder_can_be_edited(self, files: FileService) -> None:
        """recorder is YAML-only, so it is editable in configuration.yaml like
        the other plain keys (#1852)."""
        result = await files(
            SERVICE_EDIT_YAML_CONFIG,
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": "recorder",
                "content": "purge_keep_days: 5\n",
            },
        )

        assert result["success"] is True, result

    async def test_a_server_that_still_sends_the_backup_flag_is_not_rejected(
        self, files: FileService
    ) -> None:
        """The component reaches users ahead of the server, and a server older
        than 7.9.0 sends ``backup`` on every call."""
        result = await files(
            SERVICE_EDIT_YAML_CONFIG,
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": "recorder",
                "content": "purge_keep_days: 5\n",
                "backup": True,
            },
        )

        assert result["success"] is True, result

    async def test_a_key_outside_the_allowed_list_is_refused_unless_the_call_adds_it(
        self, files: FileService
    ) -> None:
        refused_edit = await self._add(files, "alert2")
        allowed_edit = await self._add(files, "alert2", extra_allowed_keys=["alert2"])

        assert refused_edit["success"] is False
        assert "not in the allowed list" in refused_edit["error"]
        assert allowed_edit["success"] is True, allowed_edit

    async def test_a_stored_key_and_a_key_on_the_call_are_both_allowed(
        self, files: FileService
    ) -> None:
        await files(SERVICE_SET_EXTRA_YAML_KEYS, {"keys": ["stored1"]})

        stored = await self._add(files, "stored1")
        sent = await self._add(files, "wire1", extra_allowed_keys=["wire1"])
        neither = await self._add(files, "other1")

        assert stored["success"] is True, stored
        assert sent["success"] is True, sent
        assert neither["success"] is False

    async def test_the_deny_floor_cannot_be_lifted_by_the_call_or_the_store(
        self, files: FileService
    ) -> None:
        stored = await files(SERVICE_SET_EXTRA_YAML_KEYS, {"keys": ["homeassistant"]})

        edit = await self._add(
            files, "homeassistant", extra_allowed_keys=["homeassistant"]
        )

        assert stored["keys"] == []
        assert edit["success"] is False
        assert "can never be edited" in edit["error"]
