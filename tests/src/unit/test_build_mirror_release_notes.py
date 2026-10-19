"""Unit tests for scripts/build_mirror_release_notes.py."""

from __future__ import annotations

import importlib.util
import subprocess
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
    tmp_path: Path, component_changed: bool
) -> None:
    """Match the server-only release reproduced on the HAOS dev instance."""

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init")
    git("config", "user.name", "Release regression")
    git("config", "user.email", "test@example.invalid")
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
