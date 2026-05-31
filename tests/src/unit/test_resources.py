"""Unit tests for package resource files.

These tests verify that bundled skill reference files are properly
accessible within the package. Dashboard guide, card types, and domain
docs content has moved to skill reference files (skills repo v1.2.0).
"""

from pathlib import Path

import pytest


def _get_resources_dir() -> Path:
    """Get the resources directory from the ha_mcp package."""
    import ha_mcp

    return Path(ha_mcp.__file__).parent / "resources"


class TestResourcesAccessibility:
    """Test that package resources are accessible."""

    def test_resources_directory_exists(self):
        """The resources directory should exist in the ha_mcp package."""
        resources_dir = _get_resources_dir()
        assert resources_dir.exists(), f"Resources directory not found: {resources_dir}"
        assert resources_dir.is_dir(), (
            f"Resources path is not a directory: {resources_dir}"
        )

    def test_skills_vendor_directory_exists(self):
        """The skills-vendor submodule directory should exist."""
        skills_dir = _get_resources_dir() / "skills-vendor" / "skills"
        assert skills_dir.exists(), f"Skills directory not found: {skills_dir}"
        assert skills_dir.is_dir(), f"Skills path is not a directory: {skills_dir}"

    def test_best_practices_skill_exists(self):
        """The home-assistant-best-practices skill should exist with SKILL.md."""
        skill_dir = (
            _get_resources_dir()
            / "skills-vendor"
            / "skills"
            / "home-assistant-best-practices"
        )
        assert skill_dir.exists(), f"Best practices skill not found: {skill_dir}"

        skill_md = skill_dir / "SKILL.md"
        assert skill_md.exists(), f"SKILL.md not found: {skill_md}"

        content = skill_md.read_text()
        assert len(content) > 0, "SKILL.md is empty"
        assert "---" in content, "SKILL.md should have YAML frontmatter"

    def test_dashboard_guide_reference_exists(self):
        """The dashboard-guide.md reference file should exist in the skill."""
        ref = (
            _get_resources_dir()
            / "skills-vendor"
            / "skills"
            / "home-assistant-best-practices"
            / "references"
            / "dashboard-guide.md"
        )
        assert ref.exists(), f"dashboard-guide.md reference not found: {ref}"
        content = ref.read_text()
        assert "dashboard" in content.lower(), (
            "dashboard-guide.md should contain dashboard content"
        )

    def test_dashboard_cards_reference_exists(self):
        """The dashboard-cards.md reference file should exist in the skill."""
        ref = (
            _get_resources_dir()
            / "skills-vendor"
            / "skills"
            / "home-assistant-best-practices"
            / "references"
            / "dashboard-cards.md"
        )
        assert ref.exists(), f"dashboard-cards.md reference not found: {ref}"
        content = ref.read_text()
        assert "card" in content.lower(), (
            "dashboard-cards.md should contain card content"
        )

    def test_domain_docs_reference_exists(self):
        """The domain-docs.md reference file should exist in the skill."""
        ref = (
            _get_resources_dir()
            / "skills-vendor"
            / "skills"
            / "home-assistant-best-practices"
            / "references"
            / "domain-docs.md"
        )
        assert ref.exists(), f"domain-docs.md reference not found: {ref}"
        content = ref.read_text()
        assert len(content) > 0, "domain-docs.md is empty"


class TestPyprojectPackageData:
    """Test that pyproject.toml correctly specifies package data."""

    def test_pyproject_includes_resources(self):
        """pyproject.toml should include resource files in package-data."""
        # Find pyproject.toml relative to ha_mcp package
        import ha_mcp

        package_dir = Path(ha_mcp.__file__).parent
        project_root = package_dir.parent.parent  # src/ha_mcp -> project root

        # Try common locations for pyproject.toml
        pyproject_paths = [
            project_root / "pyproject.toml",
            project_root.parent / "pyproject.toml",
        ]

        pyproject_path = None
        for path in pyproject_paths:
            if path.exists():
                pyproject_path = path
                break

        # Skip test if pyproject.toml not found (installed from wheel)
        if pyproject_path is None:
            pytest.skip("pyproject.toml not found - likely installed from distribution")

        content = pyproject_path.read_text()

        # Verify package-data includes skills-vendor pattern
        assert "resources/skills-vendor/**/*" in content, (
            "pyproject.toml should include 'resources/skills-vendor/**/*' in package-data"
        )

    def test_settings_assets_are_packaged(self):
        """settings.js and settings.css must be declared for both the wheel
        (pyproject package-data) and the sdist (MANIFEST.in).

        settings_ui.py reads both files at import time, and the HA add-on's
        Dockerfile copies only the installed .venv -- so the files reach the
        add-on solely via wheel package-data. A future edit dropping either
        entry would break 100% of installs at import, invisible to the unit
        suite (the dev tree always has the files on disk). Lock the packaging
        declarations here so that regression fails a test instead.
        """
        import ha_mcp

        package_dir = Path(ha_mcp.__file__).parent
        project_root = package_dir.parent.parent  # src/ha_mcp -> project root

        candidate_roots = [project_root, project_root.parent]
        pyproject_path = next(
            (
                root / "pyproject.toml"
                for root in candidate_roots
                if (root / "pyproject.toml").exists()
            ),
            None,
        )
        manifest_path = next(
            (
                root / "MANIFEST.in"
                for root in candidate_roots
                if (root / "MANIFEST.in").exists()
            ),
            None,
        )
        if pyproject_path is None or manifest_path is None:
            pytest.skip(
                "pyproject.toml / MANIFEST.in not found - likely installed from distribution"
            )

        pyproject = pyproject_path.read_text()
        manifest = manifest_path.read_text()

        for asset in ("settings.js", "settings.css"):
            assert f'"{asset}"' in pyproject, (
                f"pyproject.toml package-data must list {asset} (wheel + add-on rely on it)"
            )
            assert f"src/ha_mcp/{asset}" in manifest, (
                f"MANIFEST.in must include src/ha_mcp/{asset} (sdist relies on it)"
            )
