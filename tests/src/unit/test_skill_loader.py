"""Tests for the skill_loader utility.

The loader is the shared resolver behind both the explicit ha_get_skill_guide
tool and the write-tool MandatoryBPS parameter. Symlink + path-traversal
guards mirror server.py::_handle_skill_guide_call, but unlike that handler the
loader returns a partial dict instead of raising, so a missing reference can
never fail the write operation that's embedding the response.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ha_mcp.utils import skill_loader


@pytest.fixture
def fake_skills_dir(tmp_path: Path) -> Path:
    """Build a fake skills directory with one skill and two reference files."""
    skill = tmp_path / "home-assistant-best-practices"
    refs = skill / "references"
    refs.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# top level\n")
    (refs / "automation-patterns.md").write_text("# patterns\nuse native\n")
    (refs / "template-guidelines.md").write_text("# templates\navoid them\n")
    return tmp_path


def test_skills_dir_at_returns_none_when_missing(tmp_path: Path) -> None:
    """_skills_dir_at returns None when the directory doesn't exist."""
    assert skill_loader._skills_dir_at(tmp_path / "does-not-exist") is None


def test_skills_dir_at_returns_path_when_present(fake_skills_dir: Path) -> None:
    """_skills_dir_at returns the directory when it exists."""
    assert skill_loader._skills_dir_at(fake_skills_dir) == fake_skills_dir


def test_resolve_skill_files_happy_path(fake_skills_dir: Path) -> None:
    """resolve_skill_files returns {path: body} for each requested file."""
    result = skill_loader.resolve_skill_files(
        fake_skills_dir,
        "home-assistant-best-practices",
        ["references/automation-patterns.md", "references/template-guidelines.md"],
    )
    assert set(result.keys()) == {
        "references/automation-patterns.md",
        "references/template-guidelines.md",
    }
    assert "use native" in result["references/automation-patterns.md"]
    assert "avoid them" in result["references/template-guidelines.md"]


def test_resolve_skill_files_top_level(fake_skills_dir: Path) -> None:
    """Top-level SKILL.md resolves the same way as references/*.md."""
    result = skill_loader.resolve_skill_files(
        fake_skills_dir,
        "home-assistant-best-practices",
        ["SKILL.md"],
    )
    assert result == {"SKILL.md": "# top level\n"}


def test_resolve_skill_files_skips_missing(fake_skills_dir: Path) -> None:
    """Unknown files are silently skipped; partial map is returned."""
    result = skill_loader.resolve_skill_files(
        fake_skills_dir,
        "home-assistant-best-practices",
        ["references/automation-patterns.md", "references/does-not-exist.md"],
    )
    assert "references/automation-patterns.md" in result
    assert "references/does-not-exist.md" not in result


def test_resolve_skill_files_rejects_symlink(
    tmp_path: Path, fake_skills_dir: Path
) -> None:
    """Symlinks inside the skill dir are refused (matches _handle_skill_guide_call)."""
    skill_dir = fake_skills_dir / "home-assistant-best-practices"
    target = tmp_path / "outside.md"
    target.write_text("escaped\n")
    link = skill_dir / "references" / "evil.md"
    link.symlink_to(target)

    result = skill_loader.resolve_skill_files(
        fake_skills_dir,
        "home-assistant-best-practices",
        ["references/evil.md"],
    )
    assert result == {}  # symlink silently skipped


def test_resolve_skill_files_rejects_traversal(fake_skills_dir: Path) -> None:
    """Path-traversal attempts are refused."""
    result = skill_loader.resolve_skill_files(
        fake_skills_dir,
        "home-assistant-best-practices",
        ["../../../etc/passwd", "references/../../../etc/passwd"],
    )
    assert result == {}


def test_resolve_skill_files_rejects_absolute_path(fake_skills_dir: Path) -> None:
    """An absolute path passed directly is refused.

    ``skill_dir / "/etc/passwd"`` collapses to ``/etc/passwd`` under
    pathlib (absolute RHS wins), which then fails the
    ``is_relative_to(skill_root)`` guard. Guards against a future
    refactor to a naive string-prefix check that would let an absolute
    path slip through. Input is hardcoded today, so this is
    defense-in-depth — but cheap to pin."""
    result = skill_loader.resolve_skill_files(
        fake_skills_dir,
        "home-assistant-best-practices",
        ["/etc/passwd"],
    )
    assert result == {}


def test_resolve_skill_files_rejects_unknown_skill(fake_skills_dir: Path) -> None:
    """A skill name with no corresponding directory returns empty."""
    result = skill_loader.resolve_skill_files(
        fake_skills_dir,
        "nonexistent-skill",
        ["SKILL.md"],
    )
    assert result == {}


