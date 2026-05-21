"""
Tests for bundled skills served as MCP resources and via the polymorphic tool.

Verifies that:
- Skills are discoverable via list_resources()
- Skill content can be read via resources/read
- The ha_get_skill_guide tool exposes skills in three tiers
  (no args → list skills; skill arg → list files; skill+file → read content)
- Server instructions (bootstrap prompt) include skill guidance
- The pre-consolidation ha_list_resources / ha_read_resource pair and the
  per-skill ha_get_skill_<name> tools are no longer registered (#1134)
"""

import logging
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)

SKILL_TOOL_NAME = "ha_get_skill_guide"
EXPECTED_BUNDLED_SKILL = "home-assistant-best-practices"

# Source-tree skills directory — the same path production code resolves
# at runtime via `_get_skills_dir()`. Computed at module import so async
# tests can reference it without tripping ASYNC240. This is intentionally
# NOT a tmp_path or test fixture: the stdio regression tests below need
# to compare what the subprocess returns against the REAL on-disk source
# the production code reads from.
_SOURCE_TREE_SKILLS_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "ha_mcp"
    / "resources"
    / "skills-vendor"
    / "skills"
)

SKILLS_MISSING_HINT = (
    "Skills directory not found. Ensure the git submodule at "
    "src/ha_mcp/resources/skills-vendor/ is initialized "
    "(git submodule update --init). CI workflows use submodules: true "
    "in the checkout step to handle this automatically."
)


def _payload(result):
    """Extract the text payload from a tool call result."""
    return result.content[0].text if hasattr(result, "content") else str(result)


@pytest.mark.asyncio
async def test_skills_bootstrap_instructions(mcp_client):
    """Test that MCP server instructions contain skill guidance (bootstrap prompt).

    Verifies the observable behavior: the instructions field in the MCP
    InitializeResult contains skill blocks built from SKILL.md frontmatter.
    If instructions are None, skills failed to load silently — the exact
    regression from missing skills-vendor.
    """
    result = mcp_client.initialize_result
    assert result is not None, "MCP client has no InitializeResult"
    instructions = result.instructions
    assert instructions is not None, (
        "Server instructions are None — skills were not loaded. " + SKILLS_MISSING_HINT
    )
    assert "IMPORTANT" in instructions, (
        "Server instructions missing IMPORTANT header from skills"
    )
    assert "skill://" in instructions, "Server instructions missing skill:// URIs"
    assert SKILL_TOOL_NAME in instructions, (
        f"Server instructions missing {SKILL_TOOL_NAME} fallback reference"
    )
    logger.info(
        f"Server instructions present ({len(instructions)} chars), "
        f"contains skill guidance"
    )


@pytest.mark.asyncio
async def test_skills_resources_listed(mcp_client):
    """Test that bundled skills appear in list_resources().

    SkillsDirectoryProvider stayed registered after the #1134
    consolidation — resource-capable clients still get skill:// URIs.
    """
    logger.info("Testing skills resource discovery")

    resources = await mcp_client.list_resources()
    assert resources is not None, "list_resources() returned None"

    # Find skill:// resources
    skill_resources = [r for r in resources if str(r.uri).startswith("skill://")]
    assert len(skill_resources) > 0, (
        "No skill:// resources found. "
        "Expected bundled home-assistant-best-practices skill. " + SKILLS_MISSING_HINT
    )

    # Verify the main SKILL.md resource exists
    skill_uris = [str(r.uri) for r in skill_resources]
    skill_md_found = any("SKILL.md" in uri for uri in skill_uris)
    assert skill_md_found, f"SKILL.md not found in skill resources. Found: {skill_uris}"

    logger.info(f"Found {len(skill_resources)} skill resources: {skill_uris}")


@pytest.mark.asyncio
async def test_skills_resource_readable(mcp_client):
    """Test that skill content can be read via resources/read."""
    logger.info("Testing skill resource content retrieval")

    resources = await mcp_client.list_resources()
    skill_resources = [r for r in resources if str(r.uri).startswith("skill://")]
    assert len(skill_resources) > 0, "No skill resources to read"

    # Find the SKILL.md resource
    skill_md = next(
        (r for r in skill_resources if "SKILL.md" in str(r.uri)),
        None,
    )
    assert skill_md is not None, "SKILL.md resource not found"

    # Read the resource content
    content = await mcp_client.read_resource(skill_md.uri)
    assert content is not None, "read_resource returned None"

    # Content should be non-empty and contain expected markers
    content_text = str(content)
    assert len(content_text) > 100, "SKILL.md content too short"
    assert (
        "home assistant" in content_text.lower() or "Home Assistant" in content_text
    ), "SKILL.md should reference Home Assistant"

    logger.info(f"Successfully read SKILL.md ({len(content_text)} chars)")


