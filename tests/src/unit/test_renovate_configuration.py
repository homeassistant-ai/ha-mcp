"""Guard the boundary between immediate HA image pins and ordinary updates."""

import json
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[3]


def test_renovate_extracts_all_stable_image_inputs_once() -> None:
    """Missing or overlapping managers must not drop/duplicate a stable pin."""
    config = json.loads((_ROOT / "renovate.json").read_text())
    files = [
        ".github/workflows/pr.yml",
        ".github/workflows/e2e-tests.yml",
        ".github/workflows/performance-tests.yml",
        "tests/test_constants.py",
        "tests/haos_image_build/build_image.py",
    ]
    extracted: dict[str, list[tuple[str, str]]] = {}
    for filename in files:
        for manager in config["customManagers"]:
            if not any(
                re.search(pattern[1:-1], filename)
                for pattern in manager["managerFilePatterns"]
            ):
                continue
            for pattern in manager["matchStrings"]:
                # These managers use the RE2-compatible subset shared by Python.
                for match in re.finditer(
                    pattern.replace("(?<", "(?P<"), (_ROOT / filename).read_text()
                ):
                    groups = match.groupdict()
                    extracted.setdefault(groups["depName"], []).append(
                        (filename, groups["currentValue"])
                    )
    core = extracted["ghcr.io/home-assistant/home-assistant"]
    assert sorted(filename for filename, _ in core) == sorted(files)
    assert len({version for _, version in core}) == 1
    for dep in ("home-assistant/supervisor", "home-assistant/operating-system"):
        assert len(extracted[dep]) == 1
        assert extracted[dep][0][0] == "tests/haos_image_build/build_image.py"


def test_immediate_policy_is_limited_to_ha_release_inputs() -> None:
    """Broadening the exception must not silently bypass ordinary cooldowns."""
    config = json.loads((_ROOT / "renovate.json").read_text())
    assert config["minimumReleaseAge"] == "7 days"
    assert config["schedule"] == ["after 3pm on tuesday"]
    assert config["updateNotScheduled"] is False
    immediate = [
        r for r in config["packageRules"] if r.get("schedule") == ["at any time"]
    ]
    assert len(immediate) == 1
    rule = immediate[0]
    assert set(rule["matchDepNames"]) == {
        "ghcr.io/home-assistant/home-assistant",
        "home-assistant/supervisor",
        "home-assistant/operating-system",
    }
    assert rule["minimumReleaseAge"] is None
    assert rule["prCreation"] == "immediate"
    assert rule["prHourlyLimit"] == rule["prConcurrentLimit"] == 0


def test_scan_discovers_config_once_and_preserves_manual_events() -> None:
    """A package exemption is ineffective without scans and repository discovery."""
    workflow = yaml.safe_load((_ROOT / ".github/workflows/renovate.yml").read_text())
    assert workflow[True]["schedule"] == [{"cron": "17 * * * *"}]
    assert "workflow_dispatch" in workflow[True]
    assert workflow[True]["issues"]["types"] == ["edited"]
    assert workflow[True]["pull_request_target"] == {
        "types": ["edited"],
        "branches": ["master"],
    }
    job = workflow["jobs"]["renovate"]
    assert job["concurrency"]["cancel-in-progress"] is False
    checkout = next(s for s in job["steps"] if s.get("name") == "Checkout")
    assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkout["with"]["persist-credentials"] is False
    action = next(s for s in job["steps"] if s.get("name") == "Self-hosted Renovate")
    assert "configurationFile" not in action["with"]
    assert "RENOVATE_FORCE" not in action["env"]