def test_resolve_skill_files_empty_list(fake_skills_dir: Path) -> None:
    """Empty file list returns empty dict — no I/O."""
    result = skill_loader.resolve_skill_files(
        fake_skills_dir,
        "home-assistant-best-practices",
        [],
    )
    assert result == {}


def test_resolve_skill_files_skills_dir_none() -> None:
    """skills_dir=None (vendor missing) returns empty without raising."""
    result = skill_loader.resolve_skill_files(
        None,
        "home-assistant-best-practices",
        ["SKILL.md"],
    )
    assert result == {}


def test_resolve_skill_files_skips_empty_path(fake_skills_dir: Path) -> None:
    """Empty-string path is skipped (not treated as the skill dir itself)."""
    result = skill_loader.resolve_skill_files(
        fake_skills_dir,
        "home-assistant-best-practices",
        ["", "SKILL.md"],
    )
    assert result == {"SKILL.md": "# top level\n"}


def test_resolve_skill_files_anchored_returns_section(
    fake_skills_dir: Path,
) -> None:
    """Path#anchor refs return only the matching markdown section."""
    skill = fake_skills_dir / "home-assistant-best-practices"
    (skill / "references" / "automation-patterns.md").write_text(
        "# Automation Patterns\n"
        "\n"
        "## Native Conditions\n"
        "\n"
        "Use these.\n"
        "\n"
        "## Trigger Types\n"
        "\n"
        "Use these too.\n"
    )
    result = skill_loader.resolve_skill_files(
        fake_skills_dir,
        "home-assistant-best-practices",
        ["references/automation-patterns.md#native-conditions"],
    )
    body = result["references/automation-patterns.md#native-conditions"]
    assert body.startswith("## Native Conditions")
    assert "Use these." in body
    assert "Trigger Types" not in body


def test_resolve_skill_files_unknown_anchor_silently_skipped(
    fake_skills_dir: Path,
) -> None:
    """An anchor that matches no heading drops out of the result map."""
    skill = fake_skills_dir / "home-assistant-best-practices"
    (skill / "references" / "automation-patterns.md").write_text(
        "# Automation Patterns\n\n## Native Conditions\n\nbody\n"
    )
    result = skill_loader.resolve_skill_files(
        fake_skills_dir,
        "home-assistant-best-practices",
        ["references/automation-patterns.md#does-not-exist"],
    )
    assert result == {}


def test_resolve_skill_files_reads_file_once_for_multiple_sections(
    fake_skills_dir: Path,
) -> None:
    """Multiple anchored refs from the same file share one read."""
    skill = fake_skills_dir / "home-assistant-best-practices"
    (skill / "references" / "automation-patterns.md").write_text(
        "# Patterns\n\n## Native Conditions\n\nA\n\n## Wait Actions\n\nB\n"
    )
    result = skill_loader.resolve_skill_files(
        fake_skills_dir,
        "home-assistant-best-practices",
        [
            "references/automation-patterns.md#native-conditions",
            "references/automation-patterns.md#wait-actions",
        ],
    )
    assert set(result.keys()) == {
        "references/automation-patterns.md#native-conditions",
        "references/automation-patterns.md#wait-actions",
    }


def test_extract_section_handles_fenced_code_blocks() -> None:
    """A # comment inside a ```yaml ... ``` block is NOT a markdown heading.

    Regression: bare ``# Single state`` inside a yaml example would
    otherwise close the surrounding section after its first code block.
    """
    body = (
        "## Native Conditions\n"
        "\n"
        "Use these.\n"
        "\n"
        "```yaml\n"
        "# Single state\n"
        "condition: state\n"
        "```\n"
        "\n"
        "More prose after the code block.\n"
        "\n"
        "## Trigger Types\n"
        "\n"
        "Different section.\n"
    )
    result = skill_loader.extract_section(body, "native-conditions")
    assert result is not None
    assert "More prose after the code block" in result
    assert "Trigger Types" not in result


def test_extract_section_handles_ifthen_style_anchor() -> None:
    """Slashes in headings get stripped: '## if/then vs choose' → 'ifthen-vs-choose'."""
    body = "## if/then vs choose\n\nbody\n\n## Other\n"
    result = skill_loader.extract_section(body, "ifthen-vs-choose")
    assert result is not None
    assert result.startswith("## if/then vs choose")
    assert "Other" not in result


def test_extract_section_returns_none_when_anchor_unknown() -> None:
    body = "## A\nbody\n## B\nbody\n"
    assert skill_loader.extract_section(body, "c") is None


