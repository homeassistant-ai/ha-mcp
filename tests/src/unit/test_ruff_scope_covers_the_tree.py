"""The CI ruff invocation must cover every directory that holds Python.

``pr.yml`` passes ruff an explicit directory list while lefthook lints
``**/*.py``, so a new top-level directory gets linted on commit but not in CI —
which is exactly how ``packaging/`` went uncovered. This pins the list to the
tree, so the next one fails here instead of going quietly unlinted.

Precedent for asserting on workflow contents: ``test_triage_prompt_budget.py``.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PR_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr.yml"
_RUFF_CHECK = "uv run ruff check "


def _ruff_check_dirs() -> set[str]:
    """Path arguments of the workflow's ``uv run ruff check`` step.

    Parsed as text rather than YAML so this test needs no third-party import;
    the command is a single unbroken line in ``pr.yml``.
    """
    matches = [
        line.split(_RUFF_CHECK, 1)[1]
        for line in PR_WORKFLOW.read_text().splitlines()
        if _RUFF_CHECK in line
    ]
    assert matches, f"No '{_RUFF_CHECK.strip()}' command found in {PR_WORKFLOW.name}"
    assert len(matches) == 1, (
        f"Expected exactly one ruff check command in {PR_WORKFLOW.name}, "
        f"found {len(matches)} — update this test to cover them all."
    )
    return {
        arg.rstrip("/") for arg in shlex.split(matches[0]) if not arg.startswith("-")
    }


def _tracked_python_top_levels() -> set[str]:
    """Top-level directories containing tracked ``*.py`` files."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "*.py"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        pytest.skip("git is not installed")

    tops = set()
    for line in result.stdout.splitlines():
        parts = line.split("/")
        # A root-level .py file has no directory argument that could cover it.
        assert len(parts) > 1, f"root-level Python file is unlintable in CI: {line}"
        tops.add(parts[0])
    assert tops, "git ls-files matched no Python at all — check the invocation"
    return tops


def test_ruff_check_covers_every_directory_holding_python():
    uncovered = _tracked_python_top_levels() - _ruff_check_dirs()
    assert not uncovered, (
        "These directories contain tracked Python but are not passed to ruff in "
        f"the 'Run ruff check' step of pr.yml: {sorted(uncovered)}. Add them "
        "there — lefthook already lints them via **/*.py, so the gap shows up "
        "only in CI."
    )


def test_ruff_check_lists_no_directory_that_is_gone():
    """A stale entry would make the step fail on a missing path."""
    missing = [d for d in _ruff_check_dirs() if not (REPO_ROOT / d).exists()]
    assert not missing, f"pr.yml passes ruff paths that no longer exist: {missing}"
