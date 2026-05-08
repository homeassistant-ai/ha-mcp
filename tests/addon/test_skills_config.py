"""Test bundled skills configuration and file structure."""

from pathlib import Path


class TestSkillsConfig:
    """Verify skills configuration is unconditional (toggles removed in #1133)."""

    def test_enable_skills_field_removed(self):
        """The enable_skills field is gone; skills always register."""
        from ha_mcp.config import Settings

        assert "enable_skills" not in Settings.model_fields

    def test_enable_skills_as_tools_field_removed(self):
        """The enable_skills_as_tools field is gone; skills are always exposed as tools."""
        from ha_mcp.config import Settings

        assert "enable_skills_as_tools" not in Settings.model_fields


class TestBundledSkillFiles:
    """Verify bundled skill files are present and correctly structured."""

    def _get_skills_dir(self) -> Path:
        """Get the path to the bundled skills directory."""
        import ha_mcp

        return Path(ha_mcp.__file__).parent / "resources" / "skills-vendor" / "skills"

    def test_skills_directory_exists(self):
        """Check that the skills directory exists."""
        skills_dir = self._get_skills_dir()
        assert skills_dir.exists(), f"Skills directory not found at {skills_dir}"
        assert skills_dir.is_dir(), f"{skills_dir} is not a directory"

    def test_best_practices_skill_exists(self):
        """Check that the home-assistant-best-practices skill is bundled."""
        skill_dir = self._get_skills_dir() / "home-assistant-best-practices"
        assert skill_dir.exists(), "home-assistant-best-practices skill not found"
        assert skill_dir.is_dir()

    def test_skill_md_exists(self):
        """Check that SKILL.md is present (required by SkillsDirectoryProvider)."""
        skill_md = (
            self._get_skills_dir()
            / "home-assistant-best-practices"
            / "SKILL.md"
        )
        assert skill_md.exists(), "SKILL.md not found"
        assert skill_md.stat().st_size > 0, "SKILL.md is empty"

    def test_skill_md_has_frontmatter(self):
        """Check that SKILL.md contains YAML frontmatter with required fields."""
        skill_md = (
            self._get_skills_dir()
            / "home-assistant-best-practices"
            / "SKILL.md"
        )
        content = skill_md.read_text()
        assert content.startswith("---"), "SKILL.md should start with YAML frontmatter"
        # Should have name and description in frontmatter
        frontmatter_end = content.index("---", 3)
        frontmatter = content[3:frontmatter_end]
        assert "name:" in frontmatter, "Frontmatter should include 'name'"
        assert "description:" in frontmatter, "Frontmatter should include 'description'"

    def test_reference_files_exist(self):
        """Check that all expected reference files are bundled."""
        refs_dir = (
            self._get_skills_dir()
            / "home-assistant-best-practices"
            / "references"
        )
        assert refs_dir.exists(), "references/ directory not found"

        expected_files = [
            "automation-patterns.md",
            "device-control.md",
            "examples.yaml",
            "helper-selection.md",
            "safe-refactoring.md",
            "template-guidelines.md",
        ]

        for filename in expected_files:
            filepath = refs_dir / filename
            assert filepath.exists(), f"Reference file {filename} not found"
            assert filepath.stat().st_size > 0, f"Reference file {filename} is empty"

    def test_total_skill_content_size(self):
        """Check that bundled skill content is approximately the expected size (~68KB)."""
        skill_dir = self._get_skills_dir() / "home-assistant-best-practices"
        total_size = sum(f.stat().st_size for f in skill_dir.rglob("*") if f.is_file())
        # Should be roughly 60-80KB
        assert total_size > 50_000, f"Skill content too small: {total_size} bytes"
        assert total_size < 200_000, f"Skill content unexpectedly large: {total_size} bytes"
