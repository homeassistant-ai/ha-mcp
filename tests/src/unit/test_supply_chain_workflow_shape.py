"""Guard supply-chain hardening that cannot be exercised by PR workflows."""

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((_WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def test_pr_workflows_do_not_persist_checkout_credentials() -> None:
    for name in (
        "haos-e2e-tests.yml",
        "build-binary.yml",
        "performance-tests.yml",
    ):
        jobs = _workflow(name)["jobs"].values()
        checkouts = [
            step
            for job in jobs
            for step in job.get("steps", [])
            if "actions/checkout" in str(step.get("uses", ""))
        ]
        assert checkouts, f"{name} must contain a checkout step"
        assert all(
            (step.get("with") or {}).get("persist-credentials") is False
            for step in checkouts
        ), f"{name} persists checkout credentials in a pull_request workflow"


def test_dev_release_tag_cleanup_uses_authenticated_github_api() -> None:
    jobs = _workflow("publish-dev.yml")["jobs"]
    create_run = next(
        step["run"]
        for step in jobs["create-prerelease"]["steps"]
        if step.get("name") == "Create pre-release"
    )
    cleanup_run = next(
        step["run"]
        for step in jobs["cleanup-old-prereleases"]["steps"]
        if step.get("name") == "Delete old dev releases (keep last 5)"
    )

    assert (
        'gh api -X DELETE "repos/${GITHUB_REPOSITORY}/git/refs/tags/$TAG"' in create_run
    )
    assert (
        'gh api -X DELETE "repos/${GITHUB_REPOSITORY}/git/refs/tags/$tag"'
        in cleanup_run
    )
    assert "git push origin --delete" not in create_run + cleanup_run
