"""Run slash-controller behavior and verify cross-job credential boundaries."""

import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_slash_agent_behavior():
    node = shutil.which("node")
    assert node, "Node is required for slash-agent regression tests"
    result = subprocess.run(
        [node, "--test", str(ROOT / "tests/js/slash-agent.test.mjs")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_slash_workflow_keeps_publication_and_auth_outside_generated_code():
    workflow = yaml.safe_load((ROOT / ".github/workflows/slash-agent.yml").read_text())
    assert all(value == "read" for value in workflow["permissions"].values())
    jobs = workflow["jobs"]
    assert "secrets." not in str(jobs["admit"])
    code = jobs["code"]
    assert code["concurrency"] == {
        "group": "codex-auth-${{ github.repository }}",
        "cancel-in-progress": False,
        "queue": "max",
    }
    agent = next(step for step in code["steps"] if step.get("id") == "codex")
    assert agent["with"]["read-only-paths"].split() == ["control", "source/.git"]
    assert agent["with"]["passthrough-env"] == "GH_TOKEN"
    assert agent["with"]["working-directory"] == "source"
    assert "HA_MCP_AGENT_APP_PRIVATE_KEY" not in str(code)
    cleanup = code["steps"][-1]
    assert "always()" in cleanup["if"]
    assert "codex-update-auth" in cleanup["uses"]
    assert (
        sum(step["timeout-minutes"] for step in code["steps"]) < code["timeout-minutes"]
    )
    publisher = jobs["publish"]
    publisher_text = str(publisher)
    assert "HA_MCP_AGENT_APP_ID" in publisher_text
    assert "HA_MCP_AGENT_APP_PRIVATE_KEY" in publisher_text
    assert "HA_MCP_APP_PRIVATE_KEY" not in publisher_text
    assert publisher["needs"] == ["admit", "code"]
    assert "cancelled()" in publisher["if"]
    checkout = publisher["steps"][0]
    assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkout["with"]["persist-credentials"] is False
    assert "source" not in str(checkout["with"])
    for job in jobs.values():
        assert all("timeout-minutes" in step for step in job["steps"])


def test_review_wakeup_has_no_credentials_checkout_or_generated_scripts():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/slash-agent-review-event.yml").read_text()
    )
    assert workflow["permissions"] == {}
    assert "secrets." not in str(workflow)
    steps = workflow["jobs"]["notify"]["steps"]
    assert len(steps) == 1
    assert (
        steps[0]["run"]
        == "echo 'Review activity is available for the trusted controller.'"
    )
