"""Guard the paired component/server release automation (#2427).

The mirror sync and the dev-version script only run after a merge, so PR CI
never executes them. These tests pin the workflow's ordering and run the
stamping script against a scratch copy of the component.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_MIRROR = _WORKFLOWS / "sync-integration-mirror.yml"
_COMPONENT = _REPO_ROOT / "custom_components" / "ha_mcp_tools"
_STAMP = _REPO_ROOT / "scripts" / "stamp_component_version.py"
_DEV_VERSION = _REPO_ROOT / "scripts" / "dev_version.sh"


def _workflow(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _sync_step_names() -> list[str]:
    return [step.get("name") for step in _workflow(_MIRROR)["jobs"]["sync"]["steps"]]


class TestMirrorSyncShape:
    def test_both_tagging_legs_follow_their_publishing_workflow(self) -> None:
        # yaml reads the bare `on` key as True.
        triggers = _workflow(_MIRROR)[True]
        assert triggers["workflow_run"]["workflows"] == [
            "SemVer Release",
            "Publish Dev Channel",
        ]

    def test_gate_keys_on_the_jobs_that_publish_the_pin(self) -> None:
        gate = _workflow(_MIRROR)["jobs"]["gate"]["steps"][0]["run"]
        jobs = {
            job["name"]
            for path in (
                _WORKFLOWS / "publish-dev.yml",
                _WORKFLOWS / "semver-release.yml",
            )
            for job in _workflow(path)["jobs"].values()
            if "name" in job
        }
        assert "Semantic Release" in jobs
        assert "Publish PyPI (dev build of ha-mcp)" in jobs
        assert 'job="Semantic Release"' in gate
        assert 'job="Publish PyPI (dev build of ha-mcp)"' in gate

    def test_tags_only_after_pypi_serves_the_pin(self) -> None:
        names = _sync_step_names()
        assert names.index("Stage snapshot") < names.index("Commit and push")
        assert names.index("Commit and push") < names.index(
            "Wait for the pinned ha-mcp build on PyPI"
        )
        assert names.index("Wait for the pinned ha-mcp build on PyPI") < names.index(
            "Tag the mirror"
        )

    def test_snapshot_is_stamped(self) -> None:
        stage = next(
            step
            for step in _workflow(_MIRROR)["jobs"]["sync"]["steps"]
            if step.get("name") == "Stage snapshot"
        )
        assert "scripts/stamp_component_version.py" in stage["run"]


class TestDevVersionScript:
    def test_uses_the_semantic_release_version_the_release_runs(self) -> None:
        # The dev builds' base must be what the real release would cut, so the
        # script's pinned CLI must match the release workflow's action pin.
        script = _DEV_VERSION.read_text(encoding="utf-8")
        release = (_WORKFLOWS / "semver-release.yml").read_text(encoding="utf-8")
        script_version = re.search(r'PSR_VERSION="([\d.]+)"', script)
        action_version = re.search(
            r"python-semantic-release/python-semantic-release@\S+ # v([\d.]+)", release
        )
        assert script_version and action_version
        assert script_version.group(1) == action_version.group(1)

    @pytest.mark.parametrize("workflow", ["publish-dev.yml", "addon-publish-dev.yml"])
    def test_every_dev_surface_uses_the_script(self, workflow: str) -> None:
        assert "DEV_VERSION=$(scripts/dev_version.sh)" in (
            _WORKFLOWS / workflow
        ).read_text(encoding="utf-8")


class TestReleaseStamping:
    def test_semantic_release_stamps_the_component(self) -> None:
        pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        for variable in (
            "custom_components/ha_mcp_tools/manifest.json:version",
            "custom_components/ha_mcp_tools/manifest.json:ha-mcp",
            "custom_components/ha_mcp_tools/const.py:COMPONENT_VERSION",
        ):
            assert f'"{variable}"' in pyproject


@pytest.fixture
def component(tmp_path: Path) -> Path:
    target = tmp_path / "ha_mcp_tools"
    target.mkdir()
    for name in ("manifest.json", "const.py"):
        shutil.copy(_COMPONENT / name, target / name)
    return target


def _stamp(component: Path, version: str, pin: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(_STAMP),
            "--component-dir",
            str(component),
            "--version",
            version,
            "--pin",
            pin,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


class TestStampComponentVersion:
    def test_stamps_a_dev_pre_release(self, component: Path) -> None:
        result = _stamp(component, "9.0.0", "9.0.0.dev2901")

        assert result.returncode == 0, result.stderr
        manifest = json.loads((component / "manifest.json").read_text())
        assert manifest["version"] == "9.0.0"
        assert "ha-mcp==9.0.0.dev2901" in manifest["requirements"]
        assert sum(r.startswith("ha-mcp") for r in manifest["requirements"]) == 1
        assert 'COMPONENT_VERSION = "9.0.0"' in (component / "const.py").read_text()

    def test_keeps_the_other_requirements(self, component: Path) -> None:
        before = json.loads((component / "manifest.json").read_text())["requirements"]

        _stamp(component, "9.0.0", "9.0.0.dev1")

        after = json.loads((component / "manifest.json").read_text())["requirements"]
        assert [r for r in after if not r.startswith("ha-mcp")] == [
            r for r in before if not r.startswith("ha-mcp")
        ]

    def test_refuses_a_suffixed_component_version(self, component: Path) -> None:
        # Released servers parse the component version as integers.
        result = _stamp(component, "9.0.0.dev2901", "9.0.0.dev2901")

        assert result.returncode == 1
        assert "X.Y.Z" in result.stderr

    def test_refuses_a_manifest_without_the_server_pin(self, component: Path) -> None:
        manifest = json.loads((component / "manifest.json").read_text())
        manifest["requirements"] = [
            r for r in manifest["requirements"] if not r.startswith("ha-mcp")
        ]
        (component / "manifest.json").write_text(json.dumps(manifest))

        result = _stamp(component, "9.0.0", "9.0.0.dev1")

        assert result.returncode == 1
        assert "exactly one ha-mcp requirement" in result.stderr
