"""The MCP initialize instructions: skills section, notices, owner text."""

from unittest.mock import MagicMock, patch

import pytest

from ha_mcp.config import Settings
from ha_mcp.server_instructions import (
    build_skill_block,
    build_skills_instructions,
    compose_instructions,
    skill_tool_hidden,
)


@pytest.fixture
def settings():
    settings = MagicMock()
    settings.read_only_mode = False
    settings.enable_tool_search = False
    settings.custom_instructions = ""
    return settings


class TestBuildSkillBlock:
    """Tests for build_skill_block() instruction formatting."""

    def test_valid_skill_returns_block(self, tmp_path):
        """Valid SKILL.md produces formatted instruction block."""
        skill_md = tmp_path / "test-skill" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text(
            "---\nname: test-skill\ndescription: |\n"
            "  Best practices for testing.\n"
            "---\n# Body\n"
        )
        result = build_skill_block("test-skill", skill_md)
        assert result is not None
        assert "### Skill: test-skill" in result
        assert "skill://test-skill/SKILL.md" in result
        assert "Best practices for testing." in result

    def test_invalid_frontmatter_returns_none(self, tmp_path):
        """SKILL.md with bad frontmatter returns None."""
        skill_md = tmp_path / "bad" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("# No frontmatter\n")
        result = build_skill_block("bad", skill_md)
        assert result is None


class TestBuildSkillsInstructions:
    """Tests for build_skills_instructions() assembly logic."""

    def test_skills_dir_missing(self, settings):
        """Returns None when skills directory does not exist."""
        result = build_skills_instructions(None, settings, tool_hidden=False)
        assert result is None

    def test_valid_skill_produces_instructions(self, settings, tmp_path):
        """Valid skill directory produces instruction text with the
        ha_get_skill_guide fallback referenced in the access method."""
        from ha_mcp.server_tool_text import SKILL_TOOL_NAME

        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\n"
            "description: |\n"
            "  Best practices for my-skill tasks.\n"
            "---\n# Body\n"
        )

        result = build_skills_instructions(tmp_path, settings, tool_hidden=False)

        assert result is not None
        assert "IMPORTANT" in result
        assert "resources/read" in result
        assert "### Skill: my-skill" in result
        assert SKILL_TOOL_NAME in result
        # The pre-consolidation pair must no longer appear (#1134).
        assert "ha_list_resources" not in result
        assert "ha_read_resource" not in result

    def test_hidden_skill_tool_swaps_fallback_to_local_skills(self, settings, tmp_path):
        """#1886: when ha_get_skill_guide is disabled (possible only
        outside strict-BPS mode), the access-method fallback must not
        direct clients to the hidden tool — it points at locally
        installed skills instead."""
        from ha_mcp.server_tool_text import SKILL_TOOL_NAME

        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\n"
            "description: |\n"
            "  Best practices for my-skill tasks.\n"
            "---\n# Body\n"
        )

        result = build_skills_instructions(tmp_path, settings, tool_hidden=True)

        assert result is not None
        assert "locally installed copy" in result
        assert f"call {SKILL_TOOL_NAME}()" not in result

    def test_skill_tool_hidden_logic(self, settings):
        """skill_tool_hidden: true only when the tool is configured
        disabled AND the strict-BPS lock doesn't override it."""
        from ha_mcp.server_tool_text import SKILL_TOOL_NAME

        settings.enable_mandatory_bps = True
        settings.enable_strict_mandatory_bps = False
        with patch(
            "ha_mcp.settings_ui._persistence.effective_tool_config",
            return_value={"tools": {SKILL_TOOL_NAME: "disabled"}},
        ):
            assert skill_tool_hidden(settings) is True

        settings.enable_strict_mandatory_bps = True
        with patch(
            "ha_mcp.settings_ui._persistence.effective_tool_config",
            return_value={"tools": {SKILL_TOOL_NAME: "disabled"}},
        ):
            assert skill_tool_hidden(settings) is False

        settings.enable_strict_mandatory_bps = False
        with patch(
            "ha_mcp.settings_ui._persistence.effective_tool_config",
            return_value={"tools": {}},
        ):
            assert skill_tool_hidden(settings) is False

    def test_empty_skills_dir(self, settings, tmp_path):
        """Empty skills directory returns None."""
        result = build_skills_instructions(tmp_path, settings, tool_hidden=False)
        assert result is None

    def test_non_dir_entries_skipped(self, settings, tmp_path):
        """Files (not directories) in skills dir are skipped."""
        (tmp_path / "not-a-dir.txt").write_text("just a file")
        result = build_skills_instructions(tmp_path, settings, tool_hidden=False)
        assert result is None

    def test_dir_without_skill_md_skipped(self, settings, tmp_path):
        """Directories without SKILL.md are skipped."""
        (tmp_path / "no-skill-md").mkdir()
        result = build_skills_instructions(tmp_path, settings, tool_hidden=False)
        assert result is None


class TestBuildInstructions:
    """Tests for compose_instructions(), the full initialize instructions."""

    @pytest.mark.parametrize("skills", [None, "SKILLS"])
    def test_agents_are_told_issues_need_a_report(self, settings, skills):
        """Agents that file issues without ha_report_issue output get them
        closed; the instructions must say so with or without skills."""
        settings.read_only_mode = False
        instructions = compose_instructions(skills, settings)
        assert "run ha_report_issue and put the issue_body it returns" in instructions
        if skills:
            assert instructions.startswith(skills)

    @pytest.mark.parametrize("skills", [None, "SKILLS"])
    def test_read_only_mode_is_announced_alongside_the_issue_note(
        self, settings, skills
    ):
        """A client started in Read Only Mode must be warned up front, without
        losing the issue-filing rule."""
        settings.read_only_mode = True
        instructions = compose_instructions(skills, settings)
        assert "## Read Only Mode" in instructions
        assert "## Filing ha-mcp issues" in instructions


class TestCustomInstructions:
    """The owner-set ``custom_instructions`` setting."""

    TEXT = "Household notes are in /share/llm/home.md; read them before changes."

    @pytest.mark.parametrize("value", ["", "   \n  "])
    def test_an_unset_setting_adds_nothing(self, settings, value):
        baseline = compose_instructions("SKILLS", settings)
        settings.custom_instructions = value
        assert compose_instructions("SKILLS", settings) == baseline

    def test_every_client_receives_the_owners_text(self, settings):
        settings.custom_instructions = f"  {self.TEXT}\n"
        instructions = compose_instructions("SKILLS", settings)
        assert instructions.endswith(self.TEXT)
        assert "## Filing ha-mcp issues" in instructions

    def test_the_setting_is_read_from_its_environment_variable(self, monkeypatch):
        monkeypatch.setenv("HAMCP_CUSTOM_INSTRUCTIONS", self.TEXT)
        assert Settings().custom_instructions == self.TEXT


def test_the_server_sends_the_composed_instructions(settings, tmp_path):
    """The server's initialize instructions are this module's composition."""
    from ha_mcp.server import HomeAssistantSmartMCPServer

    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: |\n  Best practices.\n---\n# Body\n"
    )
    settings.custom_instructions = "owner text"
    server = HomeAssistantSmartMCPServer.__new__(HomeAssistantSmartMCPServer)
    server.settings = settings
    with (
        patch.object(server, "_get_skills_dir", return_value=tmp_path),
        patch("ha_mcp.server.skill_tool_hidden", return_value=False),
    ):
        instructions = server._build_instructions()
    assert "### Skill: my-skill" in instructions
    assert instructions.endswith("owner text")
