"""Unit tests for resource hosting tools module."""

import base64

import pytest
from unittest.mock import MagicMock

from ha_mcp.tools.tools_resources import (
    register_resources_tools,
    WORKER_BASE_URL,
    MAX_ENCODED_LENGTH,
    APPROX_MAX_CONTENT_SIZE,
)


class TestHaGetResourceUrl:
    """Test ha_get_resource_url tool."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server."""
        mcp = MagicMock()
        self.registered_tool = None

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tool = func
                return func
            return wrapper

        mcp.tool = tool_decorator
        return mcp

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        return MagicMock()

    @pytest.fixture
    def registered_tool(self, mock_mcp, mock_client):
        """Register tools and return the ha_create_resource_url function."""
        register_resources_tools(mock_mcp, mock_client)
        return self.registered_tool

    # --- Success Cases ---

    @pytest.mark.asyncio
    async def test_create_css_resource_url(self, registered_tool):
        """Test creating a CSS resource URL."""
        css_content = """
            .my-card {
                background: #333;
                border-radius: 8px;
            }
        """
        result = await registered_tool(content=css_content, resource_type="css")

        assert result["success"] is True
        assert "url" in result
        assert result["url"].startswith(WORKER_BASE_URL)
        assert "?type=css" in result["url"]
        assert result["resource_type"] == "css"
        assert result["size"] > 0
        assert result["encoded_size"] > 0

    @pytest.mark.asyncio
    async def test_create_module_resource_url(self, registered_tool):
        """Test creating an ES6 module resource URL."""
        js_content = """
            export function formatValue(val) {
                return val.toFixed(2);
            }
        """
        result = await registered_tool(content=js_content, resource_type="module")

        assert result["success"] is True
        assert "?type=module" in result["url"]
        assert result["resource_type"] == "module"

    @pytest.mark.asyncio
    async def test_create_js_resource_url(self, registered_tool):
        """Test creating a regular JavaScript resource URL."""
        js_content = "console.log('Hello, Home Assistant!');"
        result = await registered_tool(content=js_content, resource_type="js")

        assert result["success"] is True
        assert "?type=js" in result["url"]
        assert result["resource_type"] == "js"

    @pytest.mark.asyncio
    async def test_default_resource_type_is_module(self, registered_tool):
        """Test that default resource_type is 'module'."""
        result = await registered_tool(content="export const x = 1;")

        assert result["success"] is True
        assert "?type=module" in result["url"]
        assert result["resource_type"] == "module"

    @pytest.mark.asyncio
    async def test_url_contains_base64_encoded_content(self, registered_tool):
        """Test that URL contains base64-encoded content."""
        content = "test content"
        result = await registered_tool(content=content)

        assert result["success"] is True

        # Extract encoded part from URL (between base URL and query params)
        url = result["url"]
        encoded_part = url.replace(f"{WORKER_BASE_URL}/", "").split("?")[0]

        # Verify it's valid base64 that decodes to original content
        decoded = base64.urlsafe_b64decode(encoded_part).decode("utf-8")
        assert decoded == content

    @pytest.mark.asyncio
    async def test_deterministic_url_for_same_content(self, registered_tool):
        """Test that same content produces same URL."""
        content = "const x = 42;"

        result1 = await registered_tool(content=content)
        result2 = await registered_tool(content=content)

        assert result1["url"] == result2["url"]

    @pytest.mark.asyncio
    async def test_different_content_produces_different_url(self, registered_tool):
        """Test that different content produces different URLs."""
        result1 = await registered_tool(content="const x = 1;")
        result2 = await registered_tool(content="const x = 2;")

        assert result1["url"] != result2["url"]

    @pytest.mark.asyncio
    async def test_size_fields_are_accurate(self, registered_tool):
        """Test that size fields accurately reflect content size."""
        content = "Hello, World!"
        content_bytes = content.encode("utf-8")
        expected_size = len(content_bytes)
        expected_encoded_size = len(base64.urlsafe_b64encode(content_bytes))

        result = await registered_tool(content=content)

        assert result["size"] == expected_size
        assert result["encoded_size"] == expected_encoded_size

    @pytest.mark.asyncio
    async def test_unicode_content_is_handled(self, registered_tool):
        """Test that Unicode content is properly encoded."""
        content = "const greeting = 'Hello, World!';"
        result = await registered_tool(content=content)

        assert result["success"] is True
        # Verify the encoded content can be decoded back
        url = result["url"]
        encoded_part = url.replace(f"{WORKER_BASE_URL}/", "").split("?")[0]
        decoded = base64.urlsafe_b64decode(encoded_part).decode("utf-8")
        assert decoded == content

    @pytest.mark.asyncio
    async def test_multiline_content(self, registered_tool):
        """Test handling of multiline content."""
        content = """
        // Line 1
        const a = 1;
        // Line 2
        const b = 2;
        """
        result = await registered_tool(content=content)

        assert result["success"] is True
        assert result["size"] == len(content.encode("utf-8"))

    @pytest.mark.asyncio
    async def test_result_includes_worker_base_url(self, registered_tool):
        """Test that result includes worker base URL for reference."""
        result = await registered_tool(content="x")

        assert result["worker_base_url"] == WORKER_BASE_URL

    @pytest.mark.asyncio
    async def test_result_includes_notes(self, registered_tool):
        """Test that result includes helpful notes."""
        result = await registered_tool(content="x")

        assert "notes" in result
        assert isinstance(result["notes"], list)
        assert len(result["notes"]) > 0

    # --- Error Cases ---

    @pytest.mark.asyncio
    async def test_empty_content_returns_error(self, registered_tool):
        """Test that empty content returns an error."""
        result = await registered_tool(content="")

        assert result["success"] is False
        assert "error" in result
        assert "empty" in result["error"].lower()
        assert "suggestions" in result

    @pytest.mark.asyncio
    async def test_whitespace_only_content_returns_error(self, registered_tool):
        """Test that whitespace-only content returns an error."""
        result = await registered_tool(content="   \n\t  ")

        assert result["success"] is False
        assert "empty" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_content_too_large_returns_error(self, registered_tool):
        """Test that content exceeding size limit returns error."""
        # Create content larger than the limit
        large_content = "x" * (APPROX_MAX_CONTENT_SIZE + 1000)

        result = await registered_tool(content=large_content)

        assert result["success"] is False
        assert "too large" in result["error"].lower()
        assert "size" in result
        assert "suggestions" in result

    @pytest.mark.asyncio
    async def test_error_includes_helpful_suggestions(self, registered_tool):
        """Test that errors include helpful suggestions."""
        result = await registered_tool(content="")

        assert "suggestions" in result
        assert isinstance(result["suggestions"], list)
        assert len(result["suggestions"]) > 0

    # --- Edge Cases ---

    @pytest.mark.asyncio
    async def test_content_at_size_limit(self, registered_tool):
        """Test content exactly at the size limit."""
        # Content just under the limit should succeed
        content = "x" * (APPROX_MAX_CONTENT_SIZE - 100)

        result = await registered_tool(content=content)

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_special_characters_in_content(self, registered_tool):
        """Test handling of special characters."""
        content = """
            const regex = /[a-z]+/g;
            const url = 'https://example.com?foo=bar&baz=qux';
            const html = '<div class="test">&amp;</div>';
        """
        result = await registered_tool(content=content)

        assert result["success"] is True
        # Verify roundtrip
        url = result["url"]
        encoded_part = url.replace(f"{WORKER_BASE_URL}/", "").split("?")[0]
        decoded = base64.urlsafe_b64decode(encoded_part).decode("utf-8")
        assert decoded == content

    @pytest.mark.asyncio
    async def test_base64_urlsafe_encoding(self, registered_tool):
        """Test that URL-safe base64 encoding is used."""
        # Content that would produce +/= in standard base64
        content = "test>>>test"

        result = await registered_tool(content=content)

        assert result["success"] is True
        url = result["url"]
        # URL-safe base64 should not contain + or /
        encoded_part = url.replace(f"{WORKER_BASE_URL}/", "").split("?")[0]
        assert "+" not in encoded_part
        assert "/" not in encoded_part

    @pytest.mark.asyncio
    async def test_single_character_content(self, registered_tool):
        """Test handling of minimal content."""
        result = await registered_tool(content="x")

        assert result["success"] is True
        assert result["size"] == 1


