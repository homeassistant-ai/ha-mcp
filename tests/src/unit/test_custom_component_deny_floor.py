"""The deny floor: paths no configuration can open (#1567, #1586).

A configured extra folder or volume can never reach ``.storage``, a
``secrets.yaml`` that is not the masked one in the config root, or the approval
PIN file (#2502), however the path is spelled or linked. Home Assistant sets the
File & YAML Tools entry up, and the tests call its services the way ha-mcp does.
"""

import shutil
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

from ._component_filesystem import config_dir, put, refused, use_volume_root
from ._file_services import FileService, make_file_service
from ._symlink_support import symlink_or_skip


@pytest.fixture
async def files(hass: HomeAssistant) -> FileService:
    return await make_file_service(hass)


async def _read(files: FileService, path: str) -> dict[str, Any]:
    return await files(SERVICE_READ_FILE, {"path": path})


async def _write(files: FileService, path: str) -> dict[str, Any]:
    return await files(SERVICE_WRITE_FILE, {"path": path, "content": "x"})


async def _allow(files: FileService, *paths: str) -> dict[str, Any]:
    return await files(SERVICE_SET_ALLOWED_PATHS, {"paths": list(paths)})


class TestDeniedPaths:
    async def test_storage_cannot_be_added_and_stays_closed(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {".storage/auth": "tokens"})

        added = await _allow(files, ".storage")

        assert added["paths"] == []
        assert refused(await _read(files, ".storage/auth"))
        assert refused(await _write(files, ".storage/auth"))
        assert refused(await files(SERVICE_LIST_FILES, {"path": ".storage"}))
        assert refused(await files(SERVICE_DELETE_FILE, {"path": ".storage/auth"}))

    @pytest.mark.parametrize("path", [".STORAGE/auth", ".Storage/auth", ".STORAGE"])
    async def test_storage_is_closed_in_any_case(
        self, files: FileService, path: str
    ) -> None:
        """On a case-insensitive filesystem ``.STORAGE`` opens the real one."""
        assert refused(await _read(files, path))
        assert refused(await _write(files, path))

    async def test_the_config_root_secrets_are_read_masked(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"secrets.yaml": "api_key: SECRET\n"})

        result = await _read(files, "secrets.yaml")

        assert result["content"] == 'api_key: "[MASKED]"'

    @pytest.mark.parametrize("path", ["pyscript/secrets.yaml", "pyscript/SECRETS.YAML"])
    async def test_another_secrets_file_is_refused_as_it_would_be_unmasked(
        self, hass: HomeAssistant, files: FileService, path: str
    ) -> None:
        await put(hass, {path: "api_key: SECRET\n", "pyscript/foo.py": "a"})
        await _allow(files, "pyscript")

        assert refused(await _read(files, path))
        assert (await _read(files, "pyscript/foo.py"))["success"] is True

    @pytest.mark.parametrize(
        "path", [".ha_mcp/approval_pin.json", ".ha_mcp/APPROVAL_PIN.JSON"]
    )
    async def test_the_approval_pin_file_is_closed_to_every_service(
        self, hass: HomeAssistant, files: FileService, path: str
    ) -> None:
        """Without the floor a tool could read the digest, or replace it with
        one for a PIN of its own (#2502)."""
        await put(hass, {path: "{}"})
        await _allow(files, ".ha_mcp")

        assert refused(await _read(files, path))
        assert refused(
            await files(
                SERVICE_WRITE_FILE,
                {"path": path, "content": "{}", "overwrite": True},
            )
        )
        assert refused(await files(SERVICE_DELETE_FILE, {"path": path}))

    async def test_the_rest_of_the_pin_files_folder_stays_reachable(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        """Protecting the PIN must not take the folder around it with it."""
        await put(hass, {".ha_mcp/tool_policy.json": "{}"})
        await _allow(files, ".ha_mcp")

        assert (await _read(files, ".ha_mcp/tool_policy.json"))["success"] is True
        written = await files(
            SERVICE_WRITE_FILE,
            {"path": ".ha_mcp/tool_policy.json", "content": "{}", "overwrite": True},
        )
        assert written["success"] is True

    async def test_a_listing_does_not_enumerate_the_denied_names(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        """Otherwise a folder an operator added would report the size and
        modified time of a file no service will open."""
        await put(
            hass,
            {
                ".ha_mcp/approval_pin.json": "{}",
                ".ha_mcp/tool_policy.json": "{}",
                "www/.storage/x": "x",
                "www/style.css": "body{}",
                "www/APPROVAL_PIN.JSON": "{}",
                "www/Secrets.YAML": "k: v",
            },
        )
        await _allow(files, ".ha_mcp")

        policy = await files(SERVICE_LIST_FILES, {"path": ".ha_mcp"})
        www = await files(SERVICE_LIST_FILES, {"path": "www"})

        assert [f["name"] for f in policy["files"]] == ["tool_policy.json"]
        assert [f["name"] for f in www["files"]] == ["style.css"]


class TestSymlinks:
    """A link is judged by where it ends up."""

    async def test_a_link_into_storage_is_closed(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"evil/x": "x", ".storage/auth": "tokens"})
        await _allow(files, "evil")
        root = config_dir(hass)
        shutil.rmtree(root / "evil")
        symlink_or_skip(root / "evil", root / ".storage")

        assert refused(await _read(files, "evil/auth"))
        assert refused(await _write(files, "evil/auth2"))

    @pytest.mark.parametrize(
        ("target", "content"),
        [
            ("secrets.yaml", "api_key: SECRET\n"),
            (".ha_mcp/approval_pin.json", "{}"),
        ],
    )
    async def test_a_renamed_link_to_a_denied_file_is_closed(
        self, hass: HomeAssistant, files: FileService, target: str, content: str
    ) -> None:
        """Masking keys off the literal ``secrets.yaml``, so a link to it would
        be returned unmasked."""
        await put(hass, {target: content, "www/x": "x"})
        root = config_dir(hass)
        symlink_or_skip(root / "www" / "notes.txt", root / target)

        assert refused(await _read(files, "www/notes.txt"))

    async def test_the_root_secrets_exception_cannot_be_pointed_elsewhere(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {".ha_mcp/approval_pin.json": "{}"})
        root = config_dir(hass)
        symlink_or_skip(root / "secrets.yaml", root / ".ha_mcp" / "approval_pin.json")

        assert refused(await _read(files, "secrets.yaml"))

    async def test_a_link_followed_by_dotdot_cannot_climb_out_of_the_config_dir(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        """``www/link/..`` is ``www`` to a path normaliser, but the kernel
        follows the link first and then goes up from its real folder (#1586)."""
        await put(hass, {"www/x": "x"})
        root = config_dir(hass)
        escaped = root.parent / "escaped-by-link.txt"
        escaped.write_text("outside")
        symlink_or_skip(root / "www" / "link", root)
        path = "www/link/../escaped-by-link.txt"

        assert refused(await _read(files, path))
        assert refused(await _write(files, path))

    async def test_a_link_out_of_the_config_dir_is_closed(
        self,
        hass: HomeAssistant,
        files: FileService,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        outside = tmp_path_factory.mktemp("outside")
        (outside / "x").write_text("outside")
        await put(hass, {"www/x": "x"})
        symlink_or_skip(config_dir(hass) / "www" / "link", outside)

        assert refused(await _read(files, "www/link/x"))
        assert (await _read(files, "www/x"))["success"] is True


class TestConfigDirBelowAStorageFolder:
    """The floor looks at the part of a path below the config directory, so a
    config directory that itself sits under a ``.storage`` folder is not
    closed."""

    @pytest.fixture
    def hass_config_dir(self, tmp_path: Path) -> str:
        config = tmp_path / ".storage" / "config"
        config.mkdir(parents=True)
        return str(config)

    async def test_its_own_folders_stay_open_and_a_storage_below_it_stays_closed(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"www/x.css": "body{}", ".storage/auth": "tokens"})

        assert (await _read(files, "www/x.css"))["success"] is True
        assert (await _write(files, "www/new.css"))["success"] is True
        assert refused(await _read(files, ".storage/auth"))


class TestVolumeLinks:
    """The same holds for a configured volume (#1586)."""

    @pytest.fixture
    def volume(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Path:
        return use_volume_root(monkeypatch, tmp_path_factory.mktemp("vol"))

    async def test_a_link_followed_by_dotdot_cannot_climb_out_of_the_volume(
        self, files: FileService, volume: Path
    ) -> None:
        (volume.parent / "escaped.txt").write_text("outside")
        symlink_or_skip(volume / "link", volume.parent)
        await _allow(files, str(volume))
        path = str(volume / "link" / ".." / "escaped.txt")

        assert refused(await _read(files, path))
        assert refused(await _write(files, path))

    async def test_a_link_inside_the_volume_is_followed(
        self, files: FileService, volume: Path
    ) -> None:
        (volume / "real").mkdir()
        (volume / "real" / "f.txt").write_text("inside")
        symlink_or_skip(volume / "link", volume / "real")
        await _allow(files, str(volume))

        result = await _read(files, str(volume / "link" / "f.txt"))

        assert result["content"] == "inside"

    async def test_a_renamed_link_to_secrets_inside_the_volume_is_closed(
        self, files: FileService, volume: Path
    ) -> None:
        """It stays inside the volume root, so only the floor on the resolved
        target stops it; a volume's files are never masked."""
        (volume / "secrets.yaml").write_text("api_key: SECRET\n")
        symlink_or_skip(volume / "notes.txt", volume / "secrets.yaml")
        await _allow(files, str(volume))

        assert refused(await _read(files, str(volume / "notes.txt")))

    async def test_a_link_to_storage_inside_the_volume_is_closed(
        self, files: FileService, volume: Path
    ) -> None:
        (volume / ".storage").mkdir()
        (volume / ".storage" / "auth").write_text("tokens")
        symlink_or_skip(volume / "link", volume / ".storage")
        await _allow(files, str(volume))
        path = str(volume / "link" / "auth")

        assert refused(await _read(files, path))
        assert refused(await _write(files, path + "2"))

    async def test_a_folder_that_is_a_link_to_secrets_cannot_be_added(
        self, files: FileService, volume: Path
    ) -> None:
        (volume / "secrets.yaml").write_text("x: y\n")
        symlink_or_skip(volume / "innocent", volume / "secrets.yaml")

        result = await _allow(files, str(volume / "innocent"))

        assert result["paths"] == []

    @pytest.mark.parametrize("name", [".storage/auth", "secrets.yaml"])
    async def test_the_floor_holds_on_a_volume(
        self, files: FileService, volume: Path, name: str
    ) -> None:
        (volume / name).parent.mkdir(exist_ok=True)
        (volume / name).write_text("x")
        await _allow(files, str(volume))

        path = str(volume / name)

        assert refused(await _read(files, path))
        replaced = await files(
            SERVICE_WRITE_FILE, {"path": path, "content": "y", "overwrite": True}
        )
        assert refused(replaced)
        assert (volume / name).read_text() == "x"
