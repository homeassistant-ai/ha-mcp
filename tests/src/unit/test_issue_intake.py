"""Run the dependency-free intake behavior suite in the normal unit-test lane."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


def test_issue_intake_behavior() -> None:
    root = Path(__file__).resolve().parents[3]
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for issue-intake regression tests")
    completed = subprocess.run(
        [
            node,
            "--test",
            *map(str, sorted((root / "tests/js").glob("issue-intake*.test.mjs"))),
        ],
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
    assert triggers["workflow_dispatch"]["inputs"]["model"]["options"] == [
        "gpt-5.6-terra",
        "gpt-6-sol",
    ]
    assert "deleted" in triggers["issue_comment"]["types"]
    assert workflow["permissions"] == {"contents": "read", "issues": "read"}
    admission = workflow["jobs"]["admit"]
    assert "!github.event.issue.pull_request" in admission["if"]
    assert "github.event.comment.user.type != 'Bot'" in admission["if"]
    assert {"labeled", "unlabeled", "locked", "unlocked"} <= set(
        triggers["issues"]["types"]
    )
    assert "concurrency" not in workflow
    assert admission["concurrency"]["cancel-in-progress"] is True
    assert "github.run_id" in admission["concurrency"]["group"]
    assert (
        "!contains(fromJSON('[" in admission["if"]
        and "github.event.label.name == 'needs-info'" in admission["if"]
    )
    assert admission["steps"][0]["if"] == "github.event_name != 'workflow_dispatch'"
    job = workflow["jobs"]["document"]
    assert job["needs"] == "admit"
    assert job["if"] == "!cancelled() && needs.admit.outputs.run == 'true'"
    assert job["concurrency"]["cancel-in-progress"] is False
    assert "codex-auth-" in job["concurrency"]["group"]
    steps = job["steps"]
    assert steps[0]["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    model = next(s for s in steps if s.get("id") == "codex")
    token = next(s for s in steps if s.get("id") == "app-token")
    assert steps.index(model) < steps.index(token)
    assert model["with"]["allow-shell"] == "false"
    assert model["with"]["web-search"] == "disabled"
    assert not model.get("env")
    assert not model["with"].get("passthrough-env")


def test_report_gate_runs_trusted_code_and_mints_write_access_only_to_act() -> None:
    root = Path(__file__).resolve().parents[3]
    workflow = yaml.safe_load((root / ".github/workflows/issue-intake.yml").read_text())
    gate = workflow["jobs"]["gate"]
    # Bot events (the gate's own close/reopen/comment) must not re-enter it.
    assert "github.event.sender.type == 'User'" in gate["if"]
    assert "!github.event.issue.pull_request" in gate["if"]
    # A comment edited to add the report must be able to reopen the issue.
    assert '"edited"' in gate["if"].split("github.event_name == 'issue_comment'")[1]
    steps = gate["steps"]
    checkout = steps[0]["with"]
    assert checkout["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkout["persist-credentials"] is False
    token = next(s for s in steps if s.get("id") == "app-token")
    assert "steps.check.outputs.action" in token["if"]
    check = next(s for s in steps if s.get("id") == "check")
    assert steps.index(check) < steps.index(token)
    assert check["env"]["GH_TOKEN"] == "${{ github.token }}"
    apply = steps[-1]
    assert apply["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    # Queued, not cancelled: two runs at once could each post a notice.
    assert gate["concurrency"]["cancel-in-progress"] is False
    admit = workflow["jobs"]["admit"]
    # Admission waits for the gate but still runs when it is skipped or fails.
    assert admit["needs"] == "gate"
    assert admit["if"].startswith("!cancelled() &&")


def test_report_gate_sweep_runs_trusted_code_and_mints_write_access_only_when_due() -> (
    None
):
    root = Path(__file__).resolve().parents[3]
    workflow = yaml.safe_load((root / ".github/workflows/report-gate.yml").read_text())
    assert workflow["permissions"] == {"contents": "read", "issues": "read"}
    steps = workflow["jobs"]["sweep"]["steps"]
    assert steps[0]["with"]["persist-credentials"] is False
    check = next(s for s in steps if s.get("id") == "check")
    token = next(s for s in steps if s.get("id") == "app-token")
    assert check["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert steps.index(check) < steps.index(token)
    assert token["if"] == "steps.check.outputs.due != '0'"
    assert steps[-1]["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
