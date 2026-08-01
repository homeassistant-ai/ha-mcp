"""The CI ruff invocation must cover every directory that holds Python.

``pr.yml`` passes ruff an explicit directory list while lefthook lints
``**/*.py``, so a new top-level directory gets linted on commit but not in CI —
which is exactly how ``packaging/`` went uncovered. This pins the list to the
tree, so the next one fails here instead of going quietly unlinted.

Precedent for asserting on workflow contents: ``test_triage_prompt_budget.py``.
"""

from __future__ import annotations

import shlex
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PR_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr.yml"
_RUFF_CHECK = "uv run ruff check "

# Build/tooling directories that hold no source of ours. Anything starting with
# a dot is skipped separately. ``worktree`` is the repo's own gitignored
# worktree root (AGENTS.md) — it contains whole checkouts, so walking into it
# would report the same directories over again.
_NOT_SOURCE = frozenset(
    {
        "__pycache__",
        "build",
        "dist",
        "eggs",
        "htmlcov",
        "local",
        "node_modules",
        "sdist",
        "venv",
        "wheels",
        "worktree",
    }
)


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


def _holds_python(directory: Path) -> bool:
    """True if the tree under ``directory`` contains a ``.py`` file.

    Walked directly rather than via ``git ls-files`` so the test does not
    depend on git being installed, or on the checkout passing git's
    ownership check — CI containers run as a different UID than the checkout
    owner, which is why ``pr.yml`` re-adds ``safe.directory`` for its own git
    calls.
    """
    for path in directory.rglob("*.py"):
        if any(
            part in _NOT_SOURCE or part.startswith(".")
            for part in path.relative_to(directory).parts
        ):
            continue
        return True
    return False


def _top_levels_holding_python() -> set[str]:
    tops = {
        entry.name
        for entry in REPO_ROOT.iterdir()
        if entry.is_dir()
        and not entry.name.startswith(".")
        and entry.name not in _NOT_SOURCE
        and _holds_python(entry)
    }
    assert tops, "Found no Python anywhere in the tree — check the walk"
    return tops


def test_ruff_check_covers_every_directory_holding_python():
    uncovered = _top_levels_holding_python() - _ruff_check_dirs()
    assert not uncovered, (
        "These directories contain Python but are not passed to ruff in the "
        f"'Run ruff check' step of pr.yml: {sorted(uncovered)}. Add them "
        "there — lefthook already lints them via **/*.py, so the gap shows up "
        "only in CI."
    )


def test_no_root_level_python_escapes_the_directory_list():
    """A ``.py`` file at the repo root has no directory argument covering it."""
    root_python = [p.name for p in REPO_ROOT.glob("*.py")]
    assert not root_python, (
        f"Root-level Python files are not linted by CI: {sorted(root_python)}. "
        "Move them into a directory that pr.yml passes to ruff."
    )


def test_ruff_check_lists_no_directory_that_is_gone():
    """A stale entry would make the step fail on a missing path."""
    missing = [d for d in _ruff_check_dirs() if not (REPO_ROOT / d).exists()]
    assert not missing, f"pr.yml passes ruff paths that no longer exist: {missing}"
