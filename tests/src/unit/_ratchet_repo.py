"""Temporary git repository for the baseline ratchet tests.

The module-size, duplicate-code and type-hint ratchets read tracked and
staged files through git, so their tests run them against a repository of
their own.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"

# Inside a git hook these point every git call at the real repository.
_GIT_HOOK_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_PREFIX",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def load_script(name: str) -> ModuleType:
    """Import ``scripts/<name>.py``, which is not on the import path."""
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_ratchet_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, baseline_name: str
) -> Path:
    """Return an empty git repository with no exclusions and an empty baseline."""
    for name in _GIT_HOOK_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nextend-exclude = []\n", encoding="utf-8"
    )
    baseline = tmp_path / baseline_name
    baseline.parent.mkdir(parents=True)
    baseline.write_text("{}\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "pyproject.toml", baseline_name], cwd=tmp_path, check=True
    )
    return tmp_path


def commit(repo: Path) -> None:
    """Commit what is staged, as a throwaway author."""
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c"],
        cwd=repo,
        check=True,
    )
