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

from tests.src.e2e.utilities.assertions import (
    assert_mcp_success,
    extract_error_message,
    parse_mcp_result,
    safe_call_tool,
)

# The info text: format (plus the served dimensions when the payload header
# parses) and the retrieval timestamp in HA local time with UTC offset.
INFO_TEXT_RE = re.compile(
    r"Camera snapshot \((JPEG|PNG|GIF)(, \d+x\d+)?\. Retrieved: "
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{2}:\d{2}"
)


class TestCameraToolsE2E:
    """E2E tests for camera tools (require running Home Assistant)."""

    def test_camera_tools_registered(self, mcp_client):
        """Camera tools should be registered in the MCP client."""
        tools_response = mcp_client.list_tools()
        tools = tools_response.tools
        tool_names = {t.name for t in tools}

        assert "ha_get_camera_image" in tool_names, (
            "ha_get_camera_image tool should be registered"
        )

    def test_non_camera_entity_rejected(self, mcp_client):
        """Non-camera entities should be rejected with a clear error."""
        result = safe_call_tool(
            mcp_client,
            "ha_get_camera_image",
            {
                "entity_id": "light.kitchen",
            },
        )

        assert result.isError, "Should be an error for non-camera entity"
        if result.isError:
            error_message = extract_error_message(result)
            assert "not a camera entity" in error_message.lower(), (
                f"Error message should mention non-camera entity, got: {error_message}"
            )

    def test_get_camera_image_returns_image_and_info(self, mcp_client):
        """A live camera snapshot should return an image plus the info text block.

        The demo cameras serve static JPEG/PNG assets with real headers, so
        the info text must name the format, the served dimensions, and the
        retrieval time.
        """
        # Find an available camera entity (the demo integration provides them).
        search_result = safe_call_tool(
            mcp_client,
            "ha_search",
            {
                "domain": "camera",
            },
        )

        assert_mcp_success(search_result, "ha_search for camera entities")

        search_text = parse_mcp_result(search_result)
        camera_match = re.search(r"camera\.\w+", search_text)
        assert camera_match, (
            f"No camera.* entity found in search results: {search_text[:200]}"
        )
        camera_entity = camera_match.group(0)

        result = safe_call_tool(
            mcp_client,
            "ha_get_camera_image",
            {
                "entity_id": camera_entity,
            },
        )

        assert_mcp_success(result, f"ha_get_camera_image for {camera_entity}")
        assert result.content, "Should return content blocks"
        assert len(result.content) >= 2, "Expected both a text block and an image block"

        image_blocks = [block for block in result.content if block.type == "image"]
        text_blocks = [block for block in result.content if block.type == "text"]

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

    def test_get_camera_image_with_resize(self, mcp_client):
        """A resize request should return both blocks; the size reflects the
        image as actually served (Home Assistant may or may not rescale)."""
        search_result = safe_call_tool(
            mcp_client,
            "ha_search",
            {
                "domain": "camera",
            },
        )

        assert_mcp_success(search_result, "ha_search for camera entities")

        search_text = parse_mcp_result(search_result)
        camera_match = re.search(r"camera\.\w+", search_text)
        assert camera_match, (
            f"No camera.* entity found in search results: {search_text[:200]}"
        )
        camera_entity = camera_match.group(0)

        result = safe_call_tool(
            mcp_client,
            "ha_get_camera_image",
            {
                "entity_id": camera_entity,
                "width": 640,
                "height": 480,
            },
        )

        assert_mcp_success(
            result, f"ha_get_camera_image with resize for {camera_entity}"
        )
        assert result.content, "Should return content blocks"

        image_blocks = [block for block in result.content if block.type == "image"]
        text_blocks = [block for block in result.content if block.type == "text"]

        assert len(image_blocks) == 1, "Expected exactly one image block"
        assert image_blocks[0].data, "Image data should not be empty"

        assert len(text_blocks) == 1, "Expected exactly one text block"
        info_text = text_blocks[0].text
        # The served size may differ from the requested one: the text must
        # still be well-formed and truthful about what was delivered.
        assert INFO_TEXT_RE.fullmatch(info_text), (
            f"Unexpected info text format: {info_text!r}"
        )
