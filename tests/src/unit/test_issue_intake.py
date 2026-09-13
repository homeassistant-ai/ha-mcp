"""Run the dependency-free intake behavior suite in the normal unit-test lane."""

import shutil
import subprocess
from pathlib import Path

import yaml


def test_issue_intake_behavior() -> None:
    root = Path(__file__).resolve().parents[3]
    node = shutil.which("node")
    assert node, "Node is required for issue-intake workflow regression tests"
    completed = subprocess.run(
        [node, "--test", str(root / "tests/js/issue-intake.test.mjs")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_issue_intake_event_and_credential_boundaries() -> None:
    root = Path(__file__).resolve().parents[3]
    workflow = yaml.safe_load((root / ".github/workflows/issue-intake.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert "deleted" in triggers["issue_comment"]["types"]
    assert workflow["permissions"] == {"contents": "read", "issues": "read"}
    job = workflow["jobs"]["document"]
    assert "!github.event.issue.pull_request" in job["if"]
    assert "github.event.comment.user.type != 'Bot'" in job["if"]
    steps = job["steps"]
    assert steps[0]["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    model = next(s for s in steps if s.get("id") == "codex")
    token = next(s for s in steps if s.get("id") == "app-token")
    assert steps.index(model) < steps.index(token)
    assert model["with"]["allow-shell"] == "false"
    assert model["with"]["web-search"] == "disabled"
    assert not model.get("env")
    assert not model["with"].get("passthrough-env")
