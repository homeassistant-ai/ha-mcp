"""Unit tests for camera tools module."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_camera import register_camera_tools


class TestHaGetCameraImage:
    """Test ha_get_camera_image tool validation logic."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server."""
        mcp = MagicMock()
        # Capture the decorated function for testing
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
        client = MagicMock()
        client.httpx_client = AsyncMock()
        return client

    @pytest.fixture
    def registered_tool(self, mock_mcp, mock_client):
        """Register tools and return the ha_get_camera_image function."""
        register_camera_tools(mock_mcp, mock_client)
        return self.registered_tool

    @pytest.mark.asyncio
    async def test_invalid_entity_id_format_empty(self, registered_tool):
        """Empty entity_id raises ValueError."""
        with pytest.raises(ValueError, match="Invalid entity_id format"):
            await registered_tool(entity_id="")

    @pytest.mark.asyncio
    async def test_invalid_entity_id_format_no_dot(self, registered_tool):
        """Entity ID without dot raises ValueError."""
        with pytest.raises(ValueError, match="Invalid entity_id format"):
            await registered_tool(entity_id="front_door")

    @pytest.mark.asyncio
    async def test_non_camera_domain_raises_error(self, registered_tool):
        """Non-camera entity raises ValueError."""
        with pytest.raises(ValueError, match="not a camera entity"):
            await registered_tool(entity_id="light.living_room")

    @pytest.mark.asyncio
    async def test_non_camera_domain_sensor(self, registered_tool):
        """Sensor entity raises ValueError."""
        with pytest.raises(ValueError, match="Domain is 'sensor', expected 'camera'"):
            await registered_tool(entity_id="sensor.temperature")

    @pytest.mark.asyncio
    async def test_successful_image_retrieval(self, mock_mcp, mock_client):
        """Test successful camera image retrieval."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\xff\xd8\xff\xe0"  # JPEG magic bytes
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        # Register and get tool
        register_camera_tools(mock_mcp, mock_client)
        tool = self.registered_tool

        # Call the tool
        result = await tool(entity_id="camera.front_door")

        # Verify the API was called correctly
        mock_client.httpx_client.get.assert_called_once_with(
            "/camera_proxy/camera.front_door",
            params=None
        )

        # Verify result is an Image with correct data
        assert result.data == b"\xff\xd8\xff\xe0"
        assert result._format == "jpeg"

    @pytest.mark.asyncio
    async def test_image_retrieval_with_size_params(self, mock_mcp, mock_client):
        """Test camera image retrieval with width and height parameters."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\xff\xd8\xff\xe0"
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        # Register and get tool
        register_camera_tools(mock_mcp, mock_client)
        tool = self.registered_tool

        # Call the tool with size parameters
        result = await tool(entity_id="camera.front_door", width=640, height=480)

        # Verify the API was called with size params
        mock_client.httpx_client.get.assert_called_once_with(
            "/camera_proxy/camera.front_door",
            params={"width": "640", "height": "480"}
        )

    @pytest.mark.asyncio
    async def test_authentication_error(self, mock_mcp, mock_client):
        """Test 401 response raises PermissionError."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        register_camera_tools(mock_mcp, mock_client)
        tool = self.registered_tool

        with pytest.raises(PermissionError, match="Invalid authentication token"):
            await tool(entity_id="camera.front_door")

    @pytest.mark.asyncio
    async def test_not_found_error(self, mock_mcp, mock_client):
        """Test 404 response raises ValueError."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        register_camera_tools(mock_mcp, mock_client)
        tool = self.registered_tool

        with pytest.raises(ValueError, match="Camera entity not found"):
            await tool(entity_id="camera.nonexistent")

    @pytest.mark.asyncio
    async def test_server_error(self, mock_mcp, mock_client):
        """Test 500 response raises RuntimeError."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        register_camera_tools(mock_mcp, mock_client)
        tool = self.registered_tool

        with pytest.raises(RuntimeError, match="Failed to retrieve camera image: HTTP 500"):
            await tool(entity_id="camera.front_door")

    @pytest.mark.asyncio
    async def test_empty_image_data(self, mock_mcp, mock_client):
        """Test empty image data raises RuntimeError."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b""
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        register_camera_tools(mock_mcp, mock_client)
        tool = self.registered_tool

        with pytest.raises(RuntimeError, match="returned empty image data"):
            await tool(entity_id="camera.front_door")

    @pytest.mark.asyncio
    async def test_png_content_type(self, mock_mcp, mock_client):
        """Test PNG content type is correctly detected."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\x89PNG\r\n\x1a\n"  # PNG magic bytes
        mock_response.headers = {"content-type": "image/png"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        register_camera_tools(mock_mcp, mock_client)
        tool = self.registered_tool

        result = await tool(entity_id="camera.front_door")
        assert result._format == "png"

    @pytest.mark.asyncio
    async def test_gif_content_type(self, mock_mcp, mock_client):
        """Test GIF content type is correctly detected."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"GIF89a"  # GIF magic bytes
        mock_response.headers = {"content-type": "image/gif"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        register_camera_tools(mock_mcp, mock_client)
        tool = self.registered_tool

        result = await tool(entity_id="camera.front_door")
        assert result._format == "gif"

    @pytest.mark.asyncio
    async def test_default_to_jpeg_for_unknown_content_type(self, mock_mcp, mock_client):
        """Test unknown content type defaults to JPEG."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"some image data"
        mock_response.headers = {"content-type": "application/octet-stream"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        register_camera_tools(mock_mcp, mock_client)
        tool = self.registered_tool

        result = await tool(entity_id="camera.front_door")
        assert result._format == "jpeg"

    @pytest.mark.asyncio
    async def test_width_only_param(self, mock_mcp, mock_client):
        """Test providing only width parameter."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\xff\xd8\xff\xe0"
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        register_camera_tools(mock_mcp, mock_client)
        tool = self.registered_tool

        await tool(entity_id="camera.front_door", width=800)

        mock_client.httpx_client.get.assert_called_once_with(
            "/camera_proxy/camera.front_door",
            params={"width": "800"}
        )

    @pytest.mark.asyncio
    async def test_height_only_param(self, mock_mcp, mock_client):
        """Test providing only height parameter."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\xff\xd8\xff\xe0"
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        register_camera_tools(mock_mcp, mock_client)
        tool = self.registered_tool

        await tool(entity_id="camera.front_door", height=600)

        mock_client.httpx_client.get.assert_called_once_with(
            "/camera_proxy/camera.front_door",
            params={"height": "600"}
        )
