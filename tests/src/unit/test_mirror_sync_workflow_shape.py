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
        assert "Publish PyPI (dev channel)" in jobs
        assert 'job="Semantic Release"' in gate
        assert 'job="Publish PyPI (dev channel)"' in gate

    def test_dev_leg_pins_the_version_the_dev_build_uploaded(self) -> None:
        # Recomputing the number in the mirror could name a build PyPI never
        # got, or one built from a different commit.
        publish = _workflow(_WORKFLOWS / "publish-dev.yml")["jobs"]["prepare"]
        uploads = [
            step["with"]["name"]
            for step in publish["steps"]
            if "upload-artifact" in step.get("uses", "")
        ]
        mirror = _workflow(_MIRROR)["jobs"]["sync"]["steps"]
        downloads = [
            step["with"]["name"]
            for step in mirror
            if "download-artifact" in step.get("uses", "")
        ]
        assert uploads == downloads == ["dev-version"]
        resolve = next(
            s
            for s in mirror
            if s.get("name") == "Resolve the release this run publishes"
        )
        assert "dev_version.sh" not in resolve["run"]

    def test_tags_only_after_pypi_serves_the_pin(self) -> None:
        names = _sync_step_names()
        assert names.index("Stage snapshot") < names.index("Commit and push")
        assert names.index("Commit and push") < names.index(
            "Wait for the pinned server build on PyPI"
        )
        assert names.index("Wait for the pinned server build on PyPI") < names.index(
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

    @pytest.mark.parametrize(
        ("event", "expected"),
        [("push", "9.0.0.dev2"), ("workflow_dispatch", "9.0.0.dev3")],
    )
    def test_counts_the_built_commit_on_push(
        self, tmp_path: Path, event: str, expected: str
    ) -> None:
        # A push build whose checkout finds master already advanced must not
        # take the next push's number: PyPI keeps the first upload, so the
        # newer build would be dropped while the HACS pre-release pinned it.
        repo = tmp_path / "repo"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_uvx = bin_dir / "uvx"
        fake_uvx.write_text("#!/bin/sh\necho 9.0.0\n")
        fake_uvx.chmod(0o755)

        def git(*args: str) -> None:
            subprocess.run(
                ["git", "-C", str(repo), *args], check=True, capture_output=True
            )

        repo.mkdir()
        git("init", "-q", "-b", "master")
        for n in range(3):
            git(
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                str(n),
            )
        git("update-ref", "refs/remotes/origin/master", "HEAD")
        git("reset", "-q", "--hard", "HEAD~1")

        result = subprocess.run(
            ["bash", str(_DEV_VERSION)],
            cwd=repo,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin", "GITHUB_EVENT_NAME": event},
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected


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


def _stamp(
    component: Path, version: str, pin: str, dist: str = "ha-mcp"
) -> subprocess.CompletedProcess:
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
            "--dist",
            dist,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


class TestStampComponentVersion:
    def test_stamps_a_dev_pre_release(self, component: Path) -> None:
        result = _stamp(component, "9.0.0", "9.0.0.dev2901", dist="ha-mcp-dev")

        assert result.returncode == 0, result.stderr
        manifest = json.loads((component / "manifest.json").read_text())
        assert manifest["version"] == "9.0.0"
        # One server requirement: two would install both distributions over
        # the same ha_mcp package.
        assert [r for r in manifest["requirements"] if r.startswith("ha-mcp")] == [
            "ha-mcp-dev==9.0.0.dev2901"
        ]
        assert 'COMPONENT_VERSION = "9.0.0"' in (component / "const.py").read_text()

    def test_restamps_a_dev_snapshot(self, component: Path) -> None:
        _stamp(component, "9.0.0", "9.0.0.dev1", dist="ha-mcp-dev")

        result = _stamp(component, "9.0.0", "9.0.0.dev2", dist="ha-mcp-dev")

        assert result.returncode == 0, result.stderr
        manifest = json.loads((component / "manifest.json").read_text())
        assert "ha-mcp-dev==9.0.0.dev2" in manifest["requirements"]

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
