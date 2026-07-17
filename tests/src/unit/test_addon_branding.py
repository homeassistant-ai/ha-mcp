"""Unit tests for the Supervisor add-on icon/logo assets.

Guards against the assets silently going missing or malformed again (see
https://github.com/homeassistant-ai/ha-mcp/discussions/1893) — both flavors
had no icon.png/logo.png at all until this test was added.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_ADDON_DIRS = (
    _REPO_ROOT / "homeassistant-addon",
    _REPO_ROOT / "homeassistant-addon-dev",
)


def _read_png_size(path: Path) -> tuple[int, int]:
    """Return (width, height) from a PNG's IHDR chunk, raising if not a PNG."""
    header = path.read_bytes()[:24]
    assert header[:8] == _PNG_SIGNATURE, f"{path} is not a valid PNG"
    width, height = struct.unpack(">II", header[16:24])
    return width, height


@pytest.mark.parametrize("addon_dir", _ADDON_DIRS, ids=lambda p: p.name)
def test_icon_is_square_png(addon_dir: Path) -> None:
    """Per HA's app presentation docs, icon.png must be square (1:1)."""
    width, height = _read_png_size(addon_dir / "icon.png")
    assert width == height, f"{addon_dir}/icon.png must be square, got {width}x{height}"


@pytest.mark.parametrize("addon_dir", _ADDON_DIRS, ids=lambda p: p.name)
def test_logo_is_valid_png(addon_dir: Path) -> None:
    """logo.png just needs to be a valid PNG; aspect ratio is flexible."""
    _read_png_size(addon_dir / "logo.png")
