"""Shape pins for the always-reported CodeQL merge gate.

The workflow folds what were a two-leg language matrix and an aggregating gate
job into ONE job (#2311), so the four properties that used to be free from
GitHub's matrix semantics now have to be held by hand:

* ``fail-fast: false`` guaranteed that a python finding never hid a javascript
  one. Serially that only holds while every step from the first SARIF upload
  onward carries ``if: success() || failure()``. ``always()`` is deliberately
  rejected, exactly as in pr.yml's Fast Checks lane: it would keep burning
  runner minutes after a cancel.
* The matrix could not lose a language by editing one step. A flat step list
  can, so the analyzed language set is asserted directly.
* Separate legs wrote to separate filesystems, so sharing a SARIF filename or
  a CodeQL database directory between the languages was harmless. In one job
  neither is, and they fail one step apart: a shared SARIF name lets a failed
  analysis leave the previous language's file in place, so the next GATE step
  reports its findings under the wrong language; a shared database directory
  strands the wrong language's database, so the next ANALYZE step runs its
  suites against the other language's source.
* Each leg had its own runner and its own 20-minute budget, so python could
  not starve javascript of time. Serially they share one job, and the job cap
  is the wrong instrument for it: per the workflow-syntax docs a job
  ``timeout-minutes`` "automatically cancels" the job, and a cancelled run
  skips every ``success() || failure()`` step, so javascript would report
  nothing. A step ``timeout-minutes`` kills only that step's process
  ("killing the process"), so the step FAILS and ``failure()`` stays true
  ("Returns true when any previous step of a job fails"), and the later steps
  still report.

A comment is not a gate — these tests read the workflow YAML directly, pass on
arrival, and fire only when an edit drops a clause.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "codeql-quality.yml"

# The required-status-check context on the master ruleset. Renaming the job
# silently un-gates master until a maintainer edits the ruleset by hand.
_REQUIRED_CONTEXT = "CodeQL Gate"

_LANGUAGES = ("python", "javascript")


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _gate_job() -> dict[str, Any]:
    return _workflow()["jobs"]["code-quality-gate"]


def _steps() -> list[dict[str, Any]]:
    return _gate_job()["steps"]


def _step_name(step: dict[str, Any]) -> str:
    return step.get("name") or "(checkout)"


def _run(step: dict[str, Any]) -> str:
    return str(step.get("run", ""))


def test_pull_requests_always_emit_the_required_context() -> None:
    """A ``paths`` filter would leave the required check permanently pending
    on a PR that touches nothing matching it."""
    workflow = _workflow()
    # PyYAML resolves the bare ``on:`` key to the boolean True.
    assert "paths" not in workflow[True]["pull_request"]
    assert _gate_job()["name"] == _REQUIRED_CONTEXT


def test_the_gate_is_a_single_job() -> None:
    """The fold's whole point: one check run, not a matrix plus an aggregator.
    A second job here means the ruleset's single context no longer covers
    everything the workflow runs."""
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


def test_both_languages_are_still_analyzed() -> None:
    """A flat step list can lose a language to a single deletion; the matrix
    could not."""
    analyzed = {
        language
        for language in _LANGUAGES
        for step in _steps()
        if "database create ha-mcp-db" in _run(step)
        and f"--language={language}" in _run(step)
    }
    assert analyzed == set(_LANGUAGES)


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


def test_each_language_has_a_step_budget_the_job_cap_cannot_pre_empt() -> None:
    """A hung python analysis must not cost javascript its report.

    Only step-level caps can do that: a job-level cap cancels, and a cancelled
    run skips the ``success() || failure()`` steps this file pins above. The
    relation - not either number - is what is load-bearing, so it is asserted
    as a relation.
    """
    job = _gate_job()
    heavy = [step for step in _steps() if "gh codeql database" in _run(step)]
    assert len(heavy) == 2 * len(_LANGUAGES), (
        "expected a create and an analyze step per language"
    )
    budgets = [step.get("timeout-minutes") for step in heavy]
    assert all(isinstance(value, int) for value in budgets), (
        "an uncapped create/analyze step can burn the whole job budget and "
        "let the job cap cancel the other language out of its report"
    )
    total = sum(value for value in budgets if isinstance(value, int))
    assert job["timeout-minutes"] >= total, (
        f"job cap {job['timeout-minutes']} is below the sum of the step caps "
        f"{total} - the job would cancel before a step cap fires, and a "
        "cancelled run skips the remaining language entirely"
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
        for match in re.finditer(r"gh codeql database create (\S+)", run):
            language_flag = re.search(r"--language=(\S+)", run)
            assert language_flag, f"create step {_step_name(step)!r} names no language"
            creates.append((match.group(1), language_flag.group(1)))
        analyses.extend(
            (match.group(1), set(re.findall(r"codeql/(\w+)-queries", run)))
            for match in re.finditer(r"gh codeql database analyze (\S+)", run)
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
