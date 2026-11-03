"""Unit tests for scripts/build_mirror_release_notes.py."""

from __future__ import annotations

import importlib.util
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "build_mirror_release_notes.py"
)
_spec = importlib.util.spec_from_file_location("build_mirror_release_notes", _SCRIPT)
assert _spec and _spec.loader
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

CHANGELOG_URL = "https://github.com/homeassistant-ai/ha-mcp/releases"


@pytest.fixture
def git(tmp_path: Path) -> Callable[..., str]:
    """Run git in a fresh repository under ``tmp_path``; return its output."""

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    run("init")
    run("config", "user.name", "Release regression")
    run("config", "user.email", "test@example.invalid")
    return run


def test_format_release_notes_lists_subjects_as_bullets() -> None:
    notes = build.format_release_notes(
        "v7.9.0",
        "v7.10.0",
        ["feat: add foo tool (#100)", "fix: bar crash (#101)"],
    )
    assert "- feat: add foo tool (#100)" in notes
    assert "- fix: bar crash (#101)" in notes
    assert "Component changes since v7.9.0:" in notes
    assert CHANGELOG_URL in notes


def test_format_release_notes_names_the_server_the_release_installs() -> None:
    # The component and its server share a version, so HACS users read which
    # server an update brings in the release they are offered.
    notes = build.format_release_notes("v7.9.0", "v7.10.0", [])
    assert notes.startswith("## HA-MCP Custom Component 7.10.0")
    assert "ha-mcp 7.10.0" in notes


def test_format_release_notes_empty_subjects_is_honest_not_blank() -> None:
    notes = build.format_release_notes("v7.9.0", "v7.10.0", [])
    assert not any(line.startswith("- ") for line in notes.splitlines())
    assert "No changes to" in notes
    assert "custom_components/ha_mcp_tools/" in notes
    assert CHANGELOG_URL in notes


def test_format_release_notes_prev_none_is_initial_release() -> None:
    notes = build.format_release_notes(
        None, "v7.1.0", ["feat: first cut of the component"]
    )
    assert "Initial component release." in notes
    assert "- feat: first cut of the component" in notes
    # No prior tag to name -- must not print "since None".
    assert "since None" not in notes


def test_format_release_notes_filters_noise_subjects() -> None:
    notes = build.format_release_notes(
        "v7.9.0",
        "v7.10.0",
        [
            "feat: real component change",
            "chore(addon): publish dev addon version 7.10.0.dev481 [skip ci]",
        ],
    )
    assert "- feat: real component change" in notes
    assert "publish dev addon version" not in notes


