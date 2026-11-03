"""Writes under a HAOS volume need a mount point (#2875).

Core sees a volume only where one is mounted: Supervisor mounts /share, /media
and /ssl on HAOS and Supervised, and a Container install has only what the user
bind-mounts. An unmounted root may still exist as a plain directory (the
image's own /media, or one an earlier build created); writing there would put
the file in the container layer and report success for a file the user never
sees. A write or delete on a read-only mount (/ssl on HAOS and Supervised)
names the mount instead of a raw errno. Kept out of ``test_custom_component_filesystem.py``, which is at its
module-size baseline.
"""

import errno
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

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

import custom_components.ha_mcp_tools as comp  # noqa: E402
from custom_components.ha_mcp_tools import volume_mounts  # noqa: E402


@pytest.fixture
def volume_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A volume root under tmp_path, standing in for /media; not created."""
    root = tmp_path / "media"
    monkeypatch.setattr(comp, "ALLOWED_VOLUME_ROOTS", (str(root),))
    return root


@pytest.fixture
def mounts(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Mount points as /proc/self/mountinfo lists them, inside a container."""
    mounted: set[str] = set()
    monkeypatch.setattr(volume_mounts, "_in_container", lambda: True)
    monkeypatch.setattr(volume_mounts, "_mount_points", lambda: mounted)
    return mounted


def _mount(mounted: set[str], path: Path) -> None:
    mounted.add(os.path.realpath(path))


def _write(target: Path, tmp_path: Path) -> dict:
    return comp._write_file_sync(
        target, "x", overwrite=False, create_dirs=True, config_dir=tmp_path
    )


def test_write_to_missing_volume_is_refused_not_faked(
    volume_root: Path, mounts: set[str], tmp_path: Path
) -> None:
    result = _write(volume_root / "probe" / "x.txt", tmp_path)

    assert result == {"_error": "not_mounted", "parent": str(volume_root)}
    assert not volume_root.exists()


def test_write_to_unmounted_existing_volume_is_refused_not_faked(
    volume_root: Path, mounts: set[str], tmp_path: Path
) -> None:
    # Container's /media is a plain directory in the image, not a mount.
    volume_root.mkdir()

    result = _write(volume_root / "probe" / "x.txt", tmp_path)

    assert result == {"_error": "not_mounted", "parent": str(volume_root)}
    assert list(volume_root.iterdir()) == []


def test_write_below_mounted_volume_still_creates_directories(
    volume_root: Path, mounts: set[str], tmp_path: Path
) -> None:
    volume_root.mkdir()
    _mount(mounts, volume_root)
    target = volume_root / "probe" / "x.txt"

    result = _write(target, tmp_path)

    assert result["is_new"] is True
    assert target.read_text() == "x"


def test_write_into_a_bind_mount_below_the_volume_is_allowed(
    volume_root: Path, mounts: set[str], tmp_path: Path
) -> None:
    # A user bind-mounts /media/music on Container; /media itself is not mounted.
    music = volume_root / "music"
    music.mkdir(parents=True)
    _mount(mounts, music)
    target = music / "x.txt"

    result = _write(target, tmp_path)

    assert result["is_new"] is True
    assert target.read_text() == "x"


def test_symlink_from_a_mount_into_an_unmounted_folder_is_refused(
    volume_root: Path, mounts: set[str], tmp_path: Path
) -> None:
    # /media/music is mounted; /media/music/link -> /media/stale is not.
    music, stale = volume_root / "music", volume_root / "stale"
    music.mkdir(parents=True)
    stale.mkdir()
    _mount(mounts, music)
    (music / "link").symlink_to(stale)

    result = _write(music / "link" / "x.txt", tmp_path)

    assert result == {"_error": "not_mounted", "parent": str(volume_root)}
    assert list(stale.iterdir()) == []


def test_core_install_writes_to_an_existing_plain_volume(
    volume_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # No container: /share on a Core (venv) install is the host's own folder.
    monkeypatch.setattr(volume_mounts, "_in_container", lambda: False)
    volume_root.mkdir()
    target = volume_root / "x.txt"

    result = _write(target, tmp_path)

    assert result["is_new"] is True
    assert target.read_text() == "x"


def _handler_hass(tmp_path: Path) -> MagicMock:
    hass = MagicMock()
    hass.config.config_dir = str(tmp_path)
    hass.data = {comp.DOMAIN: {"caller_token": "t", "allowed_paths": ["/ssl/t"]}}

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = _run
    return hass


def _raise(err: OSError):
    def _sync(*_args):
        raise err

    return _sync


@pytest.mark.parametrize("build", ["write", "delete"])
async def test_read_only_mount_is_named_not_a_raw_errno(
    build: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On HAOS and Supervised, Supervisor mounts /ssl read-only into Core.
    erofs = OSError(errno.EROFS, "Read-only file system", "/ssl/t")
    monkeypatch.setattr(comp, f"_{build}_file_sync", _raise(erofs))
    handler = getattr(comp, f"_build_{build}_file_handler")(_handler_hass(tmp_path))
    call = MagicMock()
    call.data = {"_ha_mcp_token": "t", "path": "/ssl/t/a.txt", "content": "x"}

    result = await handler(call)

    assert result == {
        "success": False,
        "error": "/ssl/t/a.txt is on a read-only mount in Home Assistant Core"
        " on this install",
    }


def test_other_os_errors_keep_their_own_text() -> None:
    err = OSError(errno.ENOSPC, "No space left on device", "/share/t/a.txt")
    assert volume_mounts.os_error_text(err, "/share/t/a.txt") == str(err)
