"""Writes under a HAOS volume need a mount point (#2875).

Core sees a volume only where one is mounted: Supervisor mounts /share, /media
and /ssl on HAOS and Supervised, and a Container install has only what the user
bind-mounts. An unmounted root may still exist as a plain directory (the
image's own /media, or one an earlier build created); writing there would put
the file in the container layer and report success for a file the user never
sees. A write or delete on a read-only mount (/ssl on HAOS and Supervised)
names the mount instead of a raw errno. Kept out of ``test_custom_component_filesystem.py``, which is at its
module-size baseline.

Home Assistant sets the File & YAML Tools entry up, and the tests call its
``write_file`` / ``delete_file`` services the way ha-mcp does, with the volume
configured as an allowed directory.
"""

import errno
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

import custom_components.ha_mcp_tools as comp
from custom_components.ha_mcp_tools import (
    SERVICE_DELETE_FILE,
    SERVICE_WRITE_FILE,
    volume_mounts,
)

from ._file_services import FileService, make_file_service


@pytest.fixture
def volume_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A volume root under tmp_path, standing in for /media; not created."""
    root = tmp_path / "media"
    monkeypatch.setattr(comp, "ALLOWED_VOLUME_ROOTS", (str(root),))
    return root


@pytest.fixture
async def write_file(hass: HomeAssistant, volume_root: Path) -> FileService:
    return await make_file_service(hass, str(volume_root))


@pytest.fixture
def mounts(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Mount points as /proc/self/mountinfo lists them, inside a container."""
    mounted: set[str] = set()
    monkeypatch.setattr(volume_mounts, "_in_container", lambda: True)
    monkeypatch.setattr(volume_mounts, "_mount_points", lambda: mounted)
    return mounted


def _mount(mounted: set[str], path: Path) -> None:
    mounted.add(os.path.realpath(path))


async def _write(write_file: FileService, target: Path) -> dict[str, Any]:
    return await write_file(SERVICE_WRITE_FILE, {"path": str(target), "content": "x"})


def _not_mounted(root: Path) -> dict[str, Any]:
    return {
        "success": False,
        "error": f"{root} is not mounted into Home Assistant Core on this install",
    }


async def test_write_to_missing_volume_is_refused_not_faked(
    hass: HomeAssistant, volume_root: Path, mounts: set[str], write_file: FileService
) -> None:
    result = await _write(write_file, volume_root / "probe" / "x.txt")

    assert result == _not_mounted(volume_root)
    assert not await hass.async_add_executor_job(volume_root.exists)


async def test_write_to_unmounted_existing_volume_is_refused_not_faked(
    hass: HomeAssistant, volume_root: Path, mounts: set[str], write_file: FileService
) -> None:
    # Container's /media is a plain directory in the image, not a mount.
    await hass.async_add_executor_job(volume_root.mkdir)

    result = await _write(write_file, volume_root / "probe" / "x.txt")

    assert result == _not_mounted(volume_root)
    assert await hass.async_add_executor_job(os.listdir, volume_root) == []


async def test_write_below_mounted_volume_still_creates_directories(
    hass: HomeAssistant, volume_root: Path, mounts: set[str], write_file: FileService
) -> None:
    await hass.async_add_executor_job(volume_root.mkdir)
    _mount(mounts, volume_root)
    target = volume_root / "probe" / "x.txt"

    result = await _write(write_file, target)

    assert result["created"] is True
    assert target.read_text() == "x"


async def test_write_into_a_bind_mount_below_the_volume_is_allowed(
    volume_root: Path, mounts: set[str], write_file: FileService
) -> None:
    # A user bind-mounts /media/music on Container; /media itself is not mounted.
    music = volume_root / "music"
    music.mkdir(parents=True)
    _mount(mounts, music)
    target = music / "x.txt"

    result = await _write(write_file, target)

    assert result["created"] is True
    assert target.read_text() == "x"


async def test_symlink_from_a_mount_into_an_unmounted_folder_is_refused(
    volume_root: Path, mounts: set[str], write_file: FileService
) -> None:
    # /media/music is mounted; /media/music/link -> /media/stale is not.
    music, stale = volume_root / "music", volume_root / "stale"
    music.mkdir(parents=True)
    stale.mkdir()
    _mount(mounts, music)
    (music / "link").symlink_to(stale)

    result = await _write(write_file, music / "link" / "x.txt")

    assert result == _not_mounted(volume_root)
    assert list(stale.iterdir()) == []


async def test_core_install_writes_to_an_existing_plain_volume(
    hass: HomeAssistant,
    volume_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_file: FileService,
) -> None:
    # No container: /share on a Core (venv) install is the host's own folder.
    monkeypatch.setattr(volume_mounts, "_in_container", lambda: False)
    await hass.async_add_executor_job(volume_root.mkdir)
    target = volume_root / "x.txt"

    result = await _write(write_file, target)

    assert result["created"] is True
    assert target.read_text() == "x"


def _raise(err: OSError) -> Callable[..., dict[str, Any]]:
    def _sync(*_args: object) -> dict[str, Any]:
        raise err

    return _sync


@pytest.mark.parametrize("build", ["write", "delete"])
async def test_read_only_mount_is_named_not_a_raw_errno(
    build: str, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On HAOS and Supervised, Supervisor mounts /ssl read-only into Core.
    erofs = OSError(errno.EROFS, "Read-only file system", "/ssl/t")
    monkeypatch.setattr(comp, f"_{build}_file_sync", _raise(erofs))
    call = await make_file_service(hass, "/ssl/t")
    data: dict[str, Any] = {"path": "/ssl/t/a.txt"}
    if build == "write":
        data["content"] = "x"
    service = SERVICE_WRITE_FILE if build == "write" else SERVICE_DELETE_FILE

    result = await call(service, data)

    assert result == {
        "success": False,
        "error": "/ssl/t/a.txt is on a read-only mount in Home Assistant Core"
        " on this install",
    }


def test_other_os_errors_keep_their_own_text() -> None:
    err = OSError(errno.ENOSPC, "No space left on device", "/share/t/a.txt")
    assert volume_mounts.os_error_text(err, "/share/t/a.txt") == str(err)
