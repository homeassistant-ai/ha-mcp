"""The MCP initialize instructions: skills section, notices, owner text."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ha_mcp.server_instructions import (
    build_skills_instructions,
    compose_instructions,
    skill_tool_hidden,
)
from ha_mcp.server_tool_text import ISSUE_FILING_INSTRUCTIONS, SKILL_TOOL_NAME


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


class TestSkillsSection:
    def test_no_skills_section_without_a_skills_directory(
        self, settings: MagicMock
    ) -> None:
        assert build_skills_instructions(None, settings, tool_hidden=False) is None

    def test_agents_are_told_to_read_skills_and_how(
        self, settings: MagicMock, tmp_path: Path
    ) -> None:
        """#2802: agents that read the skill:// resources never get the
        strict-BPS key or the MandatoryBPS=false opt-out, so the served
        ha_get_skill_guide is the access method, not a fallback."""
        _write_skill(tmp_path)

        result = build_skills_instructions(tmp_path, settings, tool_hidden=False)

        assert result is not None
        assert "IMPORTANT" in result
        assert "### Skill: my-skill (skill://my-skill/SKILL.md)" in result
        assert "Best practices for my-skill tasks." in result
        assert f"Call {SKILL_TOOL_NAME}()" in result
        assert "even if your client can read the skill:// resources" in result
        assert "do not waste time or tokens" not in result
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
        assert "resources/read" in result
        assert "locally installed copy" in result
        assert f"Call {SKILL_TOOL_NAME}()" not in result

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
            ) as tool_config:
                assert skill_tool_hidden(settings) is hidden
            # The decision reads the settings it was given, not the global ones.
            tool_config.assert_called_once_with(settings)

    @pytest.mark.parametrize(
        "entry",
        [
            "empty",
            "file instead of a directory",
            "directory without SKILL.md",
            "SKILL.md without frontmatter",
        ],
    )
    def test_no_skills_section_when_no_skill_is_readable(
        self, settings: MagicMock, tmp_path: Path, entry: str
    ) -> None:
        if entry == "file instead of a directory":
            (tmp_path / "not-a-dir.txt").write_text("just a file")
        elif entry == "directory without SKILL.md":
            (tmp_path / "no-skill-md").mkdir()
        elif entry == "SKILL.md without frontmatter":
            (tmp_path / "bad").mkdir()
            (tmp_path / "bad" / "SKILL.md").write_text("# No frontmatter\n")
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
    def test_an_unset_setting_adds_nothing_after_the_last_notice(
        self, settings: MagicMock, value: str
    ) -> None:
        settings.custom_instructions = value
        assert compose_instructions("SKILLS", settings).endswith(
            ISSUE_FILING_INSTRUCTIONS
        )

    def test_every_client_receives_the_owners_text_under_its_heading(
        self, settings: MagicMock
    ) -> None:
        settings.custom_instructions = f"  {self.TEXT}\n"
        instructions = compose_instructions("SKILLS", settings)
        assert instructions.endswith(
            f"{ISSUE_FILING_INSTRUCTIONS}\n\n"
            f"## Instructions from this installation's owner\n{self.TEXT}"
        )

    def test_line_breaks_in_the_owners_text_reach_the_agent(
        self, settings: MagicMock
    ) -> None:
        settings.custom_instructions = "First rule.\nSecond rule."
        assert compose_instructions(None, settings).endswith(
            "First rule.\nSecond rule."
        )


@pytest.mark.parametrize("hidden", [False, True])
def test_the_search_tool_points_at_the_skill_tool_only_while_it_is_served(
    hidden: bool,
) -> None:
    """#2802: with tool search on, the search tool's description is the one
    skills hint every client sees; it must send agents to the served
    ha_get_skill_guide, and never to a disabled one."""
    from ha_mcp.server import HomeAssistantSmartMCPServer

    srv = HomeAssistantSmartMCPServer.__new__(HomeAssistantSmartMCPServer)
    srv.settings = MagicMock(
        enable_tool_search=True, tool_search_max_results=5, enable_code_mode=False
    )
    srv.mcp = MagicMock()
    srv._user_enabled_tools = set()
    srv._user_pinned_tools = []
    with (
        patch("ha_mcp.server.skill_tool_hidden", return_value=hidden),
        patch("ha_mcp.transforms.CategorizedSearchTransform") as transform,
    ):
        srv._apply_tool_search()

    description = transform.call_args.kwargs["search_tool_description"]
    if hidden:
        assert SKILL_TOOL_NAME not in description
        assert "locally installed copy" in description
    else:
        assert f"Call {SKILL_TOOL_NAME}" in description
        assert "even if your client supports MCP resources" in description


def test_the_server_starts_with_the_composed_instructions(tmp_path: Path) -> None:
    """A regression that drops ``instructions=`` from FastMCP is caught here."""
    from ha_mcp.server import HomeAssistantSmartMCPServer

    _write_skill(tmp_path)
    with (
        patch("ha_mcp.server.get_global_settings") as get_settings,
        patch("ha_mcp.server.FastMCP") as fastmcp,
        patch("ha_mcp.server.skill_tool_hidden", return_value=False),
        patch.object(
            HomeAssistantSmartMCPServer, "_get_skills_dir", return_value=tmp_path
        ),
        patch.object(HomeAssistantSmartMCPServer, "_initialize_server"),
    ):
        settings = get_settings.return_value
        settings.read_only_mode = False
        settings.enable_tool_search = False
        settings.custom_instructions = "owner text"
        HomeAssistantSmartMCPServer()
    instructions = fastmcp.call_args.kwargs["instructions"]
    assert "### Skill: my-skill" in instructions
    assert instructions.endswith("owner text")
