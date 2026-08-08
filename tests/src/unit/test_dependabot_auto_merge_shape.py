"""Guard the security-sensitive wiring of Dependabot auto-merge."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "dependabot-auto-merge.yml"


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def test_metadata_fetches_security_alert_state_with_approval_token() -> None:
    steps = _workflow()["jobs"]["dependabot"]["steps"]
    metadata = next(step for step in steps if step.get("id") == "metadata")

    assert metadata["with"]["alert-lookup"] is True
    assert metadata["with"]["github-token"] == (
        "${{ secrets.DEPENDABOT_APPROVAL_TOKEN }}"
    )


def test_approval_and_auto_merge_share_security_aware_eligibility() -> None:
    steps = _workflow()["jobs"]["dependabot"]["steps"]
    gated = [step for step in steps if step["name"].startswith(("Approve", "Enable"))]

    assert len(gated) == 2
    conditions = {step["if"] for step in gated}
    assert len(conditions) == 1
    condition = conditions.pop()
    assert "outputs.alert-state == 'OPEN'" in condition
    assert "security-update" not in condition
    assert "version-update:semver-minor" in condition
    assert "version-update:semver-patch" in condition
