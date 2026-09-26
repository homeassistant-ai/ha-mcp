"""Run slash-controller behavior and verify cross-job credential boundaries."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_slash_agent_behavior() -> None:
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


def test_slash_workflow_keeps_publication_and_auth_outside_generated_code() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/slash-agent.yml").read_text())
    assert all(value == "read" for value in workflow["permissions"].values())
    assert "concurrency" not in workflow
    jobs = workflow["jobs"]
    admission = jobs["admit"]
    assert "homeassistant-ai/ha-mcp" in admission["if"]
    assert "HA_MCP_AGENT_APP_SLUG != ''" in admission["if"]
    assert "HA_MCP_AGENT_CLIENT_ID != ''" in admission["if"]
    assert "coderabbitai[bot]" in admission["if"]
    assert "chatgpt-codex-connector[bot]" in admission["if"]
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
    assert "HA_MCP_APP_PRIVATE_KEY" not in str(code)
    cleanup = code["steps"][-1]
    assert "always()" in cleanup["if"]
    assert "codex-update-auth" in cleanup["uses"]
    assert (
        sum(step["timeout-minutes"] for step in code["steps"]) < code["timeout-minutes"]
    )
    publisher = jobs["publish"]
    assert publisher["concurrency"] == {
        "group": "slash-agent-publish-${{ github.repository }}",
        "cancel-in-progress": False,
        "queue": "max",
    }
    publisher_text = str(publisher)
    assert "HA_MCP_AGENT_CLIENT_ID" in publisher_text
    assert "HA_MCP_AGENT_APP_ID" not in publisher_text
    assert "HA_MCP_AGENT_APP_PRIVATE_KEY" in publisher_text
    assert "HA_MCP_APP_PRIVATE_KEY" not in publisher_text
    assert publisher["needs"] == ["admit", "code"]
    assert "cancelled()" in publisher["if"]
    assert "needs.admit.outputs.run == 'true'" in publisher["if"]
    checkout = publisher["steps"][0]
    assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkout["with"]["persist-credentials"] is False
    assert "source" not in str(checkout["with"])
    for job in jobs.values():
        assert all("timeout-minutes" in step for step in job["steps"])


def test_wakeup_names_and_artifacts_match_the_controller_contract() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/slash-agent.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))["workflow_run"]["workflows"]
    source = (ROOT / ".github/slash-agent/github.mjs").read_text()
    allowed = source.split("const allowed = [", 1)[1].split("];", 1)[0]
    paths = re.findall(r'"(\.github/workflows/[^\"]+\.yml)"', allowed)
    assert paths
    for path in paths:
        assert yaml.safe_load((ROOT / path).read_text())["name"] in triggers

    jobs = workflow["jobs"]
    admit_upload = next(
        step
        for step in jobs["admit"]["steps"]
        if step.get("with", {}).get("name") == "slash-plan"
    )
    assert admit_upload["with"]["path"] == "slash-state/plan.json"
    code_download = next(
        step
        for step in jobs["code"]["steps"]
        if step.get("with", {}).get("name") == "slash-plan"
    )
    assert code_download["with"]["path"] == "control/slash-state"
    code_upload = next(
        step
        for step in jobs["code"]["steps"]
        if step.get("with", {}).get("name") == "slash-result"
    )
    assert code_upload["with"]["path"] == "control/slash-state/result.json"
    publish_downloads = [
        step
        for step in jobs["publish"]["steps"]
        if step.get("with", {}).get("name") in {"slash-plan", "slash-result"}
    ]
    assert len(publish_downloads) == 2
    assert all(step["with"]["path"] == "slash-state" for step in publish_downloads)
    assert (
        "SLASH_STATE_DIR: control/slash-state"
        in (ROOT / ".github/workflows/slash-agent.yml").read_text()
    )


def test_worker_failure_metadata_does_not_print_model_log(tmp_path: Path) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/slash-agent.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["code"]["steps"]
        if step.get("name") == "Report safe worker failure metadata"
    )
    assert "failure()" in step["if"]
    script = step["run"].split("node -e '", 1)[1].rsplit("'", 1)[0]
    log = tmp_path / "private.log"
    log.write_text(
        '{"type":"thread.started"}\nsecret-token-DO-NOT-PUBLISH\n'
        '{"type":"turn.failed","message":"secret-token-DO-NOT-PUBLISH"}\n'
    )
    node = shutil.which("node.exe") or shutil.which("node")
    assert node
    result = subprocess.run(
        [node, "-e", script],
        env={**os.environ, "CODEX_LOG_PATH": str(log)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert '"thread.started":1' in result.stdout
    assert '"turn.failed":1' in result.stdout
    assert "secret-token-DO-NOT-PUBLISH" not in result.stdout


def test_review_wakeup_has_no_credentials_checkout_or_generated_scripts() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/slash-agent-review-event.yml").read_text()
    )
    assert workflow["permissions"] == {}
    assert workflow["jobs"]["notify"]["name"] == "Slash review event"
    assert "secrets." not in str(workflow)
    steps = workflow["jobs"]["notify"]["steps"]
    assert len(steps) == 1
    assert (
        steps[0]["run"]
        == "echo 'Review activity is available for the trusted controller.'"
    )
