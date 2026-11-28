"""Unit tests for skill frontmatter parsing and the ha_get_skill_guide tool.

The skills section of the initialize instructions is covered in
``test_server_instructions.py``."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.server_tool_text import SKILL_TOOL_NAME
from ha_mcp.utils.skill_loader import BEST_PRACTICES_SKILL_NAME, parse_skill_frontmatter

from ._server_factory import make_server
from ._symlink_support import symlink_or_skip


@pytest.fixture
def server(core_client: HomeAssistantClient, monkeypatch):
    def build(skills_dir=None):
        return make_server(core_client, monkeypatch, skills_dir)

    return build


async def _guide(server, skills_dir, file=None):
    arguments = {} if file is None else {"file": file}
    result = await server(skills_dir).mcp.call_tool(SKILL_TOOL_NAME, arguments)
    assert result.structured_content is not None
    return result.structured_content


class TestParseSkillFrontmatter:
    """Tests for _parse_skill_frontmatter() YAML parsing."""

    def test_valid_frontmatter(self, tmp_path):
        """Valid SKILL.md returns frontmatter dict."""
        skill_md = tmp_path / "test-skill" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text(
            "---\nname: test-skill\ndescription: |\n"
            "  Best practices for testing.\n"
            "---\n# Body\n"
        )
        result = parse_skill_frontmatter(skill_md)
        assert result is not None
        assert isinstance(result, dict)
        assert result["name"] == "test-skill"
        assert "Best practices" in result["description"]

    def test_dashes_inside_description_do_not_end_frontmatter(self, tmp_path):
        """Only an unindented ``---`` line closes the frontmatter; a value
        containing ``---``, inline or as an indented block-scalar line,
        must parse whole, or the skill would count as unavailable."""
        skill_md = tmp_path / "test-skill" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text(
            '---\nname: "Use --- separators"\n'
            "description: |\n  Before\n  ---\n  After\n---\n# Body\n"
        )
        result = parse_skill_frontmatter(skill_md)
        assert result is not None
        assert result["name"] == "Use --- separators"
        assert result["description"].splitlines() == ["Before", "---", "After"]

    def test_horizontal_rules_in_body_are_not_frontmatter(self, tmp_path):
        """Frontmatter must open on the first line; two Markdown rules
        further down must not be read as frontmatter."""
        skill_md = tmp_path / "test-skill" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("# Title\n---\ndescription: not frontmatter\n---\n")
        assert parse_skill_frontmatter(skill_md) is None

    def test_no_frontmatter_delimiters(self, tmp_path):
        """File without --- delimiters returns None."""
        skill_md = tmp_path / "bad-skill" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("# No frontmatter here\nJust content.\n")
        result = parse_skill_frontmatter(skill_md)
        assert result is None

    def test_invalid_yaml(self, tmp_path):
        """Malformed YAML in frontmatter returns None."""
        skill_md = tmp_path / "bad-yaml" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("---\n: invalid: yaml: [unclosed\n---\n# Body\n")
        result = parse_skill_frontmatter(skill_md)
        assert result is None

    def test_non_dict_frontmatter(self, tmp_path):
        """Frontmatter that parses to a non-dict (e.g., string) returns None."""
        skill_md = tmp_path / "string-fm" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("---\njust a string\n---\n# Body\n")
        result = parse_skill_frontmatter(skill_md)
        assert result is None

    def test_missing_description(self, tmp_path):
        """Frontmatter without description field returns None."""
        skill_md = tmp_path / "no-desc" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text("---\nname: no-desc\nversion: 1\n---\n# Body\n")
        result = parse_skill_frontmatter(skill_md)
        assert result is None

    def test_empty_description(self, tmp_path):
        """Frontmatter with empty description returns None."""
        skill_md = tmp_path / "empty" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text('---\nname: empty\ndescription: ""\n---\n# Body\n')
        result = parse_skill_frontmatter(skill_md)
        assert result is None

    @pytest.mark.parametrize(
        "yaml_desc",
        [
            "description: 42",  # int — truthy non-string
            "description: 3.14",  # float — truthy non-string
            "description: [a, b]",  # list — truthy non-string
            "description: {key: value}",  # dict — truthy non-string
            "description: true",  # bool — truthy non-string
        ],
    )
    def test_non_string_description_returns_none(self, tmp_path, yaml_desc):
        """Truthy non-string descriptions would later crash on .strip() —
        callers do ``fm["description"].strip()`` without checking type.
        Fail closed at the parser so malformed bundles only break
        themselves, not the whole registration."""
        skill_md = tmp_path / "bad-type" / "SKILL.md"
        skill_md.parent.mkdir()
        skill_md.write_text(f"---\nname: bad-type\n{yaml_desc}\n---\n# Body\n")
        result = parse_skill_frontmatter(skill_md)
        assert result is None

    def test_file_not_readable(self, tmp_path):
        """Unreadable file returns None."""
        skill_md = tmp_path / "missing" / "SKILL.md"
        # Don't create the file — read_text will raise OSError
        result = parse_skill_frontmatter(skill_md)
        assert result is None


class TestLogSkillRegistrationSummary:
    """Tests for log_skill_registration_summary's branch logic.

    The summary line is the operator-facing signal for skill-system health,
    so the warning-vs-info gating (which feeds log-grep alerts) needs to
    behave deterministically across all four meaningful states.
    """

    @pytest.fixture
    def emit(self):
        from ha_mcp.utils.skill_loader import log_skill_registration_summary

        return log_skill_registration_summary

    def test_logs_info_when_all_phases_ok_and_guidance_present(self, emit, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="ha_mcp.utils.skill_loader"):
            emit({"provider": "ok", "tool": "ok", "guidance_count": 3})
        records = [r for r in caplog.records if "Skill system summary" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO

    def test_logs_warning_when_provider_failed(self, emit, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="ha_mcp.utils.skill_loader"):
            emit({"provider": "failed", "tool": "skipped", "guidance_count": 0})
        records = [r for r in caplog.records if "Skill system summary" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING

    def test_logs_warning_when_tool_failed(self, emit, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="ha_mcp.utils.skill_loader"):
            emit({"provider": "ok", "tool": "failed", "guidance_count": 0})
        records = [r for r in caplog.records if "Skill system summary" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING

    def test_logs_warning_when_both_skipped(self, emit, caplog):
        """`skipped` is not the same as `ok` — the summary must still warn."""
        import logging

        with caplog.at_level(logging.WARNING, logger="ha_mcp.utils.skill_loader"):
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

        with caplog.at_level(logging.WARNING, logger="ha_mcp.utils.skill_loader"):
            emit({"provider": "ok", "tool": "ok", "guidance_count": 0})
        records = [r for r in caplog.records if "Skill system summary" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING

    def test_missing_guidance_key_treated_as_zero(self, emit, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="ha_mcp.utils.skill_loader"):
            emit({"provider": "ok", "tool": "ok"})
        records = [r for r in caplog.records if "Skill system summary" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert "guidance_count=0" in records[0].getMessage()


class TestHandleSkillGuideCall:
    """Tests for the ``ha_get_skill_guide`` handler.

    Each request goes through the registered MCP tool on the real server.
    """

    @pytest.fixture
    def skills_dir(self, tmp_path):
        """A skills root holding the best-practices skill, plus a file
        outside the skill that no request may reach."""
        root = tmp_path / "skills"
        skill = root / BEST_PRACTICES_SKILL_NAME
        (skill / "references").mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {BEST_PRACTICES_SKILL_NAME}\n"
            "description: |\n"
            "  Best practices for HA tasks.\n"
            "---\n# Best practices\nWorkflow content here.\n"
        )
        (skill / "references" / "reference.md").write_text(
            "# Reference\nReal reference.\n"
        )
        (tmp_path / "outside.md").write_text("# Outside\nSECRET OUTSIDE\n")
        return root

    async def test_no_file_returns_skill_md(self, server, skills_dir):
        """A call with no arguments must deliver SKILL.md itself, so a
        model that makes one call gets the workflow and the table of
        reference files."""
        result = await _guide(server, skills_dir)
        assert result["success"] is True
        assert result["file"] == "SKILL.md"
        assert "Workflow content here." in result["content"]
        assert "file=" in result["how_to_use"]

    @pytest.mark.parametrize("file", ["../outside.md", "references"])
    async def test_file_outside_allowlist_is_refused(self, server, skills_dir, file):
        """Only the exact names of regular files in the skill are served.
        A traversal to a real file, or a directory name, must fail instead
        of reading from disk."""
        from ha_mcp._vendor.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as excinfo:
            await _guide(server, skills_dir, file)
        payload = str(excinfo.value)
        assert "SECRET OUTSIDE" not in payload
        assert "RESOURCE_NOT_FOUND" in payload
        # The error names the valid files so the model can retry.
        assert "references/reference.md" in payload

    async def test_symlink_in_skill_is_refused(self, server, skills_dir):
        """A symlink placed inside the skill must not be followed, even
        though its name is inside the skill directory."""
        from ha_mcp._vendor.fastmcp.exceptions import ToolError

        skill = skills_dir / BEST_PRACTICES_SKILL_NAME
        symlink_or_skip(skill / "evil.md", skills_dir.parent / "outside.md")
        with pytest.raises(ToolError) as excinfo:
            await _guide(server, skills_dir, "evil.md")
        assert "SECRET OUTSIDE" not in str(excinfo.value)

    async def test_symlinked_skill_dir_is_refused(self, server, tmp_path):
        """A skill folder that is itself a symlink must not be walked:
        every file under its target would become readable."""
        from ha_mcp._vendor.fastmcp.exceptions import ToolError

        target = tmp_path / "elsewhere"
        target.mkdir()
        (target / "SKILL.md").write_text("# Not the bundled skill\n")
        (target / "secret.txt").write_text("SECRET OUTSIDE\n")
        root = tmp_path / "skills"
        root.mkdir()
        symlink_or_skip(root / BEST_PRACTICES_SKILL_NAME, target)
        with pytest.raises(ToolError) as excinfo:
            await _guide(server, root, "secret.txt")
        assert "SECRET OUTSIDE" not in str(excinfo.value)

    @pytest.mark.parametrize("failure", ["permission", "invalid_utf8"])
    async def test_read_failure_raises_tool_error(
        self, server, skills_dir, monkeypatch, failure
    ):
        """A read failure must surface as a structured ToolError naming the
        file, not as a success payload or an unhandled exception."""
        from ha_mcp._vendor.fastmcp.exceptions import ToolError

        reference = skills_dir / BEST_PRACTICES_SKILL_NAME / "references"
        if failure == "invalid_utf8":
            (reference / "reference.md").write_bytes(b"# Ref \xff\xfe\n")
        else:
            original_read_text = Path.read_text

            def boom(self, *args, **kwargs):
                if self.name == "reference.md":
                    raise PermissionError("simulated EACCES")
                return original_read_text(self, *args, **kwargs)

            monkeypatch.setattr(Path, "read_text", boom)

        with pytest.raises(ToolError) as excinfo:
            await _guide(server, skills_dir, "references/reference.md")
        payload = str(excinfo.value)
        assert "reference.md" in payload
        assert "INTERNAL_ERROR" in payload

    async def test_optout_hint_only_on_reference_files(self, server, skills_dir):
        """The ``MandatoryBPS=false`` hint must not ride on SKILL.md.

        Every first call returns SKILL.md. A model that took the hint
        there would switch off the reference files the write tools
        attach, before it had read any of them.
        """
        skill_md = await _guide(server, skills_dir)
        reference = await _guide(server, skills_dir, "references/reference.md")
        assert "skill_content_hint" not in skill_md
        assert "MandatoryBPS=false" in reference["skill_content_hint"]

    @pytest.mark.parametrize(
        "state",
        [
            "no_skills_dir",
            "no_best_practices_skill",
            "bad_frontmatter",
            "undecodable_skill_md",
            "symlinked_skill_md",
            "unlistable_skill_dir",
        ],
    )
    async def test_unavailable_skill_raises_with_operator_fix(
        self, server, tmp_path, monkeypatch, state
    ):
        """Without a usable bundled skill, the call must fail with an error
        that names the fix, as the tool description then promises, not
        return content the model would trust."""
        from ha_mcp._vendor.fastmcp.exceptions import ToolError

        skills_dir = None if state == "no_skills_dir" else tmp_path / "skills"
        skill = tmp_path / "skills" / BEST_PRACTICES_SKILL_NAME
        skill.mkdir(parents=True)
        valid = "---\nname: bp\ndescription: Best practices.\n---\n# Body\n"
        if state == "bad_frontmatter":
            (skill / "SKILL.md").write_text("# No frontmatter here\n")
        elif state == "undecodable_skill_md":
            (skill / "SKILL.md").write_bytes(b"---\ndescription: \xff\xfe\n---\n")
        elif state == "symlinked_skill_md":
            # The file list skips symlinks, so the guide could never serve it.
            outside = tmp_path / "outside.md"
            outside.write_text(valid)
            symlink_or_skip(skill / "SKILL.md", outside)
        elif state == "unlistable_skill_dir":
            # SKILL.md is readable, but the walk that builds the allowlist
            # fails, so the guide could not serve even SKILL.md.
            (skill / "SKILL.md").write_text(valid)

            def fail_walk(self, pattern):
                raise PermissionError("simulated unlistable directory")

            monkeypatch.setattr(Path, "rglob", fail_walk)
        with pytest.raises(ToolError) as excinfo:
            await _guide(server, skills_dir)
        assert "submodule" in str(excinfo.value)


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

    def test_skill_guide_mandatory_tracks_strict_bps(self):
        """#1886: the skill guide is locked enabled only while strict
        best-practices mode is configured on (parent AND child flags) —
        it is the sole publisher of the strict-mode acknowledgment key.
        With strict mode off, local-skills users may disable the tool."""
        from ha_mcp.server import SKILL_TOOL_NAME
        from ha_mcp.settings_ui import (
            BPS_MANDATORY_TOOLS,
            MANDATORY_TOOLS,
            effective_mandatory_tools,
        )

        assert SKILL_TOOL_NAME in BPS_MANDATORY_TOOLS
        assert SKILL_TOOL_NAME not in MANDATORY_TOOLS

        strict = MagicMock(enable_mandatory_bps=True, enable_strict_mandatory_bps=True)
        non_strict = MagicMock(
            enable_mandatory_bps=True, enable_strict_mandatory_bps=False
        )
        parent_off = MagicMock(
            enable_mandatory_bps=False, enable_strict_mandatory_bps=True
        )
        assert SKILL_TOOL_NAME in effective_mandatory_tools(strict)
        assert SKILL_TOOL_NAME not in effective_mandatory_tools(non_strict)
        assert SKILL_TOOL_NAME not in effective_mandatory_tools(parent_off)

    def test_tool_name_fits_cloudflare_cap(self):
        """#1121: Cloudflare MCP portal rejects tool names > 40 chars."""
        from ha_mcp.server import SKILL_TOOL_NAME

        assert len(SKILL_TOOL_NAME) <= 40


