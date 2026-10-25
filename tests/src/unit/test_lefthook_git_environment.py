"""Keep pytest Git hooks isolated from the repository running the hook."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from ._ratchet_repo import _GIT_HOOK_VARIABLES

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _walk_jobs(entries: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for entry in entries:
        group = entry.get("group")
        if isinstance(group, dict):
            yield from _walk_jobs(group.get("jobs", []))
        else:
            yield entry


def test_pytest_hooks_clear_the_full_git_hook_environment() -> None:
    """Temporary test repos must not inherit any Git hook repository paths."""
    config: dict[str, Any] = yaml.safe_load(
        (_REPO_ROOT / "lefthook.yml").read_text(encoding="utf-8")
    )
    jobs = {
        job["name"]: job
        for job in _walk_jobs(config["pre-commit"]["jobs"])
        if job.get("name") in {"issue-intake", "ui-js-tests", "unit-tests"}
    }

    assert set(jobs) == {"issue-intake", "ui-js-tests", "unit-tests"}
    for name, job in jobs.items():
        missing = [
            variable
            for variable in _GIT_HOOK_VARIABLES
            if f"-u {variable}" not in job["run"]
        ]
        assert not missing, f"{name} leaks Git hook variables: {missing}"
