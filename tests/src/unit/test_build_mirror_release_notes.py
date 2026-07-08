"""Unit tests for scripts/build_mirror_release_notes.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

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
        "0.15.0",
        "v7.9.0",
        "v7.10.0",
        ["feat: add foo tool (#100)", "fix: bar crash (#101)"],
    )
    assert "0.15.0" in notes
    assert "- feat: add foo tool (#100)" in notes
    assert "- fix: bar crash (#101)" in notes
    assert "Component changes since v7.9.0:" in notes
    assert CHANGELOG_URL in notes


def test_format_release_notes_version_appears_in_lead() -> None:
    notes = build.format_release_notes("0.15.0", "v7.9.0", "v7.10.0", [])
    assert notes.startswith("## ha-mcp-tools 0.15.0")


def test_format_release_notes_empty_subjects_is_honest_not_blank() -> None:
    notes = build.format_release_notes("0.15.0", "v7.9.0", "v7.10.0", [])
    assert not any(line.startswith("- ") for line in notes.splitlines())
    assert "no changes to" in notes
    assert "custom_components/ha_mcp_tools/" in notes
    assert CHANGELOG_URL in notes


def test_format_release_notes_prev_none_is_initial_release() -> None:
    notes = build.format_release_notes(
        "0.1.0", None, "v7.1.0", ["feat: first cut of the component"]
    )
    assert "Initial component release." in notes
    assert "- feat: first cut of the component" in notes
    # No prior tag to name -- must not print "since None".
    assert "since None" not in notes


def test_format_release_notes_filters_noise_subjects() -> None:
    notes = build.format_release_notes(
        "0.15.0",
        "v7.9.0",
        "v7.10.0",
        [
            "feat: real component change",
            "chore(addon): publish dev addon version 7.10.0.dev481 [skip ci]",
        ],
    )
    assert "- feat: real component change" in notes
    assert "publish dev addon version" not in notes


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


def test_select_stable_tags_raises_on_no_stable_tags() -> None:
    import pytest

    with pytest.raises(ValueError):
        build.select_stable_tags(["v7.1.0.dev1", "stable"])
