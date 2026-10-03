"""The HAOS lanes build the dev app from a staged copy of the repo.

Supervisor builds the image with the app directory as the build context, so
every repo file the dev Dockerfile copies must be staged into it. A file left
out fails the build inside HAOS, which the lanes only see as an "unknown
error" from Supervisor.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.src import haos_runtime

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "tests" / "haos_image_build"))

import build_image  # noqa: E402

# Copied as a tree by its own step.
_STAGED_SEPARATELY = {"src"}


def _repo_files_the_dockerfile_copies() -> set[str]:
    dockerfile = _REPO_ROOT / "homeassistant-addon-dev" / "Dockerfile"
    sources: set[str] = set()
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        words = line.split()
        if not words or words[0] != "COPY" or any("--from=" in w for w in words):
            continue
        sources.update(word.rstrip("/") for word in words[1:-1])
    return sources - _STAGED_SEPARATELY


@pytest.mark.parametrize(
    "module", [build_image, haos_runtime], ids=["image bake", "per-PR refresh"]
)
def test_the_staged_dev_app_context_holds_every_file_the_dockerfile_copies(
    module: object,
) -> None:
    expected = _repo_files_the_dockerfile_copies()
    assert expected, "found no COPY sources in the dev Dockerfile"
    staged = set(module.DEV_ADDON_REPO_FILES) | {  # type: ignore[attr-defined]
        f"homeassistant-addon/{name}"
        for name in module.STABLE_ADDON_FILES  # type: ignore[attr-defined]
    }

    assert sorted(expected - staged) == []
