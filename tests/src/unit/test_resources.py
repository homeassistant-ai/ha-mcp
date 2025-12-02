"""Unit tests for package resource files.

These tests verify that resource files (dashboard_guide.md, card_types.json)
are properly accessible within the package - addressing issue #225 where
the files were not included in the PyPI distribution.
"""

import json
from pathlib import Path

import pytest


class TestResourcesAccessibility:
    """Test that package resources are accessible."""

    def test_resources_directory_exists(self):
        """The resources directory should exist in the ha_mcp package."""
        from ha_mcp.tools.tools_config_dashboards import _get_resources_dir

        resources_dir = _get_resources_dir()
        assert resources_dir.exists(), f"Resources directory not found: {resources_dir}"
        assert resources_dir.is_dir(), f"Resources path is not a directory: {resources_dir}"

    def test_dashboard_guide_exists(self):
        """The dashboard_guide.md file should exist and be readable."""
        from ha_mcp.tools.tools_config_dashboards import _get_resources_dir

        resources_dir = _get_resources_dir()
        guide_path = resources_dir / "dashboard_guide.md"

        assert guide_path.exists(), f"dashboard_guide.md not found: {guide_path}"
        assert guide_path.is_file(), f"dashboard_guide.md is not a file: {guide_path}"

        # Verify file is readable and has content
        content = guide_path.read_text()
        assert len(content) > 0, "dashboard_guide.md is empty"
        assert "dashboard" in content.lower(), "dashboard_guide.md doesn't appear to contain dashboard content"

    def test_card_types_exists(self):
        """The card_types.json file should exist and be readable."""
        from ha_mcp.tools.tools_config_dashboards import _get_resources_dir

        resources_dir = _get_resources_dir()
        types_path = resources_dir / "card_types.json"

        assert types_path.exists(), f"card_types.json not found: {types_path}"
        assert types_path.is_file(), f"card_types.json is not a file: {types_path}"

        # Verify file is valid JSON with expected structure
        content = types_path.read_text()
        data = json.loads(content)

        assert "card_types" in data, "card_types.json missing 'card_types' key"
        assert "total_count" in data, "card_types.json missing 'total_count' key"
        assert isinstance(data["card_types"], list), "card_types should be a list"
        assert len(data["card_types"]) > 0, "card_types list is empty"

    def test_card_types_structure(self):
        """The card_types.json should have valid structure for all entries."""
        from ha_mcp.tools.tools_config_dashboards import _get_resources_dir

        resources_dir = _get_resources_dir()
        types_path = resources_dir / "card_types.json"
        data = json.loads(types_path.read_text())

        # Verify total_count matches actual list length
        assert data["total_count"] == len(data["card_types"]), (
            f"total_count ({data['total_count']}) doesn't match "
            f"actual card_types length ({len(data['card_types'])})"
        )

        # Verify all card types are non-empty strings
        for card_type in data["card_types"]:
            assert isinstance(card_type, str), f"Card type should be string: {card_type}"
            assert len(card_type) > 0, "Card type should not be empty string"


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

        # Verify package-data includes resource patterns
        assert "resources/*.md" in content, (
            "pyproject.toml should include 'resources/*.md' in package-data"
        )
        assert "resources/*.json" in content, (
            "pyproject.toml should include 'resources/*.json' in package-data"
        )
