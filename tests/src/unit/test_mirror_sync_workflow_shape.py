"""Guard the post-merge stranded-component gate in the mirror sync workflow.

The gate runs only on a push to master that the mirror sync picks up, so PR
CI never executes it. These tests pin its shape from the workflow file and run
its script against a local stand-in for the mirror through every outcome
(PR #2375; Codex asked for committed coverage).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "sync-integration-mirror.yml"
_GATE = "Fail a merge that changed the component under an already-released version"


def _sync_steps() -> list[dict[str, Any]]:
    data = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return list(data["jobs"]["sync"]["steps"])


def _gate_step() -> dict[str, Any]:
    return next(s for s in _sync_steps() if s.get("name") == _GATE)


class TestShape:
    def test_runs_after_the_snapshot_push_and_before_any_tag(self) -> None:
        names = [s.get("name") for s in _sync_steps()]
        i = names.index(_GATE)
        assert names[i - 1] == "Commit and push"
        assert names[i + 1] == "Tag mirror for stable release"
        assert names.index("Tag mirror dev pre-release") > i

    def test_runs_on_every_push_regardless_of_the_cached_diff(self) -> None:
        """Gating on ``component_changed`` opened a rerun hole: a failed first
        attempt has already pushed the offending snapshot, so a rerun sees no
        diff, skips the gate and turns the stranded commit green (Codex)."""
        cond = _gate_step()["if"]
        assert "github.event_name == 'push'" in cond
        assert "component_changed" not in cond
        assert "workflow_run" not in cond

    def test_a_failure_stops_the_tag_steps(self) -> None:
        gate = _gate_step()
        assert "continue-on-error" not in gate
        for step in _sync_steps():
            if step.get("name", "").startswith("Tag mirror"):
                assert "always()" not in str(step.get("if", ""))
                assert "failure()" not in str(step.get("if", ""))


# ------------------------------------------------------------------ behaviour


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _write_component(root: Path, version: str, body: str) -> None:
    comp = root / "custom_components" / "ha_mcp_tools"
    comp.mkdir(parents=True, exist_ok=True)
    (comp / "manifest.json").write_text(
        json.dumps({"domain": "ha_mcp_tools", "version": version}), encoding="utf-8"
    )
    (comp / "__init__.py").write_text(body, encoding="utf-8")


@pytest.fixture
def mirror_world(tmp_path: Path) -> dict[str, Path]:
    """A bare 'mirror' origin whose v2.1.3 tag holds one component snapshot, a
    clone of it (what the sync job works in), and a repo checkout to stage."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    mirror = tmp_path / "mirror"
    checkout = tmp_path / "checkout"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _write_component(seed, "2.1.3", "RELEASED = True\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "v2.1.3 snapshot")
    _git(seed, "tag", "v2.1.3")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main", "v2.1.3")
    _git(tmp_path, "clone", "-q", str(origin), str(mirror))
    checkout.mkdir()
    return {"mirror": mirror, "checkout": checkout}


def _stage(world: dict[str, Path]) -> None:
    """What 'Stage snapshot' + 'Commit and push' leave in the mirror clone."""
    dest = world["mirror"] / "custom_components"
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(world["checkout"] / "custom_components", dest)
    _git(world["mirror"], "add", "-A")
    _git(world["mirror"], "commit", "-q", "--allow-empty", "-m", "snapshot")


def _run_gate(world: dict[str, Path]) -> subprocess.CompletedProcess[str]:
    script = _gate_step()["run"]
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", script],
        cwd=world["checkout"],
        env={**os.environ, "MIRROR_DIR": str(world["mirror"])},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="runs the workflow's bash script; CI's ubuntu runner has bash",
)
class TestBehaviour:
    def test_pending_version_with_no_stable_tag_passes(self, mirror_world) -> None:
        _write_component(mirror_world["checkout"], "2.1.4", "NEW = True\n")
        _stage(mirror_world)
        done = _run_gate(mirror_world)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "no stable tag v2.1.4" in done.stdout

    def test_identical_content_under_the_released_version_passes(
        self, mirror_world
    ) -> None:
        _write_component(mirror_world["checkout"], "2.1.3", "RELEASED = True\n")
        _stage(mirror_world)
        done = _run_gate(mirror_world)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "identical" in done.stdout

    def test_changed_content_under_the_released_version_fails(
        self, mirror_world
    ) -> None:
        _write_component(mirror_world["checkout"], "2.1.3", "CHANGED = True\n")
        _stage(mirror_world)
        done = _run_gate(mirror_world)
        assert done.returncode == 1, done.stdout + done.stderr
        assert "::error::" in done.stdout
        assert "v2.1.3" in done.stdout and "Bump manifest.json" in done.stdout

    def test_rerun_after_the_snapshot_already_landed_still_fails(
        self, mirror_world
    ) -> None:
        """A rerun clones a mirror that already holds the offending snapshot, so
        nothing is staged; the gate still compares against the tag and fails."""
        _write_component(mirror_world["checkout"], "2.1.3", "CHANGED = True\n")
        _stage(mirror_world)
        assert _run_gate(mirror_world).returncode == 1
        # Second attempt: same checkout, mirror unchanged, no new commit.
        done = _run_gate(mirror_world)
        assert done.returncode == 1, done.stdout + done.stderr
