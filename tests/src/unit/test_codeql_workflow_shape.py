"""Invariants of the always-reported CodeQL merge gate.

The workflow folds a two-leg language matrix and an aggregating gate job into
ONE job (#2311). Three properties the matrix gave for free now hold only while
the steps stay consistent with each other, so most of these tests check
relations across the file, not the values it happens to contain. Two values
are asserted because something outside the file requires them: the job name
is the master ruleset's required check (so the workflow stays one job with no
matrix), and the code-scanning suites are the pre-merge security gate.

* A red language must not hide the other one. Serially that holds only while
  every step from the first SARIF upload onward carries
  ``if: success() || failure()``. ``always()`` is rejected, as in pr.yml's Fast
  Checks lane: it would keep burning runner minutes after a cancel.
* Separate legs wrote to separate filesystems. In one job, a shared SARIF
  name lets a failed analysis leave the previous language's file for the
  next gate step, and a shared or cross-wired database directory makes an
  analyze step run one language's suites against the other's source.
* Each leg had its own budget. Serially, the job cap must never be the cap
  that fires: per the workflow-syntax docs a job ``timeout-minutes``
  "automatically cancels" the job, which skips every
  ``success() || failure()`` step, while a step ``timeout-minutes`` only fails
  that step and the later steps still report.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "codeql-quality.yml"

# The required-status-check context on the master ruleset. A job that no
# longer emits it leaves the required check pending and blocks every merge.
_REQUIRED_CONTEXT = "CodeQL Gate"

_LANGUAGES = ("python", "javascript")

# Minutes the job cap must clear the step-cap sum by. Job setup, per-step
# overhead and the implicit post-job steps count against the job cap and
# cannot carry a `timeout-minutes` of their own.
_JOB_CAP_SLACK = 3


def _is_budget(value: object) -> bool:
    """True for a value a workflow can actually use as `timeout-minutes`.

    `bool` is a subclass of `int`, so a stray `true` would pass an
    `isinstance(value, int)` check and then sum as 1, quietly shrinking the
    total the job cap is measured against. The docs also require a positive
    integer, so 0 is not a budget either.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _workflow() -> dict[str, Any]:
    """The workflow as YAML, re-read on every call so no test caches it."""
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _gate_job() -> dict[str, Any]:
    """The single job the fold produced, keyed by its id."""
    return _workflow()["jobs"]["code-quality-gate"]


def _steps() -> list[dict[str, Any]]:
    """The gate job's steps, in workflow order - order is load-bearing here."""
    return _gate_job()["steps"]


def _step_name(step: dict[str, Any]) -> str:
    """A step's name, or a placeholder for the unnamed checkout step."""
    return step.get("name") or "(checkout)"


def _run(step: dict[str, Any]) -> str:
    """A step's shell body, empty for `uses:` steps that have none."""
    return str(step.get("run", ""))


def test_pull_requests_always_emit_the_required_context() -> None:
    """A ``paths`` filter would leave the required check permanently pending
    on a PR that touches nothing matching it."""
    workflow = _workflow()
    # PyYAML resolves the bare ``on:`` key to the boolean True.
    assert "paths" not in workflow[True]["pull_request"]
    assert _gate_job()["name"] == _REQUIRED_CONTEXT


def test_the_gate_is_a_single_job() -> None:
    """The master ruleset requires one context, ``CodeQL Gate``. A matrix
    ``strategy`` renames the check runs so that context never reports and
    every merge stays blocked; a second job reports, but outside the required
    context, so its findings no longer block a merge."""
    assert list(_workflow()["jobs"]) == ["code-quality-gate"]
    assert "strategy" not in _gate_job()


def test_every_step_after_the_first_upload_reports_on_a_red_run() -> None:
    """The failure-attribution requirement the matrix used to give for free:
    one red language cannot hide the other."""
    steps = _steps()
    first_upload = next(
        index
        for index, step in enumerate(steps)
        if _step_name(step).startswith("Upload SARIF artifact")
    )
    for step in steps[first_upload:]:
        assert step.get("if") == "success() || failure()", (
            f"step {_step_name(step)!r} would be skipped after an earlier red "
            "step, hiding its own result - and `always()` is deliberately "
            "rejected so a cancelled run stops"
        )


def test_no_step_uses_always() -> None:
    """``always()`` keeps a cancelled run paying for the remaining analysis."""
    for step in _steps():
        assert "always()" not in str(step.get("if", ""))


def test_each_language_runs_both_its_quality_and_security_suites() -> None:
    """The security suite is gated here because GitHub default setup only
    analyzes master post-merge (workflow header)."""
    analyses = "\n".join(
        _run(step) for step in _steps() if "database analyze" in _run(step)
    )
    for language in _LANGUAGES:
        for kind in ("code-quality", "code-scanning"):
            suite = f"codeql/{language}-queries:codeql-suites/{language}-{kind}.qls"
            assert suite in analyses, f"{suite} is no longer analyzed"


