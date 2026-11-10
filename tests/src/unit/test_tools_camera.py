"""Unit tests for camera tools module."""

import json
import re
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools import tools_camera
from ha_mcp.tools.tools_camera import CameraTools, _detect_image_format


def _png(width: int, height: int) -> bytes:
    """Minimal structurally-valid PNG header with the given dimensions."""
    ihdr_payload = (
        width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + len(ihdr_payload).to_bytes(4, "big")
        + b"IHDR"
        + ihdr_payload
    )


def _error_body(exc_info: pytest.ExceptionInfo[ToolError]) -> dict:
    """Parse the structured error payload carried by a raised ToolError."""
    return json.loads(exc_info.value.args[0])["error"]


class TestDetectImageFormat:
    """Content-Type header -> the declared format the sniff falls back to."""

    @pytest.mark.parametrize(
        ("content_type", "expected"),
        [
            ("image/jpeg", "jpeg"),
            ("image/jpg", "jpeg"),  # alias folding
            ("image/jpe", "jpeg"),
            ("image/jif", "jpeg"),
            ("image/png", "png"),
            ("image/gif", "gif"),
            ("image/webp", "webp"),  # unknown subtypes pass through verbatim
            ("image/tiff", "tiff"),
            ("image/avif", "avif"),
            ("image/jpeg; q=0.9", "jpeg"),  # parameters are stripped
            ("IMAGE/PNG", "png"),  # case-insensitive
            ("  image/png  ", "png"),  # whitespace is stripped
            ("application/octet-stream", "jpeg"),  # non-image -> default
            ("audio/mpeg", "jpeg"),
            ("image/", "jpeg"),  # type present, no subtype
            ("", "jpeg"),  # missing header
        ],
    )
    def test_declared_format(self, content_type: str, expected: str) -> None:
        assert _detect_image_format(content_type) == expected


