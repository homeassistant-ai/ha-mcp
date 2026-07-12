"""MCP image blocks and audit metadata for dashboard captures."""

from __future__ import annotations

import hashlib
from typing import Any

from fastmcp.utilities.types import Image
from mcp.types import ImageContent

from .capture import DashboardImageCapture


def dashboard_image_content(
    captures: list[DashboardImageCapture],
) -> list[ImageContent]:
    """Convert ordered captures to contiguous native MCP image blocks."""
    return [
        Image(data=capture.data, format=capture.image_format).to_image_content(
            mime_type=capture.mime_type
        )
        for capture in captures
    ]


def dashboard_screenshot_metadata(
    captures: list[DashboardImageCapture], render_path: str
) -> list[dict[str, Any]]:
    """Describe each image, its engine request, and local capture options."""
    metadata: list[dict[str, Any]] = []
    for content_index, capture in enumerate(captures):
        requested = capture.requested
        metadata.append(
            {
                "content_index": content_index,
                "render_path": render_path,
                "viewport": {
                    "preset": capture.preset,
                    "width": capture.width,
                    "height": capture.height,
                    "orientation": capture.orientation,
                    "zoom": requested["zoom"],
                },
                "engine_request": {
                    "viewport": f"{capture.width}x{capture.height}",
                    "zoom": requested["zoom"],
                    "wait_ms": requested["wait_ms"],
                    "theme": requested["theme"],
                    "dark_mode": requested["dark_mode"],
                    "language": requested["language"],
                    "format": capture.image_format,
                },
                "local_capture_options": {
                    "full_page": requested["full_page"],
                    "render_timeout_seconds": requested["render_timeout_seconds"],
                },
                "frontend_context_confirmed": False,
                "image": {
                    "format": capture.image_format,
                    "mime_type": capture.mime_type,
                    "size_bytes": capture.size_bytes,
                    "sha256": hashlib.sha256(capture.data).hexdigest(),
                },
            }
        )
    return metadata