def test_each_language_gates_on_its_own_sarif_file() -> None:
    """Sharing one filename lets a failed analysis leave the previous
    language's SARIF in place, so the next gate step reports findings under
    the wrong language."""
    outputs = [
        match.group(1)
        for step in _steps()
        for match in re.finditer(r"--output (\S+\.sarif)", _run(step))
    ]
    gated = [
        match.group(1)
        for step in _steps()
        for match in re.finditer(
            r"scripts/codeql_quality_gate\.py (\S+\.sarif)", _run(step)
        )
    ]
    assert len(set(outputs)) == len(outputs) == len(_LANGUAGES)
    assert gated == outputs, (
        "every analysis must be gated, each on the file it wrote, in order"
    )
    for language, sarif in zip(_LANGUAGES, outputs, strict=True):
        assert language in sarif, (
            f"{sarif!r} does not name its language - a reordering would swap "
            "the two gates without any test noticing"
        )


def test_uploaded_artifacts_are_named_per_language() -> None:
    """One artifact name for two files means the second upload collides."""
    names = [
        step["with"]["name"]
        for step in _steps()
        if _step_name(step).startswith("Upload SARIF artifact")
    ]
    assert len(set(names)) == len(names) == len(_LANGUAGES)


def test_the_job_cap_can_never_be_the_cap_that_fires() -> None:
    """A hung step must not cost the other language its report.

    Only a step cap can do that: a job cap cancels, and a cancelled run skips
    the ``success() || failure()`` steps this file pins above. So the job cap
    has to be unreachable, and it is unreachable only when EVERY step carries
    a budget and the job cap exceeds their sum. Capping just the analyses
    leaves the uncapped steps free to eat the margin, which puts the job cap
    back in play as the binding constraint - the exact hole two reviewers
    found in the first version of this guard.

    Strictly greater, not `>=`: equality leaves nothing for the implicit
    post-job steps, which take time and cannot be capped.
    """
    job = _gate_job()
    budgets = [step.get("timeout-minutes") for step in _steps()]
    uncapped = [
        _step_name(step)
        for step, budget in zip(_steps(), budgets, strict=True)
        if not _is_budget(budget)
    ]
    assert not uncapped, (
        f"steps without a usable budget: {uncapped} - an uncapped step can "
        "consume the job budget, and the job cap then cancels the run instead "
        "of a step cap failing it, taking the remaining reports with it"
    )
    total = sum(value for value in budgets if _is_budget(value))
    slack = job["timeout-minutes"] - total
    assert slack >= _JOB_CAP_SLACK, (
        f"job cap {job['timeout-minutes']} leaves {slack} minutes over the "
        f"step-cap sum {total}, under the {_JOB_CAP_SLACK} the workflow "
        "comment claims - job setup, per-step overhead and the implicit "
        "post-job steps all run against the job cap and none can be capped, "
        "so the margin is the only thing covering them"
    )


def test_each_language_builds_and_analyzes_its_own_database_directory() -> None:
    """The SARIF filename is not the only shared name the fold created.

    Separate matrix legs had separate filesystems; one job does not. A shared
    database directory strands the wrong language's database, and the next
    analyze step then runs its suites against the other language's source.

    A tally of database names is not enough here: swapping the two analyze
    steps' databases keeps every count identical while cross-wiring both
    languages, so each database is bound to the language of the step that
    uses it - the `--language=` flag for a create, the query suites for an
    analyze.
    """
    creates: list[tuple[str, str]] = []
    analyses: list[tuple[str, set[str]]] = []
    for step in _steps():
        run = _run(step)
        for match in re.finditer(r'gh codeql database create "?([^"\s]+)', run):
            language_flag = re.search(r"--language=(\S+)", run)
            assert language_flag, f"create step {_step_name(step)!r} names no language"
            creates.append((match.group(1), language_flag.group(1)))
        analyses.extend(
            (match.group(1), set(re.findall(r"codeql/(\w+)-queries", run)))
            for match in re.finditer(r'gh codeql database analyze "?([^"\s]+)', run)
        )

    assert len(creates) == len(analyses) == len(_LANGUAGES)
    assert len({database for database, _ in creates}) == len(_LANGUAGES), (
        f"the languages share a database directory: {creates}"
    )

    for database, language in creates:
        assert database.endswith(language), (
            f"database {database!r} is built with --language={language} - the "
            "name and the language must agree or the pairing is unreadable"
        )
    for database, suites in analyses:
        assert len(suites) == 1, f"analyze of {database!r} mixes languages: {suites}"
        (language,) = suites
        assert database.endswith(language), (
            f"{language} suites are analyzed against database {database!r} - "
            "cross-wiring the two analyze steps keeps every name present and "
            "every count identical, so only this pairing catches it"
        )