class TestHaGetCameraImage:
    """Test ha_get_camera_image tool validation logic."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client.

        ``base_url``/``token`` stay ``None`` so the component capability
        probe short-circuits (no WebSocket attempt), and ``get_config``
        serves the timezone for the retrieval-time label.
        """
        client = MagicMock()
        client.base_url = None
        client.token = None
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.httpx_client = AsyncMock()
        return client

    @pytest.fixture
    def camera_tools(self, mock_client):
        """Create CameraTools instance."""
        return CameraTools(mock_client)

    @pytest.mark.asyncio
    async def test_invalid_entity_id_format_empty(self, camera_tools):
        """Empty entity_id raises a structured validation error."""
        with pytest.raises(ToolError) as exc_info:
            await camera_tools.ha_get_camera_image(entity_id="")
        error = _error_body(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "Invalid entity_id format" in error["message"]

    @pytest.mark.asyncio
    async def test_invalid_entity_id_format_no_dot(self, camera_tools):
        """Entity ID without dot raises a structured validation error."""
        with pytest.raises(ToolError) as exc_info:
            await camera_tools.ha_get_camera_image(entity_id="front_door")
        error = _error_body(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"

    @pytest.mark.asyncio
    async def test_non_camera_domain_raises_error(self, camera_tools):
        """Non-camera entity raises a structured validation error."""
        with pytest.raises(ToolError) as exc_info:
            await camera_tools.ha_get_camera_image(entity_id="light.living_room")
        error = _error_body(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "not a camera or image entity" in error["message"]

    @pytest.mark.asyncio
    async def test_non_camera_domain_sensor(self, camera_tools):
        """Sensor entity raises a structured validation error naming the domain."""
        with pytest.raises(ToolError) as exc_info:
            await camera_tools.ha_get_camera_image(entity_id="sensor.temperature")
        error = _error_body(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "Domain is 'sensor', expected 'camera'" in error["message"]

    @pytest.mark.asyncio
    async def test_successful_image_retrieval(self, mock_client):
        """Test successful camera image retrieval returns info text plus image."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\xff\xd8\xff\xe0"  # JPEG magic bytes
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        text, image = await tools.ha_get_camera_image(entity_id="camera.front_door")

        mock_client.httpx_client.get.assert_called_once_with(
            "/camera_proxy/camera.front_door", params=None
        )
        assert image.data == b"\xff\xd8\xff\xe0"
        assert image.to_image_content().mime_type == "image/jpeg"
        # The 4-byte magic prefix has no parseable header, so no size.
        assert re.fullmatch(
            r"Camera snapshot \(JPEG\)\. Retrieved: \d{4}-\d{2}-\d{2} "
            r"\d{2}:\d{2}:\d{2} [+-]\d{2}:\d{2}",
            text,
        )

    @pytest.mark.asyncio
    async def test_info_text_includes_served_size(self, mock_client):
        """A parseable JPEG header puts the served size in the text block."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        sof0 = (
            b"\xff\xc0"
            + (11).to_bytes(2, "big")
            + b"\x08"
            + (600).to_bytes(2, "big")
            + (800).to_bytes(2, "big")
            + b"\x01"
            + b"\x01\x11\x00"
        )
        mock_response.content = b"\xff\xd8" + sof0 + b"\xff\xd9"
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        text, image = await tools.ha_get_camera_image(entity_id="camera.front_door")

        assert image.data == mock_response.content
        assert text.startswith("Camera snapshot (JPEG, 800x600).")
        assert "Retrieved: " in text

    @pytest.mark.asyncio
    async def test_mislabeled_content_type_follows_payload(self, mock_client):
        """A mislabeled Content-Type must not mix formats in the response.

        PNG bytes served under a JPEG header: the text label, the reported
        size, and the Image block must all describe the PNG payload.
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = _png(640, 480)
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        text, image = await tools.ha_get_camera_image(entity_id="camera.front_door")

        assert image.to_image_content().mime_type == "image/png"
        assert text.startswith("Camera snapshot (PNG, 640x480).")

    @pytest.mark.asyncio
    async def test_info_text_uses_ha_timezone(self, mock_client):
        """The retrieval time is labeled in Home Assistant's timezone."""
        mock_client.get_config = AsyncMock(
            return_value={"time_zone": "Pacific/Kiritimati"}
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\xff\xd8\xff\xe0"
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        text, _ = await tools.ha_get_camera_image(entity_id="camera.front_door")

        mock_client.get_config.assert_awaited_once()
        # Kiritimati is UTC+14 year-round, so the offset is stable.
        assert text.endswith("+14:00")
        # A successful lookup gets no fallback note.
        assert "could not determine" not in text

    @pytest.mark.asyncio
    async def test_info_text_falls_back_to_utc_on_config_failure(self, mock_client):
        """A timezone fetch failure degrades to UTC and says so in the text."""
        mock_client.get_config = AsyncMock(
            side_effect=HomeAssistantConnectionError("no HA")
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\xff\xd8\xff\xe0"
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        text, image = await tools.ha_get_camera_image(entity_id="camera.front_door")

        assert image.data == mock_response.content
        # The note keeps the fallback distinct from a genuine UTC install.
        assert text.endswith(
            "+00:00 (UTC — could not determine the Home Assistant timezone)"
        )

    @pytest.mark.asyncio
    async def test_info_text_falls_back_to_utc_on_unresolvable_zone(self, mock_client):
        """A zone name tzdata cannot resolve also falls back to UTC, with a note."""
        mock_client.get_config = AsyncMock(return_value={"time_zone": "Not/AZone"})
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\xff\xd8\xff\xe0"
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        text, image = await tools.ha_get_camera_image(entity_id="camera.front_door")

        assert image.data == mock_response.content
        assert text.endswith(
            "+00:00 (UTC — could not determine the Home Assistant timezone)"
        )

    @pytest.mark.asyncio
    async def test_image_retrieval_with_size_params(self, mock_client):
        """Test camera image retrieval with width and height parameters."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\xff\xd8\xff\xe0"
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        await tools.ha_get_camera_image(
            entity_id="camera.front_door", width=640, height=480
        )

        mock_client.httpx_client.get.assert_called_once_with(
            "/camera_proxy/camera.front_door", params={"width": "640", "height": "480"}
        )

    @pytest.mark.asyncio
    async def test_authentication_error(self, mock_client):
        """A 401 from the camera proxy raises a structured auth error."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_camera_image(entity_id="camera.front_door")
        error = _error_body(exc_info)
        assert error["code"] == "AUTH_INVALID_TOKEN"
        assert "Invalid authentication token" in error["message"]

    @pytest.mark.asyncio
    async def test_not_found_error(self, mock_client):
        """A 404 from the camera proxy raises a structured not-found error."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_camera_image(entity_id="camera.nonexistent")
        error = _error_body(exc_info)
        assert error["code"] == "ENTITY_NOT_FOUND"
        assert "not found" in error["message"]
        assert "ha_search" in error.get("details", "")

    @pytest.mark.asyncio
    async def test_server_error(self, mock_client):
        """A 500 from the camera proxy raises a structured service error."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_camera_image(entity_id="camera.front_door")
        error = _error_body(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert "HTTP 500" in error["message"]

    @pytest.mark.asyncio
    async def test_empty_image_data(self, mock_client):
        """An empty 2xx body raises a structured service error."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b""
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_camera_image(entity_id="camera.front_door")
        error = _error_body(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert "empty image" in error["message"]

    @pytest.mark.asyncio
    async def test_unexpected_failure_maps_to_structured_error(self, mock_client):
        """The catch-all routes unexpected exceptions through the shared classifier."""
        mock_client.httpx_client.get = AsyncMock(
            side_effect=HomeAssistantConnectionError("connection refused")
        )

        tools = CameraTools(mock_client)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_camera_image(entity_id="camera.front_door")
        error = _error_body(exc_info)
        # Classified by type, not swallowed as a generic "camera online" failure.
        assert error["code"] == "CONNECTION_FAILED"

    @pytest.mark.asyncio
    async def test_png_content_type(self, mock_client):
        """Test PNG content type is correctly detected."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\x89PNG\r\n\x1a\n"  # PNG magic bytes
        mock_response.headers = {"content-type": "image/png"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        text, image = await tools.ha_get_camera_image(entity_id="camera.front_door")
        assert image.to_image_content().mime_type == "image/png"
        assert text.startswith("Camera snapshot (PNG).")

    @pytest.mark.asyncio
    async def test_gif_content_type(self, mock_client):
        """Test GIF content type is correctly detected."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"GIF89a"  # GIF magic bytes
        mock_response.headers = {"content-type": "image/gif"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        text, image = await tools.ha_get_camera_image(entity_id="camera.front_door")
        assert image.to_image_content().mime_type == "image/gif"
        assert text.startswith("Camera snapshot (GIF).")

    @pytest.mark.asyncio
    async def test_default_to_jpeg_for_unknown_content_type(self, mock_client):
        """Test unknown content type defaults to JPEG."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"some image data"
        mock_response.headers = {"content-type": "application/octet-stream"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        text, image = await tools.ha_get_camera_image(entity_id="camera.front_door")
        assert image.to_image_content().mime_type == "image/jpeg"
        # No recognized magic bytes — the text reports the format only.
        assert text.startswith("Camera snapshot (JPEG).")

    @pytest.mark.asyncio
    async def test_unrecognized_image_subtype_is_not_relabelled_jpeg(self, mock_client):
        """image/webp stays image/webp end to end.

        The sniff recognizes no container in the payload, so the declared
        format is what both blocks report — and it must be webp, not a
        JPEG guess, or the Image block lies about its bytes.
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"RIFF\x00\x00\x00\x00WEBP"
        mock_response.headers = {"content-type": "image/webp"}
        mock_client.httpx_client.get = AsyncMock(return_value=mock_response)

        tools = CameraTools(mock_client)
        text, image = await tools.ha_get_camera_image(entity_id="camera.front_door")
        assert image.to_image_content().mime_type == "image/webp"
        assert text.startswith("Camera snapshot (WEBP).")

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
            "/camera_proxy/camera.front_door", params={"width": "800"}
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
            "/camera_proxy/camera.front_door", params={"height": "600"}
        )


class TestHaGetCameraImageForImageEntities:
    """``image.*`` entities are served from Core's ``/api/image_proxy`` route.

    These tests run the real httpx client against a transport that records
    requests, so streaming, headers and URL encoding are httpx's own.
    """

    @staticmethod
    def _tools(handler) -> tuple[CameraTools, list[httpx.Request]]:
        requests: list[httpx.Request] = []

        def recording(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return handler(request)

        client = MagicMock()
        client.base_url = None
        client.token = None
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.httpx_client = httpx.AsyncClient(
            base_url="http://ha.test/api",
            transport=httpx.MockTransport(recording),
        )
        return CameraTools(client), requests

    @pytest.mark.asyncio
    async def test_image_entity_uses_image_proxy(self):
        body = _png(64, 32)
        tools, requests = self._tools(
            lambda _request: httpx.Response(
                200, content=body, headers={"content-type": "image/png"}
            )
        )

        text, image = await tools.ha_get_camera_image(entity_id="image.qr_code")

        assert [r.url.path for r in requests] == ["/api/image_proxy/image.qr_code"]
        assert requests[0].url.query == b""
        assert image.data == body
        assert image.to_image_content().mime_type == "image/png"
        assert text.startswith("Image (PNG, 64x32). Retrieved: ")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dimension", ["width", "height"])
    async def test_dimensions_rejected_for_image_entities(self, dimension):
        tools, requests = self._tools(lambda _request: httpx.Response(200))

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_camera_image(
                entity_id="image.qr_code", **{dimension: 320}
            )

        error = _error_body(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert dimension in error["message"]
        assert requests == []

    @pytest.mark.asyncio
    async def test_entity_id_cannot_alter_the_request_path(self):
        tools, requests = self._tools(lambda _request: httpx.Response(404))

        with pytest.raises(ToolError):
            await tools.ha_get_camera_image(entity_id="image.x/../../states/light.a")

        assert len(requests) == 1
        assert requests[0].url.raw_path.startswith(b"/api/image_proxy/image.x%2F")
        assert b"/../" not in requests[0].url.raw_path

    @pytest.mark.asyncio
    async def test_declared_oversize_is_refused(self, monkeypatch):
        monkeypatch.setattr(tools_camera, "MAX_IMAGE_ENTITY_BYTES", 1024)
        tools, _ = self._tools(
            lambda _request: httpx.Response(
                200,
                content=b"\x89PNG\r\n\x1a\n" + b"0" * 2048,
                headers={"content-type": "image/png"},
            )
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_camera_image(entity_id="image.huge")

        error = _error_body(exc_info)
        assert error["code"] == "IMAGE_PAYLOAD_TOO_LARGE"
        body = json.loads(exc_info.value.args[0])
        assert body["limit_bytes"] == 1024
        assert body["entity_id"] == "image.huge"

    @pytest.mark.asyncio
    async def test_undeclared_oversize_stops_streaming(self, monkeypatch):
        monkeypatch.setattr(tools_camera, "MAX_IMAGE_ENTITY_BYTES", 1024)
        chunks_sent = 0

        async def endless():
            nonlocal chunks_sent
            yield b"\x89PNG\r\n\x1a\n"
            while True:
                chunks_sent += 1
                yield b"0" * 512

        tools, _ = self._tools(
            lambda _request: httpx.Response(
                200,
                stream=_AsyncBytes(endless()),
                headers={"content-type": "image/png"},
            )
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_camera_image(entity_id="image.huge")

        assert _error_body(exc_info)["code"] == "IMAGE_PAYLOAD_TOO_LARGE"
        assert chunks_sent <= 3

    @pytest.mark.asyncio
    async def test_image_exactly_at_the_limit_is_served(self, monkeypatch):
        body = _png(4, 4)
        monkeypatch.setattr(tools_camera, "MAX_IMAGE_ENTITY_BYTES", len(body))
        tools, _ = self._tools(
            lambda _request: httpx.Response(
                200, content=body, headers={"content-type": "image/png"}
            )
        )

        _, image = await tools.ha_get_camera_image(entity_id="image.small")

        assert image.data == body

    @pytest.mark.asyncio
    async def test_non_image_content_type_is_refused(self):
        tools, _ = self._tools(
            lambda _request: httpx.Response(
                200, content=b"<html></html>", headers={"content-type": "text/html"}
            )
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_camera_image(entity_id="image.qr_code")

        error = _error_body(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert "text/html" in error["message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "code"),
        [
            (401, "AUTH_INVALID_TOKEN"),
            (404, "ENTITY_NOT_FOUND"),
            (500, "SERVICE_CALL_FAILED"),
        ],
    )
    async def test_http_errors_map_to_structured_errors(self, status, code):
        tools, _ = self._tools(lambda _request: httpx.Response(status))

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_camera_image(entity_id="image.qr_code")

        error = _error_body(exc_info)
        assert error["code"] == code
        assert "camera" not in error["message"].lower()

    @pytest.mark.asyncio
    async def test_empty_body_is_a_service_error(self):
        tools, _ = self._tools(
            lambda _request: httpx.Response(
                200, content=b"", headers={"content-type": "image/png"}
            )
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_camera_image(entity_id="image.qr_code")

        error = _error_body(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert "empty image" in error["message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("entity_id", ["sensor.temperature", "light.kitchen"])
    async def test_other_domains_still_rejected(self, entity_id):
        tools, requests = self._tools(lambda _request: httpx.Response(200))

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_camera_image(entity_id=entity_id)

        error = _error_body(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "'camera' or 'image'" in error["message"]
        assert requests == []


class _AsyncBytes(httpx.AsyncByteStream):
    """Adapt an async generator to the stream httpx expects of a response."""

    def __init__(self, source) -> None:
        self._source = source

    async def __aiter__(self):
        async for chunk in self._source:
            yield chunk
