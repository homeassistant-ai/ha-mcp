"""Writes under a HAOS volume need a mount point (#2875).

Core sees a volume only where one is mounted: Supervisor mounts /share, /media
and /ssl on HAOS and Supervised, and a Container install has only what the user
bind-mounts. An unmounted root may still exist as a plain directory (the
image's own /media, or one an earlier build created); writing there would put
the file in the container layer and report success for a file the user never
sees. Kept out of ``test_custom_component_filesystem.py``, which is at its
module-size baseline.
"""

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
def mounts(monkeypatch: pytest.MonkeyPatch) -> set[Path]:
    """The paths os.path.ismount reports as mount points; empty to start."""
    mounted: set[Path] = set()
    monkeypatch.setattr(volume_mounts.os.path, "ismount", lambda p: Path(p) in mounted)
    return mounted


def _write(target: Path, tmp_path: Path) -> dict:
    return comp._write_file_sync(
        target, "x", overwrite=False, create_dirs=True, config_dir=tmp_path
    )


def test_write_to_missing_volume_is_refused_not_faked(
    volume_root: Path, mounts: set[Path], tmp_path: Path
) -> None:
    result = _write(volume_root / "probe" / "x.txt", tmp_path)

    assert result == {"_error": "not_mounted", "parent": str(volume_root)}
    assert not volume_root.exists()


def test_write_to_unmounted_existing_volume_is_refused_not_faked(
    volume_root: Path, mounts: set[Path], tmp_path: Path
) -> None:
    # Container's /media is a plain directory in the image, not a mount.
    volume_root.mkdir()

    result = _write(volume_root / "probe" / "x.txt", tmp_path)

    assert result == {"_error": "not_mounted", "parent": str(volume_root)}
    assert list(volume_root.iterdir()) == []


def test_write_below_mounted_volume_still_creates_directories(
    volume_root: Path, mounts: set[Path], tmp_path: Path
) -> None:
    volume_root.mkdir()
    mounts.add(volume_root)
    target = volume_root / "probe" / "x.txt"

    result = _write(target, tmp_path)

    assert result["is_new"] is True
    assert target.read_text() == "x"


def test_write_into_a_bind_mount_below_the_volume_is_allowed(
    volume_root: Path, mounts: set[Path], tmp_path: Path
) -> None:
    # A user bind-mounts /media/music on Container; /media itself is not mounted.
    music = volume_root / "music"
    music.mkdir(parents=True)
    mounts.add(music)
    target = music / "x.txt"

    result = _write(target, tmp_path)

    assert result["is_new"] is True
    assert target.read_text() == "x"
