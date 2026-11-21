"""Unit tests for ``ha_get_camera_image`` on ``image.*`` entities.

Core serves an image entity at ``/api/image_proxy/<entity_id>``. These tests run
the tool on the server's own client against a real ``hass`` whose ``image``
integration serves an entity of a test integration, so every reply is Core's.
"""

from contextlib import AbstractContextManager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import setup_test_component_platform

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_camera
from ha_mcp.tools.tools_camera import CameraTools

from ._camera_test_helpers import png_bytes as _png
from ._tool_errors import error_body, error_of


class TestImage(ImageEntity):
    """``image.<name>``: Core serves ``body`` with the given content type."""

    __test__ = False

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        body: bytes | None,
        content_type: str = "image/png",
    ) -> None:
        super().__init__(hass)
        self._attr_name = name
        self._attr_content_type = content_type
        self._body = body

    async def async_image(self) -> bytes | None:
        if self._body is None:
            raise HomeAssistantError("image unavailable")
        return self._body


async def _serve(hass: HomeAssistant, *images: TestImage) -> None:
    setup_test_component_platform(hass, "image", list(images))
    assert await async_setup_component(hass, "image", {"image": {"platform": "test"}})
    await hass.async_block_till_done()


def _spy_on_requests(
    client: HomeAssistantClient,
) -> AbstractContextManager[MagicMock]:
    """A spy on the client's streaming request; Core still answers."""
    return patch.object(client.httpx_client, "stream", wraps=client.httpx_client.stream)


@pytest.mark.parametrize("dimension", ["width", "height"])
async def test_dimensions_rejected_for_image_entities(
    hass: HomeAssistant, core_client: HomeAssistantClient, dimension: str
) -> None:
    await _serve(hass, TestImage(hass, "qr code", _png(4, 4)))

    with _spy_on_requests(core_client) as stream, pytest.raises(ToolError) as exc_info:
        await CameraTools(core_client).ha_get_camera_image(
            entity_id="image.qr_code", **{dimension: 320}
        )

    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert dimension in error["message"]
    stream.assert_not_called()


@pytest.mark.parametrize("domain", ["camera", "image"])
async def test_entity_id_stays_in_one_proxy_path_segment(
    core_client: HomeAssistantClient, domain: str
) -> None:
    http = core_client.httpx_client
    with patch.object(http, "send", wraps=http.send) as send, pytest.raises(ToolError):
        await CameraTools(core_client).ha_get_camera_image(
            entity_id=f"{domain}.missing/extra"
        )

    request = send.call_args.args[0]
    assert request.url.raw_path.endswith(b".missing%2Fextra")


async def test_declared_oversize_is_refused(
    hass: HomeAssistant, core_client: HomeAssistantClient, monkeypatch: Any
) -> None:
    monkeypatch.setattr(tools_camera, "MAX_IMAGE_ENTITY_BYTES", 1024)
    payload = _png(4, 4) + b"0" * 2048
    await _serve(hass, TestImage(hass, "huge", payload))

    with pytest.raises(ToolError) as exc_info:
        await CameraTools(core_client).ha_get_camera_image(entity_id="image.huge")

    assert error_of(exc_info)["code"] == "IMAGE_PAYLOAD_TOO_LARGE"
    body = error_body(exc_info)
    assert body["declared_bytes"] == len(payload)
    assert "cannot resize" in body["error"]["suggestion"]
    assert body["limit_bytes"] == 1024
    assert body["entity_id"] == "image.huge"


async def test_image_exactly_at_the_limit_is_served(
    hass: HomeAssistant, core_client: HomeAssistantClient, monkeypatch: Any
) -> None:
    body = _png(4, 4)
    monkeypatch.setattr(tools_camera, "MAX_IMAGE_ENTITY_BYTES", len(body))
    await _serve(hass, TestImage(hass, "small", body))

    _, image = await CameraTools(core_client).ha_get_camera_image(
        entity_id="image.small"
    )

    assert image.data == body


async def test_unknown_image_entity_is_not_found(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _serve(hass, TestImage(hass, "qr code", _png(4, 4)))

    with pytest.raises(ToolError) as exc_info:
        await CameraTools(core_client).ha_get_camera_image(entity_id="image.missing")

    error = error_of(exc_info)
    assert error["code"] == "ENTITY_NOT_FOUND"
    assert "camera" not in error["message"].lower()


async def test_unavailable_image_is_a_service_error(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _serve(hass, TestImage(hass, "broken", None))

    with pytest.raises(ToolError) as exc_info:
        await CameraTools(core_client).ha_get_camera_image(entity_id="image.broken")

    error = error_of(exc_info)
    assert error["code"] == "SERVICE_CALL_FAILED"
    assert "image.broken" in error["message"]
    assert "HTTP 500" in error["message"]
    assert "Home Assistant" in error["suggestion"]
