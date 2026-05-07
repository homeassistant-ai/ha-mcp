"""Unit tests for _parse_skill_frontmatter(), _build_skill_block(),
and _build_skills_instructions()."""

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


class TestParseSkillFrontmatter:
    """Tests for _parse_skill_frontmatter() YAML parsing."""

    def test_valid_frontmatter(self, server, tmp_path):
        """Valid SKILL.md returns frontmatter dict."""
        skill_md = tmp_path / "test-skill" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text(
            "---\nname: test-skill\ndescription: |\n"
            "  Best practices for testing.\n"
            "---\n# Body\n"
        )
        result = server._parse_skill_frontmatter(skill_md)
        assert result is not None
        assert isinstance(result, dict)
        assert result["name"] == "test-skill"
        assert "Best practices" in result["description"]

    def test_no_frontmatter_delimiters(self, server, tmp_path):
        """File without --- delimiters returns None."""
        skill_md = tmp_path / "bad-skill" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("# No frontmatter here\nJust content.\n")
        result = server._parse_skill_frontmatter(skill_md)
        assert result is None

    def test_invalid_yaml(self, server, tmp_path):
        """Malformed YAML in frontmatter returns None."""
        skill_md = tmp_path / "bad-yaml" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("---\n: invalid: yaml: [unclosed\n---\n# Body\n")
        result = server._parse_skill_frontmatter(skill_md)
        assert result is None

    def test_non_dict_frontmatter(self, server, tmp_path):
        """Frontmatter that parses to a non-dict (e.g., string) returns None."""
        skill_md = tmp_path / "string-fm" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("---\njust a string\n---\n# Body\n")
        result = server._parse_skill_frontmatter(skill_md)
        assert result is None

    def test_missing_description(self, server, tmp_path):
        """Frontmatter without description field returns None."""
        skill_md = tmp_path / "no-desc" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("---\nname: no-desc\nversion: 1\n---\n# Body\n")
        result = server._parse_skill_frontmatter(skill_md)
        assert result is None

    def test_empty_description(self, server, tmp_path):
        """Frontmatter with empty description returns None."""
        skill_md = tmp_path / "empty" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text('---\nname: empty\ndescription: ""\n---\n# Body\n')
        result = server._parse_skill_frontmatter(skill_md)
        assert result is None

    def test_file_not_readable(self, server, tmp_path):
        """Unreadable file returns None."""
        skill_md = tmp_path / "missing" / "SKILL.md"
        # Don't create the file — read_text will raise OSError
        result = server._parse_skill_frontmatter(skill_md)
        assert result is None


class TestBuildSkillBlock:
    """Tests for _build_skill_block() instruction formatting."""

    def test_valid_skill_returns_block(self, server, tmp_path):
        """Valid SKILL.md produces formatted instruction block."""
        skill_md = tmp_path / "test-skill" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text(
            "---\nname: test-skill\ndescription: |\n"
            "  Best practices for testing.\n"
            "---\n# Body\n"
        )
        result = server._build_skill_block("test-skill", skill_md)
        assert result is not None
        assert "### Skill: test-skill" in result
        assert "skill://test-skill/SKILL.md" in result
        assert "Best practices for testing." in result

    def test_invalid_frontmatter_returns_none(self, server, tmp_path):
        """SKILL.md with bad frontmatter returns None."""
        skill_md = tmp_path / "bad" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("# No frontmatter\n")
        result = server._build_skill_block("bad", skill_md)
        assert result is None


class TestBuildSkillsInstructions:
    """Tests for _build_skills_instructions() assembly logic."""

    def test_skills_dir_missing(self, server):
        """Returns None when skills directory does not exist."""
        with patch.object(server, "_get_skills_dir", return_value=None):
            result = server._build_skills_instructions()
        assert result is None

    def test_valid_skill_produces_instructions(self, server, tmp_path):
        """Valid skill directory produces instruction text with the
        ha_*_resource fallback referenced in the access method."""
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\n"
            "description: |\n"
            "  Best practices for my-skill tasks.\n"
            "---\n# Body\n"
        )

        with patch.object(server, "_get_skills_dir", return_value=tmp_path):
            result = server._build_skills_instructions()

        assert result is not None
        assert "IMPORTANT" in result
        assert "resources/read" in result
        assert "### Skill: my-skill" in result
        assert "ha_list_resources" in result
        assert "ha_read_resource" in result

    def test_empty_skills_dir(self, server, tmp_path):
        """Empty skills directory returns None."""
        with patch.object(server, "_get_skills_dir", return_value=tmp_path):
            result = server._build_skills_instructions()
        assert result is None

    def test_non_dir_entries_skipped(self, server, tmp_path):
        """Files (not directories) in skills dir are skipped."""
        (tmp_path / "not-a-dir.txt").write_text("just a file")
        with patch.object(server, "_get_skills_dir", return_value=tmp_path):
            result = server._build_skills_instructions()
        assert result is None

    def test_dir_without_skill_md_skipped(self, server, tmp_path):
        """Directories without SKILL.md are skipped."""
        (tmp_path / "no-skill-md").mkdir()
        with patch.object(server, "_get_skills_dir", return_value=tmp_path):
            result = server._build_skills_instructions()
        assert result is None


class TestLogSkillRegistrationSummary:
    """Tests for _log_skill_registration_summary's branch logic.

    The summary line is the operator-facing signal for skill-system health,
    so the warning-vs-info gating (which feeds log-grep alerts) needs to
    behave deterministically across all four meaningful states.
    """

    @pytest.fixture
    def emit(self):
        from ha_mcp.server import HomeAssistantSmartMCPServer

        return HomeAssistantSmartMCPServer._log_skill_registration_summary

    def test_logs_info_when_all_phases_ok_and_guidance_present(
        self, emit, caplog
    ):
        import logging

        with caplog.at_level(logging.INFO, logger="ha_mcp.server"):
            emit({"provider": "ok", "transform": "ok", "guidance_tools": 3})
        records = [r for r in caplog.records if "Skill system summary" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO

    def test_logs_warning_when_provider_failed(self, emit, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="ha_mcp.server"):
            emit({"provider": "failed", "transform": "skipped", "guidance_tools": 0})
        records = [r for r in caplog.records if "Skill system summary" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING

    def test_logs_warning_when_transform_failed(self, emit, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="ha_mcp.server"):
            emit({"provider": "ok", "transform": "failed", "guidance_tools": 2})
        records = [r for r in caplog.records if "Skill system summary" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING

    def test_logs_warning_when_both_skipped(self, emit, caplog):
        """`skipped` is not the same as `ok` — the summary must still warn."""
        import logging

        with caplog.at_level(logging.WARNING, logger="ha_mcp.server"):
            emit({"provider": "skipped", "transform": "skipped", "guidance_tools": 0})
        records = [r for r in caplog.records if "Skill system summary" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING

    def test_logs_warning_when_guidance_zero_despite_ok_phases(self, emit, caplog):
        """Both phases healthy but no skill bundle exposed → warning, not info.

        Catches the "shipped but exposes nothing" failure mode where the
        skills directory exists but is empty or every SKILL.md fails to
        parse.
        """
        import logging

        with caplog.at_level(logging.WARNING, logger="ha_mcp.server"):
            emit({"provider": "ok", "transform": "ok", "guidance_tools": 0})
        records = [r for r in caplog.records if "Skill system summary" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING

    def test_missing_guidance_key_treated_as_zero(self, emit, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="ha_mcp.server"):
            emit({"provider": "ok", "transform": "ok"})
        records = [r for r in caplog.records if "Skill system summary" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert "guidance_tools=0" in records[0].getMessage()
