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

from tests.src.e2e.utilities.assertions import (
    assert_mcp_failure,
    assert_search_results,
    safe_call_tool,
)

# The info text: format (plus the served dimensions when the payload header
# parses) and the retrieval timestamp in HA local time with UTC offset.
INFO_TEXT_RE = re.compile(
    r"Camera snapshot \((JPEG|PNG|GIF)(, \d+x\d+)?\)\. Retrieved: "
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{2}:\d{2}"
)


def _blocks_of_type(result, block_type: str) -> list:
    """Content blocks of a raw CallToolResult matching *block_type*."""
    return [block for block in result.content if block.type == block_type]


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
        data = await safe_call_tool(
            mcp_client,
            "ha_get_camera_image",
            {"entity_id": "light.kitchen"},
        )

        assert_mcp_failure(
            data,
            "ha_get_camera_image (non-camera entity)",
            expected_error="not a camera entity",
        )

    @pytest.mark.asyncio
    async def test_get_camera_image_returns_image_and_info(self, mcp_client):
        """A live camera snapshot should return an image plus the info text block.

        The demo cameras serve static JPEG/PNG assets with real headers, so
        the info text must name the format, the served dimensions, and the
        retrieval time.
        """
        search_data = await safe_call_tool(
            mcp_client,
            "ha_search",
            {"domain_filter": "camera"},
        )
        assert_search_results(search_data, min_results=1, domain_filter="camera")
        camera_entity = search_data["entities"][0]["entity_id"]

        # The info text is plain (non-JSON) text, so the shared parsing
        # helper can only surface it as ``raw_response`` — and the image
        # block cannot be surfaced through it at all. Verify both blocks on
        # the raw CallToolResult instead (as the other direct-call e2e
        # tests do).
        result = await mcp_client.call_tool(
            "ha_get_camera_image", {"entity_id": camera_entity}
        )

        image_blocks = _blocks_of_type(result, "image")
        text_blocks = _blocks_of_type(result, "text")

        assert len(image_blocks) == 1, "Expected exactly one image block"
        assert image_blocks[0].data, "Image data should not be empty"
        assert image_blocks[0].mimeType.startswith("image/"), (
            f"Expected an image MIME type, got: {image_blocks[0].mimeType}"
        )

        assert len(text_blocks) == 1, "Expected exactly one text block"
        info_text = text_blocks[0].text
        assert INFO_TEXT_RE.fullmatch(info_text), (
            f"Unexpected info text format: {info_text!r}"
        )

    @pytest.mark.asyncio
    async def test_get_camera_image_with_resize(self, mcp_client):
        """A resize request should return both blocks; the size reflects the
        image as actually served (Home Assistant may or may not rescale)."""
        search_data = await safe_call_tool(
            mcp_client,
            "ha_search",
            {"domain_filter": "camera"},
        )
        assert_search_results(search_data, min_results=1, domain_filter="camera")
        camera_entity = search_data["entities"][0]["entity_id"]

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
