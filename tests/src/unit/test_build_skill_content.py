"""Tests for util_helpers.build_skill_content (issue #1182).

The helper is the shared assembly point for the include_skill parameter on
every write tool (set_automation / _script / _scene / _helper / _dashboard /
_yaml). Behaviour exercised here applies uniformly to all six call sites.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ha_mcp.tools.util_helpers import build_skill_content


@pytest.fixture
def fake_skills_dir(tmp_path: Path) -> Path:
    """Build a fake home-assistant-best-practices skill with two reference files."""
    skill = tmp_path / "home-assistant-best-practices"
    refs = skill / "references"
    refs.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# best practices\n")
    (refs / "automation-patterns.md").write_text("# patterns\n")
    (refs / "template-guidelines.md").write_text("# templates\n")
    (refs / "helper-selection.md").write_text("# helpers\n")
    return tmp_path


@pytest.fixture
def patched_get_skills_dir(fake_skills_dir: Path):
    """Patch get_skills_dir so build_skill_content sees the fake skills root."""
    with patch(
        "ha_mcp.utils.skill_loader.get_skills_dir", return_value=fake_skills_dir
    ):
        yield


# ---------------------------------------------------------------------------
# include_skill=True path: canonical files attached
# ---------------------------------------------------------------------------


class TestIncludeSkillCanonical:
    def test_canonical_files_attached_when_on(self, patched_get_skills_dir):
        result = build_skill_content(
            include_skill=True,
            canonical_files=("references/automation-patterns.md",),
            referenced_files=None,
        )
        assert result == {"references/automation-patterns.md": "# patterns\n"}

    def test_multiple_canonical_files(self, patched_get_skills_dir):
        result = build_skill_content(
            include_skill=True,
            canonical_files=(
                "references/automation-patterns.md",
                "references/template-guidelines.md",
            ),
            referenced_files=None,
        )
        assert set(result.keys()) == {
            "references/automation-patterns.md",
            "references/template-guidelines.md",
        }

    def test_top_level_skill_md(self, patched_get_skills_dir):
        """Scenes use top-level SKILL.md as their canonical doc."""
        result = build_skill_content(
            include_skill=True,
            canonical_files=("SKILL.md",),
            referenced_files=None,
        )
        assert result == {"SKILL.md": "# best practices\n"}


# ---------------------------------------------------------------------------
# include_skill=False path: canonical files suppressed
# ---------------------------------------------------------------------------


class TestIncludeSkillOff:
    def test_canonical_suppressed_when_off(self, patched_get_skills_dir):
        result = build_skill_content(
            include_skill=False,
            canonical_files=("references/automation-patterns.md",),
            referenced_files=None,
        )
        assert result == {}

    def test_referenced_files_still_attach_when_off(self, patched_get_skills_dir):
        """include_skill=False suppresses canonical defaults but BP-warning
        referenced files still ride along — the LLM needs them to fix the
        input it just submitted."""
        result = build_skill_content(
            include_skill=False,
            canonical_files=("references/automation-patterns.md",),
            referenced_files={"references/template-guidelines.md"},
        )
        assert result == {"references/template-guidelines.md": "# templates\n"}


# ---------------------------------------------------------------------------
# Dedup: canonical ∪ referenced
# ---------------------------------------------------------------------------


class TestDedup:
    def test_canonical_and_referenced_union(self, patched_get_skills_dir):
        result = build_skill_content(
            include_skill=True,
            canonical_files=("references/automation-patterns.md",),
            referenced_files={"references/template-guidelines.md"},
        )
        assert set(result.keys()) == {
            "references/automation-patterns.md",
            "references/template-guidelines.md",
        }

    def test_overlap_collapses_to_one(self, patched_get_skills_dir):
        """Same file in both canonical and referenced is read once."""
        result = build_skill_content(
            include_skill=True,
            canonical_files=("references/automation-patterns.md",),
            referenced_files={"references/automation-patterns.md"},
        )
        assert list(result.keys()) == ["references/automation-patterns.md"]


# ---------------------------------------------------------------------------
# Degraded paths
# ---------------------------------------------------------------------------


class TestDegradedPaths:
    def test_skills_vendor_missing(self):
        """When the submodule isn't checked out, build_skill_content is a no-op."""
        with patch("ha_mcp.utils.skill_loader.get_skills_dir", return_value=None):
            result = build_skill_content(
                include_skill=True,
                canonical_files=("references/automation-patterns.md",),
                referenced_files=None,
            )
            assert result == {}

    def test_nothing_requested(self, patched_get_skills_dir):
        """include_skill=False with no referenced_files returns empty without I/O."""
        result = build_skill_content(
            include_skill=False,
            canonical_files=("references/automation-patterns.md",),
            referenced_files=None,
        )
        assert result == {}

    def test_missing_canonical_file_silently_skipped(self, patched_get_skills_dir):
        """An unknown canonical file doesn't fail the response — the others
        that resolve still come back."""
        result = build_skill_content(
            include_skill=True,
            canonical_files=(
                "references/automation-patterns.md",
                "references/does-not-exist.md",
            ),
            referenced_files=None,
        )
        assert "references/automation-patterns.md" in result
        assert "references/does-not-exist.md" not in result


# ---------------------------------------------------------------------------
# Per-tool canonical mappings exist and point to files that should exist
# ---------------------------------------------------------------------------


class TestPerToolCanonicalMappings:
    """Each write tool exports its canonical mapping constant. These tests
    pin the contract — changing a mapping is a deliberate edit, not an
    accident."""

    def test_automation_mapping(self):
        from ha_mcp.tools.tools_config_automations import _AUTOMATION_SKILL_FILES

        assert _AUTOMATION_SKILL_FILES == (
            "references/automation-patterns.md",
            "references/template-guidelines.md",
        )

    def test_script_mapping(self):
        from ha_mcp.tools.tools_config_scripts import _SCRIPT_SKILL_FILES

        assert _SCRIPT_SKILL_FILES == (
            "references/automation-patterns.md",
            "references/template-guidelines.md",
        )

    def test_scene_mapping(self):
        from ha_mcp.tools.tools_config_scenes import _SCENE_SKILL_FILES

        assert _SCENE_SKILL_FILES == ("SKILL.md",)

    def test_helper_mapping(self):
        from ha_mcp.tools.tools_config_helpers import _HELPER_SKILL_FILES

        assert _HELPER_SKILL_FILES == ("references/helper-selection.md",)

    def test_dashboard_mapping(self):
        from ha_mcp.tools.tools_config_dashboards import _DASHBOARD_SKILL_FILES

        assert _DASHBOARD_SKILL_FILES == (
            "references/dashboard-guide.md",
            "references/dashboard-cards.md",
        )

    def test_yaml_mapping(self):
        from ha_mcp.tools.tools_yaml_config import _YAML_SKILL_FILES

        assert _YAML_SKILL_FILES == ("references/template-guidelines.md",)