@pytest.fixture
def populated_dir(tmp_path):
    skill = tmp_path / BEST_PRACTICES_SKILL_NAME
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        f"---\nname: {BEST_PRACTICES_SKILL_NAME}\ndescription: Best practices.\n---\n"
    )
    (skill / "reference.md").write_text("# Ref\n")
    return tmp_path


@pytest.mark.parametrize("available", [False, True])
async def test_skill_catalog_keeps_aliases_keywords_annotations_and_tags(
    server, populated_dir, available
):
    """Both healthy and degraded catalogs tell old clients where to go."""
    from ha_mcp.server_tool_text import SEARCH_KEYWORDS

    tool = await server(populated_dir if available else None).mcp.get_tool(
        SKILL_TOOL_NAME
    )
    assert tool is not None
    for old_name in (
        "ha_list_resources",
        "ha_read_resource",
        "ha_get_skill_home_assistant_best_practices",
    ):
        assert old_name in tool.description
        assert old_name in SEARCH_KEYWORDS[SKILL_TOOL_NAME]
    for anchor in (
        "Use BEFORE:",
        "creating or editing automations",
        "writing triggers",
        "ha_config_set_automation",
    ):
        assert anchor in tool.description
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.destructive_hint is not True
    assert tool.annotations.title == "Get Home Assistant Best Practices Skill Guide"
    assert "System" in tool.tags
    assert ("currently not available" in tool.description) is not available


