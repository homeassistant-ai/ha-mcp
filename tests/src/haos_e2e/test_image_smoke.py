"""Cheap pre-boot smoke test for the HAOS test image artifact.

Validates the build → publish → consume pipeline before the conftest
fixtures attempt to boot the qcow2. If this fails, the canary tests
will also fail but with much noisier output; this gives a one-line
"image staging is broken" signal.

(Previously also called ``qemu-img info`` to validate qcow2 magic, but
that's impossible to do once the session conftest has booted QEMU
against the file — QEMU holds an exclusive lock. The successful boot
in conftest is a stronger validity check anyway.)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


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
