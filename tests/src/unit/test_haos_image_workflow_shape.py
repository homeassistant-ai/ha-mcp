"""Guard shared HAOS workflow contracts without restructuring proven lanes."""

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
_CACHE_KEY_CONSUMER_FLOOR = 5
_CACHE_KEY_OUTPUT_MARKER = "cache-key=haos-image-"
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


def _workflow_steps(path: Path) -> list[dict[str, Any]]:
    workflow = _workflow(path)
    return [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step, dict)
    ]


def _cache_key_steps(path: Path) -> list[dict[str, Any]]:
    return [
        step
        for step in _workflow_steps(path)
        if _CACHE_KEY_OUTPUT_MARKER in str(step.get("run", ""))
    ]


def _cache_key_consumers() -> list[Path]:
    paths = [
        path
        for path in sorted(_WORKFLOW_DIR.glob("*.yml"))
        if _cache_key_steps(path)
    ]
    assert len(paths) >= _CACHE_KEY_CONSUMER_FLOOR, (
        "expected at least "
        f"{_CACHE_KEY_CONSUMER_FLOOR} HAOS image cache-key consumers, found "
        f"{[path.name for path in paths]}"
    )
    return paths


def _cache_key_command(path: Path) -> str:
    steps = _cache_key_steps(path)
    assert len(steps) == 1, f"{path.name} must have one image cache-key step"
    script = str(steps[0]["run"])
    start_marker = "hash=$(git ls-tree -r HEAD"
    end_marker = 'echo "cache-key=haos-image-$hash" >> "$GITHUB_OUTPUT"\n'
    assert script.count(start_marker) == 1, (
        f"{path.name} must have one cache-key command"
    )
    assert script.count(end_marker) == 1, (
        f"{path.name} must emit one HAOS image cache key"
    )
    start = script.index(start_marker)
    end = script.index(end_marker, start) + len(end_marker)
    return script[start:end]


def test_haos_image_cache_key_command_matches_every_consumer() -> None:
    for path in _cache_key_consumers():
        assert _cache_key_command(path) == _CACHE_KEY_COMMAND, path.name
