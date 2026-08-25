"""Guard shared HAOS workflow contracts without restructuring proven lanes."""

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
_CACHE_KEY_CONSUMER_FLOOR = 5
_CACHE_KEY_OUTPUT_MARKER = "cache-key=haos-image-"
_HAOS_IMAGE_CACHE_PATH = "/tmp/haos-test-image.qcow2"
_CACHE_ACTIONS = {
    "actions/cache",
    "actions/cache/restore",
    "actions/cache/save",
}
_CACHE_KEY_COMMAND = """hash=$(git ls-tree -r HEAD \\
  tests/haos_image_build \\
  tests/initial_test_state \\
  custom_components/ha_mcp_tools \\
  homeassistant-addon-webhook-proxy \\
  | sha256sum | cut -d' ' -f1 | head -c16)
echo "cache-key=haos-image-$hash" >> "$GITHUB_OUTPUT"
"""


def _workflow(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _job_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def _cache_key_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for step in _job_steps(job)
        if _CACHE_KEY_OUTPUT_MARKER in str(step.get("run", ""))
    ]


def _uses_haos_image_cache(job: dict[str, Any]) -> bool:
    return any(
        str(step.get("uses", "")).partition("@")[0] in _CACHE_ACTIONS
        and _HAOS_IMAGE_CACHE_PATH
        in str(step.get("with", {}).get("path", "")).splitlines()
        for step in _job_steps(job)
    )


def _cache_key_consumers(
    workflow_dir: Path = _WORKFLOW_DIR,
) -> list[tuple[Path, str]]:
    workflow_paths = sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")))
    consumers = [
        (path, str(job_id))
        for path in workflow_paths
        for job_id, job in _workflow(path)["jobs"].items()
        if isinstance(job, dict) and _uses_haos_image_cache(job)
    ]
    assert len(consumers) >= _CACHE_KEY_CONSUMER_FLOOR, (
        "expected at least "
        f"{_CACHE_KEY_CONSUMER_FLOOR} HAOS image cache-key consumers, found "
        f"{[(path.name, job_id) for path, job_id in consumers]}"
    )
    return consumers


def _cache_key_command(path: Path, job_id: str) -> str:
    consumer = f"{path.name}:{job_id}"
    job = _workflow(path)["jobs"][job_id]
    assert isinstance(job, dict), f"{consumer} must be a job mapping"
    steps = _cache_key_steps(job)
    assert len(steps) == 1, f"{consumer} must have one image cache-key step"
    script = str(steps[0]["run"])
    start_marker = "hash=$(git ls-tree -r HEAD"
    end_marker = 'echo "cache-key=haos-image-$hash" >> "$GITHUB_OUTPUT"\n'
    assert script.count(start_marker) == 1, (
        f"{consumer} must have one cache-key command"
    )
    assert script.count(end_marker) == 1, (
        f"{consumer} must emit one HAOS image cache key"
    )
    start = script.index(start_marker)
    end = script.index(end_marker, start) + len(end_marker)
    return script[start:end]


def _beta_lane_jobs(
    workflow_dir: Path = _WORKFLOW_DIR,
) -> set[tuple[str, str, str]]:
    """Discover beta-image jobs independently from runtime attestation."""
    beta_jobs: set[tuple[str, str, str]] = set()
    workflow_paths = sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")))
    for path in workflow_paths:
        for job_id, job in _workflow(path)["jobs"].items():
            if not isinstance(job, dict):
                continue
            beta_signal = False
            mode: str | None = None
            for step in _job_steps(job):
                env = step.get("env", {})
                run = str(step.get("run", ""))
                step_text = str(step)
                if isinstance(env, dict):
                    if env.get("HAOS_BUILD_SUPERVISOR_CHANNEL") == "beta":
                        beta_signal = True
                    if isinstance(env.get("HAOS_TEST_MODE"), str):
                        mode = env["HAOS_TEST_MODE"]
                if (
                    "version.home-assistant.io/beta.json" in run
                    or "haos-beta-image-" in step_text
                    or "/tmp/haos-beta-test-image.qcow2" in step_text
                ):
                    beta_signal = True
            if beta_signal:
                beta_jobs.add((path.name, str(job_id), str(mode)))
    return beta_jobs


def test_haos_image_cache_key_command_matches_every_consumer() -> None:
    for path, job_id in _cache_key_consumers():
        consumer = f"{path.name}:{job_id}"
        assert _cache_key_command(path, job_id) == _CACHE_KEY_COMMAND, consumer


def test_cache_key_consumer_discovery_is_marker_independent(tmp_path: Path) -> None:
    workflow = """jobs:
  lane:
    steps:
      - uses: actions/cache/restore@pinned
        with:
          path: /tmp/haos-test-image.qcow2
          key: shared
"""
    expected = []
    for index in range(_CACHE_KEY_CONSUMER_FLOOR):
        path = tmp_path / f"lane-{index}.yaml"
        path.write_text(workflow, encoding="utf-8")
        expected.append((path, "lane"))

    assert _cache_key_consumers(tmp_path) == expected


def test_cache_key_consumer_discovery_tracks_jobs_individually(
    tmp_path: Path,
) -> None:
    path = tmp_path / "multi-lane.yaml"
    jobs = {
        f"lane-{index}": {
            "steps": [
                {
                    "uses": "actions/cache/restore@pinned",
                    "with": {"path": _HAOS_IMAGE_CACHE_PATH, "key": "shared"},
                }
            ]
        }
        for index in range(_CACHE_KEY_CONSUMER_FLOOR)
    }
    path.write_text(yaml.safe_dump({"jobs": jobs}), encoding="utf-8")

    assert _cache_key_consumers(tmp_path) == [
        (path, f"lane-{index}") for index in range(_CACHE_KEY_CONSUMER_FLOOR)
    ]


