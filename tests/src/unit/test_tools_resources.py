"""Unit tests for dashboard resource hosting tools."""

import base64

import pytest
from unittest.mock import MagicMock

from ha_mcp.tools.tools_resources import (
    register_resources_tools,
    WORKER_BASE_URL,
    MAX_ENCODED_LENGTH,
    MAX_CONTENT_SIZE,
)


class TestHaCreateDashboardResource:
    """Test ha_create_dashboard_resource tool."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server that captures all tools."""
        mcp = MagicMock()
        self.registered_tools = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func

            return wrapper

        mcp.tool = tool_decorator
        return mcp

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        return MagicMock()

    @pytest.fixture
    def tool(self, mock_mcp, mock_client):
        """Register tools and return the ha_create_dashboard_resource function."""
        register_resources_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_create_dashboard_resource"]

    # --- Success Cases ---

    @pytest.mark.asyncio
    async def test_create_css_resource(self, tool):
        """Test creating a CSS resource URL."""
        css = ".my-card { background: #333; border-radius: 8px; }"
        result = await tool(content=css, resource_type="css")

        assert result["success"] is True
        assert result["url"].startswith(WORKER_BASE_URL)
        assert "?type=css" in result["url"]
        assert result["resource_type"] == "css"
        assert result["size"] == len(css.encode("utf-8"))

    @pytest.mark.asyncio
    async def test_create_module_resource(self, tool):
        """Test creating an ES6 module resource URL."""
        js = "export const formatTemp = (v) => `${v.toFixed(1)}°C`;"
        result = await tool(content=js, resource_type="module")

        assert result["success"] is True
        assert "?type=module" in result["url"]
        assert result["resource_type"] == "module"

    @pytest.mark.asyncio
    async def test_default_type_is_module(self, tool):
        """Test that default resource_type is 'module'."""
        result = await tool(content="export const x = 1;")

        assert result["success"] is True
        assert "?type=module" in result["url"]

    @pytest.mark.asyncio
    async def test_url_contains_base64_content(self, tool):
        """Test that URL contains decodable base64 content."""
        content = "test content for encoding"
        result = await tool(content=content)

        assert result["success"] is True

        # Extract and decode
        encoded_part = result["url"].replace(f"{WORKER_BASE_URL}/", "").split("?")[0]
        decoded = base64.urlsafe_b64decode(encoded_part).decode("utf-8")
        assert decoded == content

    @pytest.mark.asyncio
    async def test_deterministic_urls(self, tool):
        """Test that same content produces same URL."""
        content = "const x = 42;"

        result1 = await tool(content=content)
        result2 = await tool(content=content)

        assert result1["url"] == result2["url"]

    @pytest.mark.asyncio
    async def test_different_content_different_urls(self, tool):
        """Test that different content produces different URLs."""
        result1 = await tool(content="const x = 1;")
        result2 = await tool(content="const x = 2;")

        assert result1["url"] != result2["url"]

    @pytest.mark.asyncio
    async def test_size_fields_accurate(self, tool):
        """Test that size fields are accurate."""
        content = "Hello, World!"
        content_bytes = content.encode("utf-8")

        result = await tool(content=content)

        assert result["size"] == len(content_bytes)
        assert result["encoded_size"] == len(base64.urlsafe_b64encode(content_bytes))

    @pytest.mark.asyncio
    async def test_unicode_content(self, tool):
        """Test handling of Unicode content."""
        content = "const greeting = '你好世界';"
        result = await tool(content=content)

        assert result["success"] is True

        # Verify roundtrip
        encoded_part = result["url"].replace(f"{WORKER_BASE_URL}/", "").split("?")[0]
        decoded = base64.urlsafe_b64decode(encoded_part).decode("utf-8")
        assert decoded == content

    @pytest.mark.asyncio
    async def test_multiline_content(self, tool):
        """Test handling of multiline content."""
        content = """
        // Line 1
        const a = 1;
        // Line 2
        const b = 2;
        """
        result = await tool(content=content)

        assert result["success"] is True
        assert result["size"] == len(content.encode("utf-8"))

    @pytest.mark.asyncio
    async def test_special_characters(self, tool):
        """Test handling of special characters."""
        content = "const url = 'https://example.com?foo=bar&baz=qux';"
        result = await tool(content=content)

        assert result["success"] is True

        # Verify roundtrip
        encoded_part = result["url"].replace(f"{WORKER_BASE_URL}/", "").split("?")[0]
        decoded = base64.urlsafe_b64decode(encoded_part).decode("utf-8")
        assert decoded == content

    @pytest.mark.asyncio
    async def test_urlsafe_base64_no_plus_slash(self, tool):
        """Test that URL-safe base64 is used (no + or /)."""
        # Content that would produce + and / in standard base64
        content = "test>>>test???"
        result = await tool(content=content)

        assert result["success"] is True
        encoded_part = result["url"].replace(f"{WORKER_BASE_URL}/", "").split("?")[0]
        assert "+" not in encoded_part
        assert "/" not in encoded_part

    # --- Error Cases ---

    @pytest.mark.asyncio
    async def test_empty_content_error(self, tool):
        """Test that empty content returns error."""
        result = await tool(content="")

        assert result["success"] is False
        assert "empty" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_whitespace_only_error(self, tool):
        """Test that whitespace-only content returns error."""
        result = await tool(content="   \n\t  ")

        assert result["success"] is False
        assert "empty" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_content_too_large_error(self, tool):
        """Test that oversized content returns error with suggestions."""
        large_content = "x" * (MAX_CONTENT_SIZE + 1000)
        result = await tool(content=large_content)

        assert result["success"] is False
        assert "too large" in result["error"].lower()
        assert "size" in result
        assert "suggestions" in result

    # --- Edge Cases ---

    @pytest.mark.asyncio
    async def test_content_at_size_limit(self, tool):
        """Test content just under the size limit succeeds."""
        content = "x" * (MAX_CONTENT_SIZE - 100)
        result = await tool(content=content)

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_single_character(self, tool):
        """Test minimal content."""
        result = await tool(content="x")

        assert result["success"] is True
        assert result["size"] == 1


