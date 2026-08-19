"""Guard shared HAOS workflow contracts without restructuring proven lanes."""

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
_CACHE_KEY_CONSUMERS = (
    "build-haos-test-image.yml",
    "haos-e2e-tests.yml",
    "haos-e2e-embedded-tests.yml",
    "haos-e2e-inaddon-tests.yml",
    "haos-e2e-stdio-tests.yml",
)
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


def _cache_key_command(path: Path) -> str:
    workflow = _workflow(path)
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "image cache key" in str(step.get("name", "")).lower()
    ]
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
    for filename in _CACHE_KEY_CONSUMERS:
        path = _WORKFLOW_DIR / filename
        assert _cache_key_command(path) == _CACHE_KEY_COMMAND, filename
