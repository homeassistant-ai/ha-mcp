"""Guard the security-sensitive wiring of Dependabot auto-merge."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "dependabot-auto-merge.yml"


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _normalize(value: str) -> str:
    return " ".join(value.split())


def test_metadata_fetches_security_alert_state_with_approval_token() -> None:
    job = _workflow()["jobs"]["dependabot"]
    steps = job["steps"]
    metadata = next(step for step in steps if step.get("id") == "metadata")

    assert _normalize(job["if"]) == _normalize(
        "github.actor == 'dependabot[bot]' && "
        "github.event.pull_request.user.login == 'dependabot[bot]' && "
        "github.event.pull_request.head.repo.full_name == github.repository"
    )
    assert metadata["with"]["alert-lookup"] is True
    assert metadata["with"]["github-token"] == (
        "${{ secrets.DEPENDABOT_APPROVAL_TOKEN }}"
    )


def test_approval_is_bound_to_the_current_head_and_dedicated_token() -> None:
    steps = _workflow()["jobs"]["dependabot"]["steps"]
    approval = next(step for step in steps if step["name"].startswith("Approve"))

    assert '-f commit_id="$HEAD_SHA"' in approval["run"]
    assert approval["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert approval["env"]["GH_TOKEN"] == ("${{ secrets.DEPENDABOT_APPROVAL_TOKEN }}")


def test_approval_and_auto_merge_share_security_aware_eligibility() -> None:
    steps = _workflow()["jobs"]["dependabot"]["steps"]
    gated = [step for step in steps if step["name"].startswith(("Approve", "Enable"))]

    assert len(gated) == 2
    expected = (
        "${{ steps.metadata.outputs.alert-state == 'OPEN' || "
        'contains(fromJson(\'["version-update:semver-minor",'
        '"version-update:semver-patch"]\'), '
        "steps.metadata.outputs.update-type) }}"
    )
    assert {_normalize(step["if"]) for step in gated} == {_normalize(expected)}
