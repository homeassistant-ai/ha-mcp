"""
Camera tools for Home Assistant MCP server.

This module provides camera-related tools including snapshot retrieval
that returns images directly to the LLM for visual analysis.
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp._vendor.fastmcp.utilities.types import Image

from .helpers import log_tool_usage, register_tool_methods

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
        entity_id: Annotated[
            str, Field(description="Camera entity ID (e.g., 'camera.front_door')")
        ],
        width: Annotated[
            int | None, Field(description="Width to resize the image to")
        ] = None,
        height: Annotated[
            int | None, Field(description="Height to resize the image to")
        ] = None,
    ) -> Image:
        """Retrieve a snapshot image from a Home Assistant camera entity.

        Fetches the current camera image and returns it directly for visual
        analysis (security checks, delivery verification, confirming a garage
        door actually closed). Only cameras exposed to Home Assistant are
        accessible; images come back in their native format (JPEG, PNG, or GIF).
        Use width/height on high-resolution cameras to reduce token usage.

        EXAMPLE: ha_get_camera_image(entity_id="camera.backyard", width=640, height=480)
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
            image_format = _detect_image_format(content_type)

            logger.info(
                f"Retrieved camera image from {entity_id} "
                f"({len(response.content)} bytes, format={image_format})"
            )

            # Return FastMCP Image object which automatically converts to MCP ImageContent
            return Image(data=response.content, format=image_format)

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
