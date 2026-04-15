"""Unit tests for camera tools module."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_camera import CameraTools


class TestHaGetCameraImage:
    """Test ha_get_camera_image tool validation logic."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.httpx_client = AsyncMock()
        return client

    @pytest.fixture
    def camera_tools(self, mock_client):
        """Create CameraTools instance."""
        return CameraTools(mock_client)

    @pytest.mark.asyncio
    async def test_invalid_entity_id_format_empty(self, camera_tools):
        """Empty entity_id raises ValueError."""
        with pytest.raises(ValueError, match="Invalid entity_id format"):
            await camera_tools.ha_get_camera_image(entity_id="")

    @pytest.mark.asyncio
    async def test_invalid_entity_id_format_no_dot(self, camera_tools):
        """Entity ID without dot raises ValueError."""
        with pytest.raises(ValueError, match="Invalid entity_id format"):
            await camera_tools.ha_get_camera_image(entity_id="front_door")

    @pytest.mark.asyncio
    async def test_non_camera_domain_raises_error(self, camera_tools):
        """Non-camera entity raises ValueError."""
        with pytest.raises(ValueError, match="not a camera entity"):
            await camera_tools.ha_get_camera_image(entity_id="light.living_room")

    @pytest.mark.asyncio
    async def test_non_camera_domain_sensor(self, camera_tools):
        """Sensor entity raises ValueError."""
        with pytest.raises(ValueError, match="Domain is 'sensor', expected 'camera'"):
            await camera_tools.ha_get_camera_image(entity_id="sensor.temperature")

    @pytest.mark.asyncio
    async def test_successful_image_retrieval(self, mock_client):
        """Test successful camera image retrieval."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\xff\xd8\xff\xe0"  # JPEG magic bytes
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        result = await tools.ha_get_camera_image(entity_id="camera.front_door")

        mock_client.httpx_client.get.assert_called_once_with(
            "/camera_proxy/camera.front_door",
            params=None
        )
        assert result.data == b"\xff\xd8\xff\xe0"
        assert result._format == "jpeg"

    @pytest.mark.asyncio
    async def test_image_retrieval_with_size_params(self, mock_client):
        """Test camera image retrieval with width and height parameters."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\xff\xd8\xff\xe0"
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        await tools.ha_get_camera_image(entity_id="camera.front_door", width=640, height=480)

        mock_client.httpx_client.get.assert_called_once_with(
            "/camera_proxy/camera.front_door",
            params={"width": "640", "height": "480"}
        )

    @pytest.mark.asyncio
    async def test_authentication_error(self, mock_client):
        """Test 401 response raises PermissionError."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        with pytest.raises(PermissionError, match="Invalid authentication token"):
            await tools.ha_get_camera_image(entity_id="camera.front_door")

    @pytest.mark.asyncio
    async def test_not_found_error(self, mock_client):
        """Test 404 response raises ValueError."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        with pytest.raises(ValueError, match="Camera entity not found"):
            await tools.ha_get_camera_image(entity_id="camera.nonexistent")

    @pytest.mark.asyncio
    async def test_server_error(self, mock_client):
        """Test 500 response raises RuntimeError."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        with pytest.raises(RuntimeError, match="Failed to retrieve camera image: HTTP 500"):
            await tools.ha_get_camera_image(entity_id="camera.front_door")

    @pytest.mark.asyncio
    async def test_empty_image_data(self, mock_client):
        """Test empty image data raises RuntimeError."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b""
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        with pytest.raises(RuntimeError, match="returned empty image data"):
            await tools.ha_get_camera_image(entity_id="camera.front_door")

    @pytest.mark.asyncio
    async def test_png_content_type(self, mock_client):
        """Test PNG content type is correctly detected."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\x89PNG\r\n\x1a\n"  # PNG magic bytes
        mock_response.headers = {"content-type": "image/png"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        result = await tools.ha_get_camera_image(entity_id="camera.front_door")
        assert result._format == "png"

    @pytest.mark.asyncio
    async def test_gif_content_type(self, mock_client):
        """Test GIF content type is correctly detected."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"GIF89a"  # GIF magic bytes
        mock_response.headers = {"content-type": "image/gif"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        result = await tools.ha_get_camera_image(entity_id="camera.front_door")
        assert result._format == "gif"

    @pytest.mark.asyncio
    async def test_default_to_jpeg_for_unknown_content_type(self, mock_client):
        """Test unknown content type defaults to JPEG."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"some image data"
        mock_response.headers = {"content-type": "application/octet-stream"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        result = await tools.ha_get_camera_image(entity_id="camera.front_door")
        assert result._format == "jpeg"

    @pytest.mark.asyncio
    async def test_width_only_param(self, mock_client):
        """Test providing only width parameter."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\xff\xd8\xff\xe0"
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        await tools.ha_get_camera_image(entity_id="camera.front_door", width=800)

        mock_client.httpx_client.get.assert_called_once_with(
            "/camera_proxy/camera.front_door",
            params={"width": "800"}
        )

    @pytest.mark.asyncio
    async def test_height_only_param(self, mock_client):
        """Test providing only height parameter."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\xff\xd8\xff\xe0"
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        await tools.ha_get_camera_image(entity_id="camera.front_door", height=600)

        mock_client.httpx_client.get.assert_called_once_with(
            "/camera_proxy/camera.front_door",
            params={"height": "600"}
        )
