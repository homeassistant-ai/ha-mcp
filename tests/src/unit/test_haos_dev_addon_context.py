"""The HAOS lanes build the dev app from a staged copy of the repo.

Supervisor builds the image with the app directory as the build context, so
every repo file the dev Dockerfile copies must be staged into it. A file left
out fails the build inside HAOS, which the lanes only see as an "unknown
error" from Supervisor.
"""

from __future__ import annotations

import shutil
import sys
import tarfile
import types
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


def _load_dev_env_holder(monkeypatch: pytest.MonkeyPatch) -> object:
    """Import ``.github/dev-ha-env/hold.py`` as the workflow runs it.

    The workflow copies the file into ``tests/src/e2e/``, so it resolves the
    repo root from that location and uses that package's relative imports;
    compiling it under that path reproduces both without copying.
    """
    from tests.src.e2e import _conftest_embedded

    source_path = _REPO_ROOT / ".github" / "dev-ha-env" / "hold.py"
    run_path = _REPO_ROOT / "tests" / "src" / "e2e" / "hold.py"
    monkeypatch.setenv("TRACK_REF", "unit-test")
    monkeypatch.setitem(
        _conftest_embedded._EMBEDDED_FEATURE_FLAGS,
        "enable_strict_mandatory_bps",
        _conftest_embedded._EMBEDDED_FEATURE_FLAGS["enable_strict_mandatory_bps"],
    )
    monkeypatch.syspath_prepend(str(_REPO_ROOT / "tests" / "src"))
    module = types.ModuleType("tests.src.e2e._dev_env_holder")
    module.__file__ = str(run_path)
    module.__package__ = "tests.src.e2e"
    exec(
        compile(source_path.read_text(encoding="utf-8"), str(run_path), "exec"),
        module.__dict__,
    )
    return module


@pytest.mark.skipif(shutil.which("tar") is None, reason="needs the tar CLI")
def test_the_dev_env_holder_stages_every_file_its_dockerfile_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``hold.py`` builds the app context itself, so check the archive it ships."""
    holder = _load_dev_env_holder(monkeypatch)

    archive = holder.build_dev_addon_source_tar(tmp_path, "abc1234")  # type: ignore[attr-defined]

    with tarfile.open(archive) as tar:
        members = set(tar.getnames())
        dockerfile = tar.extractfile("ha_mcp_dev/Dockerfile")
        assert dockerfile is not None
        lines = dockerfile.read().decode("utf-8").splitlines()
    sources: set[str] = set()
    for line in lines:
        words = line.split()
        if not words or words[0] != "COPY" or any("--from=" in w for w in words):
            continue
        sources.update(word.rstrip("/") for word in words[1:-1])
    assert sources, "found no COPY sources in the staged Dockerfile"
    missing = sorted(
        source
        for source in sources
        if f"ha_mcp_dev/{source}" not in members
        and not any(name.startswith(f"ha_mcp_dev/{source}/") for name in members)
    )
    assert missing == []
