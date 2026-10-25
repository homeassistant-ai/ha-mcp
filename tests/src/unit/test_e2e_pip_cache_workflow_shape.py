"""Shape pins for the testcontainer E2E pip-cache wiring.

Every testcontainer job builds its own Home Assistant container and
pip-installs the component's pinned wheel plus the checkout wheel inside it,
per worker. ``.github/actions/ha-e2e-pip-cache`` is what makes those downloads
reusable across runs; a new lane that copies an existing job without the step
pays the full dependency tree again and silently re-adds the PyPI dependency
to a required check. A comment is not a gate, so these tests read the workflow
YAML directly and fire only when an edit drops the wiring.
"""

import re
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
_ACTION_YML = _REPO_ROOT / ".github" / "actions" / "ha-e2e-pip-cache" / "action.yml"
_CONTAINER_LANES = ("pr.yml", "e2e-tests.yml", "e2e-beta-tests.yml")
_CACHE_STEP_PREFIX = "./.github/actions/ha-e2e-pip-cache"
# Both invocation forms in use: the literal path, and the beta workflows'
# ``pytest $PYTEST_PATHS`` with ``src/e2e/`` as the default input.
_E2E_COMMAND = re.compile(r"pytest\s+(?:src/e2e\b|\"?\$?\{?PYTEST_PATHS)")


def _jobs(name: str) -> dict[str, Any]:
    data = yaml.safe_load((_WORKFLOW_DIR / name).read_text(encoding="utf-8"))
    return data["jobs"]


def _runs_e2e(job: dict[str, Any]) -> bool:
    """True when the job executes the E2E suite in this repo."""
    return any(
        _E2E_COMMAND.search(str(step.get("run", ""))) for step in job.get("steps", [])
    )


def test_the_lane_detection_matches_every_container_lane() -> None:
    """A command rename must not shrink the set of jobs this test guards."""
    for name in _CONTAINER_LANES:
        matched = [job_name for job_name, job in _jobs(name).items() if _runs_e2e(job)]
        assert len(matched) >= 4, (
            f"{name}: only {matched} look like E2E jobs; the detection is stale"
        )


def _uses_cache_action(job: dict[str, Any]) -> bool:
    return any(
        str(step.get("uses", "")).startswith(_CACHE_STEP_PREFIX)
        for step in job.get("steps", [])
    )


def test_every_testcontainer_job_caches_the_wheel_tree() -> None:
    """A lane that runs E2E must restore the pip cache the knobs bind."""
    missing = [
        f"{name}:{job_name}"
        for name in _CONTAINER_LANES
        for job_name, job in _jobs(name).items()
        if _runs_e2e(job) and not _uses_cache_action(job)
    ]
    assert missing == [], f"E2E jobs without the pip-cache step: {missing}"


def test_the_cache_step_receives_the_lane_image() -> None:
    """The key must follow the image so wheels never cross interpreter versions."""
    for name in _CONTAINER_LANES:
        for job_name, job in _jobs(name).items():
            if not _runs_e2e(job):
                continue
            step = next(
                step
                for step in job["steps"]
                if str(step.get("uses", "")).startswith(_CACHE_STEP_PREFIX)
            )
            assert step["with"]["ha-image"] == "${{ env.HA_IMAGE_GHCR }}", (
                f"{name}:{job_name} must key the cache on the lane's image"
            )


def test_the_action_points_the_lanes_at_the_restored_directory() -> None:
    """Restoring a directory the lanes never read would cache nothing."""
    action = yaml.safe_load(_ACTION_YML.read_text(encoding="utf-8"))
    steps = action["runs"]["steps"]
    cache = next(
        step for step in steps if step.get("uses", "").startswith("actions/cache")
    )
    assert cache["with"]["path"] == "${{ runner.temp }}/ha-e2e-pip"

    export = "\n".join(str(step.get("run", "")) for step in steps)
    assert "HA_E2E_PIP_CACHE=" in export, (
        "the action must export HA_E2E_PIP_CACHE so the fixtures bind the cache"
    )
    assert "${RUNNER_TEMP}/ha-e2e-pip" in export, (
        "the exported path must be the directory the action restores"
    )
