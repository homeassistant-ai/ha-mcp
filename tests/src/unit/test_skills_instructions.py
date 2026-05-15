"""Unit tests for _parse_skill_frontmatter(), _build_skill_block(),
and _build_skills_instructions()."""

from unittest.mock import MagicMock, patch

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
            patch("ha_mcp.server.HomeAssistantSmartMCPServer._initialize_server"),
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
        ha_get_skill_guide fallback referenced in the access method."""
        from ha_mcp.server import SKILL_TOOL_NAME

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
        assert SKILL_TOOL_NAME in result
        # The pre-consolidation pair must no longer appear (#1134).
        assert "ha_list_resources" not in result
        assert "ha_read_resource" not in result

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

    def test_logs_info_when_all_phases_ok_and_guidance_present(self, emit, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="ha_mcp.server"):
            emit({"provider": "ok", "tool": "ok", "guidance_count": 3})
        records = [r for r in caplog.records if "Skill system summary" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO

    def test_logs_warning_when_provider_failed(self, emit, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="ha_mcp.server"):
            emit({"provider": "failed", "tool": "skipped", "guidance_count": 0})
        records = [r for r in caplog.records if "Skill system summary" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING

    def test_logs_warning_when_tool_failed(self, emit, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="ha_mcp.server"):
            emit({"provider": "ok", "tool": "failed", "guidance_count": 0})
        records = [r for r in caplog.records if "Skill system summary" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING

    def test_logs_warning_when_both_skipped(self, emit, caplog):
        """`skipped` is not the same as `ok` — the summary must still warn."""
        import logging

        with caplog.at_level(logging.WARNING, logger="ha_mcp.server"):
            emit({"provider": "skipped", "tool": "skipped", "guidance_count": 0})
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
            emit({"provider": "ok", "tool": "ok", "guidance_count": 0})
        records = [r for r in caplog.records if "Skill system summary" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING

    def test_missing_guidance_key_treated_as_zero(self, emit, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="ha_mcp.server"):
            emit({"provider": "ok", "tool": "ok"})
        records = [r for r in caplog.records if "Skill system summary" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert "guidance_count=0" in records[0].getMessage()


class TestHandleSkillGuideCall:
    """Tests for the three-tier ha_get_skill_guide handler.

    Validates the tier dispatch, path-traversal guards, and the
    degraded-mode behavior when no skills directory exists. The handler
    is split out from the registered tool closure specifically so it
    can be unit-tested without an MCP client round-trip.
    """

    @pytest.fixture
    def populated_skills_dir(self, tmp_path):
        """A tmp skills dir with one valid skill and one ignored entry."""
        skill = tmp_path / "best-practices"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: best-practices\n"
            "description: |\n"
            "  Best practices for HA tasks.\n"
            "---\n# Best practices\nReal content here.\n"
        )
        (skill / "reference.md").write_text("# Reference\nReal reference.\n")

        # Ignored: not a directory.
        (tmp_path / "stray.txt").write_text("ignored")

        # Ignored: dir without SKILL.md.
        (tmp_path / "no-skill-md").mkdir()
        (tmp_path / "no-skill-md" / "other.md").write_text("ignored")

        return tmp_path

    def test_tier1_lists_only_valid_skills(self, server, populated_skills_dir):
        """No-args call lists only dirs with parseable SKILL.md."""
        result = server._handle_skill_guide_call(populated_skills_dir, None, None)
        assert result["success"] is True
        assert "skills" in result
        names = [s["skill"] for s in result["skills"]]
        assert names == ["best-practices"]
        assert result["skills"][0]["uri"] == "skill://best-practices/SKILL.md"
        assert "Best practices" in result["skills"][0]["description"]

    def test_tier2_lists_files(self, server, populated_skills_dir):
        """skill arg lists every file in the skill dir."""
        result = server._handle_skill_guide_call(
            populated_skills_dir, "best-practices", None
        )
        assert result["success"] is True
        assert result["skill"] == "best-practices"
        names = sorted(f["name"] for f in result["files"])
        assert names == ["SKILL.md", "reference.md"]

    def test_tier3_reads_content(self, server, populated_skills_dir):
        """skill + file args read the file content verbatim."""
        result = server._handle_skill_guide_call(
            populated_skills_dir, "best-practices", "reference.md"
        )
        assert result["success"] is True
        assert result["skill"] == "best-practices"
        assert result["file"] == "reference.md"
        assert "Real reference" in result["content"]

    def test_unknown_skill_raises(self, server, populated_skills_dir):
        """An unknown skill name raises ToolError, not silent empty dict."""
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            server._handle_skill_guide_call(
                populated_skills_dir, "does-not-exist", None
            )

    def test_skill_traversal_raises(self, server, populated_skills_dir):
        """``../`` in the skill arg must not escape the skills dir."""
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            server._handle_skill_guide_call(populated_skills_dir, "../..", None)

    def test_file_traversal_raises(self, server, populated_skills_dir):
        """``../`` in the file arg must not escape the skill dir."""
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            server._handle_skill_guide_call(
                populated_skills_dir,
                "best-practices",
                "../../etc/passwd",
            )

    def test_missing_file_raises(self, server, populated_skills_dir):
        """A file that doesn't exist in a valid skill raises rather than 404s."""
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            server._handle_skill_guide_call(
                populated_skills_dir, "best-practices", "missing.md"
            )

    def test_degraded_mode_tier1_returns_empty_listing(self, server):
        """No skills dir → tier 1 returns an empty list with an explanation.

        This is the always-registered-tool contract: callers see a
        structured response explaining the situation, not a missing
        tool, when the skills submodule is uninitialized.
        """
        result = server._handle_skill_guide_call(None, None, None)
        assert result["success"] is True
        assert result["skills"] == []
        assert "submodule" in result["how_to_use"].lower()

    def test_degraded_mode_tier2_raises(self, server):
        """No skills dir → asking for a specific skill raises explicitly."""
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            server._handle_skill_guide_call(None, "best-practices", None)

    def test_degraded_mode_tier3_raises(self, server):
        """No skills dir → asking for a file raises explicitly."""
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            server._handle_skill_guide_call(None, "best-practices", "SKILL.md")


