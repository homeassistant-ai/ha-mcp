"""
Tests for bundled skills served as MCP resources and as tools.

Verifies that:
- Skills are discoverable via list_resources()
- Skill content can be read via resources/read
- Skills appear as ha_list_resources / ha_read_resource tools
- Server instructions (bootstrap prompt) include skill guidance
"""

import logging

import pytest

logger = logging.getLogger(__name__)

SKILLS_MISSING_HINT = (
    "Skills directory not found. Ensure the git submodule at "
    "src/ha_mcp/resources/skills-vendor/ is initialized "
    "(git submodule update --init). CI workflows use submodules: true "
    "in the checkout step to handle this automatically."
)


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
    assert "skill://" in instructions, (
        "Server instructions missing skill:// URIs"
    )
    logger.info(
        f"Server instructions present ({len(instructions)} chars), "
        f"contains skill guidance"
    )


@pytest.mark.asyncio
async def test_skills_resources_listed(mcp_client):
    """Test that bundled skills appear in list_resources()."""
    logger.info("Testing skills resource discovery")

    resources = await mcp_client.list_resources()
    assert resources is not None, "list_resources() returned None"

    # Find skill:// resources
    skill_resources = [r for r in resources if str(r.uri).startswith("skill://")]
    assert len(skill_resources) > 0, (
        "No skill:// resources found. "
        "Expected bundled home-assistant-best-practices skill. "
        + SKILLS_MISSING_HINT
    )

    # Verify the main SKILL.md resource exists
    skill_uris = [str(r.uri) for r in skill_resources]
    skill_md_found = any("SKILL.md" in uri for uri in skill_uris)
    assert skill_md_found, (
        f"SKILL.md not found in skill resources. Found: {skill_uris}"
    )

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
    assert "home assistant" in content_text.lower() or "Home Assistant" in content_text, (
        "SKILL.md should reference Home Assistant"
    )

    logger.info(f"Successfully read SKILL.md ({len(content_text)} chars)")


@pytest.mark.asyncio
async def test_skills_reference_files_readable(mcp_client):
    """Test that skill reference files are reachable via resources/read."""
    logger.info("Testing skill reference file access")

    resources = await mcp_client.list_resources()
    skill_resources = [r for r in resources if str(r.uri).startswith("skill://")]

    # Find reference file resources (anything that's not SKILL.md itself)
    reference_resources = [
        r for r in skill_resources
        if "SKILL.md" not in str(r.uri)
    ]
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
async def test_skills_as_tools_use_ha_prefix(mcp_client):
    """The ResourcesAsTools transform pair must use the ha_<verb>_<noun>
    naming convention used everywhere else in the catalog."""
    tools = await mcp_client.list_tools()
    names = {t.name for t in tools}

    assert "ha_list_resources" in names, (
        f"ha_list_resources missing from tool list. Got: {sorted(names)[:25]}"
    )
    assert "ha_read_resource" in names, (
        f"ha_read_resource missing from tool list. Got: {sorted(names)[:25]}"
    )
    # The unprefixed FastMCP defaults must not leak through.
    assert "list_resources" not in names, (
        "Unprefixed list_resources is still registered — HaResourcesAsTools "
        "rename did not apply."
    )
    assert "read_resource" not in names, (
        "Unprefixed read_resource is still registered — HaResourcesAsTools "
        "rename did not apply."
    )


@pytest.mark.asyncio
async def test_ha_list_resources_invocation(mcp_client):
    """Calling ha_list_resources end-to-end must dispatch to the underlying
    FastMCP-built handler and return the bundled skill resources. Catalog
    presence (test above) is necessary but not sufficient — this proves
    the rename doesn't break invocation routing."""
    result = await mcp_client.call_tool("ha_list_resources", {})

    # The FastMCP handler returns a JSON string of resource metadata.
    payload = result.content[0].text if hasattr(result, "content") else str(result)
    assert "skill://" in payload, (
        f"ha_list_resources output should include skill:// URIs from the "
        f"bundled provider. Got: {payload[:300]}"
    )


@pytest.mark.asyncio
async def test_ha_read_resource_invocation(mcp_client):
    """Calling ha_read_resource end-to-end must accept a skill:// URI and
    return the resource contents."""
    # Find a real skill URI to read.
    resources = await mcp_client.list_resources()
    skill_md = next(
        (r for r in resources if str(r.uri).endswith("SKILL.md")), None
    )
    assert skill_md is not None, "No SKILL.md resource available to read"

    result = await mcp_client.call_tool(
        "ha_read_resource", {"uri": str(skill_md.uri)}
    )
    payload = result.content[0].text if hasattr(result, "content") else str(result)
    assert len(payload) > 100, (
        f"ha_read_resource should return non-trivial content for {skill_md.uri}"
    )
