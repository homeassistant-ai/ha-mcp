"""Writes never create a missing HAOS volume root (#2875).

On a Container install Supervisor mounts none of /share, /media or /ssl into
Core. Creating the root would put the file in the container layer and report
success for a file the user never sees. Kept out of
``test_custom_component_filesystem.py``, which is at its module-size baseline.
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


@pytest.fixture
def volume_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A volume root under tmp_path, standing in for /ssl; not created."""
    root = tmp_path / "ssl"
    monkeypatch.setattr(comp, "ALLOWED_VOLUME_ROOTS", (str(root),))
    return root


def test_write_to_unmounted_volume_is_refused_not_faked(
    volume_root: Path, tmp_path: Path
) -> None:
    target = volume_root / "probe" / "x.txt"

    result = comp._write_file_sync(
        target, "x", overwrite=False, create_dirs=True, config_dir=tmp_path
    )

    assert result == {"_error": "no_parent", "parent": str(volume_root)}
    assert not volume_root.exists()


def test_write_below_mounted_volume_still_creates_directories(
    volume_root: Path, tmp_path: Path
) -> None:
    volume_root.mkdir()
    target = volume_root / "probe" / "x.txt"

    result = comp._write_file_sync(
        target, "x", overwrite=False, create_dirs=True, config_dir=tmp_path
    )

    assert result["is_new"] is True
    assert target.read_text() == "x"
