"""The MCP initialize instructions: skills section, notices, owner text."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ha_mcp.config import Settings
from ha_mcp.server_instructions import (
    build_skill_block,
    build_skills_instructions,
    compose_instructions,
    skill_tool_hidden,
)
from ha_mcp.server_tool_text import SKILL_TOOL_NAME


@pytest.fixture
def settings() -> MagicMock:
    settings = MagicMock()
    settings.read_only_mode = False
    settings.enable_tool_search = False
    settings.custom_instructions = ""
    return settings


def _write_skill(skills_dir: Path, name: str = "my-skill") -> Path:
    skill_md = skills_dir / name / "SKILL.md"
    skill_md.parent.mkdir()
    skill_md.write_text(
        f"---\nname: {name}\ndescription: |\n"
        f"  Best practices for {name} tasks.\n---\n# Body\n"
    )
    return skill_md


class TestSkillBlock:
    def test_agents_see_each_skill_with_its_uri_and_description(
        self, tmp_path: Path
    ) -> None:
        result = build_skill_block("my-skill", _write_skill(tmp_path))
        assert result is not None
        assert "### Skill: my-skill" in result
        assert "skill://my-skill/SKILL.md" in result
        assert "Best practices for my-skill tasks." in result

    def test_a_skill_without_frontmatter_is_left_out(self, tmp_path: Path) -> None:
        skill_md = tmp_path / "bad" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("# No frontmatter\n")
        assert build_skill_block("bad", skill_md) is None


class TestSkillsSection:
    def test_no_skills_section_without_a_skills_directory(
        self, settings: MagicMock
    ) -> None:
        assert build_skills_instructions(None, settings, tool_hidden=False) is None

    def test_agents_are_told_to_read_skills_and_how(
        self, settings: MagicMock, tmp_path: Path
    ) -> None:
        """The access method names resources and the ha_get_skill_guide fallback."""
        _write_skill(tmp_path)

        result = build_skills_instructions(tmp_path, settings, tool_hidden=False)

        assert result is not None
        assert "IMPORTANT" in result
        assert "resources/read" in result
        assert "### Skill: my-skill" in result
        assert f"call {SKILL_TOOL_NAME}()" in result
        # The pre-consolidation pair must no longer appear (#1134).
        assert "ha_list_resources" not in result
        assert "ha_read_resource" not in result

    def test_a_disabled_skill_tool_is_not_offered_as_fallback(
        self, settings: MagicMock, tmp_path: Path
    ) -> None:
        """#1886: with ha_get_skill_guide disabled (possible only outside
        strict-BPS mode), agents are pointed at locally installed skills."""
        _write_skill(tmp_path)

        result = build_skills_instructions(tmp_path, settings, tool_hidden=True)

        assert result is not None
        assert "locally installed copy" in result
        assert f"call {SKILL_TOOL_NAME}()" not in result

    def test_the_skill_tool_counts_as_hidden_only_when_disabled_and_unlocked(
        self, settings: MagicMock
    ) -> None:
        """The strict-BPS lock keeps the tool served even when disabled."""
        settings.enable_mandatory_bps = True
        cases = [
            (False, {SKILL_TOOL_NAME: "disabled"}, True),
            (True, {SKILL_TOOL_NAME: "disabled"}, False),
            (False, {}, False),
        ]
        for strict, tools, hidden in cases:
            settings.enable_strict_mandatory_bps = strict
            with patch(
                "ha_mcp.settings_ui._persistence.effective_tool_config",
                return_value={"tools": tools},
            ):
                assert skill_tool_hidden(settings) is hidden

    @pytest.mark.parametrize(
        "entry", ["empty", "file instead of a directory", "directory without SKILL.md"]
    )
    def test_no_skills_section_when_no_skill_is_readable(
        self, settings: MagicMock, tmp_path: Path, entry: str
    ) -> None:
        if entry == "file instead of a directory":
            (tmp_path / "not-a-dir.txt").write_text("just a file")
        elif entry == "directory without SKILL.md":
            (tmp_path / "no-skill-md").mkdir()
        assert build_skills_instructions(tmp_path, settings, tool_hidden=False) is None


class TestNotices:
    @pytest.mark.parametrize("skills", [None, "SKILLS"])
    def test_agents_are_told_issues_need_a_report(
        self, settings: MagicMock, skills: str | None
    ) -> None:
        """Agents that file issues without ha_report_issue output get them
        closed; the instructions must say so with or without skills."""
        instructions = compose_instructions(skills, settings)
        assert "run ha_report_issue and put the issue_body it returns" in instructions
        if skills:
            assert instructions.startswith(skills)

    @pytest.mark.parametrize("skills", [None, "SKILLS"])
    def test_read_only_mode_is_announced_alongside_the_issue_note(
        self, settings: MagicMock, skills: str | None
    ) -> None:
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
    def test_an_unset_setting_adds_nothing(
        self, settings: MagicMock, value: str
    ) -> None:
        baseline = compose_instructions("SKILLS", settings)
        settings.custom_instructions = value
        assert compose_instructions("SKILLS", settings) == baseline

    def test_every_client_receives_the_owners_text(self, settings: MagicMock) -> None:
        settings.custom_instructions = f"  {self.TEXT}\n"
        instructions = compose_instructions("SKILLS", settings)
        assert instructions.endswith(self.TEXT)
        assert "## Filing ha-mcp issues" in instructions

    def test_the_setting_is_read_from_its_environment_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HAMCP_CUSTOM_INSTRUCTIONS", self.TEXT)
        assert Settings().custom_instructions == self.TEXT


def test_the_server_sends_the_composed_instructions(
    settings: MagicMock, tmp_path: Path
) -> None:
    """The server's initialize instructions are this module's composition."""
    from ha_mcp.server import HomeAssistantSmartMCPServer

    _write_skill(tmp_path)
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
