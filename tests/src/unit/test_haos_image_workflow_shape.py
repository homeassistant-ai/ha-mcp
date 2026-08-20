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
