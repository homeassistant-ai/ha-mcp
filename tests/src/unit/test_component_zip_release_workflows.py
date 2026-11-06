"""Consumers select the artifact built from the stamped release checkout."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]


def _steps(path: str, job: str) -> list[dict]:
    return yaml.safe_load((_ROOT / path).read_text(encoding="utf-8"))["jobs"][job][
        "steps"
    ]


def test_each_release_uploads_the_zip_before_publishing() -> None:
    for path, job, create_name in [
        (
            ".github/workflows/semver-release.yml",
            "semantic-release",
            "Create draft GitHub release",
        ),
        (
            ".github/workflows/publish-dev.yml",
            "create-prerelease",
            "Create pre-release",
        ),
    ]:
        steps = _steps(path, job)
        build = next(
            i
            for i, s in enumerate(steps)
            if s.get("name") == "Build the stamped integration ZIP"
        )
        create = next(i for i, s in enumerate(steps) if s.get("name") == create_name)
        assert build < create
        assert "uv run -s scripts/build_component_zip.py" in steps[build]["run"]
        assert "ha_mcp_tools.zip" in steps[create]["run"]
        if job == "create-prerelease":
            # With an asset, gh owns the draft/upload/publish lifecycle and
            # attempts cleanup on failure; explicit --draft disables cleanup.
            assert "--draft" not in steps[create]["run"]
            assert "gh release edit" not in steps[create]["run"]
            stamp = next(
                i
                for i, s in enumerate(steps)
                if s.get("name") == "Stamp the component for this build"
            )
            assert stamp < build
        else:
            # Stable publishing is finalized separately, after its PyPI gate.
            assert "--draft" in steps[create]["run"]


def test_the_mirror_copies_the_verified_upstream_artifact_without_rebuilding() -> None:
    steps = _steps(".github/integration-mirror-workflows/release-on-tag.yml", "release")
    copied = next(
        i
        for i, s in enumerate(steps)
        if s.get("name") == "Copy the upstream integration ZIP"
    )
    create = next(
        i for i, s in enumerate(steps) if s.get("name") == "Create release for tag"
    )
    assert copied < create
    assert "gh release download" in steps[copied]["run"]
    assert "sha256sum --check" in steps[copied]["run"]
    assert "build_component_zip.py" not in steps[copied]["run"]
    assert "ha_mcp_tools.zip" in steps[create]["run"]
    stage = next(
        s
        for s in _steps(".github/workflows/sync-integration-mirror.yml", "sync")
        if s.get("name") == "Stage snapshot"
    )["run"]
    assert "component-zip-source.json" in stage
    assert "os.environ['PIN']" in stage
    assert "SOURCE_REPOSITORY" in stage
    names = [
        s.get("name")
        for s in _steps(".github/workflows/sync-integration-mirror.yml", "sync")
    ]
    assert names.index(
        "Wait for the upstream integration ZIP before tagging"
    ) < names.index("Tag the mirror")


def test_focused_e2e_dispatch_keeps_other_backend_runs_as_the_default() -> None:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/e2e-tests.yml").read_text())
    selector = workflow[True]["workflow_dispatch"]["inputs"]["update_path_only"]
    assert selector["type"] == "boolean" and selector["default"] is False
    for name, job in workflow["jobs"].items():
        if name != "e2e-tests-update-path":
            for event, focused, expected in [
                ("push", False, True),
                ("push", True, True),
                ("workflow_dispatch", False, True),
                ("workflow_dispatch", True, False),
            ]:
                condition = (
                    job["if"]
                    .replace(
                        "github.event_name != 'workflow_dispatch'",
                        str(event != "workflow_dispatch").lower(),
                    )
                    .replace("!inputs.update_path_only", str(not focused).lower())
                )
                actual = any(
                    all(term.strip() == "true" for term in group.split("&&"))
                    for group in condition.split("||")
                )
                assert actual is expected, (event, focused, job["if"])
    target = workflow["jobs"]["e2e-tests-update-path"]
    assert "if" not in target
    assert any(s.get("env", {}).get("E2E_UPDATE_PATH") == "1" for s in target["steps"])


@pytest.mark.parametrize("draft", [False, True])
def test_mirror_tag_requires_a_public_zip_not_a_token_visible_draft(tmp_path, draft):
    """The source token can see a draft that the mirror cannot download."""
    marker = tmp_path / ".github"
    marker.mkdir()
    (marker / "component-zip-source.json").write_text(
        json.dumps({"repository": "upstream/repo", "tag": "v9.1.1"})
    )
    release = {
        "draft": draft,
        "assets": [
            {
                "name": "ha_mcp_tools.zip",
                "state": "uploaded",
                "size": 42,
                "digest": "sha256:" + "a" * 64,
            }
        ],
    }
    stub = (
        f'gh() {{ printf "%s\\n" {shlex.quote(json.dumps(release))} | jq -r "$4"; }}\n'
        "seq() { echo 1; }\nsleep() { :; }\n"
    )
    step = next(
        s
        for s in _steps(".github/workflows/sync-integration-mirror.yml", "sync")
        if s.get("name") == "Wait for the upstream integration ZIP before tagging"
    )
    script = (
        step["run"]
        .replace("/tmp/mirror", tmp_path.as_posix())
        .replace("python3 ", shlex.quote(Path(sys.executable).as_posix()) + " ")
    )
    result = subprocess.run(
        [shutil.which("bash") or "bash", "-e", "-c", stub + script],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (result.returncode == 0) is not draft, result.stderr
