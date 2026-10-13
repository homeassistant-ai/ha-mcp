"""The HAOS lanes build the dev app from a staged copy of the repo.

Supervisor builds the image with the app directory as the build context, so
every repo file the dev Dockerfile copies must be staged into it. A file left
out fails the build inside HAOS, which the lanes only see as an "unknown
error" from Supervisor.
"""

from __future__ import annotations

import sys
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
    flags = _conftest_embedded._EMBEDDED_FEATURE_FLAGS
    # The holder edits the shared flags at import; restore them afterwards.
    monkeypatch.setitem(
        flags, "enable_strict_mandatory_bps", flags["enable_strict_mandatory_bps"]
    )
    monkeypatch.setitem(flags, "enable_dev_mode", False)
    monkeypatch.syspath_prepend(str(_REPO_ROOT / "tests" / "src"))
    module = types.ModuleType("tests.src.e2e._dev_env_holder")
    module.__file__ = str(run_path)
    module.__package__ = "tests.src.e2e"
    exec(
        compile(source_path.read_text(encoding="utf-8"), str(run_path), "exec"),
        module.__dict__,
    )
    return module


def test_the_dev_env_runs_its_embedded_server_in_developer_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/dev-ha-env.md updates the embedded server with ha_dev_manage_server,
    which only a server in developer mode exposes."""
    from tests.src.e2e import _conftest_embedded

    _load_dev_env_holder(monkeypatch)

    assert _conftest_embedded._EMBEDDED_FEATURE_FLAGS["enable_dev_mode"] is True