@pytest.mark.parametrize("available", [False, True])
def test_registration_summary_reports_available_skill_count(
    server, populated_dir, available, caplog
):
    import logging

    with caplog.at_level(logging.INFO):
        server(populated_dir if available else None)
    summaries = [r for r in caplog.records if "Skill system summary" in r.message]
    assert len(summaries) == 1
    assert summaries[0].levelno == (logging.INFO if available else logging.WARNING)
    assert f"guidance_count={int(available)}" in summaries[0].message


async def test_skills_extension_lists_and_gets_the_bundled_skill(server, populated_dir):
    """A client using the SEP-2640 Skills extension finds no skills when the
    server does not advertise the extension or lists the skill wrongly."""
    from ha_mcp._vendor.fastmcp import Client
    from ha_mcp._vendor.fastmcp.skills.client import SkillsClientExtension

    uri = f"skill://{BEST_PRACTICES_SKILL_NAME}/SKILL.md"
    mcp = server(populated_dir).mcp
    async with Client(mcp, extensions=[SkillsClientExtension()]) as client:
        listed = await client.list_skills()
        fetched = await client.get_skill(uri)
    assert [skill.uri for skill in listed] == [uri]
    assert fetched.frontmatter.name == BEST_PRACTICES_SKILL_NAME
    assert isinstance(fetched.resources, list)
    assert {str(resource.uri) for resource in fetched.resources} == {
        uri,
        f"skill://{BEST_PRACTICES_SKILL_NAME}/reference.md",
    }