class TestToolRegistration:
    """Test tool registration."""

    def test_registers_tool(self):
        """Test that register_resources_tools registers the tool."""
        mcp = MagicMock()
        registered = []

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                registered.append(func.__name__)
                return func

            return wrapper

        mcp.tool = tool_decorator
        register_resources_tools(mcp, MagicMock())

        assert "ha_create_dashboard_resource" in registered

    def test_tool_annotations(self):
        """Test tool has correct annotations."""
        mcp = MagicMock()
        captured = {}

        def tool_decorator(*args, **kwargs):
            captured.update(kwargs.get("annotations", {}))

            def wrapper(func):
                return func

            return wrapper

        mcp.tool = tool_decorator
        register_resources_tools(mcp, MagicMock())

        assert captured.get("readOnlyHint") is True
        assert captured.get("idempotentHint") is True
        assert "resources" in captured.get("tags", [])
        assert "dashboard" in captured.get("tags", [])


class TestConstants:
    """Test module constants."""

    def test_worker_url_is_https(self):
        """Test worker URL uses HTTPS."""
        assert WORKER_BASE_URL.startswith("https://")

    def test_size_limits_reasonable(self):
        """Test size limits allow useful content."""
        assert MAX_CONTENT_SIZE >= 20000  # At least 20KB
        assert MAX_ENCODED_LENGTH >= 30000  # At least 30KB

    def test_base64_overhead_accounted(self):
        """Test content limit accounts for base64 expansion."""
        # Base64 increases size by 4/3
        expected_encoded = (MAX_CONTENT_SIZE * 4 + 2) // 3
        assert expected_encoded <= MAX_ENCODED_LENGTH
