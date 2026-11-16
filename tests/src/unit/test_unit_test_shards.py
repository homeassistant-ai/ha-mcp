"""The unit suite runs on several CI runners; no test may fall between them."""

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from ._shards import in_shard

_PR_YML = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "pr.yml"
# The check name branch protection requires (the Master ruleset).
_REQUIRED_CHECK = "Unit Tests"


def _jobs() -> dict[str, Any]:
    jobs: dict[str, Any] = yaml.safe_load(_PR_YML.read_text(encoding="utf-8"))["jobs"]
    return jobs


def _shards() -> list[str]:
    shards: list[str] = _jobs()["unit-tests"]["strategy"]["matrix"]["shard"]
    return shards


_IDS = [
    f"tests/src/unit/test_file_{file}.py::test_case_{case}"
    for file in range(200)
    for case in range(10)
]


def test_every_test_runs_on_exactly_one_runner() -> None:
    """A shard list with a gap or a repeat would skip tests or run them twice."""
    shards = _shards()
    for nodeid in _IDS:
        assert [in_shard(nodeid, shard) for shard in shards].count(True) == 1


def test_the_runners_take_similar_shares() -> None:
    """One runner with most of the suite would be as slow as no split."""
    shares = [sum(in_shard(nodeid, shard) for nodeid in _IDS) for shard in _shards()]
    assert max(shares) <= 1.2 * min(shares)


# The stable job and its mirror on the beta Core.
_UNIT_JOBS = ["unit-tests", "unit-tests-beta"]


@pytest.mark.parametrize("job", _UNIT_JOBS)
def test_each_runner_is_told_its_own_shard(job: str) -> None:
    """Without the variable every runner would run the whole suite."""
    (run,) = [
        step
        for step in _jobs()[job]["steps"]
        if "pytest tests/src/unit/" in step.get("run", "")
    ]
    assert run["env"]["UNIT_TEST_SHARD"] == "${{ matrix.shard }}"


def test_the_beta_job_is_split_like_the_stable_one() -> None:
    """A beta matrix with its own shard list could skip tests the stable job runs."""
    assert _jobs()["unit-tests-beta"]["strategy"]["matrix"]["shard"] == _shards()


@pytest.mark.parametrize("job", _UNIT_JOBS)
def test_the_app_tests_run_on_one_runner_only(job: str) -> None:
    """They are not split by shard, so every other runner would repeat them."""
    (run,) = [
        step
        for step in _jobs()[job]["steps"]
        if "pytest tests/addon/" in step.get("run", "")
    ]
    assert "matrix.shard == '1/2'" in run["if"]


def test_the_required_check_passes_only_when_every_runner_did() -> None:
    """A matrix reports one check per runner, none of them under the required
    name; the gate carries that name and must see every runner's result."""
    jobs = _jobs()
    assert jobs["unit-tests"]["name"] != _REQUIRED_CHECK
    (gate,) = [job for job in jobs.values() if job["name"] == _REQUIRED_CHECK]
    assert gate["needs"] == "unit-tests"
    # ``always()``: a job that waits on a failed one is skipped by default, and
    # a skipped required check counts as passed.
    assert gate["if"] == "always()"


@pytest.mark.parametrize(
    ("result", "passes"),
    [("success", True), ("failure", False), ("cancelled", False), ("skipped", False)],
)
def test_the_gate_passes_for_no_runner_result_but_success(
    result: str, passes: bool
) -> None:
    """The gate's own step decides the required check: run it as the runner
    does, with the result it would be handed."""
    (gate,) = [job for job in _jobs().values() if job["name"] == _REQUIRED_CHECK]
    (step,) = gate["steps"]
    assert step["env"] == {"RESULT": "${{ needs.unit-tests.result }}"}

    ran = subprocess.run(
        ["bash", "-ec", step["run"]],
        env={**os.environ, "RESULT": result},
        capture_output=True,
        check=False,
    )

    assert (ran.returncode == 0) is passes


@pytest.mark.parametrize("shard", ["0/2", "3/2", "2"])
def test_a_shard_outside_the_split_is_refused(shard: str) -> None:
    """A typo in the matrix would otherwise select no test and pass."""
    with pytest.raises(ValueError):
        in_shard("tests/src/unit/test_x.py::test_y", shard)
