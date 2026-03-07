"""Unit tests for _build_skill_block() and _build_skills_instructions()."""

from unittest.mock import patch

import pytest


@pytest.fixture
def server():
    """Create a server instance with mocked dependencies for testing."""
    with (
        patch("ha_mcp.server.get_global_settings") as mock_settings,
        patch("ha_mcp.server.FastMCP"),
    ):
        settings = mock_settings.return_value
        settings.mcp_server_name = "test"
        settings.mcp_server_version = "0.0.1"
        settings.enable_skills = True
        settings.enable_skills_as_tools = False
        settings.enabled_tool_modules = "all"
        settings.enable_dashboard_partial_tools = True

        # Patch out tool registration and skills registration
        with (
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer._initialize_server"
            ),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer._build_skills_instructions",
                return_value=None,
            ),
        ):
            from ha_mcp.server import HomeAssistantSmartMCPServer

            srv = HomeAssistantSmartMCPServer.__new__(HomeAssistantSmartMCPServer)
            srv.settings = settings
            return srv


class TestBuildSkillBlock:
    """Tests for _build_skill_block() frontmatter parsing."""

    def test_valid_frontmatter(self, server, tmp_path):
        """Valid SKILL.md returns formatted block with description."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            "---\nname: test-skill\ndescription: |\n"
            "  This skill helps with testing.\n---\n# Body\n"
        )
        result = server._build_skill_block("test-skill", skill_md)
        assert result is not None
        assert "### Skill: test-skill" in result
        assert "skill://test-skill/SKILL.md" in result
        assert "This skill helps with testing." in result

    def test_no_frontmatter_delimiters(self, server, tmp_path):
        """File without --- delimiters returns None."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("# No frontmatter here\nJust content.\n")
        result = server._build_skill_block("bad-skill", skill_md)
        assert result is None

    def test_invalid_yaml(self, server, tmp_path):
        """Malformed YAML in frontmatter returns None."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\n: invalid: yaml: [unclosed\n---\n# Body\n")
        result = server._build_skill_block("bad-yaml", skill_md)
        assert result is None

    def test_non_dict_frontmatter(self, server, tmp_path):
        """Frontmatter that parses to a non-dict (e.g., string) returns None."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\njust a string\n---\n# Body\n")
        result = server._build_skill_block("string-fm", skill_md)
        assert result is None

    def test_missing_description(self, server, tmp_path):
        """Frontmatter without description field returns None."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: no-desc\nversion: 1\n---\n# Body\n")
        result = server._build_skill_block("no-desc", skill_md)
        assert result is None

    def test_empty_description(self, server, tmp_path):
        """Frontmatter with empty description returns None."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text('---\nname: empty\ndescription: ""\n---\n# Body\n')
        result = server._build_skill_block("empty", skill_md)
        assert result is None

    def test_file_not_readable(self, server, tmp_path):
        """Unreadable file returns None."""
        skill_md = tmp_path / "SKILL.md"
        # Don't create the file — read_text will raise OSError
        result = server._build_skill_block("missing", skill_md)
        assert result is None

    def test_description_whitespace_stripped(self, server, tmp_path):
        """Description whitespace is stripped in output."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            "---\nname: ws\ndescription: |"
            "\n  Padded description.  \n---\n# Body\n"
        )
        result = server._build_skill_block("ws", skill_md)
        assert result is not None
        assert "Padded description." in result


class TestBuildSkillsInstructions:
    """Tests for _build_skills_instructions() assembly logic."""

    def test_skills_disabled(self, server):
        """Returns None when enable_skills is False."""
        server.settings.enable_skills = False
        result = server._build_skills_instructions()
        assert result is None

    def test_skills_dir_missing(self, server):
        """Returns None when skills directory does not exist."""
        server.settings.enable_skills = True
        with patch.object(server, "_get_skills_dir", return_value=None):
            result = server._build_skills_instructions()
        assert result is None

    def test_valid_skill_produces_instructions(self, server, tmp_path):
        """Valid skill directory produces instruction text."""
        # Create a skill directory with a valid SKILL.md
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\n"
            "description: Helps with my-skill tasks.\n---\n# Body\n"
        )

        server.settings.enable_skills = True
        server.settings.enable_skills_as_tools = False
        with patch.object(server, "_get_skills_dir", return_value=tmp_path):
            result = server._build_skills_instructions()

        assert result is not None
        assert "IMPORTANT" in result
        assert "resources/read" in result
        assert "### Skill: my-skill" in result

    def test_skills_as_tools_access_method(self, server, tmp_path):
        """enable_skills_as_tools changes the access method text."""
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\n"
            "description: Helps with my-skill tasks.\n---\n# Body\n"
        )

        server.settings.enable_skills = True
        server.settings.enable_skills_as_tools = True
        with patch.object(server, "_get_skills_dir", return_value=tmp_path):
            result = server._build_skills_instructions()

        assert result is not None
        assert "read_resource tool" in result

    def test_empty_skills_dir(self, server, tmp_path):
        """Empty skills directory returns None."""
        server.settings.enable_skills = True
        with patch.object(server, "_get_skills_dir", return_value=tmp_path):
            result = server._build_skills_instructions()
        assert result is None

    def test_non_dir_entries_skipped(self, server, tmp_path):
        """Files (not directories) in skills dir are skipped."""
        (tmp_path / "not-a-dir.txt").write_text("just a file")
        server.settings.enable_skills = True
        with patch.object(server, "_get_skills_dir", return_value=tmp_path):
            result = server._build_skills_instructions()
        assert result is None

    def test_dir_without_skill_md_skipped(self, server, tmp_path):
        """Directories without SKILL.md are skipped."""
        (tmp_path / "no-skill-md").mkdir()
        server.settings.enable_skills = True
        with patch.object(server, "_get_skills_dir", return_value=tmp_path):
            result = server._build_skills_instructions()
        assert result is None