@pytest.mark.parametrize("component_changed", [False, True])
def test_release_stamp_does_not_count_as_a_component_change(
    tmp_path: Path, git: Callable[..., str], component_changed: bool
) -> None:
    """Match the server-only release reproduced on the HAOS dev instance."""
    component = tmp_path / "custom_components" / "ha_mcp_tools"
    component.mkdir(parents=True)
    version = component / "const.py"
    version.write_text('COMPONENT_VERSION = "9.0.0"\n', encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "feat: baseline")
    git("tag", "v9.0.0")
    target = component if component_changed else tmp_path
    (target / "behavior.py").write_text("FIXED = True\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "fix: behavior correction")
    version.write_text('COMPONENT_VERSION = "9.0.1"\n', encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "9.0.1")
    git("tag", "v9.0.1")
    git("commit", "--allow-empty", "-m", "fix: later queued release")
    git("tag", "v9.0.2")
    output = tmp_path / "notes.md"
    assert (
        build.main(
            ["--repo-dir", str(tmp_path), "--tag", "v9.0.1", "--out", str(output)]
        )
        == 0
    )
    notes = output.read_text(encoding="utf-8")
    assert notes.startswith("## HA-MCP Custom Component 9.0.1\n")
    assert "- 9.0.1" not in notes
    assert ("- fix: behavior correction" in notes) is component_changed
    assert ("No changes to" in notes) is not component_changed


def test_select_stable_tags_filters_dev_and_stable_pointer() -> None:
    tags = ["v7.9.0", "v7.9.0.dev759", "v7.10.0.dev772", "v7.10.0", "stable"]
    prev, curr = build.select_stable_tags(tags)
    assert prev == "v7.9.0"
    assert curr == "v7.10.0"


def test_select_stable_tags_sorts_numerically_not_lexically() -> None:
    # Lexical sort would put "v7.10.0" before "v7.9.0".
    tags = ["v7.10.0", "v7.9.0", "v7.2.0"]
    prev, curr = build.select_stable_tags(tags)
    assert prev == "v7.9.0"
    assert curr == "v7.10.0"


def test_select_stable_tags_single_tag_returns_none_prev() -> None:
    prev, curr = build.select_stable_tags(["v7.1.0"])
    assert prev is None
    assert curr == "v7.1.0"


def test_selected_release_ignores_newer_tags() -> None:
    # A queued mirror run can still be publishing 9.0.1 after 9.0.2 is tagged.
    assert build.select_stable_tags(
        ["v9.0.0", "v9.0.1", "v9.0.2"], current_tag="v9.0.1"
    ) == ("v9.0.0", "v9.0.1")
    assert build.select_stable_tags(["v9.0.0", "v9.0.1"], current_tag="v9.0.0") == (
        None,
        "v9.0.0",
    )


@pytest.mark.parametrize("tag", ["v9.0.3", "v9.0.1.dev1"])
def test_selected_release_must_be_an_existing_stable_tag(tag: str) -> None:
    with pytest.raises(ValueError, match="stable tag"):
        build.select_stable_tags(["v9.0.0", "v9.0.1"], current_tag=tag)


def test_select_stable_tags_raises_on_no_stable_tags() -> None:
    import pytest

    with pytest.raises(ValueError):
        build.select_stable_tags(["v7.1.0.dev1", "stable"])


def test_previous_dev_tag_is_the_newest_older_build() -> None:
    tags = ["v9.0.0.dev3096", "v8.7.0.dev3080", "v9.0.0.dev3100", "v9.0.0", "stable"]
    assert build.previous_dev_tag(tags, "9.0.0.dev3100") == "v9.0.0.dev3096"


def test_previous_dev_tag_compares_build_numbers_numerically() -> None:
    tags = ["v9.0.0.dev999", "v9.0.0.dev1000"]
    assert build.previous_dev_tag(tags, "9.0.0.dev1001") == "v9.0.0.dev1000"


def test_the_first_dev_build_has_no_previous_one() -> None:
    assert build.previous_dev_tag(["v9.0.0"], "9.0.0.dev1") is None


def test_a_pin_that_is_not_a_dev_build_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    output = tmp_path / "notes.md"
    argv = ["--repo-dir", str(tmp_path), "--dev-pin", "9.0.0", "--out", str(output)]
    assert build.main(argv) == 1
    assert "not a dev build version: 9.0.0" in capsys.readouterr().err
    assert not output.exists()


def test_dev_notes_list_the_commits_since_the_previous_dev_build(
    tmp_path: Path, git: Callable[..., str]
) -> None:
    """Dev pre-release notes must list only the pull requests merged after the
    previous dev build up to the snapshotted commit, and name the build they
    install. It passes the options publish-dev passes: the triggering commit's
    SHA as the source, before the new build has a tag. Like publish-dev, it puts
    the previous build's tag on a stamp commit that only the tag references."""
    git("commit", "--allow-empty", "-m", "feat: before the previous build (#1)")
    git("commit", "--allow-empty", "-m", "chore: stamp the previous build")
    git("tag", "v9.0.0.dev1")
    git("reset", "--hard", "HEAD~1")
    git("commit", "--allow-empty", "-m", "fix: in the snapshot (#2)")
    snapshot = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "feat: after the snapshot (#3)")
    output = tmp_path / "notes.md"
    argv = [
        "--repo-dir",
        str(tmp_path),
        "--dev-pin",
        "9.0.0.dev2",
        "--dist",
        "ha-mcp-test",
        "--source",
        snapshot,
        "--source-label",
        "owner/repo@abc1234",
        "--out",
        str(output),
    ]
    assert build.main(argv) == 0
    notes = output.read_text(encoding="utf-8")
    assert notes.startswith(
        "## HA-MCP Custom Component 9.0.0 (development pre-release)\n"
    )
    assert "from owner/repo@abc1234," in notes
    assert "Installs ha-mcp-test==9.0.0.dev2." in notes
    assert "- fix: in the snapshot (#2)" in notes
    assert "before the previous build" not in notes
    assert "after the snapshot" not in notes


def test_dev_notes_list_each_merged_pull_request() -> None:
    notes = build.format_dev_release_notes(
        "9.0.0.dev3100",
        "ha-mcp-dev",
        "homeassistant-ai/ha-mcp@37fb92e0a",
        [
            "deps(deps): bump cyclopts from 5.1.1 to 5.2.0 (#2779)",
            "chore(internal): sync ratchet baselines after merge [skip ci]",
            "feat(component): something (#2742)",
        ],
    )
    assert "## HA-MCP Custom Component 9.0.0 (development pre-release)" in notes
    assert "Installs ha-mcp-dev==9.0.0.dev3100." in notes
    assert "- deps(deps): bump cyclopts from 5.1.1 to 5.2.0 (#2779)" in notes
    assert "- feat(component): something (#2742)" in notes
    assert "[skip ci]" not in notes


def test_dev_notes_without_changes_say_so() -> None:
    notes = build.format_dev_release_notes("9.0.0.dev1", "ha-mcp-dev", "x@y", [])
    assert "No changes since the previous pre-release." in notes
