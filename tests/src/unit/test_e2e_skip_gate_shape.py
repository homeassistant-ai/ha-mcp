"""Guard the shape of the e2e change-classifier skip gates.

The e2e suites are gated by a `changes` job that classifies the PR's files and
sets `run`. Skipping is legitimate for a docs/website-only PR, and the suites
are the required status checks, so a skipped suite reports Success to branch
protection — which makes the gate's exact predicate a merge-safety control.

Two ways it has failed open, both fixed and both trivially reintroducible by
copy-pasting an existing lane:

1. A job-level `if:` with no status-check function implicitly ANDs with
   `success()` on `needs`, so a classifier job that *failed* (a runner never
   assigned, a script error) skipped the suite and reported Success.
2. A non-success classifier leaves `run` empty, which `run == 'true'` and
   `run != 'true'` both read the same as a deliberate docs-only `false`.

So every gate must consult `needs.changes.result` and must require an explicit
`false` before honoring a skip. These tests read the workflow YAML directly;
they pass on arrival and only fire when a new or edited lane drops a clause.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"


def _classifier_gated_jobs() -> list[tuple[str, str, str]]:
    """Return (workflow, job, if-expression) for every classifier-gated job."""
    found: list[tuple[str, str, str]] = []
    for path in sorted(_WORKFLOW_DIR.glob("*.yml")):
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        for job_name, job in (data.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            needs = job.get("needs") or []
            needs = [needs] if isinstance(needs, str) else needs
            condition = str(job.get("if") or "")
            if "changes" in needs and "needs.changes.outputs.run" in condition:
                found.append((path.name, job_name, condition))
    return found


def test_there_are_classifier_gated_jobs_to_check() -> None:
    """Fail loudly if the discovery walk stops matching anything."""
    jobs = _classifier_gated_jobs()
    assert len(jobs) >= 6, f"expected the e2e lanes to be discovered, got {jobs}"


@pytest.mark.parametrize(
    ("workflow", "job", "condition"),
    _classifier_gated_jobs(),
    ids=lambda v: v if isinstance(v, str) and not v.startswith("$") else "",
)
def test_gate_survives_a_classifier_that_did_not_succeed(
    workflow: str, job: str, condition: str
) -> None:
    """Each gated lane must run when the classifier did not succeed."""
    assert "needs.changes.result" in condition, (
        f"{workflow}::{job} gates on `run` without consulting "
        f"`needs.changes.result`, so a failed classifier skips it and the "
        f"required check reports Success: {condition}"
    )
    assert "cancelled()" in condition, (
        f"{workflow}::{job} needs a status-check function (e.g. `!cancelled()`) "
        f"or the implicit success() on `needs` skips it anyway: {condition}"
    )


@pytest.mark.parametrize(
    ("workflow", "job", "condition"),
    _classifier_gated_jobs(),
    ids=lambda v: v if isinstance(v, str) and not v.startswith("$") else "",
)
def test_gate_requires_an_explicit_false_to_skip(
    workflow: str, job: str, condition: str
) -> None:
    """Only a classifier that said `false` out loud may authorize a skip."""
    assert "!= 'false'" in condition, (
        f"{workflow}::{job} must skip only on an explicit `run != 'false'`; "
        f"an empty output (what a non-success classifier leaves) would "
        f"otherwise read as docs-only: {condition}"
    )


def test_pr_gate_step_requires_a_successful_classifier_before_skipping() -> None:
    """`pr.yml`'s always-run gate must not honor an ambiguous skip either.

    The matrix lanes cannot be required contexts (a matrix `if:`-skip emits no
    contexts), so `e2e-validation-gate` is the required check. Its early-exit
    is the same authorization decision as the lane predicates and has to make
    it the same way.
    """
    data: Any = yaml.safe_load((_WORKFLOW_DIR / "pr.yml").read_text(encoding="utf-8"))
    gate = data["jobs"]["e2e-validation-gate"]
    assert "always()" in str(gate.get("if")), (
        "the gate must always report, or a failed classifier skips the required "
        "check itself"
    )
    script = "\n".join(str(step.get("run", "")) for step in gate["steps"])
    assert "needs.changes.result" in script, (
        "the gate honors a skip without checking the classifier result, so a "
        "runner hiccup passes it without judging any lane"
    )
    assert '= "false"' in script, (
        "the gate must require an explicit `false` to skip; an empty output "
        "from a non-success classifier would otherwise pass it"
    )
