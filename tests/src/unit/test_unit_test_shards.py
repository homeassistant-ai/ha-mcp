"""The unit suite runs on several CI runners; no test may fall between them."""

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
    for nodeid in _IDS:
        assert [in_shard(nodeid, shard) for shard in _shards()].count(True) == 1


def test_the_runners_take_similar_shares() -> None:
    """One runner with most of the suite would be as slow as no split."""
    shares = [sum(in_shard(nodeid, shard) for nodeid in _IDS) for shard in _shards()]
    assert max(shares) <= 1.2 * min(shares)


def test_each_runner_is_told_its_own_shard() -> None:
    """Without the variable every runner would run the whole suite."""
    (run,) = [
        step
        for step in _jobs()["unit-tests"]["steps"]
        if "pytest tests/src/unit/" in step.get("run", "")
    ]
    assert run["env"]["UNIT_TEST_SHARD"] == "${{ matrix.shard }}"


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


@pytest.mark.parametrize("shard", ["0/2", "3/2", "2"])
def test_a_shard_outside_the_split_is_refused(shard: str) -> None:
    """A typo in the matrix would otherwise select no test and pass."""
    with pytest.raises(ValueError):
        in_shard("tests/src/unit/test_x.py::test_y", shard)
