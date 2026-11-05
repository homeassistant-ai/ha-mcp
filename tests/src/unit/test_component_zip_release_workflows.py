"""Consumers select the artifact built from the stamped release checkout."""

from pathlib import Path

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
        (
            ".github/integration-mirror-workflows/release-on-tag.yml",
            "release",
            "Create release for tag",
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
        assert "--draft" in steps[create]["run"]
        if job == "create-prerelease":
            stamp = next(
                i
                for i, s in enumerate(steps)
                if s.get("name") == "Stamp the component for this build"
            )
            assert stamp < build


def test_hacs_and_the_mirror_use_the_same_zip_filename() -> None:
    import json

    manifests = [
        json.loads((_ROOT / name).read_text(encoding="utf-8"))
        for name in ("hacs.json", "custom_components/ha_mcp_tools/hacs.json")
    ]
    assert all(m["zip_release"] for m in manifests)
    assert {m["filename"] for m in manifests} == {"ha_mcp_tools.zip"}
    stage = next(
        s
        for s in _steps(".github/workflows/sync-integration-mirror.yml", "sync")
        if s.get("name") == "Stage snapshot"
    )["run"]
    assert "scripts/build_component_zip.py.lock" in stage
    assert "/tmp/mirror/scripts/" in stage


def test_focused_e2e_dispatch_keeps_other_backend_runs_as_the_default() -> None:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/e2e-tests.yml").read_text())
    selector = workflow[True]["workflow_dispatch"]["inputs"]["update_path_only"]
    assert selector["type"] == "boolean" and selector["default"] is False
    for name, job in workflow["jobs"].items():
        if name != "e2e-tests-update-path":
            assert "github.event_name != 'workflow_dispatch'" in job["if"]
            assert "!inputs.update_path_only" in job["if"]
    target = workflow["jobs"]["e2e-tests-update-path"]
    assert "if" not in target
    assert any(s.get("env", {}).get("E2E_UPDATE_PATH") == "1" for s in target["steps"])
