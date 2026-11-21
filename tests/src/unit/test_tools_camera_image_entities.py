"""Unit tests for ``ha_get_camera_image`` on ``image.*`` entities.

Core serves an image entity at ``/api/image_proxy/<entity_id>``. These tests run
the tool on the server's own client against a real ``hass`` whose ``image``
integration serves an entity of a test integration, so every reply is Core's.
"""

from typing import Any

import httpx
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


async def test_image_with_requested_dimensions_returns_original(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    original = _png(4, 4)
    await _serve(hass, TestImage(hass, "qr code", original))

    text, image = await CameraTools(core_client).ha_get_camera_image(
        entity_id="image.qr_code", width=320, height=240
    )

    assert image.data == original
    assert "4x4" in text


@pytest.mark.parametrize("domain", ["camera", "image"])
async def test_entity_id_stays_in_one_proxy_path_segment(
    core_client: HomeAssistantClient, domain: str
) -> None:
    paths: list[bytes] = []

    async def record_path(request: httpx.Request) -> None:
        paths.append(request.url.raw_path)

    core_client.httpx_client.event_hooks["request"].append(record_path)
    with pytest.raises(ToolError):
        await CameraTools(core_client).ha_get_camera_image(
            entity_id=f"{domain}.missing/extra"
        )

    assert len(paths) == 1
    assert paths[0].endswith(b".missing%2Fextra")


@pytest.mark.parametrize("declared_length", [True, False], ids=["declared", "streamed"])
async def test_oversize_image_is_refused(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: Any,
    declared_length: bool,
) -> None:
    async def omit_length(response: httpx.Response) -> None:
        # A proxy may stream Core's bytes without forwarding their length.
        del response.headers["content-length"]

    if not declared_length:
        core_client.httpx_client.event_hooks["response"].append(omit_length)

    monkeypatch.setattr(tools_camera, "MAX_IMAGE_PAYLOAD_BYTES", 1024)
    payload = _png(4, 4) + b"0" * 2048
    await _serve(hass, TestImage(hass, "huge", payload))

    with pytest.raises(ToolError) as exc_info:
        await CameraTools(core_client).ha_get_camera_image(entity_id="image.huge")

    assert error_of(exc_info)["code"] == "IMAGE_PAYLOAD_TOO_LARGE"
    body = error_body(exc_info)
    assert body["declared_bytes"] == (len(payload) if declared_length else None)
    assert body["limit_bytes"] == 1024
    assert body["entity_id"] == "image.huge"


async def test_image_exactly_at_the_limit_is_served(
    hass: HomeAssistant, core_client: HomeAssistantClient, monkeypatch: Any
) -> None:
    body = _png(4, 4)
    monkeypatch.setattr(tools_camera, "MAX_IMAGE_PAYLOAD_BYTES", len(body))
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
