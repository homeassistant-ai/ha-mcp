"""ha_get_camera_image against Core's real camera proxy.

Each test sets up Core's ``camera`` integration with one camera device that
serves fixed bytes under a chosen Content-Type, and calls the tool through
``core_client``: the HTTP status, headers and body are Core's.
"""

from __future__ import annotations

import re
import zoneinfo
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import setup_test_component_platform

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import response_helpers, tools_camera
from ha_mcp.tools.tools_camera import CameraTools

from ._camera_test_helpers import png_bytes as _png
from ._module_clock import set_module_clock
from ._tool_errors import error_of

ENTITY_ID = "camera.front_door"
JPEG_MAGIC = b"\xff\xd8\xff\xe0"
# No container the sniff recognizes: the declared Content-Type decides.
UNSNIFFABLE = b"some image data"
FALLBACK_NOTE = "+00:00 (UTC — could not determine the Home Assistant timezone)"


def _jpeg(width: int, height: int) -> bytes:
    """A JPEG whose SOF0 frame header declares the given dimensions."""
    sof0 = (
        b"\xff\xc0"
        + (11).to_bytes(2, "big")
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + sof0 + b"\xff\xd9"


class SnapshotCamera(Camera):
    """A camera device: serves fixed bytes, records the size Core asked for."""

    _attr_name = "Front door"
    _attr_should_poll = False

    def __init__(self, image: bytes | None, content_type: str) -> None:
        super().__init__()
        self.content_type = content_type
        self.image = image
        self.requested_sizes: list[tuple[int | None, int | None]] = []

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        self.requested_sizes.append((width, height))
        return self.image


async def serve(
    hass: HomeAssistant, image: bytes | None, content_type: str = "image/jpeg"
) -> SnapshotCamera:
    """Set up Core's camera integration with one camera serving *image*."""
    camera = SnapshotCamera(image, content_type)
    setup_test_component_platform(hass, CAMERA_DOMAIN, [camera])
    assert await async_setup_component(
        hass, CAMERA_DOMAIN, {CAMERA_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()
    assert camera.entity_id == ENTITY_ID
    return camera


@pytest.fixture
async def bad_token_client(
    core_client: HomeAssistantClient,
) -> AsyncIterator[HomeAssistantClient]:
    """A client of the same Home Assistant whose token Core does not accept."""
    client = HomeAssistantClient(
        base_url=core_client.base_url, token="not-a-valid-token", verify_ssl=False
    )
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.parametrize("entity_id", ["", "front_door"])
async def test_malformed_entity_id_is_refused_before_any_request(
    core_client: HomeAssistantClient, entity_id: str
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await CameraTools(core_client).ha_get_camera_image(entity_id=entity_id)
    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "Invalid entity_id format" in error["message"]


async def test_non_camera_entity_is_refused_naming_its_domain(
    core_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await CameraTools(core_client).ha_get_camera_image(
            entity_id="sensor.temperature"
        )
    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "not a camera or image entity" in error["message"]
    assert "Domain is 'sensor', expected 'camera' or 'image'" in error["message"]


async def test_snapshot_comes_back_as_image_with_format_and_retrieval_time(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    camera = await serve(hass, JPEG_MAGIC)

    text, image = await CameraTools(core_client).ha_get_camera_image(
        entity_id=ENTITY_ID
    )

    assert camera.requested_sizes == [(None, None)]
    assert image.data == JPEG_MAGIC
    assert image.to_image_content().mime_type == "image/jpeg"
    # The 4-byte magic prefix has no parseable header, so no size.
    assert re.fullmatch(
        r"Camera snapshot \(JPEG\)\. Retrieved: \d{4}-\d{2}-\d{2} "
        r"\d{2}:\d{2}:\d{2} [+-]\d{2}:\d{2}",
        text,
    )


async def test_info_text_reports_the_served_image_size(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await serve(hass, _jpeg(800, 600))

    text, _ = await CameraTools(core_client).ha_get_camera_image(entity_id=ENTITY_ID)

    assert text.startswith("Camera snapshot (JPEG, 800x600).")


async def test_mislabeled_content_type_does_not_mix_formats_in_the_response(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """PNG bytes served as image/jpeg: label, size and Image block say PNG."""
    await serve(hass, _png(640, 480), "image/jpeg")

    text, image = await CameraTools(core_client).ha_get_camera_image(
        entity_id=ENTITY_ID
    )

    assert image.to_image_content().mime_type == "image/png"
    assert text.startswith("Camera snapshot (PNG, 640x480).")


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("image/jpg", "jpeg"),  # JPEG alias folded
        ("image/webp", "webp"),  # unknown image subtype kept, not called JPEG
        ("image/png; q=0.9", "png"),  # parameters stripped
        ("IMAGE/PNG", "png"),  # case-insensitive
        ("application/octet-stream", "jpeg"),  # not an image type
        ("image/", "jpeg"),  # no subtype
    ],
)
async def test_unsniffable_payload_is_labelled_by_its_content_type(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    content_type: str,
    expected: str,
) -> None:
    await serve(hass, UNSNIFFABLE, content_type)

    text, image = await CameraTools(core_client).ha_get_camera_image(
        entity_id=ENTITY_ID
    )

    assert image.to_image_content().mime_type == f"image/{expected}"
    assert text.startswith(f"Camera snapshot ({expected.upper()}).")


NOON_UTC = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def noon_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    set_module_clock(monkeypatch, tools_camera, NOON_UTC)


@pytest.mark.usefixtures("noon_utc")
async def test_retrieval_time_is_in_home_assistant_timezone(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await hass.config.async_set_time_zone("Pacific/Kiritimati")
    await serve(hass, JPEG_MAGIC)

    text, _ = await CameraTools(core_client).ha_get_camera_image(entity_id=ENTITY_ID)

    # Kiritimati is UTC+14 year-round.
    assert text.endswith("Retrieved: 2026-11-02 02:00:00 +14:00")


async def test_timezone_fetch_without_answer_falls_back_to_utc_with_a_note(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await hass.config.async_set_time_zone("Pacific/Kiritimati")
    await serve(hass, JPEG_MAGIC)
    monkeypatch.setattr(
        core_client,
        "get_config",
        AsyncMock(side_effect=HomeAssistantConnectionError("no HA")),
    )

    text, image = await CameraTools(core_client).ha_get_camera_image(
        entity_id=ENTITY_ID
    )

    assert image.data == JPEG_MAGIC
    assert text.endswith(FALLBACK_NOTE)


async def test_zone_the_server_cannot_resolve_falls_back_to_utc_with_a_note(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core reports a zone the server's tzdata does not know."""
    await hass.config.async_set_time_zone("Pacific/Kiritimati")
    await serve(hass, JPEG_MAGIC)

    def no_tzdata(key: str) -> zoneinfo.ZoneInfo:
        raise zoneinfo.ZoneInfoNotFoundError(key)

    monkeypatch.setattr(response_helpers, "ZoneInfo", no_tzdata)

    text, image = await CameraTools(core_client).ha_get_camera_image(
        entity_id=ENTITY_ID
    )

    assert image.data == JPEG_MAGIC
    assert text.endswith(FALLBACK_NOTE)


@pytest.mark.parametrize(("width", "height"), [(640, 480), (800, None), (None, 600)])
async def test_requested_size_reaches_the_camera(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    width: int | None,
    height: int | None,
) -> None:
    camera = await serve(hass, JPEG_MAGIC)

    await CameraTools(core_client).ha_get_camera_image(
        entity_id=ENTITY_ID, width=width, height=height
    )

    assert camera.requested_sizes == [(width, height)]


async def test_rejected_token_is_reported_as_an_auth_error(
    hass: HomeAssistant, bad_token_client: HomeAssistantClient
) -> None:
    await serve(hass, JPEG_MAGIC)

    with pytest.raises(ToolError) as exc_info:
        await CameraTools(bad_token_client).ha_get_camera_image(entity_id=ENTITY_ID)
    error = error_of(exc_info)
    assert error["code"] == "AUTH_INVALID_TOKEN"
    assert "Invalid authentication token" in error["message"]


async def test_unknown_camera_is_reported_not_found_with_a_search_hint(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await serve(hass, JPEG_MAGIC)

    with pytest.raises(ToolError) as exc_info:
        await CameraTools(core_client).ha_get_camera_image(
            entity_id="camera.nonexistent"
        )
    error = error_of(exc_info)
    assert error["code"] == "ENTITY_NOT_FOUND"
    assert "not found" in error["message"]
    assert "ha_search" in error.get("details", "")


async def test_camera_without_an_image_is_reported_as_a_failed_call(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core answers HTTP 500 when the camera hands it no image; the caller is
    told the camera may be offline, not only the status code."""
    await serve(hass, None)

    with pytest.raises(ToolError) as exc_info:
        await CameraTools(core_client).ha_get_camera_image(entity_id=ENTITY_ID)
    error = error_of(exc_info)
    assert error["code"] == "SERVICE_CALL_FAILED"
    assert "HTTP 500" in error["message"]
    assert "may be offline" in error["message"]


async def test_unreachable_home_assistant_is_reported_as_a_connection_failure(
    unreachable_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await CameraTools(unreachable_client).ha_get_camera_image(entity_id=ENTITY_ID)
    assert error_of(exc_info)["code"] == "CONNECTION_FAILED"
