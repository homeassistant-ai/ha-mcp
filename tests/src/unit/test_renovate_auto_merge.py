"""Guard the credential boundary of Renovate's separate-account approval."""

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[3]


def test_renovate_approval_never_executes_pr_code() -> None:
    path = _ROOT / ".github/workflows/renovate-auto-merge.yml"
    assert path.is_file(), "Renovate needs its own trusted approval event handler"
    workflow = yaml.safe_load(path.read_text())
    assert workflow[True] == {
        "pull_request_target": {
            "types": ["auto_merge_enabled", "synchronize"],
            "branches": ["master"],
        }
    }
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["approve"]["steps"]
    assert len(steps) == 1
    assert steps[0]["uses"].startswith("actions/github-script@")
    assert steps[0]["with"]["github-token"] == ("${{ secrets.GH_TOKEN_CODEX_COMMENT }}")
    assert "run" not in steps[0]