class TestResourceToolsRegistration:
    """Test that resource tools are properly registered."""

    def test_register_resources_tools_creates_tool(self):
        """Test that register_resources_tools registers the tool."""
        mcp = MagicMock()
        registered_tools = []

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                registered_tools.append(func.__name__)
                return func
            return wrapper

        mcp.tool = tool_decorator
        client = MagicMock()

        register_resources_tools(mcp, client)

        assert "ha_get_resource_url" in registered_tools

    def test_tool_has_correct_annotations(self):
        """Test that tool is registered with correct annotations."""
        mcp = MagicMock()
        captured_annotations = {}

        def tool_decorator(*args, **kwargs):
            captured_annotations.update(kwargs.get("annotations", {}))
            def wrapper(func):
                return func
            return wrapper

        mcp.tool = tool_decorator
        client = MagicMock()

        register_resources_tools(mcp, client)

        assert captured_annotations.get("readOnlyHint") is True
        assert captured_annotations.get("idempotentHint") is True
        assert "resources" in captured_annotations.get("tags", [])
        assert "dashboard" in captured_annotations.get("tags", [])


class TestConstants:
    """Test module constants are properly defined."""

    def test_worker_base_url_is_https(self):
        """Test that worker URL uses HTTPS."""
        assert WORKER_BASE_URL.startswith("https://")

    def test_max_encoded_length_is_reasonable(self):
        """Test that max encoded length allows useful content."""
        # Should allow at least 10KB of content
        assert MAX_ENCODED_LENGTH >= 10000

    def test_approx_max_content_size_accounts_for_base64_overhead(self):
        """Test that content size limit accounts for base64 expansion."""
        # Base64 encoding increases size by exactly 4/3 (1.333...)
        # So max content * 4/3 should be <= max encoded length
        # Using ceiling division to be safe
        expected_encoded = (APPROX_MAX_CONTENT_SIZE * 4 + 2) // 3
        assert expected_encoded <= MAX_ENCODED_LENGTH
