"""Smoke test for the HAOS test image artifact.

Validates the build → publish → consume pipeline end-to-end without yet
booting the image: the artifact is present at the expected path, is a
non-trivial size, and is recognised by ``qemu-img info`` as a valid qcow2.

Real lifecycle tests (boot HAOS, install ha-mcp PR build via Supervisor,
exercise addon-aware tools) land in follow-up commits on this branch
once the conftest fixture refactor is in.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.haos


def _image_path() -> Path:
    raw = os.environ.get("HAOS_TEST_IMAGE_PATH")
    if not raw:
        pytest.skip("HAOS_TEST_IMAGE_PATH not set — workflow has not staged the image")
    return Path(raw)


def test_image_artifact_present() -> None:
    path = _image_path()
    assert path.exists(), f"qcow2 missing at {path}"
    # Vanilla HAOS alone decompresses to several GB. Anything under 100 MB
    # almost certainly means the download/extract failed silently.
    size_mb = path.stat().st_size / 1024 / 1024
    assert size_mb > 100, f"qcow2 implausibly small ({size_mb:.1f} MB) — likely corrupt"


def test_image_is_valid_qcow2() -> None:
    path = _image_path()
    if not shutil.which("qemu-img"):
        pytest.skip("qemu-img not installed on this runner")
    result = subprocess.run(
        ["qemu-img", "info", "--output=json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert '"format": "qcow2"' in result.stdout, (
        f"qemu-img did not recognise {path} as qcow2:\n{result.stdout}"
    )
