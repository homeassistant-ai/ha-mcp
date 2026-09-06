"""Exercise report collection and the cleanup/platform contracts without OAuth."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


def load(relative):
    return yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))


def bash():
    if os.name == "nt":
        executable = (
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
            / "Git/bin/bash.exe"
        )
        assert executable.is_file(), (
            "Git Bash is required; never use the Windows WSL launcher"
        )
        return str(executable)
    executable = shutil.which("bash")
    assert executable
    return executable


def shell(script, directory, **env):
    if os.name == "nt":
        # Native jq otherwise adds CRLF to cursors read by the Ubuntu script.
        script = 'jq() { command jq --binary "$@"; }\n' + script
    return subprocess.run(
        [bash(), "--noprofile", "--norc", "-c", script],
        cwd=directory,
        env={**os.environ, **env},
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("name", ["test", "codex-review-issues", "codex-review-prs"])
def test_every_step_is_bounded_with_cleanup_outside_the_agent_budget(name):
    workflow = load(f".github/workflows/{name}.yml")
    job = next(iter(workflow["jobs"].values()))
    steps = job["steps"]
    agent = next(s for s in steps if s.get("id") == "codex")
    cleanup = steps[-1]
    assert "codex-update-auth" in cleanup["uses"]
    assert "always()" in cleanup["if"]
    assert cleanup["timeout-minutes"] >= 3
    assert int(agent["with"]["timeout-minutes"]) < agent["timeout-minutes"]
    # Even when every preceding step reaches its cap, cleanup still fits.
    assert sum(s["timeout-minutes"] for s in steps) < job["timeout-minutes"]


def test_unsupported_platform_is_rejected_before_reading_credentials(tmp_path):
    action = load(".github/actions/codex-run/action.yml")
    first = action["runs"]["steps"][0]
    assert "codex-auth" not in str(first.get("env", {}))
    result = shell(first["run"], tmp_path, RUNNER_OS="macOS")
    assert result.returncode != 0
    assert b"Ubuntu" in result.stdout + result.stderr


def collection():
    job = next(iter(load(".github/workflows/codex-review-prs.yml")["jobs"].values()))
    return next(
        s["run"]
        for s in job["steps"]
        if s["name"] == "Collect authorized pull-request context"
    )


def test_byte_limit_keeps_utf8_valid_and_preserves_complete_lines(tmp_path):
    path = tmp_path / ".codex-context/pull-requests"
    path.mkdir(parents=True)
    data = b"header\n" + b"a" * 49992 + "é\nlast line\n".encode()
    (path / "pr-1.patch").write_bytes(data)
    command = next(
        line.strip()
        for line in collection().splitlines()
        if '.patch"' in line and ("head -c" in line or "awk " in line)
    )
    result = shell(command, tmp_path, number="1")
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert len(result.stdout) <= 50000
    assert result.stdout.decode("utf-8") == "header\n"


def test_collects_late_replies_after_the_first_hundred(tmp_path):
    first_comments = [{"body": f"earlier reply {i}"} for i in range(100)]
    connection = {
        "nodes": first_comments,
        "pageInfo": {"hasNextPage": True, "endCursor": "comment-100"},
    }
    first_page = [
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [{"id": "thread-1", "comments": connection}],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": "thread-end",
                            },
                        }
                    }
                }
            }
        }
    ]
    later_pages = [
        {
            "data": {
                "node": {
                    "comments": {
                        "nodes": [{"body": "late maintainer reply"}],
                        "pageInfo": {"hasNextPage": True, "endCursor": "comment-200"},
                    }
                }
            }
        },
        {
            "data": {
                "node": {
                    "comments": {
                        "nodes": [{"body": "fix verified on final page"}],
                        "pageInfo": {"hasNextPage": False, "endCursor": "comment-end"},
                    }
                }
            }
        },
    ]
    (tmp_path / "first.json").write_text(json.dumps(first_page), encoding="utf-8")
    (tmp_path / "later.json").write_text(json.dumps(later_pages), encoding="utf-8")
    fake_gh = r"""
    gh() {
      case "$1 $2" in
        'pr list') echo 1 ;;
        'pr view') echo '{"number":1}' ;;
        'pr diff') printf 'sample patch\n' ;;
        'api graphql')
          if [[ "$*" == *'node(id:'* ]]; then
            [[ "$*" == *'endCursor=comment-100'* ]] || return 1
            [[ "$*" == *'--paginate --slurp'* ]] || return 1
            cat later.json
          else
            cat first.json
          fi ;;
        *) return 1 ;;
      esac
    }
    """
    result = shell(
        fake_gh + collection(), tmp_path, ITEM_LIMIT="1", GITHUB_REPOSITORY="owner/repo"
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    context = json.loads(
        (tmp_path / ".codex-context/pull-requests/pr-1-threads.json").read_text()
    )
    comments = context[0]["comments"]
    assert len(comments["nodes"]) == 102
    assert comments["nodes"][-1]["body"] == "fix verified on final page"
    assert comments["pageInfo"]["hasNextPage"] is False
    assert (
        "fix verified on final page"
        in (tmp_path / ".codex-context/pr-review-prompt.txt").read_text()
    )
