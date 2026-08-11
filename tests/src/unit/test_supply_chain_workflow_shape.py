"""Guard supply-chain hardening that cannot be exercised by PR workflows."""

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((_WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def _triggers(data: dict[str, Any]) -> set[str]:
    on_node = data.get(True) or data.get("on") or {}
    if isinstance(on_node, str):
        return {on_node}
    if isinstance(on_node, list):
        return set(on_node)
    return set(on_node)


def test_pr_workflows_do_not_persist_checkout_credentials() -> None:
    checked = 0
    workflow_paths = sorted(
        path for path in _WORKFLOW_DIR.iterdir() if path.suffix in {".yml", ".yaml"}
    )
    for path in workflow_paths:
        data = _workflow(path.name)
        if "pull_request" not in _triggers(data):
            continue

        jobs = data["jobs"].values()
        checkouts = [
            step
            for job in jobs
            for step in job.get("steps", [])
            if "actions/checkout" in str(step.get("uses", ""))
        ]
        if not checkouts:
            continue

        checked += 1
        assert all(
            (step.get("with") or {}).get("persist-credentials") is False
            for step in checkouts
        ), f"{path.name} persists checkout credentials in a pull_request workflow"

    assert checked, "trigger derivation matched no pull_request workflow with checkout"


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
