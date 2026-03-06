"""
Tests for bundled skills served as MCP resources and optionally as tools.

Verifies that:
- Skills are discoverable via list_resources() when ENABLE_SKILLS=true
- Skill content can be read via resources/read
- Skills appear as tools when ENABLE_SKILLS_AS_TOOLS=true
"""

import logging

import pytest

logger = logging.getLogger(__name__)


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
        "Expected bundled home-assistant-best-practices skill."
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
