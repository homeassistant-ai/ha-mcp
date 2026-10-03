"""Unit tests for package resource files.

These tests verify that bundled skill reference files are properly
accessible within the package. Dashboard guide, card types, and domain
docs content has moved to skill reference files (skills repo v1.2.0).
"""

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]


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


@pytest.fixture(scope="module")
def wheel_files(tmp_path_factory: pytest.TempPathFactory) -> set[str]:
    """Build a wheel from this checkout and list the files it holds."""
    out = tmp_path_factory.mktemp("wheel")
    result = subprocess.run(
        [
            shutil.which("uv") or "uv",
            "build",
            "--wheel",
            "--offline",
            "--out-dir",
            str(out),
            str(_REPO_ROOT),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
        env={
            **os.environ,
            "UV_CACHE_DIR": str(tmp_path_factory.mktemp("uv-cache")),
            "UV_NO_CONFIG": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    (wheel,) = out.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        return set(archive.namelist())


class TestWheelContents:
    """The published wheel is what PyPI users and the HA app install."""

    def test_the_wheel_installs_only_the_ha_mcp_package(
        self, wheel_files: set[str]
    ) -> None:
        """Any other top-level package lands in every user's site-packages,
        where it can shadow a package of the same name from another project."""
        tops = {name.split("/")[0] for name in wheel_files}
        assert {top for top in tops if not top.endswith(".dist-info")} == {"ha_mcp"}

    def test_the_wheel_carries_every_file_installs_need(
        self, wheel_files: set[str]
    ) -> None:
        """The settings UI and the skill guides are read from files beside
        the code. A file missing from the wheel breaks every install, while
        a checkout still has it on disk. The vendored licenses must ship
        with the vendored code (BSD-3 for websockets)."""
        package = _REPO_ROOT / "src" / "ha_mcp"
        assets = [
            package / "settings_ui" / name for name in ("settings.html", "settings.css")
        ]
        assets += (package / "settings_ui" / "settings_js").glob("*.js")
        assets += (package / "settings_ui" / "locales").glob("*.json")
        # Bug reports detect a PyPI install by this marker.
        assets.append(package / "_pypi_marker")
        assets += (package / "_vendor").glob("*/LICENSE")
        assets += (
            path
            for path in (package / "resources" / "skills-vendor" / "skills").rglob("*")
            if path.is_file()
        )
        expected = {path.relative_to(package.parent).as_posix() for path in assets}
        assert len(expected) > 3, "found no locale or skill files to check"

        assert sorted(expected - wheel_files) == []

    def test_the_wheel_carries_only_the_skills_from_the_skills_submodule(
        self, wheel_files: set[str]
    ) -> None:
        """The skills repo's eval scripts and repo files are its own tooling.
        Shipped, its .py files install as ha_mcp modules on every server."""
        vendor = "ha_mcp/resources/skills-vendor/"
        shipped = {
            name[len(vendor) :]
            for name in wheel_files
            if name.startswith(vendor) and not name.endswith("/")
        }
        extra = {
            name
            for name in shipped
            if name != "LICENSE" and not name.startswith("skills/")
        }
        assert sorted(extra) == []