@pytest.mark.asyncio
async def test_skills_reference_files_readable(mcp_client):
    """Test that skill reference files are reachable via resources/read."""
    logger.info("Testing skill reference file access")

    resources = await mcp_client.list_resources()
    skill_resources = [r for r in resources if str(r.uri).startswith("skill://")]

    # Find reference file resources (anything that's not SKILL.md itself)
    reference_resources = [r for r in skill_resources if "SKILL.md" not in str(r.uri)]
    assert len(reference_resources) > 0, (
        "No reference file resources found. "
        "SkillsDirectoryProvider should expose reference files."
    )

    # Read the first reference file to verify accessibility
    ref = reference_resources[0]
    content = await mcp_client.read_resource(ref.uri)
    assert content is not None, f"read_resource returned None for {ref.uri}"
    assert len(str(content)) > 0, f"Reference file {ref.uri} is empty"

    logger.info(
        f"Found {len(reference_resources)} reference resources, "
        f"verified {ref.uri} is readable"
    )


@pytest.mark.asyncio
async def test_skill_guide_tool_registered(mcp_client):
    """The polymorphic ha_get_skill_guide tool replaces the prior trio.

    Verifies both presence of the new tool AND absence of the old ones
    (#1134 consolidation). Without the negative assertions a regression
    that re-adds the old transforms would slip past.
    """
    tools = await mcp_client.list_tools()
    names = {t.name for t in tools}

    assert SKILL_TOOL_NAME in names, (
        f"{SKILL_TOOL_NAME} missing from tool list. Got: {sorted(names)[:25]}"
    )
    # Name must stay <= 40 chars for Cloudflare's MCP portal (#1121).
    assert len(SKILL_TOOL_NAME) <= 40, (
        f"{SKILL_TOOL_NAME} is {len(SKILL_TOOL_NAME)} chars; must be <= 40"
    )
    # Pre-consolidation tools must not leak through.
    assert "ha_list_resources" not in names
    assert "ha_read_resource" not in names
    assert "list_resources" not in names
    assert "read_resource" not in names
    # Per-skill guidance tools are gone too.
    assert not any(
        n.startswith("ha_get_skill_") and n != SKILL_TOOL_NAME for n in names
    ), (
        "Per-skill guidance tools should be consolidated into "
        f"{SKILL_TOOL_NAME}. Stragglers: "
        f"{sorted(n for n in names if n.startswith('ha_get_skill_'))}"
    )


@pytest.mark.asyncio
async def test_skill_guide_tier1_lists_skills(mcp_client):
    """Calling ha_get_skill_guide with no args lists bundled skills."""
    result = await mcp_client.call_tool(SKILL_TOOL_NAME, {})
    payload = _payload(result)
    assert EXPECTED_BUNDLED_SKILL in payload, (
        f"No-args call should list bundled skill {EXPECTED_BUNDLED_SKILL!r}. "
        f"Got: {payload[:400]}"
    )
    assert "skill://" in payload, (
        "Tier 1 listing should include skill:// URIs for cross-referencing "
        f"with resources/read. Got: {payload[:400]}"
    )


@pytest.mark.asyncio
async def test_skill_guide_tier2_lists_files(mcp_client):
    """Calling with skill arg lists reference files for that skill."""
    result = await mcp_client.call_tool(
        SKILL_TOOL_NAME, {"skill": EXPECTED_BUNDLED_SKILL}
    )
    payload = _payload(result)
    assert "SKILL.md" in payload, (
        f"Tier 2 listing should include SKILL.md. Got: {payload[:400]}"
    )
    assert "files" in payload, (
        f"Tier 2 response should contain a 'files' key. Got: {payload[:400]}"
    )


@pytest.mark.asyncio
async def test_skill_guide_tier3_reads_content(mcp_client):
    """Calling with skill + file args returns the file content."""
    result = await mcp_client.call_tool(
        SKILL_TOOL_NAME,
        {"skill": EXPECTED_BUNDLED_SKILL, "file": "SKILL.md"},
    )
    payload = _payload(result)
    assert len(payload) > 200, (
        f"Tier 3 read should return non-trivial content. Got: {payload[:200]}"
    )
    assert "content" in payload, (
        f"Tier 3 response should contain a 'content' key. Got: {payload[:400]}"
    )


