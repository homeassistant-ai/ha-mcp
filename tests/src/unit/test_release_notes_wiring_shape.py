"""Pin the release workflows to the release-note extractor they call.

``semver-release.yml`` cannot run in PR CI, so nothing else checks that the
filenames in its release step still agree: the
script's ``--out``, the ``[ ! -s ... ]`` emptiness guard, the fallback write,
and ``gh release create --notes-file``. A missing script is loud (the step runs
under ``bash -e``), but a drifting ``--out`` name is silent — the guard finds no
file, the fallback fires, and the release ships ``Release vX.Y.Z`` as its entire
body on a green run, for that release and every one after it. These tests catch
that before merge, the only coverage a workflow that cannot run pre-merge can
have.

The assertions are scoped to the one step that builds and publishes the body,
so a match somewhere else in the file cannot stand in for that step's wiring.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
_SCRIPT = "scripts/extract_release_notes.py"
_STEP = "Create draft GitHub release"
_WORKFLOWS = ["semver-release.yml"]
_OUT_RE = re.compile(r"extract_release_notes\.py[^\n]*--out\s+(\S+)")
_RUNS_OF_BLANKS = re.compile(r"[ \t]+")


def _release_step_run(workflow: str) -> str:
    """The `run` body of the single step that writes and publishes the notes."""
    data: Any = yaml.safe_load((_WORKFLOW_DIR / workflow).read_text(encoding="utf-8"))
    steps = [
        step
        for job in data["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == _STEP
    ]
    assert len(steps) == 1, f"{workflow} must have exactly one {_STEP!r} step"
    run = steps[0].get("run")
    assert run, f"{workflow}'s {_STEP!r} step has no run body"
    # Comment lines are dropped first: the step is commented, and a commented-out
    # command must not be able to satisfy an assertion about what it does.
    active = "\n".join(
        line for line in run.splitlines() if not line.lstrip().startswith("#")
    )
    return _RUNS_OF_BLANKS.sub(" ", active)


def test_manual_force_requests_a_patch_release() -> None:
    data: Any = yaml.safe_load(
        (_WORKFLOW_DIR / "semver-release.yml").read_text(encoding="utf-8")
    )
    semantic_steps = data["jobs"]["semantic-release"]["steps"]
    semantic = next(step for step in semantic_steps if step.get("id") == "semantic")

    assert semantic["with"]["force"] == "${{ inputs.force && 'patch' || '' }}"


def test_the_extractor_the_workflows_call_exists() -> None:
    assert (_REPO_ROOT / _SCRIPT).is_file(), (
        f"{_SCRIPT} is gone, but the release workflows still invoke it"
    )


@pytest.mark.parametrize("workflow", _WORKFLOWS)
def test_the_release_step_wires_one_filename_end_to_end(workflow: str) -> None:
    run = _release_step_run(workflow)

    out = _OUT_RE.search(run)
    assert out, f"{workflow}'s {_STEP!r} step does not call {_SCRIPT} with --out"
    notes = out.group(1)

    stages = [
        (f"python3 {_SCRIPT}", "call the extractor"),
        (f"[ ! -s {notes} ]", f"guard {notes} for emptiness"),
        ("::warning::no changelog section extracted", "warn when the extract is empty"),
        (f"> {notes}", f"write its fallback body to {notes}"),
        (f"--notes-file {notes}", f"publish {notes}"),
    ]
    for needle, what in stages:
        assert needle in run, f"{workflow}'s {_STEP!r} step does not {what}"

    positions = [run.index(needle) for needle, _ in stages]
    assert positions == sorted(positions), (
        f"{workflow}'s {_STEP!r} step must extract, guard the empty file, warn, "
        "write the fallback, then publish that same file — in that order"
    )
