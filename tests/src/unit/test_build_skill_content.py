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
    """Build a fake home-assistant-best-practices skill with reference files
    structured so section-extraction tests have anchors to target."""
    skill = tmp_path / "home-assistant-best-practices"
    refs = skill / "references"
    refs.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# best practices\n")
    (refs / "automation-patterns.md").write_text(
        "# Automation Patterns\n"
        "\n"
        "Top intro prose.\n"
        "\n"
        "## Native Conditions\n"
        "\n"
        "Native conditions are validated at config load.\n"
        "\n"
        "## Trigger Types\n"
        "\n"
        "Trigger types are event-driven.\n"
    )
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
        body = result["references/automation-patterns.md"]
        # Full file: every H2 section present, no slicing applied.
        assert "Native Conditions" in body
        assert "Trigger Types" in body

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
        # Only the referenced file is attached; canonical is suppressed.
        assert set(result.keys()) == {"references/template-guidelines.md"}


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


# ---------------------------------------------------------------------------
# Anchor extraction (reactive section-slicing, issue #1182 Q3)
# ---------------------------------------------------------------------------


class TestAnchorExtraction:
    """Reactive auto-embed ships just the markdown section pointed to by
    the warning's #anchor, not the whole reference file."""

    def test_anchored_ref_returns_section(self, patched_get_skills_dir):
        """An anchored referenced_file returns only that section's text."""
        result = build_skill_content(
            include_skill=False,
            canonical_files=(),
            referenced_files={"references/automation-patterns.md#native-conditions"},
        )
        # Section content, not the whole file:
        body = result["references/automation-patterns.md#native-conditions"]
        assert body.startswith("## Native Conditions")
        assert "Native conditions are validated" in body
        # Stops before the next H2 — does not bleed into Trigger Types.
        assert "Trigger Types" not in body

    def test_bare_supersedes_anchored_for_same_file(self, patched_get_skills_dir):
        """If canonical already ships the whole file, the section ref is dropped.

        Otherwise we'd ship the same content twice in different shapes —
        once whole, once sliced. Wasted bytes for the LLM.
        """
        result = build_skill_content(
            include_skill=True,
            canonical_files=("references/automation-patterns.md",),
            referenced_files={"references/automation-patterns.md#native-conditions"},
        )
        assert "references/automation-patterns.md" in result
        assert "references/automation-patterns.md#native-conditions" not in result

    def test_bare_does_not_supersede_anchored_for_different_file(
        self, patched_get_skills_dir
    ):
        """Dedup only collapses bare-vs-section for the SAME file. Cross-file
        refs are independent."""
        result = build_skill_content(
            include_skill=True,
            canonical_files=("references/template-guidelines.md",),
            referenced_files={"references/automation-patterns.md#native-conditions"},
        )
        assert "references/template-guidelines.md" in result
        assert "references/automation-patterns.md#native-conditions" in result

    def test_missing_anchor_silently_skipped(self, patched_get_skills_dir):
        """An anchor that doesn't match any heading is silently omitted —
        same contract as missing files."""
        result = build_skill_content(
            include_skill=False,
            canonical_files=(),
            referenced_files={"references/automation-patterns.md#does-not-exist"},
        )
        assert result == {}
