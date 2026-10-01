"""Release notes show users only the changes that reach them.

Renders `templates/CHANGELOG.md.j2` with the real semantic-release against a
throwaway git repository, using this repository's own release configuration.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _git(repo: Path, env: dict[str, str], *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def _commit(repo: Path, env: dict[str, str], subject: str, *paths: str) -> None:
    for path in paths:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(subject)
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "--allow-empty", "-m", subject)


@pytest.fixture(scope="module")
def changelog(tmp_path_factory: pytest.TempPathFactory) -> str:
    """CHANGELOG.md rendered for v1.0.0 (mixed commits) and v1.0.1 (CI fix only)."""
    home = tmp_path_factory.mktemp("home")
    repo = tmp_path_factory.mktemp("repo")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    _git(repo, env, "init", "-b", "master")
    _git(repo, env, "remote", "add", "origin", "https://github.com/x/y.git")
    shutil.copy(_REPO_ROOT / "pyproject.toml", repo / "pyproject.toml")
    shutil.copytree(_REPO_ROOT / "templates", repo / "templates")
    _commit(repo, env, "chore: seed the repository")

    _commit(repo, env, "fix: wait for the test VM", "tests/src/haos_runtime.py")
    _commit(
        repo,
        env,
        "docs: rewrite the agent test rules",
        "src/ha_mcp/settings_ui/AGENTS.md",
        "docs/agents/testing.md",
    )
    _commit(repo, env, "build: switch the image base", "Dockerfile")
    _commit(
        repo,
        env,
        "fix: keep the alias on rename",
        "src/ha_mcp/alias.py",
        "tests/src/unit/test_alias.py",
    )
    _commit(repo, env, "perf: cache the registry", "src/ha_mcp/registry.py")
    _commit(repo, env, "docs: explain OAuth setup", "docs/OAUTH.md")
    _commit(repo, env, "chore(addon): publish dev addon version 1.0.0.dev1 [skip ci]")
    _git(repo, env, "tag", "-a", "v1.0.0", "-m", "v1.0.0")
    _commit(repo, env, "fix: retry the flaky CI job", ".github/workflows/pr.yml")
    _git(repo, env, "tag", "-a", "v1.0.1", "-m", "v1.0.1")

    subprocess.run(
        [sys.executable, "-m", "semantic_release", "changelog"],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    )
    return (repo / "CHANGELOG.md").read_text()


def _release(changelog: str, version: str) -> tuple[str, str]:
    """Split one release's notes into (shown to users, collapsed)."""
    body = changelog.split(f"## {version} ", 1)[1].split("\n## v", 1)[0]
    visible, _, internal = body.partition("<details>")
    return visible, internal


@pytest.fixture(scope="module")
def release_notes(changelog: str) -> dict[str, dict[str, str]]:
    """v1.0.0 notes as {"visible" | "internal": {heading: entries}}."""
    visible, internal = _release(changelog, "v1.0.0")
    return {"visible": _by_heading(visible), "internal": _by_heading(internal)}


def _by_heading(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    heading = ""
    for line in text.splitlines():
        if line.startswith("### "):
            heading = line[4:]
        else:
            sections[heading] = sections.get(heading, "") + line + "\n"
    return sections


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param("Wait for the test VM", id="fix-that-only-changes-tests"),
        pytest.param("Rewrite the agent test rules", id="docs-for-agents-only"),
        pytest.param("Switch the image base", id="build-commit"),
    ],
)
def test_change_users_never_receive_is_collapsed(
    release_notes: dict[str, dict[str, str]], entry: str
) -> None:
    """Test, tooling and agent-instruction changes must not crowd the notes users read."""
    assert entry not in "".join(release_notes["visible"].values())
    assert entry in "".join(release_notes["internal"].values())


@pytest.mark.parametrize(
    ("entry", "heading"),
    [
        pytest.param("Keep the alias on rename", "Fixed", id="fix-with-tests"),
        pytest.param("Cache the registry", "Changed", id="perf"),
        pytest.param("Explain OAuth setup", "Changed", id="user-docs"),
    ],
)
def test_change_to_shipped_files_is_shown_under_its_category(
    release_notes: dict[str, dict[str, str]], entry: str, heading: str
) -> None:
    """A change users receive must stay above the fold, in a Keep a Changelog section."""
    assert entry in release_notes["visible"].get(heading, "")


def test_dev_build_bookkeeping_is_left_out(
    release_notes: dict[str, dict[str, str]],
) -> None:
    """Per-merge dev version bumps must not bury the other internal entries."""
    rendered = "".join(
        section for part in release_notes.values() for section in part.values()
    )
    assert "Publish dev addon version" not in rendered


def test_release_with_only_internal_changes_says_so(changelog: str) -> None:
    """A release must not read as blank when every change in it is collapsed."""
    visible, internal = _release(changelog, "v1.0.1")
    assert "No user-facing changes." in visible
    assert "Retry the flaky CI job" in internal


def test_release_with_user_changes_carries_no_empty_notice(changelog: str) -> None:
    """The empty-release notice must not appear beside real entries."""
    visible, _ = _release(changelog, "v1.0.0")
    assert "No user-facing changes." not in visible