def test_beta_lane_discovery_does_not_depend_on_attestation(tmp_path: Path) -> None:
    """A beta build is discovered even when runtime attestation is missing."""
    workflow = {
        "jobs": {
            "beta": {
                "steps": [
                    {
                        "run": "curl https://version.home-assistant.io/beta.json",
                    },
                    {
                        "run": "pytest src/e2e",
                        "env": {"HAOS_TEST_MODE": "inaddon"},
                    },
                ]
            }
        }
    }
    path = tmp_path / "beta.yml"
    path.write_text(yaml.safe_dump(workflow), encoding="utf-8")

    assert _beta_lane_jobs(tmp_path) == {("beta.yml", "beta", "inaddon")}


def test_beta_lanes_share_a_current_supervisor_and_core_image() -> None:
    """Both beta workflows share one manifest-keyed qcow2 cache contract."""
    lane_specs = (
        (
            "haos-e2e-inaddon-beta-tests.yml",
            "haos-e2e-inaddon-beta",
            "inaddon",
            "haos-e2e-inaddon-tests.yml",
            "haos-e2e-inaddon",
        ),
        (
            "haos-e2e-embedded-beta-tests.yml",
            "haos-e2e-embedded-beta",
            "embedded",
            "haos-e2e-embedded-tests.yml",
            "haos-e2e-embedded",
        ),
    )
    beta_resolvers: list[str] = []
    beta_cache_keys: list[str] = []
    assert _beta_lane_jobs() == {
        (beta_name, beta_job_id, mode)
        for beta_name, beta_job_id, mode, _stable_name, _stable_job_id in lane_specs
    }

    for beta_name, beta_job_id, mode, stable_name, stable_job_id in lane_specs:
        beta_path = _WORKFLOW_DIR / beta_name
        assert beta_path.is_file(), f"missing beta lane cloned from {stable_name}"
        workflow = _workflow(beta_path)
        job = workflow["jobs"][beta_job_id]

        triggers = workflow[True]
        assert triggers["pull_request"] is None
        assert triggers["push"]["branches"] == ["master"]
        assert triggers["schedule"]
        assert all(entry.get("cron") for entry in triggers["schedule"])
        classifier = next(
            step
            for step in _job_steps(workflow["jobs"]["changes"])
            if step.get("id") == "filter"
        )
        assert (
            "git diff --no-renames --name-only --diff-filter=ACMD" in classifier["run"]
        )
        assert job["needs"] == "changes"
        assert "needs.changes.outputs.run != 'false'" in job["if"]
        steps = _job_steps(job)

        resolve = next(
            step
            for step in steps
            if step.get("name") == "Resolve current beta Supervisor and Core versions"
        )
        assert "version.home-assistant.io/beta.json" in resolve["run"]
        assert '["supervisor"]' in resolve["run"]
        assert '["homeassistant"]["qemux86-64"]' in resolve["run"]
        beta_resolvers.append(resolve["run"])

        cache_key = next(
            step for step in steps if step.get("name") == "Compute beta image cache key"
        )
        cache_script = cache_key["run"]
        assert "haos-beta-image-" in cache_script
        assert "steps.versions.outputs.supervisor_version" in cache_script
        assert "steps.versions.outputs.core_version" in cache_script
        beta_cache_keys.append(cache_script)

        build = next(
            step
            for step in steps
            if step.get("name") == "Build image locally (cache miss or forced rebuild)"
        )
        assert build["env"] == {
            "HAOS_BUILD_SUPERVISOR_CHANNEL": "beta",
            "HAOS_BUILD_SUPERVISOR_MIN_VERSION": (
                "${{ steps.versions.outputs.supervisor_version }}"
            ),
            "HAOS_BUILD_CORE_VERSION": "${{ steps.versions.outputs.core_version }}",
        }

        restore = next(
            step for step in steps if step.get("name") == "Restore image from cache"
        )
        assert restore["with"]["path"] == "/tmp/haos-beta-test-image.qcow2"

        run_step = next(
            step for step in steps if step.get("env", {}).get("HAOS_TEST_MODE")
        )
        run_env = run_step["env"]
        assert run_env["HAOS_TEST_MODE"] == mode
        assert run_env["HAOS_TEST_IMAGE_PATH"] == "/tmp/haos-beta-test-image.qcow2"
        assert run_env["HAOS_EXPECTED_SUPERVISOR_CHANNEL"] == "beta"
        assert run_env["HAOS_EXPECTED_SUPERVISOR_MIN_VERSION"] == (
            "${{ steps.versions.outputs.supervisor_version }}"
        )
        assert run_env["HAOS_EXPECTED_CORE_VERSION"] == (
            "${{ steps.versions.outputs.core_version }}"
        )
        assert "src/e2e/" in run_step["run"]
        assert (
            workflow[True]["workflow_dispatch"]["inputs"]["pytest_paths"]["default"]
            == "src/e2e/"
        )

        if mode == "inaddon":
            diagnostics = next(
                step
                for step in steps
                if step.get("name", "").startswith("Extract HAOS")
            )
            diagnostics_script = diagnostics["run"]
            assert "targets=(/tmp/haos-beta-test-image-gw*.qcow2)" in diagnostics_script
            assert "targets+=(/tmp/haos-beta-test-image.qcow2)" in diagnostics_script

        stable = _workflow(_WORKFLOW_DIR / stable_name)
        stable_steps = _job_steps(stable["jobs"][stable_job_id])
        stable_build = next(
            step
            for step in stable_steps
            if step.get("name") == "Build image locally (cache miss or forced rebuild)"
        )
        assert "env" not in stable_build

    assert beta_resolvers[0] == beta_resolvers[1]
    assert beta_cache_keys[0] == beta_cache_keys[1]
