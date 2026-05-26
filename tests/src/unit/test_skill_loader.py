"""Tests for the skill_loader utility.

The loader is the shared resolver behind both the explicit ha_get_skill_guide
tool and the write-tool include_skill parameter. Symlink + path-traversal
guards mirror server.py::_handle_skill_guide_call, but unlike that handler the
loader returns a partial dict instead of raising, so a missing reference can
never fail the write operation that's embedding the response.
"""

from __future__ import annotations

from pathlib import Path

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