def test_extract_section_empty_anchor_returns_whole_body() -> None:
    """Convenience: empty anchor means caller wants the full file."""
    body = "# Some doc\n\nfull body\n"
    assert skill_loader.extract_section(body, "") == body


def test_extract_section_tolerates_trailing_whitespace_in_anchor() -> None:
    """Anchor is slugified before comparison — trailing whitespace doesn't miss."""
    body = "## Native Conditions\n\nbody\n## Next\n"
    assert skill_loader.extract_section(body, "native-conditions ") is not None
    assert skill_loader.extract_section(body, " native-conditions") is not None


def test_extract_section_tolerates_mixed_case_anchor() -> None:
    """Anchor case doesn't matter — slugifier lowercases both sides."""
    body = "## Native Conditions\n\nbody\n"
    assert skill_loader.extract_section(body, "Native-Conditions") is not None


def test_extract_section_tolerates_double_hash_typo() -> None:
    """A leading extra ``#`` on the anchor doesn't silently miss."""
    body = "## Native Conditions\n\nbody\n"
    # _slugify strips leading punctuation, so "#native-conditions" → "native-conditions"
    assert skill_loader.extract_section(body, "#native-conditions") is not None


def test_extract_section_section_runs_to_eof() -> None:
    """Match on the last heading in the file → section runs to EOF."""
    body = "## First\n\nintro\n\n## Last\n\nfinal section body\n"
    result = skill_loader.extract_section(body, "last")
    assert result is not None
    assert result.startswith("## Last")
    assert "final section body" in result


def test_extract_section_first_match_wins_on_collision() -> None:
    """If two headings slugify to the same anchor, the FIRST wins.

    Deterministic behaviour pinned by this test — a future refactor
    that tries to scan-all-matches would have to update it deliberately.
    """
    body = "## Same\n\nfirst body\n\n## Same\n\nsecond body\n"
    result = skill_loader.extract_section(body, "same")
    assert result is not None
    assert "first body" in result
    assert "second body" not in result


def test_get_skills_dir_returns_bundled_path_or_none() -> None:
    """get_skills_dir returns the real bundled path when the submodule is present.

    Skipped silently if the vendor submodule isn't checked out in this clone —
    we don't want to fail the test on a fresh clone that just hasn't run
    git submodule update --init yet.
    """
    result = skill_loader.get_skills_dir()
    if result is None:
        pytest.skip("skills-vendor submodule not initialised in this checkout")
    assert result.is_dir()
    assert (result / "home-assistant-best-practices" / "SKILL.md").is_file()


def test_resolve_skill_files_oserror_during_resolve_returns_skipped(
    fake_skills_dir: Path,
) -> None:
    """``Path.resolve()`` can raise OSError on the host (e.g. ELOOP from
    a circular symlink, EACCES from permission-restricted parents) inside
    ``_read_file_safely``. The silent-skip contract requires the loader
    to swallow it and return the ref omitted — never propagate, because
    the surrounding write operation has already committed.

    Scope the patch to only the ``.md`` candidate resolution inside
    ``_read_file_safely`` — a global ``Path.resolve`` patch would also
    fire on the ``skill_dir.resolve()`` call earlier in
    ``resolve_skill_files`` (line ~178), which is not the path we're
    trying to exercise."""
    real_resolve = Path.resolve

    def selective_resolve(self: Path, *args, **kwargs):
        if self.suffix == ".md":
            raise OSError("ELOOP")
        return real_resolve(self, *args, **kwargs)

    with patch.object(Path, "resolve", selective_resolve):
        result = skill_loader.resolve_skill_files(
            fake_skills_dir,
            "home-assistant-best-practices",
            ["references/automation-patterns.md"],
        )
    assert result == {}


def test_resolve_skill_files_invalid_utf8_returns_skipped(
    fake_skills_dir: Path,
) -> None:
    """A skill file with invalid UTF-8 bytes raises ``UnicodeDecodeError``
    on ``read_text(encoding='utf-8')``. ``UnicodeDecodeError`` subclasses
    ``ValueError``, NOT ``OSError`` — without the dedicated except clause
    it would propagate and fail the surrounding write the agent already
    committed."""
    bad = fake_skills_dir / "home-assistant-best-practices" / "references" / "bad.md"
    bad.write_bytes(b"\xff\xfe# not valid utf-8\n")
    result = skill_loader.resolve_skill_files(
        fake_skills_dir,
        "home-assistant-best-practices",
        ["references/bad.md"],
    )
    assert result == {}