@pytest.mark.asyncio
async def test_skill_guide_rejects_unknown_skill(mcp_client):
    """Unknown skill names raise a ToolError, not return a silent empty dict."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        await mcp_client.call_tool(SKILL_TOOL_NAME, {"skill": "does-not-exist"})


@pytest.mark.asyncio
async def test_skill_guide_rejects_path_traversal(mcp_client):
    """Path-traversal in either arg must raise rather than escape the skills dir."""
    from fastmcp.exceptions import ToolError

    # Traversal in the skill arg.
    with pytest.raises(ToolError):
        await mcp_client.call_tool(SKILL_TOOL_NAME, {"skill": "../../etc"})

    # Traversal in the file arg (a valid skill name, a malicious file path).
    with pytest.raises(ToolError):
        await mcp_client.call_tool(
            SKILL_TOOL_NAME,
            {"skill": EXPECTED_BUNDLED_SKILL, "file": "../../../etc/passwd"},
        )


# ---------------------------------------------------------------------------
# Stdio-transport coverage
#
# Everything above this line uses the default ``mcp_client`` fixture, which
# is an in-memory transport — same dispatch code as production, but bypasses
# subprocess startup, JSON serialization framing, and the installed-wheel
# side of the contract. These tests are the only ones that validate the
# transport real users hit (Claude Desktop, claude CLI, uvx, Docker stdio).
#
# #1280 was exactly the class of bug the in-memory tests can't catch: the
# skills-vendor submodule wasn't packaged into the stable PyPI wheel, so
# stdio installs saw the tool surface but with no bundled skills behind
# it. The in-memory transport reads files from the source tree directly
# and could never reproduce it. These stdio tests close that gap.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdio_skill_guide_tool_registered(stdio_mcp_client):
    """Spawning ha-mcp as a subprocess and listing tools must include the
    skill guide. Catches packaging regressions (missing entry point,
    broken module discovery) and subprocess startup hangs."""
    tools = await stdio_mcp_client.list_tools()
    names = {t.name for t in tools}
    assert SKILL_TOOL_NAME in names, (
        f"{SKILL_TOOL_NAME} missing from stdio subprocess catalog — "
        f"the wheel may not be packaging the tool correctly. "
        f"Got first 25: {sorted(names)[:25]}"
    )


@pytest.mark.asyncio
async def test_stdio_skill_guide_tier1_lists_bundled_skill(stdio_mcp_client):
    """Tier-1 call over real stdio JSON-RPC must surface the bundled skill
    from the SAME location production code resolves to.

    This is the #1280 regression test in spirit. The subprocess imports
    ``ha_mcp`` from the test venv's site-packages (an editable install in
    CI, a real wheel install in a real user's environment). In both
    cases ``_get_skills_dir()`` resolves to
    ``<ha_mcp package>/resources/skills-vendor/skills/`` — no tmp_path
    monkey-patching, no test fixture directory. If the bundled skill
    files are missing from that real location (e.g., the submodule
    wasn't initialized, or the wheel was built without it), this test
    fails loudly.

    Strengthened assertion: the set of skills the subprocess reports
    must EXACTLY equal the set of skill directories visible on disk in
    the source-tree submodule that the test process can see. A drift
    here means either the subprocess is reading a different location
    than expected (mock/test fixture), or the source-tree submodule
    isn't initialized — both of which would invalidate this regression
    test's premise.
    """
    import json

    # The source-tree skills dir is computed at module level
    # (_SOURCE_TREE_SKILLS_DIR). Check it's populated; the subprocess
    # MUST be reading from this same on-disk location.
    assert _SOURCE_TREE_SKILLS_DIR.is_dir(), (
        f"Source-tree skills dir missing at {_SOURCE_TREE_SKILLS_DIR}. "
        f"Run `git submodule update --init` and re-run the test."
    )
    on_disk_skills = {
        d.name
        for d in _SOURCE_TREE_SKILLS_DIR.iterdir()
        if d.is_dir() and (d / "SKILL.md").exists()
    }
    assert on_disk_skills, (
        f"Source-tree skills dir {_SOURCE_TREE_SKILLS_DIR} contains no "
        f"valid skill subdirectories — submodule may be empty."
    )

    result = await stdio_mcp_client.call_tool(SKILL_TOOL_NAME, {})
    payload = _payload(result)

    # Degraded flag MUST be absent in a healthy install. If it's set,
    # the subprocess couldn't find the skills directory at all — wheel
    # packaging regression (#1280) or submodule-not-initialized.
    assert '"degraded": true' not in payload.lower(), (
        f"Tier 1 reports degraded: True on stdio — the subprocess can't "
        f"find {_SOURCE_TREE_SKILLS_DIR}. Wheel packaging regression "
        f"(see #1280) or uninitialized submodule. Got: {payload[:400]}"
    )

    # Parse and compare: the subprocess's reported skills must match the
    # on-disk set the test process sees. Equal sets prove the subprocess
    # is reading from the production location, not a mock.
    # Payload is JSON inside a TextContent; tolerate either raw dict or
    # JSON-string-of-dict.
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        # Fall back to a permissive substring check if the test runner
        # wraps the payload unexpectedly.
        for name in on_disk_skills:
            assert name in payload, (
                f"On-disk skill {name!r} missing from stdio tier-1 payload "
                f"(JSON parse failed too). Got: {payload[:400]}"
            )
        return

    subprocess_skills = {s["skill"] for s in data.get("skills", [])}
    assert subprocess_skills == on_disk_skills, (
        "Subprocess reports a different set of bundled skills than the "
        "source-tree submodule:\n"
        f"  on-disk in {_SOURCE_TREE_SKILLS_DIR}: {sorted(on_disk_skills)}\n"
        f"  reported by ha-mcp subprocess: {sorted(subprocess_skills)}\n"
        "Possible causes: (a) wheel packaging dropped some skill dirs "
        "(#1280-class regression), (b) the subprocess is reading from a "
        "different ha_mcp installation than the test venv, (c) a stale "
        "submodule on disk. Investigate before merging."
    )
    # Belt-and-suspenders: the canonical bundled skill must be present.
    assert EXPECTED_BUNDLED_SKILL in subprocess_skills, (
        f"{EXPECTED_BUNDLED_SKILL!r} (canonical bundled skill) missing "
        f"from subprocess tier-1 list: {sorted(subprocess_skills)}"
    )


@pytest.mark.asyncio
async def test_stdio_skill_guide_tier3_reads_large_payload(stdio_mcp_client):
    """Tier-3 call over real stdio must return the exact on-disk content.

    SKILL.md is several KB. The in-memory transport doesn't exercise
    JSON framing or pipe buffering at all; this call proves the full
    serialization + transport pipeline handles a real-world response
    size without truncation, framing errors, or buffer-related hangs.

    Strengthened: rather than checking just for the ``content`` key, we
    independently read the on-disk SKILL.md and assert it matches what
    the subprocess returns byte-for-byte. This catches three classes of
    bug at once: (1) the subprocess is reading from a different
    location than the test process (mock/test fixture), (2) JSON
    serialization is silently dropping or corrupting bytes, (3) the
    bundled file in the wheel diverges from the source tree.
    """
    import json

    # The canonical on-disk SKILL.md (same source production reads).
    on_disk_skill_md = _SOURCE_TREE_SKILLS_DIR / EXPECTED_BUNDLED_SKILL / "SKILL.md"
    assert on_disk_skill_md.is_file(), (
        f"Source-tree SKILL.md missing at {on_disk_skill_md}. "
        f"Run `git submodule update --init`."
    )
    expected_content = on_disk_skill_md.read_text(encoding="utf-8")

    result = await stdio_mcp_client.call_tool(
        SKILL_TOOL_NAME,
        {"skill": EXPECTED_BUNDLED_SKILL, "file": "SKILL.md"},
    )
    payload = _payload(result)
    data = json.loads(payload)

    assert data.get("success") is True, (
        f"Tier 3 stdio response not success-flagged: {payload[:400]}"
    )
    assert "content" in data, (
        f"Tier 3 stdio response missing 'content' key: {payload[:400]}"
    )
    assert data["content"] == expected_content, (
        "Tier 3 stdio content does NOT match the on-disk source — the "
        "subprocess is either reading from a different location than the "
        "test process, or JSON serialization is corrupting bytes.\n"
        f"  on-disk path: {on_disk_skill_md}\n"
        f"  on-disk length: {len(expected_content)}\n"
        f"  subprocess length: {len(data['content'])}\n"
        f"  on-disk first 200: {expected_content[:200]!r}\n"
        f"  subprocess first 200: {data['content'][:200]!r}"
    )
