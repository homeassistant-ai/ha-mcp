"""Temporary git repository for the baseline ratchet tests.

The module-size and duplicate-code ratchets read tracked and staged files
through git, so their tests run them against a repository of their own.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def make_ratchet_repo(tmp_path: Path, baseline_name: str) -> Path:
    """Return an empty git repository with no exclusions and an empty baseline.

    Git isolation comes from ``_unit_test_env``, so call this from a test or a
    function-scoped fixture.
    """
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
