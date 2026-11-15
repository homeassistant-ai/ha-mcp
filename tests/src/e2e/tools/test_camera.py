"""E2E tests for the ha_get_camera_image MCP tool against a real Home Assistant instance.

The camera tool returns the snapshot image directly to the LLM, plus a short
text block stating the served snapshot's size and retrieval time (Home
Assistant local time with UTC offset). The info text never contains Home
Assistant entity data, so it is safe under both hidden and open
``llm_exposure`` modes.

The demo integration's fake cameras serve static JPEG/PNG assets with real
headers, so the info text reports the assets' actual dimensions.
"""

import re

import pytest

from ..utilities.assertions import MCPAssertions, assert_search_results

# The info text: format, served dimensions, and the retrieval timestamp in
# HA local time with UTC offset. The size is MANDATORY, not optional: the
# demo cameras always serve real image files with parseable headers, so a
# missing size means the parser failed on genuine encoder output. This is
# the one test that exercises the dimension parsers for real — the unit
# fixtures are hand-built.
INFO_TEXT_RE = re.compile(
    r"Camera snapshot \((JPEG|PNG|GIF), \d+x\d+\)\. Retrieved: "
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{2}:\d{2}"
)

# Image entities are labelled "Image" rather than "Camera snapshot".
IMAGE_INFO_TEXT_RE = re.compile(
    r"Image \((JPEG|PNG|GIF), \d+x\d+\)\. Retrieved: "
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{2}:\d{2}"
)


def _blocks_of_type(result, block_type: str) -> list:
    """Content blocks of a raw CallToolResult matching *block_type*."""
    return [block for block in result.content if block.type == block_type]


async def _first_camera_entity(mcp: MCPAssertions) -> str:
    """Pick one live camera entity, asserting the search succeeds."""
    search_data = await mcp.call_tool_success("ha_search", {"domain_filter": "camera"})
    assert_search_results(search_data, min_results=1, domain_filter="camera")
    return search_data["entities"][0]["entity_id"]


class TestCameraToolsE2E:
    """E2E tests for camera tools (require running Home Assistant)."""

    @pytest.mark.asyncio
    async def test_camera_tools_registered(self, mcp_client):
        """Camera tools should be registered in the MCP client."""
        tools = await mcp_client.list_tools()
        tool_names = {tool.name for tool in tools}

        assert "ha_get_camera_image" in tool_names, (
            "ha_get_camera_image tool should be registered"
        )

    @pytest.mark.asyncio
    async def test_non_camera_entity_rejected(self, mcp_client):
        """Non-camera entities should be rejected with a clear error."""
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_failure(
                "ha_get_camera_image",
                {"entity_id": "light.kitchen"},
                expected_error="not a camera or image entity",
            )

    @pytest.mark.asyncio
    async def test_image_entity_rejects_dimensions(self, mcp_client):
        """width/height are refused for image.* entities before any request."""
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_failure(
                "ha_get_camera_image",
                {"entity_id": "image.anything", "width": 320, "height": 240},
                expected_error="apply only to camera entities",
            )

    @pytest.mark.asyncio
    async def test_unknown_image_entity_reports_not_found(self, mcp_client):
        """An image.* entity Home Assistant does not know maps to a not-found error."""
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_failure(
                "ha_get_camera_image",
                {"entity_id": "image.does_not_exist_e2e"},
                expected_error="not found",
            )

    @pytest.mark.asyncio
    async def test_get_image_entity_returns_image(self, mcp_client):
        """The seeded kitchen_sink image entity is served through Core's image proxy.

        kitchen_sink (loaded by the E2E configuration) ships a real image
        entity serving a PNG, so this exercises the genuine ``image_proxy``
        route end to end.
        """
        result = await mcp_client.call_tool(
            "ha_get_camera_image", {"entity_id": "image.qr_code"}
        )

        assert [block.type for block in result.content] == ["text", "image"]
        assert re.fullmatch(IMAGE_INFO_TEXT_RE, result.content[0].text), (
            f"Unexpected info text format: {result.content[0].text!r}"
        )
        assert result.content[1].data, "Image data should not be empty"
        assert result.content[1].mime_type == "image/png"

    @pytest.mark.asyncio
    async def test_get_camera_image_returns_image_and_info(self, mcp_client):
        """A live camera snapshot should return an image plus the info text block.

        The demo cameras serve static JPEG/PNG assets with real headers, so
        the info text must name the format, the served dimensions, and the
        retrieval time.
        """
        async with MCPAssertions(mcp_client) as mcp:
            camera_entity = await _first_camera_entity(mcp)

            # The info text is plain (non-JSON) text, so the shared parsing
            # helper can only surface it as ``raw_response`` — and the image
            # block cannot be surfaced through it at all. Verify both blocks
            # on the raw CallToolResult instead (as the other direct-call
            # e2e tests do).
            result = await mcp_client.call_tool(
                "ha_get_camera_image", {"entity_id": camera_entity}
            )

        # Wire shape: the tuple return must map to exactly these content
        # blocks, in this order, with no structuredContent — a
        # schema-inferring annotation would add one and duplicate the
        # (potentially large) base64 image payload in the result envelope.
        block_types = [block.type for block in result.content]
        assert block_types == ["text", "image"], (
            f"Expected content blocks [text, image], got {block_types}"
        )
        assert result.structured_content is None, (
            "A structured result would duplicate the image payload"
        )

        info_text = result.content[0].text
        assert INFO_TEXT_RE.fullmatch(info_text), (
            f"Unexpected info text format: {info_text!r}"
        )

        image_block = result.content[1]
        assert image_block.data, "Image data should not be empty"
        assert image_block.mime_type.startswith("image/"), (
            f"Expected an image MIME type, got: {image_block.mime_type}"
        )

    @pytest.mark.asyncio
    async def test_get_camera_image_with_resize(self, mcp_client):
        """A resize request should return both blocks; the size reflects the
        image as actually served (Home Assistant may or may not rescale)."""
        async with MCPAssertions(mcp_client) as mcp:
            camera_entity = await _first_camera_entity(mcp)

            result = await mcp_client.call_tool(
                "ha_get_camera_image",
                {"entity_id": camera_entity, "width": 640, "height": 480},
            )

        image_blocks = _blocks_of_type(result, "image")
        text_blocks = _blocks_of_type(result, "text")

        assert len(image_blocks) == 1, "Expected exactly one image block"
        assert image_blocks[0].data, "Image data should not be empty"

        assert len(text_blocks) == 1, "Expected exactly one text block"
        info_text = text_blocks[0].text
        # The served size may differ from the requested one: the text must
        # still be well-formed and truthful about what was delivered.
        assert INFO_TEXT_RE.fullmatch(info_text), (
            f"Unexpected info text format: {info_text!r}"
        )
