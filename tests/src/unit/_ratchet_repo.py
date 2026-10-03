"""Temporary git repository for the baseline ratchet tests.

The module-size and duplicate-code ratchets read tracked and staged files
through git, so their tests run them against a repository of their own.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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
