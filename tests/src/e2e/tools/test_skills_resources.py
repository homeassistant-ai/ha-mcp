"""
Tests for bundled skills served as MCP resources and via ha_get_skill_guide.

Verifies that:
- Server instructions (bootstrap prompt) include skill guidance
- SKILL.md and the reference files are listed as skill:// resources
- ha_get_skill_guide returns SKILL.md with no arguments, and every
  reference file SKILL.md links by the path it links
- An installed ha-mcp serves the bundled SKILL.md over stdio (#1280)
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
    negotiation result contains skill blocks built from SKILL.md frontmatter.
    If instructions are None, skills failed to load silently — the exact
    regression from missing skills-vendor.
    """
    # Era-neutral: the 2026-07-28 protocol has no InitializeResult.
    instructions = mcp_client.instructions
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
    """Resource-capable clients must see SKILL.md and the reference files
    as skill:// resources. Reference files are listed only because the
    provider is registered with ``supporting_files="resources"``."""
    resources = await mcp_client.list_resources()
    uris = [str(r.uri) for r in resources if str(r.uri).startswith("skill://")]
    base = f"skill://{EXPECTED_BUNDLED_SKILL}/"
    assert f"{base}SKILL.md" in uris, (
        f"SKILL.md not listed. Found: {uris}. " + SKILLS_MISSING_HINT
    )
    assert any(u.startswith(f"{base}references/") for u in uris), (
        f"No reference files listed. Found: {uris}"
    )


@pytest.mark.asyncio
async def test_skill_guide_every_linked_reference_is_readable(mcp_client):
    """A model reads SKILL.md, then passes a path from its table as
    ``file``. Every relative link in the bundled SKILL.md must be a path
    the tool serves, or the model hits an error following the guide."""
    import json
    import re

    result = await mcp_client.call_tool(SKILL_TOOL_NAME, {})
    data = json.loads(_payload(result))
    assert data["file"] == "SKILL.md"
    links = set(re.findall(r"\]\((references/[^)#\s]+)", data["content"]))
    assert links, f"No reference links found in SKILL.md: {data['content'][:400]}"

    for link in sorted(links):
        linked = json.loads(
            _payload(await mcp_client.call_tool(SKILL_TOOL_NAME, {"file": link}))
        )
        assert linked["content"], f"{link} returned no content"


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
async def test_stdio_skill_guide_returns_bundled_skill_md(stdio_mcp_client):
    """The no-argument call over real stdio must return the exact on-disk
    SKILL.md from the location production code resolves to.

    This is the #1280 regression test: the subprocess imports ``ha_mcp``
    from the test venv (an editable install in CI, a wheel for real
    users), so a wheel built without the skills-vendor submodule returns
    an error here instead of the file. Comparing byte-for-byte against
    the source tree also catches a subprocess reading a different
    location or a wheel whose bundled file diverges.
    """
    import json

    # The canonical on-disk SKILL.md (same source production reads).
    on_disk_skill_md = _SOURCE_TREE_SKILLS_DIR / EXPECTED_BUNDLED_SKILL / "SKILL.md"
    assert on_disk_skill_md.is_file(), (
        f"Source-tree SKILL.md missing at {on_disk_skill_md}. "
        f"Run `git submodule update --init`."
    )
    expected_content = on_disk_skill_md.read_text(encoding="utf-8")

    result = await stdio_mcp_client.call_tool(SKILL_TOOL_NAME, {})
    payload = _payload(result)
    data = json.loads(payload)

    assert data.get("success") is True, (
        f"stdio response not success-flagged: {payload[:400]}"
    )
    assert "content" in data, f"stdio response missing 'content' key: {payload[:400]}"
    assert data["content"] == expected_content, (
        "stdio content does NOT match the on-disk source: the "
        "subprocess is reading from a different location than the "
        "test process.\n"
        f"  on-disk path: {on_disk_skill_md}\n"
        f"  on-disk length: {len(expected_content)}\n"
        f"  subprocess length: {len(data['content'])}\n"
        f"  on-disk first 200: {expected_content[:200]!r}\n"
        f"  subprocess first 200: {data['content'][:200]!r}"
    )
