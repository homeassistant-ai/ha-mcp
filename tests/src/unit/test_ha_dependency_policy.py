"""Keep HA-owned dependencies compatible across component-supported Core versions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement

_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("name", "current", "next_patch", "breaking"),
    [
        ("pydantic", "2.13.4", "2.13.5", "3.0.0"),
        ("httpx", "0.28.1", "0.28.2", "0.29.0"),
    ],
)
def test_ha_owned_requirements_allow_core_patch_updates(
    name, current, next_patch, breaking
):
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    deps = {
        dep.name: dep for dep in map(Requirement, project["project"]["dependencies"])
    }
    assert current in deps[name].specifier
    assert next_patch in deps[name].specifier
    assert breaking not in deps[name].specifier
    if name == "httpx":
        assert deps[name].extras == {"socks"}


@pytest.mark.parametrize("name", ["pydantic", "httpx"])
def test_dependabot_defers_version_updates_without_suppressing_security(name):
    config = yaml.safe_load((_ROOT / ".github/dependabot.yml").read_text())
    uv = next(item for item in config["updates"] if item["package-ecosystem"] == "uv")
    rule = next(item for item in uv["ignore"] if item["dependency-name"] == name)
    assert set(rule["update-types"]) == {
        "version-update:semver-major",
        "version-update:semver-minor",
        "version-update:semver-patch",
    }
    assert "versions" not in rule


@pytest.mark.parametrize(
    ("current_status", "floor_status", "expected"),
    [
        (0, 0, 0),
        (0, 1, 1),
        (1, 0, 1),
        (78, 1, 1),
        (1, 78, 1),
        (0, 2, 1),
        (78, 0, 0),
        (0, 78, 0),
    ],
)
def test_alignment_checks_both_versions_and_preserves_failures(
    tmp_path, current_status, floor_status, expected
):
    workflow = yaml.safe_load((_ROOT / ".github/workflows/pr.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["fast-checks"]["steps"]
        if step.get("name") == "Check dependency alignment with HA core constraints"
    )
    (tmp_path / "hacs.json").write_text(json.dumps({"homeassistant": "2026.8.0"}))
    (tmp_path / "python3").symlink_to(sys.executable)
    uv = tmp_path / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        "version = sys.argv[-1]\n"
        "with Path('checked').open('a') as output:\n"
        "    output.write(version + '\\n')\n"
        f"sys.exit({{'2026.9.1': {current_status}, '2026.8.0': {floor_status}}}[version])\n"
    )
    uv.chmod(0o755)
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "HA_IMAGE_GHCR": "ghcr.io/home-assistant/home-assistant:2026.9.1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert set((tmp_path / "checked").read_text().splitlines()) == {
        "2026.8.0",
        "2026.9.1",
    }
