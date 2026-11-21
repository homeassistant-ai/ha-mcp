"""Retrieve camera snapshots and image entities with their dimensions and retrieval time."""

import logging
from datetime import UTC, datetime
from typing import Annotated, Any, NoReturn
from urllib.parse import quote

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp._vendor.fastmcp.utilities.types import Image
from ha_mcp.dashboard_screenshot.capture import (
    MAX_IMAGE_PAYLOAD_BYTES,
    _declared_content_length,
    _read_limited_response,
)
from ha_mcp.errors import (
    ErrorCode,
    create_auth_error,
    create_entity_not_found_error,
    create_error_response,
)
from ha_mcp.image_info import resolve_image_info

from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .response_helpers import fetch_ha_timezone, resolve_local_timezone
from .tool_hints import read_only_hints

logger = logging.getLogger(__name__)

# Image entities are handed to the model inline, so the server bounds what it
# will read. Same ceiling the dashboard screenshot tool uses for inline images.
MAX_IMAGE_ENTITY_BYTES = MAX_IMAGE_PAYLOAD_BYTES


# Subtypes that denote JPEG under an alternate spelling. Every other
# image/* subtype is reported verbatim (image/webp -> "webp"); the JPEG
# default applies only when the header names no image subtype at all.
_JPEG_ALIASES = {"jpg": "jpeg", "jpe": "jpeg", "jif": "jpeg"}


def _detect_image_format(content_type: str) -> str:
    """Report the declared image format from a Content-Type header.

    Only the media type's subtype is relevant (``image/jpeg`` -> ``jpeg``);
    parameters such as ``; q=0.9`` are stripped and JPEG's alternate
    spellings are folded in. Unknown image subtypes pass through verbatim —
    relabelling ``image/webp`` as JPEG would misidentify the Image block.
    The JPEG default applies only when the header is missing, empty, or
    not an ``image/*`` type with a subtype (e.g. ``application/octet-stream``).
    """
    media_type = content_type.split(";", 1)[0].strip().lower()
    primary, _, subtype = media_type.partition("/")
    if primary != "image" or not subtype:
        return "jpeg"
    return _JPEG_ALIASES.get(subtype, subtype)


def _snapshot_info_text(
    image_format: str,
    image_size: tuple[int, int] | None,
    retrieved: datetime,
    timezone_fallback: bool = False,
    label: str = "Camera snapshot",
) -> str:
    """Describe a camera snapshot or image entity in Home Assistant local time.

    Reports the size of the image actually served (which may differ from
    the camera's native resolution when Home Assistant rescaled it) and
    when the snapshot was retrieved, in Home Assistant local time with
    UTC offset. When the timezone lookup fell back to UTC, the text says
    so, so the label cannot be mistaken for a genuine UTC install.

    The block deliberately describes only the bytes that were served —
    never the entity's name or state. Home Assistant can mark an entity
    ``llm_exposure: hidden``, asking the LLM to know nothing about that
    entity; that setting does not un-expose this tool, so entity data in
    the text would keep reporting on the entity despite the setting.
    """
    prefix = f"{label} ({image_format.upper()}"
    if image_size is None:
        detail = f"{prefix})"
    else:
        width, height = image_size
        detail = f"{prefix}, {width}x{height})"
    note = (
        " (UTC — could not determine the Home Assistant timezone)"
        if timezone_fallback
        else ""
    )
    return f"{detail}. Retrieved: {retrieved:%Y-%m-%d %H:%M:%S %:z}{note}"


