"""
Camera tools for Home Assistant MCP server.

This module provides camera-related tools including snapshot retrieval
that returns images directly to the LLM for visual analysis, alongside a
short text block stating the served snapshot's size and retrieval time.
The text block never contains Home Assistant entity data.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp._vendor.fastmcp.utilities.types import Image
from ha_mcp.image_info import resolve_image_info

from .helpers import log_tool_usage, register_tool_methods
from .util_helpers import fetch_ha_timezone, resolve_local_timezone

logger = logging.getLogger(__name__)


_CONTENT_TYPE_MAP = {
    "jpeg": "jpeg",
    "jpg": "jpeg",
    "png": "png",
    "gif": "gif",
}


def _detect_image_format(content_type: str) -> str:
    """Detect image format from Content-Type header, defaulting to JPEG."""
    for key, fmt in _CONTENT_TYPE_MAP.items():
        if key in content_type:
            return fmt
    return "jpeg"


def _snapshot_info_text(
    image_format: str,
    image_size: tuple[int, int] | None,
    retrieved: datetime,
    timezone_fallback: bool = False,
) -> str:
    """Build the text half of the camera image response.

    Reports the size of the image actually served (which may differ from
    the camera's native resolution when Home Assistant rescaled it) and
    when the snapshot was retrieved, in Home Assistant local time with
    UTC offset. When the timezone lookup fell back to UTC, the text says
    so, so the label cannot be mistaken for a genuine UTC install.
    """
    if image_size is None:
        detail = f"Camera snapshot ({image_format.upper()})"
    else:
        width, height = image_size
        detail = f"Camera snapshot ({image_format.upper()}, {width}x{height})"
    note = (
        " (UTC — could not determine the Home Assistant timezone)"
        if timezone_fallback
        else ""
    )
    return f"{detail}. Retrieved: {retrieved:%Y-%m-%d %H:%M:%S %:z}{note}"


class CameraTools:
    """Camera snapshot retrieval tools."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def _check_response(response: Any, entity_id: str) -> None:
        """Validate camera proxy HTTP response status and content, raising on errors."""
        if response.status_code == 401:
            raise PermissionError("Invalid authentication token for camera access")
        if response.status_code == 404:
            raise ValueError(
                f"Camera entity not found: {entity_id}. "
                "Use ha_search() to find available cameras."
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Failed to retrieve camera image: HTTP {response.status_code}"
            )
        if not response.content:
            raise RuntimeError(
                f"Camera {entity_id} returned empty image data. "
                "The camera may be offline or unavailable."
            )

    @tool(
        name="ha_get_camera_image",
        tags={"Camera"},
        annotations={
            "openWorldHint": False,
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Camera Image",
        },
    )
    @log_tool_usage
    async def ha_get_camera_image(
        self,
        entity_id: str,
        width: int | None = None,
        height: int | None = None,
    ) -> tuple[str, Image]:
        """
        Retrieve a snapshot image from a Home Assistant camera entity.

        This tool fetches the current camera image and returns it directly for visual
        analysis. Use this when you need to see what a camera is currently viewing.

        **Parameters:**
        - entity_id: Camera entity ID (e.g., 'camera.front_door', 'camera.living_room')
        - width: Optional width to resize the image (reduces token usage for large images)
        - height: Optional height to resize the image

        **Use Cases:**
        - Security checks: "Is someone at the front door?"
        - Pet monitoring: "Is my dog still on the couch?"
        - Delivery verification: "Did my package get delivered?"
        - Visual confirmation: "Did the garage door actually close?"
        - Incident investigation: "What triggered the motion sensor?"

        **Example Usage:**
        ```python
        # Get current snapshot from front door camera
        ha_get_camera_image(entity_id="camera.front_door")

        # Get resized image to reduce token usage
        ha_get_camera_image(entity_id="camera.backyard", width=640, height=480)
        ```

        **Notes:**
        - Only cameras exposed to Home Assistant are accessible
        - The existing HA authentication/authorization applies
        - Images are returned in their native format (JPEG, PNG, or GIF)
        - Use width/height parameters for large high-resolution cameras to reduce
          token usage when full resolution is not needed
        - The response includes a short text block with the served snapshot's
          size and retrieval time (Home Assistant local time). When Home
          Assistant rescales the image (supported resize request, or a
          still derived from a stream), the reported size may differ from
          the camera's native resolution
        - If the Content-Type header mislabels the payload, the reported
          format, size, and image block follow the bytes themselves

        **Related Services:**
        - camera.snapshot: Save snapshot to file on HA server
        - camera.turn_on/turn_off: Control camera power
        - camera.enable_motion_detection: Enable motion detection
        """
        if not entity_id or "." not in entity_id:
            raise ValueError(
                f"Invalid entity_id format: {entity_id}. "
                "Expected format: camera.entity_name"
            )

        domain = entity_id.split(".", maxsplit=1)[0]
        if domain != "camera":
            raise ValueError(
                f"Entity {entity_id} is not a camera entity. "
                f"Domain is '{domain}', expected 'camera'."
            )

        # Build the camera proxy URL with optional size parameters
        # Home Assistant camera proxy API: /api/camera_proxy/<entity_id>
        endpoint = f"/camera_proxy/{entity_id}"

        params = {}
        if width is not None:
            params["width"] = str(width)
        if height is not None:
            params["height"] = str(height)

        try:
            response = await self._client.httpx_client.get(
                endpoint, params=params or None
            )
            self._check_response(response, entity_id)

            content_type = response.headers.get("content-type", "image/jpeg")
            # Cameras can mislabel their payload, so resolve the format
            # from the bytes: the reported label, size, and Image block
            # must all describe what was actually served.
            image_format, image_size = resolve_image_info(
                response.content, _detect_image_format(content_type)
            )
            # Sample the clock the moment HA handed us the bytes; the zone
            # is resolved separately so a slow timezone lookup only delays
            # the label, never the timestamp.
            retrieved_at = datetime.now(UTC)
            ha_timezone, fetch_failed = await fetch_ha_timezone(self._client)
            local_tz, resolved_timezone = resolve_local_timezone(ha_timezone)
            # A failed fetch, or a zone name tzdata cannot resolve, both
            # fall back to UTC — the note keeps that distinct from a
            # genuine UTC install.
            timezone_fallback = fetch_failed or resolved_timezone != ha_timezone

            logger.info(
                f"Retrieved camera image from {entity_id} "
                f"({len(response.content)} bytes, format={image_format})"
            )

            # Return the info text plus a FastMCP Image object (auto-converted
            # to MCP TextContent and ImageContent, in this order)
            return (
                _snapshot_info_text(
                    image_format,
                    image_size,
                    retrieved_at.astimezone(local_tz),
                    timezone_fallback,
                ),
                Image(data=response.content, format=image_format),
            )

        except (PermissionError, ValueError, RuntimeError):
            raise
        except Exception as e:
            logger.error(f"Error retrieving camera image from {entity_id}: {e}")
            raise RuntimeError(
                f"Failed to retrieve camera image from {entity_id}: {str(e)}. "
                "Ensure the camera is online and accessible."
            ) from e


def register_camera_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant camera tools."""
    register_tool_methods(mcp, CameraTools(client))
