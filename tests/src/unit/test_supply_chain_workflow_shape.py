"""Guard supply-chain hardening that cannot be exercised by PR workflows."""

from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"


def _workflow(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(data: dict[str, Any]) -> set[str]:
    on_node = data.get(True) or data.get("on") or {}
    if isinstance(on_node, str):
        return {on_node}
    if isinstance(on_node, list):
        return set(on_node)
    return set(on_node)


def _assert_checkout_credentials_are_not_persisted(
    path: Path, repo_root: Path, visited: set[Path] | None = None
) -> int:
    path = path.resolve()
    visited = visited or set()
    if path in visited:
        return 0
    visited.add(path)

    data = _workflow(path)
    checked = 0
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            if "actions/checkout" not in str(step.get("uses", "")):
                continue
            checked += 1
            persist_credentials = (step.get("with") or {}).get("persist-credentials")
            assert persist_credentials is False or persist_credentials == "false", (
                f"{path.relative_to(repo_root)} persists checkout credentials in a "
                "pull_request workflow"
            )

        uses = job.get("uses")
        if isinstance(uses, str) and uses.startswith("./.github/workflows/"):
            called_path = repo_root / uses.removeprefix("./")
            checked += _assert_checkout_credentials_are_not_persisted(
                called_path, repo_root, visited
            )

    return checked


def test_pr_workflows_do_not_persist_checkout_credentials() -> None:
    checked_workflows = 0
    checked_checkouts = 0
    workflow_paths = sorted(
        path for path in _WORKFLOW_DIR.iterdir() if path.suffix in {".yml", ".yaml"}
    )
    for path in workflow_paths:
        data = _workflow(path)
        if "pull_request" not in _triggers(data):
            continue

        checked_workflows += 1
        checked_checkouts += _assert_checkout_credentials_are_not_persisted(
            path, _REPO_ROOT
        )

    assert checked_workflows, "trigger derivation matched no pull_request workflow"
    assert checked_checkouts, "pull_request workflows contained no checkout steps"


def test_pr_checkout_guard_follows_reusable_workflows(tmp_path: Path) -> None:
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    caller = workflow_dir / "caller.yml"
    caller.write_text(
        "jobs:\n  delegated:\n    uses: ./.github/workflows/reusable.yaml\n",
        encoding="utf-8",
    )
    reusable = workflow_dir / "reusable.yaml"
    reusable.write_text(
        "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@v4\n"
        '        with:\n          persist-credentials: "false"\n',
        encoding="utf-8",
    )
    assert _assert_checkout_credentials_are_not_persisted(caller, tmp_path) == 1

    reusable.write_text(
        "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@v4\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match=r"reusable\.yaml persists"):
        _assert_checkout_credentials_are_not_persisted(caller, tmp_path)


def test_dev_release_tag_cleanup_uses_authenticated_github_api() -> None:
    jobs = _workflow(_WORKFLOW_DIR / "publish-dev.yml")["jobs"]
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
