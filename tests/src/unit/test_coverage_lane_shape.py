"""Pin the two coverage lanes (#2311) against silent, green data loss.

Coverage is collected so that decisions about the test suite — which lanes a
test needs to run on, what a test costs — can be read off measurements. Every
failure mode below produces a passing lane and a well-formed artifact that
happens to describe less than it claims, which is worse than no measurement at
all: nobody re-checks a number that arrived without an error.

Each assertion here stands for one measured failure, not for a style
preference:

* ``COVERAGE_CORE=sysmon`` with ``--cov-context``: measured on
  ``test_helper_response_shape.py``, the sysmon core recorded contexts for 27
  of the 35 tests and said nothing about the remaining 8, while the ctrace core
  recorded all 35. The coverage.py configuration reference lists dynamic
  contexts as unsupported by that core; as of coverage 7.10.6 on Python 3.13 it
  neither warns nor falls back. Without contexts the two cores agree exactly (13,344 lines over 222
  files, zero difference either way), which is why the unit lane may use it.
* A missing ``--cov-config`` on a lane that runs pytest from ``tests/``:
  coverage.py reads its configuration from the working directory only, so the
  root ``pyproject.toml`` is never seen and the run measures a different set of
  files (118 rather than 222 in the same probe).
* The default data-file name: ``actions/upload-artifact`` skips hidden files
  unless told otherwise, so a ``.coverage`` artifact uploads nothing and
  reports success.
* ``source`` on the command line rather than in the config file: the
  configuration reference makes it a precondition for ``relative_files``, and
  without relative paths a data file cannot be reported or diffed anywhere but
  the machine that produced it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

# `--cov` as its own flag. A plain substring test is satisfied by
# `--cov-report`, so a lane that stopped collecting coverage while still
# printing a report would read as instrumented.
_BARE_COV = re.compile(r"(?<![\w-])--cov(?![\w-])")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"

# (workflow, job_id, step name fragment) for every lane that collects coverage.
_UNIT_LANE = ("pr.yml", "unit-tests", "Run unit tests")
_E2E_LANE = ("e2e-tests.yml", "e2e-tests", "Run full E2E test suite")
# Explicit ids: the default would embed the step name, whose spaces truncate the
# test id in any reader that splits pytest's summary line on whitespace.
_COVERAGE_LANES = (
    pytest.param(*_UNIT_LANE, id="pr.yml::unit-tests"),
    pytest.param(*_E2E_LANE, id="e2e-tests.yml::e2e-tests"),
)


def _workflow(name: str) -> dict[str, Any]:
    data = yaml.safe_load((_WORKFLOW_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{name} must parse as a workflow mapping"
    return data


def _job(workflow: str, job_id: str) -> dict[str, Any]:
    job = _workflow(workflow)["jobs"][job_id]
    assert isinstance(job, dict), f"{workflow}::{job_id} must be a job mapping"
    return job


def _step(workflow: str, job_id: str, name_fragment: str) -> dict[str, Any]:
    steps = [
        step
        for step in _job(workflow, job_id).get("steps", [])
        if isinstance(step, dict) and name_fragment in str(step.get("name", ""))
    ]
    assert len(steps) == 1, (
        f"{workflow}::{job_id} must have exactly one step named like "
        f"{name_fragment!r}, found {[s.get('name') for s in steps]}"
    )
    return steps[0]


def _env_in_scope(workflow: str, job_id: str, step: dict[str, Any]) -> dict[str, Any]:
    """Every env var the step sees, across the three scopes that set them.

    Workflow-level env is inherited by every job, and job-level by every step,
    so a core selected two scopes up governs this step as surely as its own
    ``env:`` block does — and is the easier place to set one by accident.
    """
    return {
        **(_workflow(workflow).get("env") or {}),
        **(_job(workflow, job_id).get("env") or {}),
        **(step.get("env") or {}),
    }


@pytest.mark.parametrize(("workflow", "job_id", "step_name"), _COVERAGE_LANES)
def test_coverage_lane_still_collects_coverage(
    workflow: str, job_id: str, step_name: str
) -> None:
    """The lane this module pins must still be a coverage lane at all.

    Without this, every check below passes vacuously the moment the flag is
    dropped: no ``--cov`` means no contexts to lose, no config to miss and no
    data file to misname.
    """
    run = str(_step(workflow, job_id, step_name).get("run", ""))
    assert _BARE_COV.search(run), (
        f"{workflow}::{job_id} no longer collects coverage, so the guarantees "
        "in this module are about a lane that stopped existing (#2311)"
    )


@pytest.mark.parametrize(("workflow", "job_id", "step_name"), _COVERAGE_LANES)
def test_coverage_lane_names_its_data_file(
    workflow: str, job_id: str, step_name: str
) -> None:
    """A hidden ``.coverage`` uploads as an empty artifact, successfully."""
    env = _env_in_scope(workflow, job_id, _step(workflow, job_id, step_name))
    data_file = str(env.get("COVERAGE_FILE", ""))
    assert data_file, (
        f"{workflow}::{job_id} leaves COVERAGE_FILE unset, so coverage.py "
        "writes the hidden default `.coverage` and upload-artifact skips it "
        "without failing"
    )
    assert not Path(data_file).name.startswith("."), (
        f"{workflow}::{job_id} writes {data_file!r}, a hidden file that "
        "upload-artifact excludes by default"
    )


@pytest.mark.parametrize(("workflow", "job_id", "step_name"), _COVERAGE_LANES)
def test_the_uploaded_artifact_is_the_file_the_lane_wrote(
    workflow: str, job_id: str, step_name: str
) -> None:
    """An upload pointing somewhere else is the same empty artifact again.

    Matched on the file name rather than on a substring: `coverage-unit.dat`
    occurs inside `not-coverage-unit.dat` too, and an upload of the wrong file
    is the failure this check exists to catch.
    """
    step = _step(workflow, job_id, step_name)
    written = Path(str(_env_in_scope(workflow, job_id, step)["COVERAGE_FILE"])).name
    uploads = [
        candidate
        for candidate in _job(workflow, job_id).get("steps", [])
        if isinstance(candidate, dict)
        and "upload-artifact" in str(candidate.get("uses", ""))
        and Path(str((candidate.get("with") or {}).get("path", "")).strip()).name
        == written
    ]
    assert len(uploads) == 1, (
        f"{workflow}::{job_id} writes {written} but no upload-artifact step "
        "takes that path — the measurement stays on the runner"
    )
    assert (uploads[0].get("with") or {}).get("if-no-files-found") == "error", (
        f"{workflow}::{job_id} uploads {written} without if-no-files-found: "
        "error, so a run that produced no data reports a green upload"
    )


def test_the_context_lane_does_not_select_a_core_that_drops_contexts() -> None:
    """sysmon records a fraction of the contexts, greenly (#2311).

    The check is on the lane that asks for contexts, in every env scope, rather
    than a blanket ban: the unit lane's line-only run is measurably identical
    on either core, and forbidding the fast core there would cost wall clock on
    a required lane for no gain.
    """
    workflow, job_id, step_name = _E2E_LANE
    step = _step(workflow, job_id, step_name)
    assert "--cov-context" in str(step.get("run", "")), (
        f"{workflow}::{job_id} stopped collecting per-test contexts, which is "
        "the whole reason this lane carries coverage"
    )
    core = str(_env_in_scope(workflow, job_id, step).get("COVERAGE_CORE", ""))
    assert core != "sysmon", (
        f"{workflow}::{job_id} asks for dynamic contexts under the sysmon "
        "core, which does not support them: it records only part of them and "
        "neither warns nor falls back. Leave COVERAGE_CORE unset here."
    )


def test_a_lane_running_pytest_from_a_subdirectory_points_at_the_config() -> None:
    """coverage.py reads its config from the working directory only."""
    workflow, job_id, step_name = _E2E_LANE
    run = str(_step(workflow, job_id, step_name).get("run", ""))
    assert "cd tests" in run, (
        f"{workflow}::{job_id} no longer runs pytest from tests/, so this "
        "check no longer describes it — confirm the config is still found and "
        "update the reason here"
    )
    assert "--cov-config" in run, (
        f"{workflow}::{job_id} runs pytest from tests/ without --cov-config, "
        "so coverage.py never reads the root pyproject.toml: the run silently "
        "measures a different set of files than every other lane"
    )


def test_coverage_source_is_configured_in_the_file_not_on_the_command_line() -> None:
    """`relative_files` needs `source` in the config, per coverage.py's docs.

    Absolute paths in a data file pin it to the machine that produced it, which
    defeats collecting on one lane and comparing against another.
    """
    config = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    run_config = config["tool"]["coverage"]["run"]
    assert run_config.get("source") == ["ha_mcp"], (
        "[tool.coverage.run] source must name the package in the config file: "
        "passed as `--cov=ha_mcp` instead, relative_files cannot resolve the "
        "source origin and the data file records absolute paths"
    )
    assert run_config.get("relative_files") is True, (
        "[tool.coverage.run] relative_files must stay on, or a data file can "
        "only be reported on the machine that wrote it"
    )