class TestSkillToolMandatoryPinning:
    """Mandatory-pinning invariants for the consolidated skill tool.

    The skill guide carries the bundled best-practices trigger
    conditions in its description — when tool search hides the catalog,
    only pinned tools stay visible. Disabling or unpinning it would
    silently break the "consult skill before writing config" workflow,
    so both the default-pinned tuple AND the always-enabled set must
    include it. These tests fail loudly if a future refactor drops
    either side.
    """

    def test_default_pinned_tools_includes_skill_guide(self):
        from ha_mcp.server import SKILL_TOOL_NAME
        from ha_mcp.transforms import DEFAULT_PINNED_TOOLS

        assert SKILL_TOOL_NAME in DEFAULT_PINNED_TOOLS

    def test_mandatory_tools_includes_skill_guide(self):
        from ha_mcp.server import SKILL_TOOL_NAME
        from ha_mcp.settings_ui import MANDATORY_TOOLS

        assert SKILL_TOOL_NAME in MANDATORY_TOOLS

    def test_tool_name_fits_cloudflare_cap(self):
        """#1121: Cloudflare MCP portal rejects tool names > 40 chars."""
        from ha_mcp.server import SKILL_TOOL_NAME

        assert len(SKILL_TOOL_NAME) <= 40


class TestSkillToolAliasKeywords:
    """The consolidated tool must mention the names it replaced.

    Two surfaces: (1) BM25 keyword enrichment so agents searching for
    the old tool names get routed to the new one; (2) the tool's own
    description so a human or LLM reading the catalog sees the redirect
    inline. Both regress silently if the alias text disappears.
    """

    def test_search_keywords_mention_old_tools(self):
        from ha_mcp.server import SKILL_TOOL_NAME, HomeAssistantSmartMCPServer

        keywords = HomeAssistantSmartMCPServer._SEARCH_KEYWORDS.get(SKILL_TOOL_NAME)
        assert keywords is not None, (
            f"{SKILL_TOOL_NAME} must have an entry in _SEARCH_KEYWORDS so "
            "BM25 retrieval on old tool names routes to the replacement."
        )
        for old_name in (
            "ha_list_resources",
            "ha_read_resource",
            "ha_get_skill_home_assistant_best_practices",
        ):
            assert old_name in keywords, (
                f"BM25 keywords for {SKILL_TOOL_NAME} should mention {old_name} "
                "so retrieval on the pre-#1134 name finds the replacement."
            )

    def test_tool_description_mentions_old_tools(self, server, tmp_path):
        """The description passed to mcp.tool() must include the alias text."""
        from ha_mcp.server import SKILL_TOOL_NAME

        # Build a minimal valid skill so the populated-mode description
        # branch runs.
        skill = tmp_path / "best-practices"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: best-practices\ndescription: |\n"
            "  Best practices for HA tasks.\n---\n"
        )

        captured: dict = {}

        def fake_tool(*, name, description, **kwargs):
            def _decorator(fn):
                captured[name] = description
                return fn

            return _decorator

        server.mcp = MagicMock()
        server.mcp.tool.side_effect = fake_tool

        server._register_skill_guide_tool(tmp_path)
        desc = captured[SKILL_TOOL_NAME]

        for old_name in (
            "ha_list_resources",
            "ha_read_resource",
            "ha_get_skill_home_assistant_best_practices",
        ):
            assert old_name in desc, (
                f"Tool description for {SKILL_TOOL_NAME} should mention "
                f"{old_name} (alias redirect for agents trained on the "
                "pre-#1134 catalog)."
            )

    def test_degraded_description_also_mentions_old_tools(self, server):
        """Even in the no-skills-available branch, the alias text appears."""
        from ha_mcp.server import SKILL_TOOL_NAME

        captured: dict = {}

        def fake_tool(*, name, description, **kwargs):
            def _decorator(fn):
                captured[name] = description
                return fn

            return _decorator

        server.mcp = MagicMock()
        server.mcp.tool.side_effect = fake_tool

        server._register_skill_guide_tool(None)
        desc = captured[SKILL_TOOL_NAME]

        assert "ha_list_resources" in desc
        assert "ha_get_skill_home_assistant_best_practices" in desc