class CameraTools:
    """Camera and image entity retrieval."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def _check_status(response: Any, entity_id: str, domain: str) -> None:
        """Map an HTTP error status to a structured ToolError.

        401 maps to an authentication error, 404 to a missing-entity error,
        and any other 4xx/5xx to a service-call failure.
        """
        if response.status_code == 401:
            raise_tool_error(
                create_auth_error(
                    f"Invalid authentication token for {domain} access",
                    context={"entity_id": entity_id},
                )
            )
        if response.status_code == 404:
            raise_tool_error(
                create_entity_not_found_error(
                    entity_id,
                    details=f"Use ha_search to find available {domain}s",
                )
            )
        if response.status_code >= 400:
            message = f"Could not retrieve an image from {entity_id}: HTTP {response.status_code}."
            if response.status_code >= 500:
                message += f" The {domain} entity may be offline or temporarily unavailable."
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    message,
                    context={"entity_id": entity_id},
                    suggestions=[
                        "Open the entity in Home Assistant and check whether it shows an image."
                    ],
                )
            )

    async def _fetch_image_entity(self, entity_id: str) -> tuple[bytes, str]:
        """Fetch an image entity's bytes through HA's image proxy, size-bounded.

        Core serves ``/api/image_proxy/<entity_id>``, the route its own
        frontend uses. The body is streamed so an oversized image is
        refused without being buffered in full.
        """
        limit = MAX_IMAGE_ENTITY_BYTES
        endpoint = f"/image_proxy/{quote(entity_id, safe='')}"
        async with self._client.httpx_client.stream("GET", endpoint) as response:
            self._check_status(response, entity_id, "image")

            content_type = response.headers.get("content-type", "")
            declared = _declared_content_length(response)
            if declared is not None and declared > limit:
                self._raise_too_large(entity_id, limit, declared)
            body, exceeded = await _read_limited_response(response, limit)
            if exceeded:
                self._raise_too_large(entity_id, limit, None)

        return body, content_type

    @staticmethod
    def _raise_too_large(entity_id: str, limit: int, declared: int | None) -> NoReturn:
        raise_tool_error(
            create_error_response(
                ErrorCode.IMAGE_PAYLOAD_TOO_LARGE,
                f"Image entity {entity_id} exceeds the server's safe "
                f"inline-image limit of {limit} bytes.",
                suggestions=[
                    "Reduce the source image size or choose a smaller image entity. "
                    "Home Assistant's image proxy cannot resize it."
                ],
                context={
                    "entity_id": entity_id,
                    "limit_bytes": limit,
                    "declared_bytes": declared,
                },
            )
        )

    async def _fetch_camera(
        self, entity_id: str, width: int | None, height: int | None
    ) -> tuple[bytes, str]:
        params = {}
        if width is not None:
            params["width"] = str(width)
        if height is not None:
            params["height"] = str(height)
        response = await self._client.httpx_client.get(
            f"/camera_proxy/{quote(entity_id, safe='')}", params=params or None
        )
        self._check_status(response, entity_id, "camera")
        return response.content, response.headers.get("content-type", "image/jpeg")

    @tool(
        name="ha_get_camera_image",
        tags={"Camera"},
        annotations=read_only_hints("Get Camera Image", open_world=False),
    )
    @log_tool_usage
    async def ha_get_camera_image(
        self,
        entity_id: Annotated[
            str,
            Field(
                description="Camera or image entity ID "
                "(e.g., 'camera.front_door', 'image.album_art')"
            ),
        ],
        width: Annotated[
            int | None,
            Field(
                description="Width for a camera snapshot. Rejected for image entities. Home Assistant "
                "rescales only when both width and height are given and "
                "the camera serves JPEG."
            ),
        ] = None,
        height: Annotated[
            int | None,
            Field(
                description="Height for a camera snapshot. Rejected for image entities. Home Assistant "
                "rescales only when both width and height are given and "
                "the camera serves JPEG."
            ),
        ] = None,
    ) -> tuple[str, Image]:
        """Get a snapshot image from a Home Assistant camera or image entity.

        Returns the image in its served format, with its available dimensions
        and retrieval time in Home Assistant local time. Use ha_get_state for
        entity state and attributes. Use this tool to inspect a camera scene
        or view content such as a QR code, album cover, or map.

        EXAMPLES:
        ha_get_camera_image(entity_id="camera.backyard", width=640, height=480)
        ha_get_camera_image(entity_id="image.album_art")
        """
        if not entity_id or "." not in entity_id:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid entity_id format: {entity_id}. "
                    "Expected camera.entity_name or image.entity_name.",
                )
            )

        domain = entity_id.split(".", maxsplit=1)[0]
        if domain not in ("camera", "image"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Entity {entity_id} is not a camera or image entity. "
                    f"Domain is '{domain}', expected 'camera' or 'image'.",
                    context={"entity_id": entity_id},
                )
            )

        if domain == "image" and (width is not None or height is not None):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "width and height apply only to camera entities. Home "
                    "Assistant's image proxy cannot resize image entities.",
                    context={"entity_id": entity_id},
                )
            )
        try:
            if domain == "image":
                body, content_type = await self._fetch_image_entity(entity_id)
                label = "Image"
            else:
                body, content_type = await self._fetch_camera(entity_id, width, height)
                label = "Camera snapshot"
            retrieved_at = datetime.now(UTC)

            image_format, image_size = resolve_image_info(
                body, _detect_image_format(content_type)
            )
            ha_timezone, fetch_failed = await fetch_ha_timezone(self._client)
            local_tz, resolved_timezone = resolve_local_timezone(ha_timezone)
            # A failed fetch, or a zone name tzdata cannot resolve, both
            # fall back to UTC — the note keeps that distinct from a
            # genuine UTC install.
            timezone_fallback = fetch_failed or resolved_timezone != ha_timezone

            logger.info(
                f"Retrieved image from {entity_id} "
                f"({len(body)} bytes, format={image_format})"
            )

            # Return the info text plus a FastMCP Image object (auto-converted
            # to MCP TextContent and ImageContent, in this order)
            return (
                _snapshot_info_text(
                    image_format,
                    image_size,
                    retrieved_at.astimezone(local_tz),
                    timezone_fallback,
                    label=label,
                ),
                Image(data=body, format=image_format),
            )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error retrieving image from {entity_id}: {e}")
            # The default raise_error=True path raises inside the helper and
            # never returns; the surrounding raise keeps that termination
            # explicit so no path through this function falls off the end.
            raise exception_to_structured_error(
                e,
                context={"entity_id": entity_id},
                suggestions=[f"Ensure the {domain} entity is available in Home Assistant"],
            ) from e


def register_camera_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant camera tools."""
    register_tool_methods(mcp, CameraTools(client))
