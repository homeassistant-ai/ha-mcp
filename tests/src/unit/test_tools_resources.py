"""Unit tests for dashboard resource tools."""

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_resources import (
    MAX_CONTENT_SIZE,
    MAX_ENCODED_LENGTH,
    WORKER_BASE_URL,
    _decode_inline_url,
    _encode_content,
    _is_inline_url,
    register_resources_tools,
)


class TestHelperFunctions:
    """Test helper functions."""

    def test_encode_content(self):
        """Test content encoding."""
        content = "test content"
        encoded, content_size, encoded_size = _encode_content(content)

        assert content_size == len(content.encode("utf-8"))
        assert encoded_size == len(encoded)
        assert base64.urlsafe_b64decode(encoded).decode("utf-8") == content

    def test_is_inline_url_true(self):
        """Test inline URL detection."""
        url = f"{WORKER_BASE_URL}/abc123?type=module"
        assert _is_inline_url(url) is True

    def test_is_inline_url_false(self):
        """Test non-inline URL detection."""
        assert _is_inline_url("/local/card.js") is False
        assert _is_inline_url("https://cdn.example.com/card.js") is False

    def test_decode_inline_url(self):
        """Test decoding inline URL."""
        content = "export const x = 1;"
        encoded = base64.urlsafe_b64encode(content.encode()).decode()
        url = f"{WORKER_BASE_URL}/{encoded}?type=module"

        decoded = _decode_inline_url(url)
        assert decoded == content

    def test_decode_inline_url_non_inline(self):
        """Test decoding non-inline URL returns None."""
        assert _decode_inline_url("/local/card.js") is None


class TestHaConfigListDashboardResources:
    """Test ha_config_list_dashboard_resources tool."""

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
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def list_tool(self, mock_mcp, mock_client):
        """Register tools and return the list function."""
        register_resources_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_config_list_dashboard_resources"]

    @pytest.mark.asyncio
    async def test_list_empty(self, list_tool, mock_client):
        """Test listing when no resources exist."""
        mock_client.send_websocket_message.return_value = {"result": []}

        result = await list_tool()

        assert result["success"] is True
        assert result["count"] == 0
        assert result["resources"] == []

    @pytest.mark.asyncio
    async def test_list_external_resources(self, list_tool, mock_client):
        """Test listing external resources."""
        mock_client.send_websocket_message.return_value = {
            "result": [
                {"id": "1", "type": "module", "url": "/local/card.js"},
                {"id": "2", "type": "css", "url": "/local/theme.css"},
            ]
        }

        result = await list_tool()

        assert result["success"] is True
        assert result["count"] == 2
        assert result["by_type"]["module"] == 1
        assert result["by_type"]["css"] == 1

    @pytest.mark.asyncio
    async def test_list_inline_resources_decoded(self, list_tool, mock_client):
        """Test that inline resources are decoded with preview."""
        content = "export const myFunction = () => 'hello world';"
        encoded = base64.urlsafe_b64encode(content.encode()).decode()
        inline_url = f"{WORKER_BASE_URL}/{encoded}?type=module"

        mock_client.send_websocket_message.return_value = {
            "result": [{"id": "1", "type": "module", "url": inline_url}]
        }

        result = await list_tool()

        assert result["success"] is True
        assert result["inline_count"] == 1
        resource = result["resources"][0]
        assert resource["_inline"] is True
        assert resource["_size"] == len(content)
        assert resource["_preview"] == content
        assert resource["url"] == "[inline]"  # URL replaced

    @pytest.mark.asyncio
    async def test_list_inline_preview_truncated(self, list_tool, mock_client):
        """Test that long inline content preview is truncated."""
        content = "x" * 200
        encoded = base64.urlsafe_b64encode(content.encode()).decode()
        inline_url = f"{WORKER_BASE_URL}/{encoded}?type=module"

        mock_client.send_websocket_message.return_value = {
            "result": [{"id": "1", "type": "module", "url": inline_url}]
        }

        result = await list_tool()

        resource = result["resources"][0]
        assert resource["_preview"].endswith("...")
        assert len(resource["_preview"]) == 153  # 150 + "..."

    @pytest.mark.asyncio
    async def test_list_include_content_flag(self, list_tool, mock_client):
        """Test that include_content=True returns full content."""
        content = "x" * 200
        encoded = base64.urlsafe_b64encode(content.encode()).decode()
        inline_url = f"{WORKER_BASE_URL}/{encoded}?type=module"

        mock_client.send_websocket_message.return_value = {
            "result": [{"id": "1", "type": "module", "url": inline_url}]
        }

        result = await list_tool(include_content=True)

        resource = result["resources"][0]
        assert "_content" in resource
        assert resource["_content"] == content
        assert "_preview" not in resource  # Preview not included when content is