@pytest.mark.parametrize("failure", ["invalid_skill", "add"])
async def test_resource_provider_failure_keeps_guide_tool(
    server, populated_dir, monkeypatch, caplog, failure
):
    """A skills folder the provider rejects, or a provider that fails to
    register, must not take the skill guide tool down with it."""
    import logging

    from ha_mcp.http_transport import HttpTransportFastMCP

    if failure == "invalid_skill":
        stray = populated_dir / "stray-skill"
        stray.mkdir()
        (stray / "SKILL.md").write_text(
            "---\nname: other-name\ndescription: Name does not match.\n---\n"
        )
    else:
        from ha_mcp._vendor.fastmcp.skills.provider import SkillsDirectoryProvider

        original_add = HttpTransportFastMCP.add_provider

        def add_provider(self, provider, **kwargs):
            if isinstance(provider, SkillsDirectoryProvider):
                raise RuntimeError("simulated provider registration crash")
            return original_add(self, provider, **kwargs)

        monkeypatch.setattr(HttpTransportFastMCP, "add_provider", add_provider)
    with caplog.at_level(logging.WARNING):
        result = await _guide(server, populated_dir)
    assert result["success"] is True
    assert any(
        "Skill system summary" in rec.message and rec.levelno == logging.WARNING
        for rec in caplog.records
    )


def test_guide_registration_failure_is_reported_without_aborting_startup(
    server, populated_dir, monkeypatch, caplog
):
    import logging

    from ha_mcp.http_transport import HttpTransportFastMCP

    original_tool = HttpTransportFastMCP.tool

    def tool(self, *args, **kwargs):
        if kwargs.get("name") == SKILL_TOOL_NAME:
            raise RuntimeError("simulated tool registration crash")
        return original_tool(self, *args, **kwargs)

    monkeypatch.setattr(HttpTransportFastMCP, "tool", tool)
    with caplog.at_level(logging.WARNING):
        server(populated_dir)
    assert any(
        SKILL_TOOL_NAME in r.message and "Failed to register" in r.message
        for r in caplog.records
    )
    summaries = [r for r in caplog.records if "Skill system summary" in r.message]
    assert len(summaries) == 1
    assert summaries[0].levelno == logging.WARNING