class TestHaConfigSetDashboardResource:
    """Test ha_config_set_dashboard_resource tool (inline and URL modes)."""

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
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def set_tool(self, mock_mcp, mock_client):
        """Register tools and return the set function."""
        register_resources_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_config_set_dashboard_resource"]

    # ---- Input validation ----

    @pytest.mark.asyncio
    async def test_neither_content_nor_url_error(self, set_tool, mock_client):
        """Test that providing neither content nor url returns error."""
        result = await set_tool()

        assert result["success"] is False
        assert "required" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_both_content_and_url_error(self, set_tool, mock_client):
        """Test that providing both content and url returns error."""
        result = await set_tool(content="const x = 1;", url="/local/card.js")

        assert result["success"] is False
        assert "not both" in result["error"].lower()

    # ---- Inline content mode ----

    @pytest.mark.asyncio
    async def test_create_inline_module(self, set_tool, mock_client):
        """Test creating an inline module resource."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "new-id-123"}
        }

        content = "export const x = 1;"
        result = await set_tool(content=content, resource_type="module")

        assert result["success"] is True
        assert result["action"] == "created"
        assert result["resource_id"] == "new-id-123"
        assert result["resource_type"] == "module"
        assert result["size"] == len(content.encode("utf-8"))

        # Verify WebSocket call uses Cloudflare Worker URL
        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["type"] == "lovelace/resources/create"
        assert call_args["res_type"] == "module"
        assert WORKER_BASE_URL in call_args["url"]

    @pytest.mark.asyncio
    async def test_create_inline_css(self, set_tool, mock_client):
        """Test creating an inline CSS resource."""
        mock_client.send_websocket_message.return_value = {"result": {"id": "css-123"}}

        result = await set_tool(content=".card { color: red; }", resource_type="css")

        assert result["success"] is True
        assert result["resource_type"] == "css"

    @pytest.mark.asyncio
    async def test_default_type_is_module(self, set_tool, mock_client):
        """Test that default resource_type is 'module'."""
        mock_client.send_websocket_message.return_value = {"result": {"id": "123"}}

        await set_tool(content="const x = 1;")

        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["res_type"] == "module"

    @pytest.mark.asyncio
    async def test_update_inline_resource(self, set_tool, mock_client):
        """Test updating an existing inline resource."""
        mock_client.send_websocket_message.return_value = {"result": {"id": "existing"}}

        result = await set_tool(
            content="export const x = 2;",
            resource_type="module",
            resource_id="existing",
        )

        assert result["success"] is True
        assert result["action"] == "updated"
        assert result["resource_id"] == "existing"

        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["type"] == "lovelace/resources/update"
        assert call_args["resource_id"] == "existing"

    @pytest.mark.asyncio
    async def test_inline_empty_content_error(self, set_tool, mock_client):
        """Test that empty content returns error."""
        result = await set_tool(content="")

        assert result["success"] is False
        assert "empty" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_inline_whitespace_only_error(self, set_tool, mock_client):
        """Test that whitespace-only content returns error."""
        result = await set_tool(content="   \n\t  ")

        assert result["success"] is False
        assert "empty" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_inline_content_too_large_error(self, set_tool, mock_client):
        """Test that oversized content returns error."""
        large_content = "x" * (MAX_CONTENT_SIZE + 1000)
        result = await set_tool(content=large_content)

        assert result["success"] is False
        assert "too large" in result["error"].lower()
        assert "suggestions" in result

    @pytest.mark.asyncio
    async def test_inline_js_type_not_supported(self, set_tool, mock_client):
        """Test that resource_type='js' is rejected for inline content."""
        result = await set_tool(content="var x = 1;", resource_type="js")

        assert result["success"] is False
        assert "js" in result["error"].lower()

    # ---- URL mode ----

    @pytest.mark.asyncio
    async def test_create_local_resource(self, set_tool, mock_client):
        """Test creating a local resource."""
        mock_client.send_websocket_message.return_value = {"result": {"id": "local-1"}}

        result = await set_tool(url="/local/my-card.js", resource_type="module")

        assert result["success"] is True
        assert result["action"] == "created"
        assert result["url"] == "/local/my-card.js"

    @pytest.mark.asyncio
    async def test_create_hacs_resource(self, set_tool, mock_client):
        """Test creating a HACS resource."""
        mock_client.send_websocket_message.return_value = {"result": {"id": "hacs-1"}}

        result = await set_tool(
            url="/hacsfiles/lovelace-mushroom/mushroom.js", resource_type="module"
        )

        assert result["success"] is True
        assert result["action"] == "created"

    @pytest.mark.asyncio
    async def test_update_url_resource(self, set_tool, mock_client):
        """Test updating an existing URL resource."""
        mock_client.send_websocket_message.return_value = {"result": {"id": "existing"}}

        result = await set_tool(
            url="/local/card-v2.js", resource_type="module", resource_id="existing"
        )

        assert result["success"] is True
        assert result["action"] == "updated"

        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["type"] == "lovelace/resources/update"

    @pytest.mark.asyncio
    async def test_url_supports_js_type(self, set_tool, mock_client):
        """Test that legacy js type is supported for URL resources."""
        mock_client.send_websocket_message.return_value = {"result": {"id": "123"}}

        result = await set_tool(url="/local/legacy.js", resource_type="js")

        assert result["success"] is True
        assert result["resource_type"] == "js"


class TestHaConfigDeleteDashboardResource:
    """Test ha_config_delete_dashboard_resource tool."""

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
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def delete_tool(self, mock_mcp, mock_client):
        """Register tools and return the delete function."""
        register_resources_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_config_delete_dashboard_resource"]

    @pytest.mark.asyncio
    async def test_delete_success(self, delete_tool, mock_client):
        """Test successful deletion."""
        mock_client.send_websocket_message.return_value = {"success": True}

        result = await delete_tool(resource_id="abc123")

        assert result["success"] is True
        assert result["action"] == "delete"
        assert result["resource_id"] == "abc123"

    @pytest.mark.asyncio
    async def test_delete_idempotent_not_found(self, delete_tool, mock_client):
        """Test that deleting non-existent resource is idempotent."""
        mock_client.send_websocket_message.return_value = {
            "success": False,
            "error": {"message": "Resource not found"},
        }

        result = await delete_tool(resource_id="nonexistent")

        assert result["success"] is True  # Idempotent
        assert "already deleted" in result["message"].lower()


class TestToolRegistration:
    """Test tool registration."""

    def test_registers_three_tools(self):
        """Test that exactly three tools are registered (list, set, delete)."""
        mcp = MagicMock()
        registered = []

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                registered.append(func.__name__)
                return func

            return wrapper

        mcp.tool = tool_decorator
        register_resources_tools(mcp, MagicMock())

        assert "ha_config_list_dashboard_resources" in registered
        assert "ha_config_set_dashboard_resource" in registered
        assert "ha_config_delete_dashboard_resource" in registered
        assert "ha_config_set_inline_dashboard_resource" not in registered
        assert len(registered) == 3

    def test_set_tool_has_destructive_hint(self):
        """Test set tool has destructiveHint."""
        mcp = MagicMock()
        captured: dict[str, dict] = {}

        def tool_decorator(*args, **kwargs):
            annotations = kwargs.get("annotations", {})

            def wrapper(func):
                captured[func.__name__] = annotations
                return func

            return wrapper

        mcp.tool = tool_decorator
        register_resources_tools(mcp, MagicMock())

        assert captured["ha_config_set_dashboard_resource"].get("destructiveHint") is True


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
